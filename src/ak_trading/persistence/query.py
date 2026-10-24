from __future__ import annotations

import csv
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ak_trading.market_reference import SHANGHAI_TZ
from ak_trading.persistence.config import DatabaseSettings, redact_database_text
from ak_trading.persistence.database import DatabaseOperationError
from ak_trading.persistence.market_data import datetime_from_nano, expected_kline_columns

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


_MAX_QUERY_LIMIT = 10_000
_SUBSECOND = re.compile(r"[T ]\d{2}:\d{2}:\d{2}[.,](\d+)")


class MarketDataQueryError(DatabaseOperationError):
    """A credential-safe read, quality, or export failure."""


@dataclass(frozen=True)
class MarketBarRecord:
    symbol: str
    duration_seconds: int
    datetime_nano: int
    timestamp: str
    trading_day: str | None
    trading_day_source: str
    open: str | None
    high: str | None
    low: str | None
    close: str | None
    volume: str | None
    open_oi: str | None
    close_oi: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class MarketDataQuality:
    symbol: str
    duration_seconds: int
    rows: int
    distinct_datetime_nanos: int
    duplicate_identities: int
    first_datetime_nano: int
    last_datetime_nano: int
    first_timestamp: str
    last_timestamp: str
    null_trading_days: int
    null_ohlc: int
    invalid_ohlc: int
    null_volume_or_oi: int
    invalid_volume_or_oi: int
    non_increasing_identities: int
    largest_observed_interval_nano: int | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class MarketBarExportResult:
    path: str
    symbol: str
    duration_seconds: int
    rows: int
    first_datetime_nano: int | None
    last_datetime_nano: int | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def parse_aware_datetime_nano(value: str, field: str) -> int:
    if not isinstance(value, str) or not value.strip():
        raise MarketDataQueryError(f"{field} must be an ISO datetime")
    text = value.strip()
    fraction = _SUBSECOND.search(text)
    if fraction is not None and len(fraction.group(1)) > 6:
        raise MarketDataQueryError(
            f"{field} cannot use sub-microsecond precision"
        )
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MarketDataQueryError(f"{field} must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketDataQueryError(f"{field} must include a timezone offset")
    utc_value = parsed.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = utc_value - epoch
    result = (
        delta.days * 86_400_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000
    )
    datetime_from_nano(result)
    return result


def _required_symbol(symbol: object) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise MarketDataQueryError("symbol must be a non-empty string")
    return symbol.strip()


def _positive_duration(duration_seconds: object) -> int:
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int):
        raise MarketDataQueryError("duration_seconds must be an integer")
    if duration_seconds <= 0:
        raise MarketDataQueryError("duration_seconds must be positive")
    return duration_seconds


def _bounded_limit(limit: object) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise MarketDataQueryError("limit must be an integer")
    if limit < 1 or limit > _MAX_QUERY_LIMIT:
        raise MarketDataQueryError("limit must be between 1 and 10000")
    return limit


def _validate_bounds(
    start_nano: int | None, end_nano: int | None
) -> tuple[int | None, int | None]:
    for value, field in ((start_nano, "start_nano"), (end_nano, "end_nano")):
        if value is not None:
            try:
                integer = int(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise MarketDataQueryError(f"{field} must be an integer") from exc
            if isinstance(value, bool) or integer != value:
                raise MarketDataQueryError(f"{field} must be an integer")
            datetime_from_nano(integer)
    if start_nano is not None and end_nano is not None and start_nano >= end_nano:
        raise MarketDataQueryError("start must be before end")
    return start_nano, end_nano


def _safe_failure(
    action: str, exc: BaseException, settings: DatabaseSettings | None
) -> MarketDataQueryError:
    return MarketDataQueryError(
        f"{action} failed: {redact_database_text(exc, settings)}"
    )


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _record(row: Any) -> MarketBarRecord:
    return MarketBarRecord(
        symbol=row.symbol,
        duration_seconds=row.duration_seconds,
        datetime_nano=row.datetime_nano,
        timestamp=row.ts.astimezone(SHANGHAI_TZ).isoformat(),
        trading_day=row.trading_day.isoformat() if row.trading_day else None,
        trading_day_source=row.trading_day_source,
        open=_decimal_text(row.open),
        high=_decimal_text(row.high),
        low=_decimal_text(row.low),
        close=_decimal_text(row.close),
        volume=_decimal_text(row.volume),
        open_oi=_decimal_text(row.open_oi),
        close_oi=_decimal_text(row.close_oi),
    )


def _bar_select(symbol: str, duration_seconds: int, start_nano, end_nano):
    from sqlalchemy import select

    from ak_trading.persistence.schema import market_bars

    statement = select(market_bars).where(
        market_bars.c.symbol == symbol,
        market_bars.c.duration_seconds == duration_seconds,
    )
    if start_nano is not None:
        statement = statement.where(market_bars.c.datetime_nano >= start_nano)
    if end_nano is not None:
        statement = statement.where(market_bars.c.datetime_nano < end_nano)
    return statement.order_by(market_bars.c.datetime_nano.asc())


def query_market_bars(
    engine: Engine,
    *,
    symbol: str,
    duration_seconds: int,
    start_nano: int | None = None,
    end_nano: int | None = None,
    limit: int = 100,
    settings: DatabaseSettings | None = None,
) -> list[MarketBarRecord]:
    symbol = _required_symbol(symbol)
    duration_seconds = _positive_duration(duration_seconds)
    start_nano, end_nano = _validate_bounds(start_nano, end_nano)
    limit = _bounded_limit(limit)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                _bar_select(symbol, duration_seconds, start_nano, end_nano).limit(limit)
            ).all()
        return [_record(row) for row in rows]
    except MarketDataQueryError:
        raise
    except Exception as exc:
        raise _safe_failure("market-bar query", exc, settings) from None


def summarize_market_data_quality(
    engine: Engine,
    *,
    symbol: str | None = None,
    duration_seconds: int | None = None,
    settings: DatabaseSettings | None = None,
) -> list[MarketDataQuality]:
    from sqlalchemy import and_, case, func, or_, select

    from ak_trading.persistence.schema import market_bars

    if symbol is not None:
        symbol = _required_symbol(symbol)
    if duration_seconds is not None:
        duration_seconds = _positive_duration(duration_seconds)
    filters = []
    if symbol is not None:
        filters.append(market_bars.c.symbol == symbol)
    if duration_seconds is not None:
        filters.append(market_bars.c.duration_seconds == duration_seconds)
    previous_nano = func.lag(market_bars.c.datetime_nano).over(
        partition_by=(market_bars.c.symbol, market_bars.c.duration_seconds),
        order_by=market_bars.c.datetime_nano,
    )
    ordered = select(
        market_bars,
        previous_nano.label("previous_nano"),
    ).where(*filters).cte("ordered_market_bars")
    invalid_ohlc = or_(
        ordered.c.high < ordered.c.low,
        ordered.c.high < ordered.c.open,
        ordered.c.high < ordered.c.close,
        ordered.c.low > ordered.c.open,
        ordered.c.low > ordered.c.close,
    )
    null_volume_oi = or_(
        ordered.c.volume.is_(None),
        ordered.c.open_oi.is_(None),
        ordered.c.close_oi.is_(None),
    )
    invalid_volume_oi = or_(
        ordered.c.volume < 0,
        ordered.c.open_oi < 0,
        ordered.c.close_oi < 0,
    )
    statement = (
        select(
            ordered.c.symbol,
            ordered.c.duration_seconds,
            func.count().label("rows"),
            func.count(func.distinct(ordered.c.datetime_nano)).label("distinct_nanos"),
            func.min(ordered.c.datetime_nano).label("first_nano"),
            func.max(ordered.c.datetime_nano).label("last_nano"),
            func.sum(case((ordered.c.trading_day.is_(None), 1), else_=0)).label(
                "null_trading_days"
            ),
            func.sum(
                case(
                    (
                        or_(
                            ordered.c.open.is_(None),
                            ordered.c.high.is_(None),
                            ordered.c.low.is_(None),
                            ordered.c.close.is_(None),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("null_ohlc"),
            func.sum(case((invalid_ohlc, 1), else_=0)).label("invalid_ohlc"),
            func.sum(case((null_volume_oi, 1), else_=0)).label(
                "null_volume_oi"
            ),
            func.sum(case((invalid_volume_oi, 1), else_=0)).label(
                "invalid_volume_oi"
            ),
            func.sum(
                case(
                    (
                        and_(
                            ordered.c.previous_nano.is_not(None),
                            ordered.c.datetime_nano <= ordered.c.previous_nano,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("non_increasing"),
            func.max(
                ordered.c.datetime_nano - ordered.c.previous_nano
            ).label("largest_interval"),
        )
        .group_by(ordered.c.symbol, ordered.c.duration_seconds)
        .order_by(ordered.c.symbol, ordered.c.duration_seconds)
    )
    try:
        with engine.connect() as connection:
            rows = connection.execute(statement).all()
        results = []
        for row in rows:
            duplicate_identities = row.rows - row.distinct_nanos
            results.append(
                MarketDataQuality(
                    symbol=row.symbol,
                    duration_seconds=row.duration_seconds,
                    rows=row.rows,
                    distinct_datetime_nanos=row.distinct_nanos,
                    duplicate_identities=duplicate_identities,
                    first_datetime_nano=row.first_nano,
                    last_datetime_nano=row.last_nano,
                    first_timestamp=datetime_from_nano(row.first_nano)
                    .astimezone(SHANGHAI_TZ)
                    .isoformat(),
                    last_timestamp=datetime_from_nano(row.last_nano)
                    .astimezone(SHANGHAI_TZ)
                    .isoformat(),
                    null_trading_days=row.null_trading_days,
                    null_ohlc=row.null_ohlc,
                    invalid_ohlc=row.invalid_ohlc,
                    null_volume_or_oi=row.null_volume_oi,
                    invalid_volume_or_oi=row.invalid_volume_oi,
                    non_increasing_identities=row.non_increasing,
                    largest_observed_interval_nano=row.largest_interval,
                )
            )
        return results
    except MarketDataQueryError:
        raise
    except Exception as exc:
        raise _safe_failure("market-data quality query", exc, settings) from None


def _csv_value(value: Decimal | None) -> str:
    return "nan" if value is None else format(value, "f")


def export_market_bars_csv(
    engine: Engine,
    path: str | Path,
    *,
    symbol: str,
    duration_seconds: int,
    start_nano: int | None = None,
    end_nano: int | None = None,
    force: bool = False,
    settings: DatabaseSettings | None = None,
) -> MarketBarExportResult:
    symbol = _required_symbol(symbol)
    duration_seconds = _positive_duration(duration_seconds)
    start_nano, end_nano = _validate_bounds(start_nano, end_nano)
    target = Path(path)
    if target.exists() and not force:
        raise MarketDataQueryError(f"export target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temp_path = Path(raw_temp)
    rows = 0
    first_nano = None
    last_nano = None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(expected_kline_columns(symbol))
            with engine.connect().execution_options(stream_results=True) as connection:
                result = connection.execute(
                    _bar_select(symbol, duration_seconds, start_nano, end_nano)
                )
                for row in result:
                    if first_nano is None:
                        first_nano = row.datetime_nano
                    last_nano = row.datetime_nano
                    display = datetime_from_nano(row.datetime_nano).astimezone(
                        SHANGHAI_TZ
                    ).strftime("%Y-%m-%d %H:%M:%S.%f")
                    writer.writerow(
                        [
                            display,
                            row.datetime_nano,
                            _csv_value(row.open),
                            _csv_value(row.high),
                            _csv_value(row.low),
                            _csv_value(row.close),
                            _csv_value(row.volume),
                            _csv_value(row.open_oi),
                            _csv_value(row.close_oi),
                        ]
                    )
                    rows += 1
            handle.flush()
            os.fsync(handle.fileno())
        if rows == 0:
            raise MarketDataQueryError("no market bars match the export range")
        if force:
            os.replace(temp_path, target)
        else:
            # A same-directory hard link publishes the fully fsynced inode while
            # atomically failing if another writer created the target first.
            os.link(temp_path, target)
            temp_path.unlink()
        return MarketBarExportResult(
            path=str(target.resolve()),
            symbol=symbol,
            duration_seconds=duration_seconds,
            rows=rows,
            first_datetime_nano=first_nano,
            last_datetime_nano=last_nano,
        )
    except MarketDataQueryError:
        raise
    except FileExistsError:
        raise MarketDataQueryError(f"export target already exists: {target}") from None
    except Exception as exc:
        raise _safe_failure("market-bar export", exc, settings) from None
    finally:
        temp_path.unlink(missing_ok=True)
