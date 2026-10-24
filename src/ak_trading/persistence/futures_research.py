"""Bounded read-only PostgreSQL export for the futures research library.

Owns exactly one connection per export (finalized-plan §六.1-4): the snapshot
is opened with an explicit ``SET TRANSACTION ISOLATION LEVEL REPEATABLE READ
READ ONLY`` as the first statement after BEGIN, verified via ``SHOW`` before
any business SELECT, and every page shares that connection and snapshot. No
writes, no migrations, no DSN in any artifact. Callers pass an explicit
engine/connection; downstream code never builds engines here.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from ak_trading.futures.data import (
    ContractInstance,
    SHANGHAI_TZ,
    UniversePolicy,
    datetime_to_nano,
    load_catalog_products,
    load_universe_cache,
    resolve_product_contracts,
)
from ak_trading.futures.io import (
    OutputDirectoryError,
    assert_output_directory_is_new,
    bars_rows_from_bars,
    publish_input_bundle,
)
from ak_trading.futures.spec import (
    ContractBar,
    ProductKey,
)
from ak_trading.persistence.schema import market_bars

__all__ = [
    "ExportLimits",
    "ExportRequest",
    "ExportOutcome",
    "ReadOnlySnapshotEvidence",
    "BarValidationCounts",
    "BAR_COLUMNS",
    "select_contract_bars",
    "begin_read_only_snapshot",
    "validate_bar_row",
    "export_futures_bundle",
    "validate_export_request",
    "build_availability_payload",
    "build_contracts_payload",
]

BAR_COLUMNS: tuple[str, ...] = (
    "symbol",
    "duration_seconds",
    "datetime_nano",
    "ts",
    "trading_day",
    "trading_day_source",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_oi",
    "close_oi",
    "source",
    "batch_id",
    "imported_at",
)


@dataclass(frozen=True)
class ExportLimits:
    """Frozen default bounds (§六.4); over-limit is scope_limit, never a
    silent truncation."""

    max_products: int = 6
    max_contracts: int = 600
    max_bar_rows: int = 200_000
    page_size: int = 5_000
    max_bundle_bytes: int = 268_435_456


@dataclass(frozen=True)
class ExportRequest:
    products: tuple[ProductKey, ...]
    span_start: datetime
    span_end: datetime
    as_of: datetime
    universe_cache_path: Path
    catalog_path: Path
    duration_seconds: int = 86400
    local_metadata_paths: tuple[Path, ...] = ()
    limits: ExportLimits = ExportLimits()

    @property
    def start_nano(self) -> int:
        return datetime_to_nano(self.span_start)

    @property
    def end_nano(self) -> int:
        return datetime_to_nano(self.span_end)


@dataclass(frozen=True)
class ReadOnlySnapshotEvidence:
    """What the exporter observed about its single read-only snapshot."""

    mechanism: str
    driver: str
    observed_isolation: str
    observed_read_only: str
    verified: bool
    not_executed_reason: str | None = None


@dataclass
class BarValidationCounts:
    """Mutable accumulator used while paging; reported in outcomes."""

    valid_rows: int = 0
    duplicate_keys: int = 0
    invalid_ohlc: int = 0
    ordering_errors: int = 0
    negative_volume_oi: int = 0
    conflicting_identity: int = 0

    @property
    def has_rejections(self) -> bool:
        return (
            self.duplicate_keys
            + self.invalid_ohlc
            + self.ordering_errors
            + self.negative_volume_oi
            + self.conflicting_identity
            > 0
        )


@dataclass(frozen=True)
class ExportOutcome:
    status: str  # published | export_blocked | scope_limit | empty_real_export | validation_rejected
    reasons: tuple[str, ...]
    row_counts: Mapping[str, int]
    manifest: Mapping[str, object] | None = None
    output_path: str | None = None
    read_only_evidence: ReadOnlySnapshotEvidence | None = None


def select_contract_bars(
    connection,
    symbols: Sequence[str],
    duration_seconds: int,
    start_nano: int,
    end_nano: int,
    page_size: int = 5000,
) -> Iterator[list[Mapping[str, object]]]:
    """Keyset-paginated bounded SELECT over ``market_bars`` (§六.3).

    One statement shape, bind parameters only, ordering by
    (symbol, datetime_nano); pages share the caller's connection. No OFFSET,
    no unbounded DISTINCT.
    """
    from sqlalchemy import bindparam, select, tuple_

    table = market_bars
    columns = [table.c[name] for name in BAR_COLUMNS]
    last_symbol: str | None = None
    last_nano: int | None = None
    while True:
        statement = (
            select(*columns)
            .where(
                table.c.symbol.in_(bindparam("symbols", expanding=True)),
                table.c.duration_seconds == bindparam("duration"),
                table.c.datetime_nano >= bindparam("start_nano"),
                table.c.datetime_nano < bindparam("end_nano"),
            )
            .order_by(table.c.symbol, table.c.datetime_nano)
            .limit(bindparam("page_size"))
        )
        if last_symbol is not None and last_nano is not None:
            statement = statement.where(
                tuple_(table.c.symbol, table.c.datetime_nano)
                > tuple_(bindparam("last_symbol"), bindparam("last_nano"))
            )
        rows = connection.execute(
            statement,
            {
                "symbols": list(symbols),
                "duration": duration_seconds,
                "start_nano": start_nano,
                "end_nano": end_nano,
                "page_size": page_size,
                "last_symbol": last_symbol,
                "last_nano": last_nano,
            },
        ).mappings().all()
        if not rows:
            return
        yield [dict(row) for row in rows]
        if len(rows) < page_size:
            return
        last_symbol = str(rows[-1]["symbol"])
        last_nano = int(rows[-1]["datetime_nano"])


def begin_read_only_snapshot(connection) -> ReadOnlySnapshotEvidence:
    """Open the transaction snapshot and verify READ ONLY + RR via SHOW.

    The explicit ``SET TRANSACTION`` must be the first statement after BEGIN
    (§六.1-2). If verification fails the caller must treat the export as
    blocked; there is no fallback to a read-write transaction.
    """
    from sqlalchemy import text

    driver = getattr(connection.dialect, "driver", "")
    transaction = connection.begin()
    try:
        connection.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        )
        isolation = str(
            connection.execute(text("SHOW transaction_isolation")).scalar_one()
        )
        read_only = str(
            connection.execute(text("SHOW transaction_read_only")).scalar_one()
        )
    except Exception as exc:  # pragma: no cover - driver-specific failure path
        transaction.rollback()
        return ReadOnlySnapshotEvidence(
            mechanism="explicit_set_transaction_read_only",
            driver=str(driver),
            observed_isolation="",
            observed_read_only="",
            verified=False,
            not_executed_reason=f"read_only_setup_failed:{type(exc).__name__}",
        )
    verified = (
        isolation.strip().lower() == "repeatable read"
        and read_only.strip().lower() == "on"
    )
    return ReadOnlySnapshotEvidence(
        mechanism="explicit_set_transaction_read_only",
        driver=str(driver),
        observed_isolation=isolation,
        observed_read_only=read_only,
        verified=verified,
        not_executed_reason=None if verified else "show_verification_mismatch",
    )


def _valid_price(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def validate_bar_row(row: Mapping[str, object]) -> tuple[str | None, ContractBar | None]:
    """Validate one raw row; returns (rejection_reason, bar) (§六.4).

    NULL volume/OI is preserved; NULL or non-positive OHLC is rejected;
    high/low ordering must hold; negative volume/OI is rejected.
    """
    open_price = _valid_price(row.get("open"))
    high_price = _valid_price(row.get("high"))
    low_price = _valid_price(row.get("low"))
    close_price = _valid_price(row.get("close"))
    if open_price is None or high_price is None or low_price is None or close_price is None:
        return "invalid_ohlc", None
    if not (high_price >= low_price and high_price >= open_price and high_price >= close_price
            and low_price <= open_price and low_price <= close_price):
        return "ordering_error", None

    def _oi(name: str) -> tuple[str | None, Decimal | None]:
        raw = row.get(name)
        if raw is None:
            return None, None
        try:
            value = raw if isinstance(raw, Decimal) else Decimal(str(raw))
        except Exception:
            return "negative_volume_oi", None
        if not value.is_finite() or value < 0:
            return "negative_volume_oi", None
        return None, value

    volume_error, volume = _oi("volume")
    if volume_error:
        return volume_error, None
    open_oi_error, open_oi = _oi("open_oi")
    if open_oi_error:
        return open_oi_error, None
    close_oi_error, close_oi = _oi("close_oi")
    if close_oi_error:
        return close_oi_error, None

    ts = row.get("ts")
    if not isinstance(ts, datetime):
        return "conflicting_identity", None
    if ts.tzinfo is None:
        # Drivers without tz-aware DATETIME (e.g. the SQLite test fixture)
        # lose the offset; the market timezone is the only legal reading.
        ts = ts.replace(tzinfo=SHANGHAI_TZ)
    imported_at = row.get("imported_at")
    available_at: datetime | None = None
    if isinstance(imported_at, datetime):
        # imported_at is a conservative availability bound: the bar was
        # visible to research no earlier than its pipeline import.
        available_at = imported_at if imported_at.tzinfo else imported_at.replace(
            tzinfo=SHANGHAI_TZ
        )
    trading_day = row.get("trading_day")

    bar = ContractBar(
        symbol=str(row["symbol"]),
        duration_seconds=int(row["duration_seconds"]),  # type: ignore[arg-type]
        datetime_nano=int(row["datetime_nano"]),  # type: ignore[arg-type]
        ts=ts,
        source=str(row.get("source") or ""),
        trading_day=trading_day if hasattr(trading_day, "isoformat") else None,
        trading_day_source=str(row.get("trading_day_source") or ""),
        available_at=available_at,
        open=open_price,
        high=high_price,
        low=low_price,
        close=close_price,
        volume=volume,
        open_oi=open_oi,
        close_oi=close_oi,
    )
    return None, bar


def build_contracts_payload(
    instances: Sequence[ContractInstance], request_start: datetime
) -> list[dict[str, object]]:
    """contracts.json payload: cache identity with the 730-day window rule."""
    payload: list[dict[str, object]] = []
    for instance in instances:
        window_start, listing_flag = instance.research_window_start(request_start)
        payload.append(
            {
                "instance_id": instance.instance_id,
                "raw_symbol": instance.raw_symbol,
                "product": str(instance.product),
                "expiry_at": instance.expiry_at.isoformat() if instance.expiry_at else None,
                "expired_in_cache": instance.expired_in_cache,
                "identity_status": instance.identity_status,
                "listing_at": instance.listing_at.isoformat() if instance.listing_at else None,
                "listing_flag": listing_flag,
                "observation_window_start": window_start.isoformat(),
                "identity_issues": list(instance.identity_issues),
            }
        )
    return payload


def build_availability_payload(
    products: Sequence[ProductKey],
    source_hashes: Mapping[str, str],
    row_counts_per_product: Mapping[str, int],
) -> dict[str, object]:
    """availability.json payload for a real export (§六.10, §七.8).

    Strict execution fields remain ``missing_local_approved_evidence`` unless
    a graded source proves them; bars coverage records actual exported rows.
    """
    strict_fields = (
        "historical_session_windows",
        "trading_day_source_verified",
        "parameter_available_at",
        "last_trade_at",
        "delivery_cutoff_at",
        "multiplier",
        "price_tick",
        "fee_basis_close_today",
        "margin_basis_settlement_price",
        "limit_bands",
    )
    products_payload: dict[str, object] = {}
    for product in products:
        products_payload[str(product)] = {
            "exported_bar_rows": row_counts_per_product.get(str(product), 0),
            "strict_fields": {
                name: {
                    "status": "missing_local_approved_evidence",
                    "reason": "no approved historical evidence in local sources",
                }
                for name in strict_fields
            },
        }
    return {
        "products": products_payload,
        "member_factor": {
            "status": "not_requested",
            "reason": "deferred_no_default_strategy_dependency",
        },
        "source_hashes": dict(source_hashes),
    }


# Inventory bounds are separate from the frozen bar-export limits so default
# bundles retain their original byte representation.
_METADATA_LIMITS = {"files": 32, "file_bytes": 8 * 1024 * 1024,
                    "total_bytes": 32 * 1024 * 1024, "rows": 100_000,
                    "nodes": 200_000, "depth": 12}
_EXCHANGES = {name: name for name in ("SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX")}
_EXCHANGES.update({"上期所": "SHFE", "上海期货交易所": "SHFE", "能源中心": "INE",
                   "大商所": "DCE", "郑商所": "CZCE", "中金所": "CFFEX", "广期所": "GFEX"})
_NUMERIC_FIELDS = {
    "合约乘数": "multiplier", "最小跳动": "price_tick",
    "开仓费率": "fees", "开仓费用/手": "fees", "平仓费率": "fees", "平仓费用/手": "fees",
    "平今费率": "fees", "平今费用/手": "fees", "做多保证金率": "margin", "做空保证金率": "margin",
    "做多保证金/手": "margin", "做空保证金/手": "margin", "上日结算价": "settlement",
    "multiplier": "multiplier", "contract_size_tons": "multiplier", "contract_size.value": "multiplier",
    "price_tick": "price_tick", "price_tick_cny_per_ton": "price_tick", "price_tick.value": "price_tick",
    "VolumeMultiple": "multiplier", "PriceTick": "price_tick", "margin_long": "margin",
    "margin_short": "margin", "spec_long": "margin", "SPECLONGMARGINRATIO": "margin",
    "SPECSHORTMARGINRATIO": "margin", "margin.rate_of_notional": "margin",
    "margin.exchange_rate_of_notional": "margin", "fee_open_ratio": "fees",
    "fee_open_per_lot": "fees", "fee_close_today_ratio": "fees", "fee_close_today_per_lot": "fees",
    "fee_ratio": "fees", "fee_unit": "fees", "ct_fee_ratio": "fees", "ct_fee_unit": "fees",
    "TRADEFEERATIO": "fees", "TRADEFEEUNIT": "fees", "TTRADEFEERATIO": "fees", "TTRADEFEEUNIT": "fees",
    "settlement_price": "settlement", "SETTLEMENTPRICE": "settlement",
    "limit_up": "limits", "limit_down": "limits", "UPPERLIMITPRICE": "limits", "LOWERLIMITPRICE": "limits",
    "lot_step": "lot_rules", "min_open_lots": "lot_rules",
    **{f"fees.components.{action}.value": "fees" for action in ("open", "close", "close_today")},
    **{f"fees.actions.{action}.component_value": "fees" for action in ("open", "close", "close_today")},
}
_DATE_FIELDS = {"更新时间", "updated_at", "observed_at", "generated_at", "day", "date",
                "trading_day", "TradingDay", "publication_date", "transcribed_at", "last_reviewed_date",
                "last_trading_day", "last_trade_at", "delivery_cutoff_at", "expire_date", "ExpireDate",
                "effective_from", "effective_through", "available_at", "valid_from", "valid_through",
                "must_reverify_before", "closed_dates"}
_ID_FIELDS = {"交易所", "合约代码", "品种代码", "exchange", "code", "product", "symbol", "raw_symbol",
              "research_contract", "applicable_contracts", "InstrumentID", "ExchangeID", "INSTRUMENTID",
              "DELIVERYMONTH", "PRODUCTID"}
_DATE_SEMANTICS = {"last_trading_day": "lifecycle", "last_trade_at": "lifecycle",
                   "delivery_cutoff_at": "lifecycle", "available_at": "parameter_availability"}


class _MetadataInputError(ValueError):
    """Fixed, credential-safe reason code; source text is never included."""


def _metadata_date(value: object) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-?\d{2}-?\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?)?", value.strip()):
        return None
    try:
        text = value.strip()
        return datetime.fromisoformat(text).isoformat() if "T" in text or " " in text else date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _metadata_symbol(value: object, exchange: str = "") -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if "." not in text and exchange:
        text = f"{exchange}.{text}"
    return text if re.fullmatch(r"(?:SHFE|INE|DCE|CZCE|CFFEX|GFEX)\.[A-Za-z]{1,8}[0-9]{3,4}", text) else None


def _metadata_flatten(row: Mapping, prefix: str = "") -> Iterator[tuple[str, object]]:
    for key, value in row.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            # Traverse allowlisted namespaces only: free text and credentials
            # hidden under unrelated containers never become output headers.
            if name in ("fees", "margin", "fees.components", "fees.actions", "sessions", "calendar",
                        "static_provenance", "fees.provenance", "margin.provenance", "provenance",
                        "contract_size", "price_tick") or any(field.startswith(name + ".") for field in _NUMERIC_FIELDS):
                yield from _metadata_flatten(value, name)
        elif name in _NUMERIC_FIELDS or str(key) in _DATE_FIELDS or str(key) in _ID_FIELDS or name in (
            "sessions.day", "sessions.night", "currency", "fees.basis", "margin.price_basis",
        ):
            yield name, value


def _metadata_rows(payload: bytes, format_name: str) -> tuple[list[Mapping], str, set[str], Mapping]:
    if format_name == "csv":
        try:
            reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""), strict=True)
            headers = reader.fieldnames or []
            if not headers or len(headers) != len(set(headers)):
                raise _MetadataInputError("local_metadata_invalid_csv_header")
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise _MetadataInputError("local_metadata_invalid_csv_row")
                rows.append(row)
                if len(rows) > _METADATA_LIMITS["rows"]:
                    raise _MetadataInputError("local_metadata_rows_scope_limit")
        except (UnicodeError, csv.Error):
            raise _MetadataInputError("local_metadata_invalid_csv") from None
        allowed = set(headers) & (set(_NUMERIC_FIELDS) | _DATE_FIELDS | _ID_FIELDS)
        if not allowed:
            raise _MetadataInputError("local_metadata_unsupported_shape")
        shape = "openctp_raw" if {"交易所", "合约代码"} <= set(headers) else "csv_records"
        return rows, shape, allowed, {}

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise _MetadataInputError("local_metadata_duplicate_json_key")
            result[key] = value
        return result

    try:
        document = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=unique_object)
    except (UnicodeError, ValueError, RecursionError):
        raise _MetadataInputError("local_metadata_invalid_json") from None
    pending = [(document, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > _METADATA_LIMITS["depth"] or nodes > _METADATA_LIMITS["nodes"]:
            raise _MetadataInputError("local_metadata_json_scope_limit")
        if isinstance(item, Mapping):
            pending.extend((value, depth + 1) for value in item.values())
        elif isinstance(item, list):
            pending.extend((value, depth + 1) for value in item)
    context = document if isinstance(document, Mapping) else {}
    if isinstance(document, list):
        rows, shape = document, "json_records"
    elif not isinstance(document, Mapping):
        raise _MetadataInputError("local_metadata_unsupported_shape")
    elif "products" in document:
        rows, shape = document["products"], "reference_products"
    elif "official" in document or "reference" in document:
        rows, shape = [], "settlement_snapshot"
        for section in ("official", "reference"):
            entries = document.get(section, {})
            if not isinstance(entries, Mapping):
                raise _MetadataInputError("local_metadata_invalid_json_records")
            for key, value in entries.items():
                if not isinstance(value, Mapping):
                    raise _MetadataInputError("local_metadata_invalid_json_records")
                rows.append({**value, "product": key})
    else:
        rows, shape = [document], "json_record"
        for key in ("o_cursor", "records", "data", "contracts", "snapshots"):
            if key in document:
                rows, shape = document[key], key + "_records"
                break
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise _MetadataInputError("local_metadata_invalid_json_records")
    if len(rows) > _METADATA_LIMITS["rows"]:
        raise _MetadataInputError("local_metadata_rows_scope_limit")
    if not rows and shape == "json_record":
        raise _MetadataInputError("local_metadata_unsupported_shape")
    return rows, shape, set(), context


def _inventory_record(payload: bytes, format_name: str) -> dict[str, object]:
    rows, shape, observed_fields, context = _metadata_rows(payload, format_name)
    dates: dict[str, set[str]] = {}
    numeric: dict[str, dict[str, object]] = {}
    contracts: dict[str, dict] = {}
    products: dict[str, dict] = {}
    for name, value in _metadata_flatten(context):
        if name.rsplit(".", 1)[-1] in _DATE_FIELDS:
            observed_fields.add(name)
            for raw in value if isinstance(value, list) else [value]:
                detected = _metadata_date(raw)
                if detected:
                    dates.setdefault(name, set()).add(detected)
    for row in rows:
        row_fields, semantics = set(), set()
        field_semantics: dict[str, str] = {}
        for name, value in _metadata_flatten(row):
            observed_fields.add(name)
            row_fields.add(name)
            if name in _NUMERIC_FIELDS and not isinstance(value, bool):
                try:
                    number = Decimal(str(value))
                except (InvalidOperation, ValueError):
                    continue
                if not number.is_finite() or number < 0 or abs(number.adjusted()) > 100:
                    continue
                semantic = _NUMERIC_FIELDS[name]
                if semantic in ("multiplier", "price_tick", "lot_rules") and number <= 0:
                    continue
                semantics.add(semantic)
                field_semantics[name] = semantic
                stats = numeric.setdefault(name, {"count": 0, "min": number, "max": number})
                stats["count"] += 1
                stats["min"] = min(stats["min"], number)
                stats["max"] = max(stats["max"], number)
            if name in ("sessions.day", "sessions.night") and isinstance(value, list) and any(
                isinstance(window, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", window)
                for window in value
            ):
                semantics.add("sessions")
                field_semantics[name] = "sessions"
            if name == "currency" and value in ("CNY", "USD", "EUR", "HKD"):
                semantics.add("currency")
                field_semantics[name] = "currency"
            leaf = name.rsplit(".", 1)[-1]
            if leaf in _DATE_FIELDS:
                for raw in value if isinstance(value, list) else [value]:
                    detected = _metadata_date(raw)
                    if detected:
                        dates.setdefault(name, set()).add(detected)
                        if leaf in _DATE_SEMANTICS:
                            semantics.add(_DATE_SEMANTICS[leaf])
                            field_semantics[name] = _DATE_SEMANTICS[leaf]
        exchange = _EXCHANGES.get(str(row.get("交易所", row.get("exchange", row.get("ExchangeID", "")))).strip(), "")
        symbols = {_metadata_symbol(row.get(key), exchange) for key in ("合约代码", "symbol", "raw_symbol", "research_contract", "InstrumentID", "INSTRUMENTID")}
        symbols.discard(None)
        scoped_sections = {section for section in ("fees", "margin") if isinstance(row.get(section), Mapping)
                           and "applicable_contracts" in row[section]}
        base_fields = {name for name in row_fields if name.split(".", 1)[0] not in scoped_sections}
        symbol_fields = {symbol: set(base_fields) for symbol in symbols}
        for section in scoped_sections:
            applicable = row[section]["applicable_contracts"]
            if isinstance(applicable, list):
                for raw in applicable:
                    symbol = _metadata_symbol(raw)
                    if symbol:
                        symbol_fields.setdefault(symbol, set()).update(
                            name for name in row_fields if name.startswith(section + ".")
                        )
        symbols = set(symbol_fields)
        product_names = {symbol.split(".", 1)[0] + "." + _product_of(symbol) for symbol in symbols}
        for symbol, scoped_fields in symbol_fields.items():
            entry = contracts.setdefault(symbol, {"row_count": 0, "fields": set(), "semantic_fields": set()})
            entry["row_count"] += 1
            entry["fields"].update(scoped_fields)
            entry["semantic_fields"].update(field_semantics[name] for name in scoped_fields if name in field_semantics)
        declared_product = row.get("product")
        if isinstance(declared_product, str) and re.fullmatch(r"(?:SHFE|INE|DCE|CZCE|CFFEX|GFEX)\.[A-Za-z]{1,8}", declared_product):
            product_names.add(declared_product.upper())
        code = str(row.get("品种代码", row.get("code", ""))).strip()
        if exchange and re.fullmatch(r"[A-Za-z]{1,8}", code):
            product_names.add(f"{exchange}.{code.upper()}")
        for key in product_names:
            entry = products.setdefault(key, {"row_count": 0, "fields": set(), "semantic_fields": set()})
            entry["row_count"] += 1
            entry["fields"].update(row_fields)
            entry["semantic_fields"].update(semantics)
    if rows and not observed_fields:
        raise _MetadataInputError("local_metadata_unsupported_shape")
    digest = hashlib.sha256(payload).hexdigest()
    def presence(entries: Mapping) -> dict:
        return {key: {"row_count": value["row_count"], "fields": sorted(value["fields"]),
                      "semantic_fields": sorted(value["semantic_fields"])} for key, value in sorted(entries.items())}
    return {
        "source_id": "local_metadata:" + digest, "sha256": digest, "size_bytes": len(payload),
        "format": format_name, "shape": shape, "row_count": len(rows), "fields": sorted(observed_fields),
        "detected_dates": {key: sorted(value) for key, value in sorted(dates.items())},
        "date_interpretation": "observed_source_labels_only_not_validity_periods",
        "numeric_summary": {key: {"count": value["count"], "min": str(value["min"]), "max": str(value["max"])}
                            for key, value in sorted(numeric.items())},
        "contract_presence": presence(contracts), "product_presence": presence(products),
        "approval_status": "unapproved", "historical_validity": "not_assessed",
        "raw_contents_copied": False,
    }


def _load_metadata_inventory(paths: Sequence[Path]) -> dict[str, object]:
    if len(paths) > _METADATA_LIMITS["files"]:
        raise _MetadataInputError("local_metadata_files_scope_limit")
    total = 0
    records: dict[str, dict] = {}
    for path in paths:
        if path.suffix.lower() not in (".csv", ".json"):
            raise _MetadataInputError("local_metadata_unsupported_format")
        try:
            if not path.is_file():
                raise _MetadataInputError("local_metadata_not_readable_file")
            with path.open("rb") as handle:
                payload = handle.read(_METADATA_LIMITS["file_bytes"] + 1)
        except OSError:
            raise _MetadataInputError("local_metadata_not_readable_file") from None
        total += len(payload)
        if len(payload) > _METADATA_LIMITS["file_bytes"] or total > _METADATA_LIMITS["total_bytes"]:
            raise _MetadataInputError("local_metadata_bytes_scope_limit")
        record = _inventory_record(payload, path.suffix.lower()[1:])
        records[record["source_id"]] = record
    return {"schema_version": "futures_local_metadata_inventory_v1", "status": "loaded",
            "scope": "explicit_local_files_only", "sources": [records[key] for key in sorted(records)],
            "limits": dict(_METADATA_LIMITS), "raw_contents_copied": False}


def _inventory_presence(inventory: Mapping, scope: str, key: str) -> dict[str, object]:
    matched = [(source["source_id"], source[scope][key]) for source in inventory["sources"] if key in source[scope]]
    return {
        "status": "present_reference_only" if matched else "not_observed_in_loaded_sources",
        "scope": "explicit_local_files_only", "source_ids": sorted(source_id for source_id, _ in matched),
        "semantic_fields": sorted({field for _, entry in matched for field in entry["semantic_fields"]}),
        "field_sources": {field: sorted(source_id for source_id, entry in matched if field in entry["semantic_fields"])
                          for field in sorted({field for _, entry in matched for field in entry["semantic_fields"]})},
        "approval_status": "unapproved", "historical_validity": "not_assessed",
        "identity_interpretation": "source_labels_only_not_historical_contract_instance_validity",
    }


def validate_export_request(request: ExportRequest, output: Path) -> ExportOutcome | None:
    """Validate local inputs before callers construct an engine; no writes.

    Returned reasons are fixed codes or validated product identities, never
    exception messages or source contents. The exporter repeats this check
    because callers may invoke it directly or change files after preflight.
    """
    def blocked(reason: str, status: str = "export_blocked") -> ExportOutcome:
        return ExportOutcome(status=status, reasons=(reason,), row_counts={})

    if not request.products:
        return blocked("products_required")
    if (isinstance(request.duration_seconds, bool)
            or not isinstance(request.duration_seconds, int)
            or request.duration_seconds <= 0):
        return blocked("duration_must_be_positive_integer")
    if any(not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None
           for value in (request.span_start, request.span_end, request.as_of)):
        return blocked("timezone_aware_datetimes_required")
    if request.span_start >= request.span_end:
        return blocked("start_must_precede_end")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for value in vars(request.limits).values()):
        return blocked("export_limits_must_be_positive_integers")
    ordered, issues = UniversePolicy().review(request.products)
    if issues:
        return blocked("invalid_or_unsupported_products")
    if len(ordered) > request.limits.max_products:
        return blocked("max_products_exceeded", "scope_limit")
    try:
        target = Path(output).resolve()
        if Path(output).is_symlink():
            return blocked("output_directory_exists")
        assert_output_directory_is_new(target, [
            request.universe_cache_path.parent.resolve(), request.catalog_path.parent.resolve(),
            *(path.resolve() for path in request.local_metadata_paths),
        ])
        ancestor = target.parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            return blocked("output_parent_not_directory")
    except (OSError, RuntimeError, OutputDirectoryError):
        return blocked("output_directory_invalid_or_overlapping")
    try:
        universe = load_universe_cache(request.universe_cache_path)
    except Exception:
        return blocked("universe_cache_unreadable_or_invalid")
    try:
        catalog = load_catalog_products(request.catalog_path)
    except Exception:
        return blocked("catalog_unreadable_or_invalid")
    if any(product not in catalog for product in ordered):
        return blocked("product_not_in_catalog")
    count = sum(len(resolve_product_contracts(universe, product)) for product in ordered)
    if count > request.limits.max_contracts:
        return blocked("max_contracts_exceeded", "scope_limit")
    if not count:
        return blocked("no_contracts_in_requested_universe")
    try:
        _load_metadata_inventory(request.local_metadata_paths)
    except _MetadataInputError as exc:
        reason = str(exc)
        return blocked(reason, "scope_limit" if "scope_limit" in reason else "export_blocked")
    return None


def export_futures_bundle(
    engine,
    request: ExportRequest,
    output: Path,
) -> ExportOutcome:
    """Run one bounded read-only export and publish the bundle exclusively."""
    failure = validate_export_request(request, output)
    if failure is not None:
        return failure
    limits = request.limits
    universe = load_universe_cache(request.universe_cache_path)
    catalog = load_catalog_products(request.catalog_path)

    policy = UniversePolicy()
    ordered, issues = policy.review(list(request.products))
    if issues:
        return ExportOutcome(
            status="export_blocked",
            reasons=tuple(f"product_issue:{issue}" for issue in issues),
            row_counts={},
        )
    if len(ordered) > limits.max_products:
        return ExportOutcome(
            status="scope_limit",
            reasons=(f"max_products_exceeded:{len(ordered)}>{limits.max_products}",),
            row_counts={},
        )
    outside_catalog = [str(p) for p in ordered if p not in catalog]
    if outside_catalog:
        return ExportOutcome(
            status="export_blocked",
            reasons=tuple(f"product_not_in_catalog:{name}" for name in outside_catalog),
            row_counts={},
        )

    symbols_by_product: dict[str, list[str]] = {}
    instances: list[ContractInstance] = []
    for product in ordered:
        members = resolve_product_contracts(universe, product)
        instances.extend(members)
        symbols_by_product[str(product)] = [member.raw_symbol for member in members]
    if len(instances) > limits.max_contracts:
        return ExportOutcome(
            status="scope_limit",
            reasons=(
                f"max_contracts_exceeded:{len(instances)}>{limits.max_contracts}",
            ),
            row_counts={"contracts": len(instances)},
        )
    all_symbols = sorted({symbol for symbols in symbols_by_product.values() for symbol in symbols})
    inventory = _load_metadata_inventory(request.local_metadata_paths) if request.local_metadata_paths else None
    local_hashes = {source["source_id"]: source["sha256"] for source in inventory["sources"]} if inventory else {}
    catalog_hash = _file_hash(request.catalog_path)

    try:
        connection = engine.connect()
    except Exception:
        return ExportOutcome(status="export_blocked", reasons=("database_connection_failed",), row_counts={})
    evidence = None
    cleanup_failed = False
    try:
        evidence = begin_read_only_snapshot(connection)
        if not evidence.verified:
            return ExportOutcome(
                status="export_blocked",
                reasons=(f"read_only_snapshot_unverified:{evidence.not_executed_reason}",),
                row_counts={},
                read_only_evidence=evidence,
            )
        bars: list[ContractBar] = []
        seen_keys: set[tuple[str, int, int]] = set()
        counts = BarValidationCounts()
        rows_per_product: dict[str, int] = {}
        for page in select_contract_bars(
            connection,
            all_symbols,
            request.duration_seconds,
            request.start_nano,
            request.end_nano,
            page_size=limits.page_size,
        ):
            for row in page:
                key = (
                    str(row["symbol"]),
                    int(row["duration_seconds"]),  # type: ignore[arg-type]
                    int(row["datetime_nano"]),  # type: ignore[arg-type]
                )
                if key in seen_keys:
                    counts.duplicate_keys += 1
                    continue
                seen_keys.add(key)
                reason, bar = validate_bar_row(row)
                if reason is None and bar is not None:
                    bars.append(bar)
                    counts.valid_rows += 1
                    product_name = str(bar.symbol).split(".", 1)[0] + "." + _product_of(bar.symbol)
                    rows_per_product[product_name] = rows_per_product.get(product_name, 0) + 1
                elif reason in ("invalid_ohlc",):
                    counts.invalid_ohlc += 1
                elif reason == "ordering_error":
                    counts.ordering_errors += 1
                elif reason == "negative_volume_oi":
                    counts.negative_volume_oi += 1
                else:
                    counts.conflicting_identity += 1
            if counts.valid_rows > limits.max_bar_rows:
                return ExportOutcome(
                    status="scope_limit",
                    reasons=(f"max_bar_rows_exceeded:{counts.valid_rows}>{limits.max_bar_rows}",),
                    row_counts={"valid_rows": counts.valid_rows},
                    read_only_evidence=evidence,
                )
        connection.commit()
    except Exception:
        return ExportOutcome(
            status="export_blocked", reasons=("database_read_failed",), row_counts={}, read_only_evidence=evidence,
        )
    finally:
        try:
            connection.close()
        except Exception:
            cleanup_failed = True
    if cleanup_failed:
        return ExportOutcome(
            status="export_blocked", reasons=("database_connection_cleanup_failed",), row_counts={}, read_only_evidence=evidence,
        )

    if counts.valid_rows == 0:
        return ExportOutcome(
            status="empty_real_export",
            reasons=("no_rows_in_requested_range",),
            row_counts={"valid_rows": 0},
            read_only_evidence=evidence,
        )
    if counts.has_rejections:
        return ExportOutcome(
            status="validation_rejected",
            reasons=(
                f"duplicate_keys={counts.duplicate_keys}",
                f"invalid_ohlc={counts.invalid_ohlc}",
                f"ordering_errors={counts.ordering_errors}",
                f"negative_volume_oi={counts.negative_volume_oi}",
                f"conflicting_identity={counts.conflicting_identity}",
            ),
            row_counts={"valid_rows": counts.valid_rows},
            read_only_evidence=evidence,
        )

    bars_rows = bars_rows_from_bars(bars)
    estimated_csv_bytes = sum(
        len(str(cell)) + 1
        for row in bars_rows
        for cell in row.values()
        if cell is not None
    ) + 16 * len(BAR_COLUMNS)
    if estimated_csv_bytes > limits.max_bundle_bytes:
        return ExportOutcome(
            status="scope_limit",
            reasons=(
                f"max_bundle_bytes_exceeded:{estimated_csv_bytes}>{limits.max_bundle_bytes}",
            ),
            row_counts={"valid_rows": counts.valid_rows},
            read_only_evidence=evidence,
        )
    contracts_payload = build_contracts_payload(instances, request.span_start)
    availability = build_availability_payload(
        ordered,
        {"universe_cache": universe.sha256, **local_hashes},
        rows_per_product,
    )
    metadata_payload = {
        "graded_sources": {
            "universe_cache": {
                "role": "identity/expiry candidates",
                "grade": "reconstructed_static",
                "sha256": universe.sha256,
                "generated_at": universe.generated_at,
            },
            "catalog": {
                "role": "product classification/token mapping",
                "grade": "review_required",
            },
        },
        "fields": {},  # historical rules are not constructed by the inventory
        "revision_history_unavailable": True,
        "local_metadata_paths": [],  # never persist possibly sensitive filesystem paths
    }
    if inventory:
        metadata_payload["source_inventory"] = inventory
        availability["source_inventory_status"] = "loaded"
        availability["historical_metadata_adapter"] = "not_implemented"
        availability["database_metadata_status"] = "not_assessed"
        for product in ordered:
            entry = availability["products"][str(product)]
            entry["local_source_presence"] = _inventory_presence(inventory, "product_presence", str(product))
            entry["contract_source_presence"] = {
                symbol: _inventory_presence(inventory, "contract_presence", symbol)
                for symbol in sorted(symbols_by_product[str(product)])
            }
            for claim in entry["strict_fields"].values():
                claim["reason"] = "historical_approval_not_established_by_source_inventory"
                claim["interpretation"] = "required_historical_field_not_loaded_as_validated_evidence"

    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    manifest = publish_input_bundle(
        output,
        dataset_kind="real_export",
        created_at=created_at,
        bars_rows=bars_rows,
        contracts=contracts_payload,
        metadata=metadata_payload,
        availability=availability,
        request_info={
            "products": [str(p) for p in ordered],
            "start": request.span_start.isoformat(),
            "end": request.span_end.isoformat(),
            "as_of": request.as_of.isoformat(),
            "duration_seconds": request.duration_seconds,
            "start_nano": request.start_nano,
            "end_nano": request.end_nano,
            "request_digest": _request_digest(request, ordered),
        },
        source_hashes={
            "universe_cache": universe.sha256,
            "catalog": catalog_hash,
            **local_hashes,
        },
        read_only_transaction={
            "executed": True,
            "mechanism": evidence.mechanism,
            "driver": evidence.driver,
            "observed_isolation": evidence.observed_isolation,
            "observed_read_only": evidence.observed_read_only,
            "verified": evidence.verified,
        },
        clock_policy={
            "calendar_rule_version": "calendar_union_v1",
            "d_status": "unavailable_real_input_lacks_verified_calendar",
            "bar_completion": "raw_timestamp_only_no_session_evidence",
        },
        session_windows_rows=None,
        limits={
            "max_products": limits.max_products,
            "max_contracts": limits.max_contracts,
            "max_bar_rows": limits.max_bar_rows,
            "page_size": limits.page_size,
            "max_bundle_bytes": limits.max_bundle_bytes,
        },
        forbidden_overlaps=[request.universe_cache_path.parent, request.catalog_path.parent],
        notes={"member_ranks": "not_requested"},
    )
    return ExportOutcome(
        status="published",
        reasons=(),
        row_counts={"valid_rows": counts.valid_rows, "contracts": len(instances)},
        manifest=manifest,
        output_path=str(output),
        read_only_evidence=evidence,
    )


def _product_of(symbol: str) -> str:
    """Extract the product code from EXCHANGE.productMMDD-ish symbols."""
    _, _, instrument = symbol.partition(".")
    letters = []
    for char in instrument:
        if char.isalpha():
            letters.append(char)
        else:
            break
    return "".join(letters).upper()


def _file_hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _request_digest(request: ExportRequest, products: Sequence[ProductKey]) -> str:
    import hashlib
    import json

    payload = json.dumps(
        {
            "products": sorted(str(p) for p in products),
            "start": request.span_start.isoformat(),
            "end": request.span_end.isoformat(),
            "duration": request.duration_seconds,
            "start_nano": request.start_nano,
            "end_nano": request.end_nano,
        },
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
