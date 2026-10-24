from __future__ import annotations

import csv
import hashlib
import math
import operator
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from uuid import uuid4

from ak_trading.market_reference import (
    ReferenceDataError,
    SHANGHAI_TZ,
    TradingCalendar,
)
from ak_trading.persistence.config import DatabaseSettings, redact_database_text
from ak_trading.persistence.database import DatabaseOperationError

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


_BAR_FIELDS = ("open", "high", "low", "close", "volume", "open_oi", "close_oi")
_PRICE_FIELDS = frozenset({"open", "high", "low", "close"})
_MAX_BIGINT = 9_223_372_036_854_775_807
_NIGHT_SESSION_BOUNDARY = time(20, 0)


class MarketDataImportError(DatabaseOperationError):
    """A credential-safe K-line validation or database error."""


@dataclass(frozen=True)
class MarketBar:
    symbol: str
    duration_seconds: int
    datetime_nano: int
    ts: datetime
    trading_day: date | None
    trading_day_source: str
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    open_oi: Decimal | None
    close_oi: Decimal | None


@dataclass(frozen=True)
class MarketBarImportResult:
    batch_id: str
    source_sha256: str
    inserted: bool
    rows_read: int
    rows_written: int
    null_trading_days: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RecentKlineCsvResult:
    symbol: str
    rows: int
    first_datetime_nano: int
    last_datetime_nano: int


def _frame_decimal_text(value: object, field: str) -> str:
    if value is None:
        return "nan"
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketDataImportError(f"{field} is not numeric") from exc
    if math.isnan(number):
        return "nan"
    if not math.isfinite(number) or number < 0:
        raise MarketDataImportError(f"{field} must be finite and non-negative")
    scale = 10 if field in _PRICE_FIELDS else 8
    quantum = Decimal(1).scaleb(-scale)
    try:
        decimal_value = Decimal(str(value)).quantize(
            quantum, rounding=ROUND_HALF_EVEN
        )
    except (InvalidOperation, ValueError) as exc:
        raise MarketDataImportError(f"{field} exceeds database precision") from exc
    # Reuse the database-bound parser so snapshot formatting cannot bypass its limits.
    parsed = _parse_decimal(format(decimal_value, "f"), field)
    if parsed is None:
        raise MarketDataImportError(f"{field} unexpectedly became NULL")
    return format(parsed, "f")


def write_recent_kline_frame_csv(
    frame: Any,
    path: str | Path,
    *,
    symbol: str,
) -> RecentKlineCsvResult:
    """Write one TqSdk recent K-line frame in the strict import schema."""
    columns = expected_kline_columns(symbol)
    try:
        frame_columns = set(frame.columns)
    except (AttributeError, TypeError) as exc:
        raise MarketDataImportError("K-line frame must expose tabular columns") from exc
    required = {"datetime", *_BAR_FIELDS}
    missing = sorted(required - frame_columns)
    if missing:
        raise MarketDataImportError(
            f"K-line frame is missing columns: {', '.join(missing)}"
        )

    source = Path(path)
    first_nano: int | None = None
    last_nano: int | None = None
    rows = 0
    source.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(columns)
            field_positions = {
                name: frame.columns.get_loc(name) for name in required
            }
            for row in frame.itertuples(index=False, name=None):
                raw_nano = row[field_positions["datetime"]]
                try:
                    datetime_nano = operator.index(raw_nano)
                except TypeError as exc:
                    raise MarketDataImportError(
                        "K-line frame datetime must be an integer nanosecond value"
                    ) from exc
                timestamp = datetime_from_nano(datetime_nano)
                if datetime_nano == 0:
                    continue
                if last_nano is not None and datetime_nano <= last_nano:
                    raise MarketDataImportError(
                        "K-line frame datetime values must be strictly increasing"
                    )
                if first_nano is None:
                    first_nano = datetime_nano
                last_nano = datetime_nano
                display = timestamp.astimezone(SHANGHAI_TZ).strftime(
                    "%Y-%m-%d %H:%M:%S.%f"
                )
                writer.writerow(
                    [
                        display,
                        datetime_nano,
                        *(
                            _frame_decimal_text(row[field_positions[field]], field)
                            for field in _BAR_FIELDS
                        ),
                    ]
                )
                rows += 1
    except Exception:
        source.unlink(missing_ok=True)
        raise
    if rows == 0 or first_nano is None or last_nano is None:
        source.unlink(missing_ok=True)
        raise MarketDataImportError("K-line frame contains no valid rows")
    return RecentKlineCsvResult(
        symbol=symbol.strip(),
        rows=rows,
        first_datetime_nano=first_nano,
        last_datetime_nano=last_nano,
    )


def expected_kline_columns(symbol: str) -> tuple[str, ...]:
    if not isinstance(symbol, str) or not symbol.strip():
        raise MarketDataImportError("symbol must be a non-empty string")
    prefix = f"{symbol.strip()}."
    return ("datetime", "datetime_nano", *(prefix + field for field in _BAR_FIELDS))


def datetime_from_nano(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarketDataImportError("datetime_nano must be an integer")
    if value < 0 or value > _MAX_BIGINT:
        raise MarketDataImportError("datetime_nano is outside PostgreSQL BIGINT range")
    seconds, nanoseconds = divmod(value, 1_000_000_000)
    try:
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            seconds=seconds, microseconds=nanoseconds // 1_000
        )
    except OverflowError as exc:
        raise MarketDataImportError("datetime_nano is outside datetime range") from exc


def trading_day_for_timestamp(
    timestamp: datetime, calendar: TradingCalendar | None
) -> tuple[date | None, str]:
    if calendar is None:
        return None, "calendar_not_configured"
    local = timestamp.astimezone(SHANGHAI_TZ)
    local_date = local.date()
    if local_date < calendar.valid_from or local_date > calendar.valid_through:
        return None, "outside_reference_calendar"
    if local.time() >= _NIGHT_SESSION_BOUNDARY:
        try:
            return calendar.next_trading_day(local_date), "reference_calendar_night"
        except ReferenceDataError:
            return None, "outside_reference_calendar"
    try:
        if calendar.is_trading_day(local_date):
            return local_date, "reference_calendar_date"
    except ReferenceDataError:
        return None, "outside_reference_calendar"
    return None, "non_trading_calendar_date"


def _parse_decimal(raw: str | None, field: str) -> Decimal | None:
    if raw is None:
        raise MarketDataImportError(f"missing CSV field {field}")
    text = raw.strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise MarketDataImportError(f"{field} is not a decimal") from exc
    if not value.is_finite() or value < 0:
        raise MarketDataImportError(f"{field} must be a finite non-negative decimal")
    scale = 10 if field in _PRICE_FIELDS else 8
    quantum = Decimal(1).scaleb(-scale)
    try:
        if value != value.quantize(quantum):
            raise MarketDataImportError(f"{field} exceeds database scale {scale}")
    except InvalidOperation as exc:
        raise MarketDataImportError(f"{field} exceeds database precision") from exc
    integer_digits = max(value.adjusted() + 1, 1) if value != 0 else 1
    if integer_digits > 28 - scale:
        raise MarketDataImportError(f"{field} exceeds database precision")
    return value


def _validate_display_datetime(raw: str | None, timestamp: datetime) -> None:
    if raw is None or not raw.strip():
        raise MarketDataImportError("datetime must be a non-empty string")
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise MarketDataImportError("datetime is not an ISO-compatible timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    else:
        parsed = parsed.astimezone(SHANGHAI_TZ)
    expected = timestamp.astimezone(SHANGHAI_TZ)
    if parsed.replace(tzinfo=None) != expected.replace(tzinfo=None):
        raise MarketDataImportError(
            "datetime does not match authoritative datetime_nano"
        )


def iter_tqsdk_kline_csv(
    path: str | Path,
    *,
    symbol: str,
    duration_seconds: int,
    calendar: TradingCalendar | None = None,
) -> Iterator[MarketBar]:
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int):
        raise MarketDataImportError("duration_seconds must be an integer")
    if duration_seconds <= 0:
        raise MarketDataImportError("duration_seconds must be positive for K-lines")
    source = Path(path)
    columns = expected_kline_columns(symbol)
    prefix = f"{symbol.strip()}."
    try:
        handle = source.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise MarketDataImportError(f"cannot open K-line CSV: {source}") from exc
    with handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != columns:
            raise MarketDataImportError(
                "K-line CSV columns do not match the exact single-symbol DataDownloader schema"
            )
        for line_no, row in enumerate(reader, start=2):
            if None in row:
                raise MarketDataImportError(f"extra CSV field at line {line_no}")
            try:
                datetime_nano = int((row["datetime_nano"] or "").strip())
            except ValueError as exc:
                raise MarketDataImportError(
                    f"datetime_nano is not an integer at line {line_no}"
                ) from exc
            timestamp = datetime_from_nano(datetime_nano)
            _validate_display_datetime(row["datetime"], timestamp)
            trading_day, trading_day_source = trading_day_for_timestamp(
                timestamp, calendar
            )
            values = {
                field: _parse_decimal(row[prefix + field], field)
                for field in _BAR_FIELDS
            }
            yield MarketBar(
                symbol=symbol.strip(),
                duration_seconds=duration_seconds,
                datetime_nano=datetime_nano,
                ts=timestamp,
                trading_day=trading_day,
                trading_day_source=trading_day_source,
                **values,
            )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise MarketDataImportError(f"cannot read K-line CSV: {path}") from exc
    return digest.hexdigest()


def import_market_bars(
    engine: Engine,
    path: str | Path,
    *,
    symbol: str,
    duration_seconds: int,
    calendar: TradingCalendar | None = None,
    reference_sha256: str | None = None,
    source_label: str | None = None,
    settings: DatabaseSettings | None = None,
    chunk_size: int = 1000,
) -> MarketBarImportResult:
    from sqlalchemy import func, select, update
    from sqlalchemy.dialects.postgresql import insert

    from ak_trading.persistence.schema import ingestion_batches, market_bars

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise MarketDataImportError("chunk_size must be a positive integer")
    source = Path(path)
    source_sha256 = _file_sha256(source)
    source_path = source_label or str(source.resolve())
    if not isinstance(source_path, str) or not source_path.strip():
        raise MarketDataImportError("source_label must be a non-empty string")
    source_path = source_path.strip()
    batch_id = uuid4().hex
    rows_read = 0
    rows_written = 0
    null_trading_days = 0

    def write_chunk(connection, values: list[dict[str, object]]) -> int:
        statement = insert(market_bars).values(values)
        excluded = statement.excluded
        statement = statement.on_conflict_do_update(
            constraint="uq_market_bars_symbol_duration_nano",
            set_={
                "ts": excluded.ts,
                "trading_day": excluded.trading_day,
                "trading_day_source": excluded.trading_day_source,
                "open": excluded.open,
                "high": excluded.high,
                "low": excluded.low,
                "close": excluded.close,
                "volume": excluded.volume,
                "open_oi": excluded.open_oi,
                "close_oi": excluded.close_oi,
                "source": excluded.source,
                "batch_id": excluded.batch_id,
                "imported_at": func.now(),
            },
        ).returning(market_bars.c.id)
        return len(connection.execute(statement).scalars().all())

    try:
        with engine.begin() as connection:
            batch_statement = (
                insert(ingestion_batches)
                .values(
                    batch_id=batch_id,
                    kind="tqsdk_kline_csv",
                    source=source_path,
                    source_sha256=source_sha256,
                    status="running",
                    details={
                        "symbol": symbol.strip(),
                        "duration_seconds": duration_seconds,
                        "reference_sha256": reference_sha256,
                    },
                )
                .on_conflict_do_nothing(
                    constraint="uq_ingestion_batches_kind_source"
                )
                .returning(ingestion_batches.c.batch_id)
            )
            inserted_batch = connection.execute(batch_statement).scalar_one_or_none()
            if inserted_batch is None:
                existing = connection.execute(
                    select(
                        ingestion_batches.c.batch_id,
                        ingestion_batches.c.status,
                        ingestion_batches.c.rows_read,
                        ingestion_batches.c.rows_written,
                        ingestion_batches.c.null_trading_days,
                        ingestion_batches.c.details,
                    ).where(
                        ingestion_batches.c.kind == "tqsdk_kline_csv",
                        ingestion_batches.c.source_sha256 == source_sha256,
                    )
                ).one()
                if existing.status != "completed":
                    raise MarketDataImportError(
                        f"existing ingestion batch {existing.batch_id} is not complete"
                    )
                expected_details = {
                    "symbol": symbol.strip(),
                    "duration_seconds": duration_seconds,
                    "reference_sha256": reference_sha256,
                }
                if dict(existing.details) != expected_details:
                    raise MarketDataImportError(
                        "the same CSV content was already imported with different metadata"
                    )
                return MarketBarImportResult(
                    batch_id=existing.batch_id,
                    source_sha256=source_sha256,
                    inserted=False,
                    rows_read=existing.rows_read,
                    rows_written=existing.rows_written,
                    null_trading_days=existing.null_trading_days,
                )

            chunk: list[dict[str, object]] = []
            for bar in iter_tqsdk_kline_csv(
                source,
                symbol=symbol,
                duration_seconds=duration_seconds,
                calendar=calendar,
            ):
                rows_read += 1
                if bar.trading_day is None:
                    null_trading_days += 1
                values = asdict(bar)
                values.update({"source": source_path, "batch_id": batch_id})
                chunk.append(values)
                if len(chunk) >= chunk_size:
                    rows_written += write_chunk(connection, chunk)
                    chunk.clear()
            if chunk:
                rows_written += write_chunk(connection, chunk)
            if rows_read == 0:
                raise MarketDataImportError("K-line CSV contains no data rows")
            connection.execute(
                update(ingestion_batches)
                .where(ingestion_batches.c.batch_id == batch_id)
                .values(
                    status="completed",
                    rows_read=rows_read,
                    rows_written=rows_written,
                    null_trading_days=null_trading_days,
                    finished_at=func.now(),
                )
            )
        return MarketBarImportResult(
            batch_id=batch_id,
            source_sha256=source_sha256,
            inserted=True,
            rows_read=rows_read,
            rows_written=rows_written,
            null_trading_days=null_trading_days,
        )
    except MarketDataImportError:
        raise
    except Exception as exc:
        message = redact_database_text(exc, settings)
        raise MarketDataImportError(f"market-bar import failed: {message}") from None
