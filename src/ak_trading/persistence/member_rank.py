"""Idempotent PostgreSQL import for member position rank rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from ak_trading.member_rank import METRIC_LONG, METRIC_SHORT
from ak_trading.persistence.database import DatabaseOperationError
from ak_trading.persistence.schema import member_position_ranks

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from ak_trading.member_rank import MemberRankRow

_UNIQUE_CONSTRAINT = "uq_member_ranks_day_instrument_metric_rank_member"


class MemberRankImportError(DatabaseOperationError):
    """A credential-safe member-rank import failure."""


@dataclass(frozen=True)
class MemberRankImportResult:
    exchange: str
    trading_day: str
    rows_received: int
    rows_written: int

    def as_dict(self) -> dict[str, object]:
        return {
            "exchange": self.exchange,
            "trading_day": self.trading_day,
            "rows_received": self.rows_received,
            "rows_written": self.rows_written,
        }


def import_member_rank_rows(
    engine: Engine,
    rows: Sequence["MemberRankRow"],
    *,
    chunk_size: int = 500,
) -> MemberRankImportResult:
    """Upsert member-rank rows; repeated imports of the same disclosure are no-ops."""

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise MemberRankImportError("chunk_size must be a positive integer")
    if not rows:
        raise MemberRankImportError("no rows to import")
    first = rows[0]
    written = 0
    try:
        with engine.begin() as connection:
            for start in range(0, len(rows), chunk_size):
                chunk = rows[start:start + chunk_size]
                values = [
                    {
                        "exchange": row.exchange,
                        "trading_day": row.trading_day,
                        "product_id": row.product_id,
                        "instrument_id": row.instrument_id,
                        "metric": row.metric,
                        "rank_no": row.rank_no,
                        "member_id": row.member_id,
                        "member_name": row.member_name,
                        "quantity": row.quantity,
                        "change_qty": row.change_qty,
                        "basis": row.basis,
                        "source_url": row.source_url,
                    }
                    for row in chunk
                ]
                statement = insert(member_position_ranks).values(values)
                excluded = statement.excluded
                statement = statement.on_conflict_do_update(
                    constraint=_UNIQUE_CONSTRAINT,
                    set_={
                        "member_id": excluded.member_id,
                        "quantity": excluded.quantity,
                        "change_qty": excluded.change_qty,
                        "basis": excluded.basis,
                        "source_url": excluded.source_url,
                        "collected_at": func.now(),
                    },
                ).returning(member_position_ranks.c.id)
                written += len(connection.execute(statement).scalars().all())
    except Exception:
        raise MemberRankImportError("member-rank import failed") from None
    return MemberRankImportResult(
        exchange=first.exchange,
        trading_day=str(first.trading_day),
        rows_received=len(rows),
        rows_written=written,
    )


@dataclass(frozen=True)
class ProductNetFactor:
    exchange: str
    trading_day: str
    product_id: str
    long_top20: int
    short_top20: int
    net_top20: int


def top20_net_by_product(
    engine: Engine,
    day: date,
    *,
    exchange: str | None = None,
) -> list[ProductNetFactor]:
    """Per-product top-20 member net position for one trading day.

    Net = sum of long top-20 minus sum of short top-20. Products disclosed at
    the aggregate level (SHFE/INE/CZCE/DCE) use their ``ALL`` rows directly;
    products disclosed only per contract (CFFEX/GFEX) are rolled up by
    summing contract-level rows. A positive net means disclosed top members
    lean net long.
    """

    base_columns = (
        member_position_ranks.c.exchange,
        member_position_ranks.c.product_id,
        member_position_ranks.c.metric,
        func.sum(member_position_ranks.c.quantity).label("quantity"),
    )
    filters = [
        member_position_ranks.c.trading_day == day,
        member_position_ranks.c.rank_no.between(1, 20),
        member_position_ranks.c.metric.in_([METRIC_LONG, METRIC_SHORT]),
    ]
    if exchange is not None:
        filters.append(member_position_ranks.c.exchange == exchange)

    def _statement(instrument_filter) -> object:
        return (
            select(*base_columns)
            .where(*filters, instrument_filter)
            .group_by(
                member_position_ranks.c.exchange,
                member_position_ranks.c.product_id,
                member_position_ranks.c.metric,
            )
        )

    buckets: dict[tuple[str, str], dict[str, int]] = {}

    def _absorb(rows) -> set[tuple[str, str]]:
        covered: set[tuple[str, str]] = set()
        for row in rows:
            key = (row.exchange, row.product_id)
            covered.add(key)
            bucket = buckets.setdefault(key, {"long": 0, "short": 0})
            bucket[row.metric] += int(row.quantity or 0)
        return covered

    try:
        with engine.begin() as connection:
            aggregate_keys = _absorb(
                connection.execute(
                    _statement(member_position_ranks.c.instrument_id == "ALL")
                )
            )
            contract_rows = list(
                connection.execute(
                    _statement(member_position_ranks.c.instrument_id != "ALL")
                )
            )
    except Exception:
        raise MemberRankImportError("top20 net query failed") from None

    _absorb(
        row for row in contract_rows
        if (row.exchange, row.product_id) not in aggregate_keys
    )
    return [
        ProductNetFactor(
            exchange=exchange_id,
            trading_day=f"{day:%Y-%m-%d}",
            product_id=product_id,
            long_top20=values["long"],
            short_top20=values["short"],
            net_top20=values["long"] - values["short"],
        )
        for (exchange_id, product_id), values in sorted(buckets.items())
    ]
