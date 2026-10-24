"""Bounded freshness of requested stored rows, without fetching or calendar guesses.

This is a prerequisite monitor, not historical completeness or trading-session
certification. Daily bars compare the Asia/Shanghai date of their raw timestamp.
Other bars use the latest row's stored trading_day when present, otherwise its
raw Shanghai date. Stored trading-day provenance is reported, never verified or
repaired here. A date comparison does not establish that a session has finished.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

from ak_trading.market_reference import SHANGHAI_TZ
from ak_trading.persistence.config import DatabaseSettings, redact_database_text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


MAX_CHECKS = 100


@dataclass(frozen=True)
class FreshnessRequest:
    """One explicit reference date and duration for the requested symbol sample."""

    expected_through: str
    symbols: tuple[str, ...]
    duration_seconds: int
    exchanges: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.expected_through, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", self.expected_through
        ):
            raise ValueError("--expected-through must be YYYY-MM-DD")
        try:
            date.fromisoformat(self.expected_through)
        except ValueError:
            raise ValueError("--expected-through must be a valid date") from None
        if type(self.duration_seconds) is not int or not 1 <= self.duration_seconds <= 2_147_483_647:
            raise ValueError("--duration must be an integer between 1 and 2147483647")
        if isinstance(self.symbols, str) or isinstance(self.exchanges, str):
            raise ValueError("symbols and exchanges must be sequences")
        if not self.symbols or len(self.symbols) + len(self.exchanges) > MAX_CHECKS:
            raise ValueError("request requires symbols and at most 100 total checks")
        for value in self.symbols:
            if (not isinstance(value, str) or not value.strip() or len(value) > 256
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise ValueError("--symbol must be nonblank, at most 256 characters, without controls")
        for value in self.exchanges:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,7}", value):
                raise ValueError("--exchange must be a 1-8 character exchange identifier")
        object.__setattr__(self, "symbols", tuple(dict.fromkeys(value.strip() for value in self.symbols)))
        object.__setattr__(self, "exchanges", tuple(dict.fromkeys(value.upper() for value in self.exchanges)))


def _new_report(request: FreshnessRequest, observed_at: datetime) -> dict[str, Any]:
    if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be a timezone-aware datetime")
    checks = [
        {"kind": "market_bars", "symbol": symbol,
         "duration_seconds": request.duration_seconds,
         "latest_timestamp": None, "latest_raw_date": None,
         "latest_trading_day": None, "trading_day_source": None,
         "trading_day_source_verified": False, "comparison_date": None,
         "basis": None, "status": "empty"}
        for symbol in request.symbols
    ] + [
        {"kind": "member_position_ranks", "exchange": exchange,
         "latest_trading_day": None, "comparison_date": None,
         "basis": "stored_ranking_trading_day", "status": "empty"}
        for exchange in request.exchanges
    ]
    return {
        "schema_version": 1, "expected_through": request.expected_through,
        "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        "read_only": True, "isolation_level": "repeatable read",
        "statement_timeout_ms": 5000,
        "coverage": {
            "scope": "requested_samples_only", "symbols": list(request.symbols),
            "duration_seconds": request.duration_seconds, "exchanges": list(request.exchanges),
            "rows_per_check": 1, "universe_coverage": False,
            "historical_completeness_certified": False,
            "note": (
                "Only the newest stored row per requested symbol/duration or exchange is checked. "
                "No gap, universe, session-completion or historical trading-day certification. "
                "Daily basis: raw Asia/Shanghai date. Other bars: stored trading_day if present, "
                "otherwise raw Asia/Shanghai date; provenance remains unverified. "
                "The caller supplies the reference date; no holiday or current-date inference."
            ),
        },
        "checks": checks, "errors": [],
    }


def _finish(report: dict[str, Any]) -> dict[str, Any]:
    statuses = {check["status"] for check in report["checks"]}
    if report["errors"] or "error" in statuses:
        status, code = "error", 2
    elif "stale" in statuses:
        status, code = "stale", 1
    elif "empty" in statuses or not statuses:
        status, code = "empty", 1
    else:
        status, code = "fresh", 0
    report.update(overall_status=status, passed=code == 0, exit_code=code)
    return report


def freshness_error_report(
    request: FreshnessRequest, *, observed_at: datetime, error: Exception, stage: str,
) -> dict[str, Any]:
    """Report setup failure for every requested sample; never expose error text."""
    report = _new_report(request, observed_at)
    failure = {"stage": stage, "error_type": type(error).__name__}
    report["errors"].append(failure)
    for check in report["checks"]:
        check.update(status="error", **failure)
    return _finish(report)


def _date(value: object) -> date:
    if type(value) is not date:
        raise ValueError("stored trading_day must be a date")
    return value


def _describe_bar(row, duration: int, settings: DatabaseSettings) -> dict[str, Any]:
    timestamp = row["ts"]
    if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
        raise ValueError("stored timestamp must include a timezone")
    local = timestamp.astimezone(SHANGHAI_TZ)
    raw_date = local.date()
    trading_day = _date(row["trading_day"]) if row["trading_day"] is not None else None
    source = row["trading_day_source"]
    # Provenance is a short identifier, not a free-form driver/source payload.
    if not isinstance(source, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", source):
        source = "unrecognized"
    if duration == 86400:
        comparison, basis = raw_date, "daily_raw_date_asia_shanghai"
    elif trading_day is not None:
        comparison, basis = trading_day, "stored_trading_day"
    else:
        comparison, basis = raw_date, "raw_date_asia_shanghai_fallback"
    return {
        "latest_timestamp": local.isoformat(), "latest_raw_date": raw_date.isoformat(),
        "latest_trading_day": trading_day.isoformat() if trading_day is not None else None,
        "trading_day_source": redact_database_text(source, settings),
        "comparison_date": comparison.isoformat(), "basis": basis,
    }


def inspect_data_freshness(
    engine: Engine, *, settings: DatabaseSettings, request: FreshnessRequest,
    observed_at: datetime,
) -> dict[str, Any]:
    """Read one row per sample in a single snapshot; caller owns engine disposal.

    observed_at is supplied by the caller and never determines expected_through.
    Each query has a savepoint so denied/timed-out queries cannot hide later
    samples. Closing the connection rolls back the read-only transaction.
    """
    report = _new_report(request, observed_at)
    report["statement_timeout_ms"] = min(settings.statement_timeout_ms, 5000)
    try:
        from sqlalchemy import select, text

        from ak_trading.persistence.schema import market_bars, member_position_ranks

        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            connection.execute(
                text("SELECT pg_catalog.set_config('statement_timeout', :timeout, true)"),
                {"timeout": f"{report['statement_timeout_ms']}ms"},
            )
            for check in report["checks"]:
                try:
                    with connection.begin_nested():
                        if check["kind"] == "market_bars":
                            # Backwards scan of ix_market_bars_symbol_duration_ts.
                            statement = select(
                                market_bars.c.ts, market_bars.c.trading_day,
                                market_bars.c.trading_day_source,
                            ).where(
                                market_bars.c.symbol == check["symbol"],
                                market_bars.c.duration_seconds == check["duration_seconds"],
                            ).order_by(market_bars.c.ts.desc()).limit(1)
                        else:
                            # Backwards scan of ix_member_ranks_exchange_day.
                            statement = select(member_position_ranks.c.trading_day).where(
                                member_position_ranks.c.exchange == check["exchange"],
                            ).order_by(member_position_ranks.c.trading_day.desc()).limit(1)
                        row = connection.execute(statement).mappings().first()
                        if row is not None:
                            if check["kind"] == "market_bars":
                                values = _describe_bar(row, check["duration_seconds"], settings)
                            else:
                                day = _date(row["trading_day"]).isoformat()
                                values = {"latest_trading_day": day, "comparison_date": day}
                            check.update(values)
                            check["status"] = (
                                "fresh" if check["comparison_date"] >= request.expected_through else "stale"
                            )
                except Exception as exc:
                    check.update(status="error", error_type=type(exc).__name__)
    except Exception as exc:
        # Includes connect/setup/connection-close failures, whose messages may
        # contain unknown credentials. Fail every sample if the snapshot failed.
        return freshness_error_report(
            request, observed_at=observed_at, error=exc, stage="database",
        )
    return _finish(report)
