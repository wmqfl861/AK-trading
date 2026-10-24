"""Read-only PostgreSQL inventory; market history is never scanned by default."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ak_trading.persistence.config import DatabaseSettings
from ak_trading.persistence.database import DatabaseOperationError

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


_CATALOG_SQL = """
SELECT n.nspname AS schema_name, c.relname AS table_name,
       c.relkind AS relation_kind, c.reltuples AS estimated_rows,
       pg_catalog.pg_table_is_visible(c.oid) AS visible
FROM pg_catalog.pg_namespace AS n
LEFT JOIN pg_catalog.pg_class AS c
  ON c.relnamespace = n.oid AND c.relkind IN ('r', 'p', 'f', 'm')
WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
ORDER BY n.nspname, c.relname
"""


@dataclass(frozen=True)
class InspectionRequest:
    """A bounded, earliest-first coverage sample with inclusive/exclusive bounds."""

    symbol: str | None = None
    duration_seconds: int | None = None
    start: str | None = None
    end: str | None = None
    limit: int | None = None
    start_nano: int | None = field(init=False, default=None)
    end_nano: int | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if (self.symbol is None) != (self.duration_seconds is None):
            raise ValueError("--symbol and --duration must be supplied together")
        if self.symbol is None:
            if self.start is not None or self.end is not None or self.limit is not None:
                raise ValueError("--start, --end and --limit require --symbol and --duration")
        else:
            if not isinstance(self.symbol, str) or not self.symbol.strip():
                raise ValueError("--symbol must not be blank")
            object.__setattr__(self, "symbol", self.symbol.strip())
            duration = self.duration_seconds
            if type(duration) is not int or not 1 <= duration <= 2_147_483_647:
                raise ValueError("--duration must be an integer between 1 and 2147483647")
        limit = 1000 if self.limit is None else self.limit
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("--limit must be an integer between 1 and 10000")
        object.__setattr__(self, "limit", limit)
        if self.start is not None or self.end is not None:
            from ak_trading.persistence.query import parse_aware_datetime_nano

            for name in ("start", "end"):
                value = getattr(self, name)
                if value is not None:
                    try:
                        parsed = parse_aware_datetime_nano(value, f"--{name}")
                    except Exception:
                        raise ValueError(
                            f"--{name} must be a timezone-aware ISO datetime within "
                            "the supported nanosecond range and microsecond precision"
                        ) from None
                    object.__setattr__(self, name + "_nano", parsed)
        if self.start_nano is not None and self.end_nano is not None and self.start_nano >= self.end_nano:
            raise ValueError("--start must be before --end")


def _optional_read(connection, statement, *, scalar: bool = False) -> dict[str, Any]:
    # PostgreSQL aborts a transaction after a denied SHOW or SELECT. Recover at
    # the savepoint before attempting any further inventory sections.
    try:
        with connection.begin_nested():
            result = connection.execute(statement)
            value = result.scalar_one() if scalar else result.mappings().all()
        return {"status": "available", "value": value}
    except Exception as exc:
        original = getattr(exc, "orig", exc)
        code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        reason = {
            "42501": "permission_denied", "57014": "statement_timeout",
            "42P01": "table_absent", "42703": "column_absent",
        }.get(code, "query_failed")
        return {"status": "unavailable", "reason": reason}


def _market_coverage(connection, table, request: InspectionRequest) -> dict[str, Any]:
    from sqlalchemy import select

    from ak_trading.persistence.market_data import datetime_from_nano

    # This follows the existing (symbol, duration_seconds, datetime_nano) index.
    # LIMIT applies to raw rows, never to an aggregate over the entire series.
    statement = select(table.c.datetime_nano, table.c.trading_day).where(
        table.c.symbol == request.symbol,
        table.c.duration_seconds == request.duration_seconds,
    )
    if request.start_nano is not None:
        statement = statement.where(table.c.datetime_nano >= request.start_nano)
    if request.end_nano is not None:
        statement = statement.where(table.c.datetime_nano < request.end_nano)
    result = _optional_read(connection, statement.order_by(
        table.c.datetime_nano.asc(),
    ).limit(request.limit + 1))
    if result["status"] != "available":
        return result
    fetched = result.pop("value")
    truncated = len(fetched) > request.limit
    rows = fetched[:request.limit]
    days = [row["trading_day"] for row in rows if row["trading_day"] is not None]
    first = rows[0]["datetime_nano"] if rows else None
    last = rows[-1]["datetime_nano"] if rows else None
    result.update({
        "scope": "earliest_matching_rows", "symbol": request.symbol,
        "duration_seconds": request.duration_seconds,
        "start_nano_inclusive": request.start_nano, "end_nano_exclusive": request.end_nano,
        "rows": len(rows), "limit": request.limit,
        "truncated": truncated, "complete_for_filter": not truncated,
        "first_datetime_nano": first, "last_datetime_nano": last,
        "first_timestamp": datetime_from_nano(first).isoformat() if first is not None else None,
        "last_timestamp": datetime_from_nano(last).isoformat() if last is not None else None,
        "first_trading_day": min(days).isoformat() if days else None,
        "last_trading_day": max(days).isoformat() if days else None,
        "null_trading_days": len(rows) - len(days),
        "note": "Coverage describes sampled stored rows; it does not establish gap-free history.",
    })
    return result


def inspect_database(
    engine: Engine, *, settings: DatabaseSettings, request: InspectionRequest | None = None,
) -> dict[str, Any]:
    """Inventory catalog truth independently of Alembic's recorded revisions."""
    request = request or InspectionRequest()
    try:
        from sqlalchemy import MetaData, func, select, text

        from ak_trading.persistence.schema import metadata

        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            connection.execute(
                text("SELECT pg_catalog.set_config('statement_timeout', :timeout, true)"),
                {"timeout": f"{min(settings.statement_timeout_ms, 5000)}ms"},
            )
            identity = connection.execute(text(
                "SELECT current_database() AS database, "
                "current_setting('server_version') AS server_version, "
                "current_schema() AS schema_name"
            )).mappings().one()
            catalog = connection.execute(text(_CATALOG_SQL)).mappings().all()
            schemas = sorted({row["schema_name"] for row in catalog})
            tables = [
                {
                    "schema": row["schema_name"], "name": row["table_name"],
                    "kind": row["relation_kind"], "visible": bool(row["visible"]),
                    "estimated_rows": (
                        int(row["estimated_rows"])
                        if row["estimated_rows"] is not None and row["estimated_rows"] >= 0
                        else None
                    ),
                    "approximate": True,
                }
                for row in catalog if row["table_name"] is not None
            ]
            schema = identity["schema_name"]
            resolved = {
                row["name"]: row["schema"] for row in tables
                if row["visible"] and row["kind"] in ("r", "p", "f")
            }
            present = set(resolved)
            expected = sorted(metadata.tables)
            report = {
                "read_only": True, "isolation_level": "repeatable read",
                "location": settings.safe_location,
                "database": identity["database"],
                "server_version": identity["server_version"],
                "schemas": schemas, "tables": tables,
                "estimates": {
                    "source": "pg_catalog.pg_class.reltuples", "approximate": True,
                    "note": "Planner estimates may be stale; null means unknown. Partition parents are not totals.",
                },
                "expected_schema": {
                    "schema": schema, "resolution": "postgresql_search_path",
                    "expected_tables": expected,
                    "resolved_tables": {name: resolved[name] for name in expected if name in resolved},
                    "present_tables": sorted(set(expected) & present),
                    "missing_tables": sorted(set(expected) - present),
                },
                "market_coverage": {"status": "not_requested"},
            }
            directory = _optional_read(connection, text("SHOW data_directory"), scalar=True)
            if directory["status"] == "available":
                directory["path"] = directory.pop("value")
            report["data_directory"] = directory
            size = _optional_read(connection, text(
                "SELECT pg_catalog.pg_database_size(current_database()) AS bytes"
            ), scalar=True)
            if size["status"] == "available":
                size["bytes"] = size.pop("value")
            report["database_size"] = size
            if "alembic_version" in present:
                from sqlalchemy import Column, String, Table

                version = Table("alembic_version", MetaData(), Column("version_num", String), schema=resolved["alembic_version"])
                revisions = _optional_read(connection,
                    select(version.c.version_num).order_by(version.c.version_num).limit(100)
                )
                if revisions["status"] == "available":
                    revisions["revisions"] = [row["version_num"] for row in revisions.pop("value")]
                report["migration"] = revisions
            else:
                report["migration"] = {"status": "unavailable", "reason": "table_absent"}
            reference_counts = {}
            for name in ("reference_snapshots", "reference_products", "reference_closed_dates"):
                if name not in present:
                    reference_counts[name] = {"status": "unavailable", "reason": "table_absent"}
                    continue
                table = metadata.tables[name].to_metadata(MetaData(), schema=resolved[name])
                count = _optional_read(connection, select(func.count()).select_from(table), scalar=True)
                if count["status"] == "available":
                    count["rows"] = count.pop("value")
                    count["exact"] = True
                reference_counts[name] = count
            report["reference_counts"] = reference_counts
            if "reference_snapshots" in present:
                table = metadata.tables["reference_snapshots"].to_metadata(MetaData(), schema=resolved["reference_snapshots"])
                calendar = _optional_read(connection, select(
                    table.c.id, table.c.calendar_valid_from,
                    table.c.calendar_valid_through, table.c.imported_at,
                ).order_by(table.c.id.desc()).limit(1))
                if calendar["status"] == "available":
                    rows = calendar.pop("value")
                    latest = rows[0] if rows else None
                    calendar.update({
                        "status": "available" if latest else "empty",
                        "scope": "latest_reference_snapshot",
                        "snapshot_id": latest["id"] if latest else None,
                        "valid_from": latest["calendar_valid_from"].isoformat() if latest else None,
                        "valid_through": latest["calendar_valid_through"].isoformat() if latest else None,
                    })
                report["calendar_coverage"] = calendar
            else:
                report["calendar_coverage"] = {"status": "unavailable", "reason": "table_absent"}
            if request.symbol is not None:
                if "market_bars" in present:
                    table = metadata.tables["market_bars"].to_metadata(MetaData(), schema=resolved["market_bars"])
                    report["market_coverage"] = _market_coverage(connection, table, request)
                else:
                    report["market_coverage"] = {"status": "unavailable", "reason": "table_absent"}
            return report
    except ImportError:
        raise DatabaseOperationError("PostgreSQL support is not installed") from None
    except Exception:
        raise DatabaseOperationError("database inspection failed") from None
