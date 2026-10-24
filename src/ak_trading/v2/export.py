"""Injected-connection bounded read-only export for v2 frozen inputs.

The exporter reuses exactly the two low-level interfaces
``begin_read_only_snapshot`` and ``select_contract_bars`` from
``ak_trading.persistence.futures_research``; every higher-level input is
injected by the caller (connection, instruments, sessions, rules). No
configuration loading, no ``.env``, no engine construction, no legacy daily
``ContractBar`` conversion of minute rows (devplan-v2.1.0 M1 §4.2).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

from sqlalchemy.engine import Connection

from ak_trading.v2 import CONTRACT_VERSION, INPUT_SCHEMA_VERSION
from ak_trading.v2.artifacts import publish_bundle, sha256_hex, stable_id
from ak_trading.v2.contracts import (
    DataQuality,
    FrozenInput,
    Instrument,
    MarketBar,
    SessionWindow,
    TradingRule,
)
from ak_trading.v2.data import read_frozen_input
from ak_trading.v2.schemas import (
    AVAILABILITY_SCHEMA_ID,
    canonical_json_bytes,
    encode_csv_bytes,
    encode_record_file,
    INSTRUMENTS_SCHEMA_ID,
    MACRO_ITEMS_SCHEMA_ID,
    RULES_SCHEMA_ID,
    SESSIONS_SCHEMA_ID,
)

try:
    from ak_trading.persistence.futures_research import (
        begin_read_only_snapshot,
        select_contract_bars,
    )
except ImportError:  # pragma: no cover - postgres extra absent
    begin_read_only_snapshot = None
    select_contract_bars = None

DEFAULT_PAGE_SIZE = 5_000
DEFAULT_MAXIMUM_ROWS = 200_000
DEFAULT_MAXIMUM_BYTES = 268_435_456
MAX_CONTRACTS_PER_BUNDLE = 6
FIXED_DURATIONS = (60, 86400)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SHANGHAI_OFFSET = timedelta(hours=8)
_MINUTE_NANO = 60_000_000_000


class ExportError(ValueError):
    """Fixed-reason export violation; never a driver message or row content."""


def _rollback_quietly(connection: Connection) -> None:
    """Best-effort rollback of the injected read transaction (F08)."""
    try:
        connection.rollback()
    except Exception:  # pragma: no cover - caller-injected connection quirk
        pass




@dataclass(frozen=True)
class ExportRequestV2:
    contract_ids: tuple[str, ...]
    raw_symbols: tuple[str, ...]
    start_nano: int
    end_nano: int
    as_of_nano: int
    durations: tuple[int, ...]
    instruments: tuple[Instrument, ...]
    sessions: tuple[SessionWindow, ...]
    rules: tuple[TradingRule, ...]
    page_size: int
    maximum_rows: int
    maximum_bytes: int


def _check_positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExportError(f"{name}_must_be_positive_int")


def _validate_request(request: ExportRequestV2, output: Path) -> None:
    if not isinstance(request, ExportRequestV2):
        raise TypeError("request_must_be_ExportRequestV2")
    if not isinstance(output, Path):
        raise TypeError("output_must_be_Path")
    if output.exists() or output.is_symlink():
        raise ExportError("output_directory_exists")
    if not request.contract_ids or not request.raw_symbols:
        raise ExportError("contract_ids_and_raw_symbols_required")
    if len(request.contract_ids) != len(request.raw_symbols) or len(request.contract_ids) != len(request.instruments):
        raise ExportError("contract_lists_length_mismatch")
    if len(set(request.contract_ids)) != len(request.contract_ids):
        raise ExportError("duplicate_contract_ids")
    if len(request.contract_ids) > MAX_CONTRACTS_PER_BUNDLE:
        raise ExportError("max_contracts_exceeded")
    if tuple(request.durations) != FIXED_DURATIONS:
        raise ExportError("durations_must_be_exactly_60_86400")
    for name in ("start_nano", "end_nano", "as_of_nano"):
        _check_positive_int(getattr(request, name), name)
    if request.start_nano >= request.end_nano:
        raise ExportError("start_must_precede_end")
    for name in ("page_size", "maximum_rows", "maximum_bytes"):
        _check_positive_int(getattr(request, name), name)
    if any(not isinstance(instrument, Instrument) for instrument in request.instruments):
        raise TypeError("instruments_must_be_tuple_of_Instrument")
    if any(not isinstance(session, SessionWindow) for session in request.sessions):
        raise TypeError("sessions_must_be_tuple_of_SessionWindow")
    if any(not isinstance(rule, TradingRule) for rule in request.rules):
        raise TypeError("rules_must_be_tuple_of_TradingRule")
    declared = {instrument.contract_id: instrument for instrument in request.instruments}
    if set(declared) != set(request.contract_ids):
        raise ExportError("instruments_do_not_match_contract_ids")
    for contract_id, raw_symbol in zip(request.contract_ids, request.raw_symbols):
        if declared[contract_id].raw_symbol != raw_symbol:
            raise ExportError(f"raw_symbol_mismatch:{contract_id}")
    for rule in request.rules:
        if rule.contract_id not in declared:
            raise ExportError(f"rule_contract_not_requested:{rule.contract_id}")


def _datetime_to_nano(value: object) -> int | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone(offset=_SHANGHAI_OFFSET))
    delta = value.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def _positive_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def _non_negative_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0:
        return None
    return number


def _row_to_market_bar(row: Mapping[str, object], contract_id: str, product: str,
                       sessions: tuple[SessionWindow, ...], as_of_nano: int,
) -> MarketBar:
    raw_symbol = str(row["symbol"])
    duration_seconds = int(row["duration_seconds"])
    datetime_nano = int(row["datetime_nano"])
    open_ = _positive_decimal(row.get("open"))
    high = _positive_decimal(row.get("high"))
    low = _positive_decimal(row.get("low"))
    close = _positive_decimal(row.get("close"))
    if any(price is None for price in (open_, high, low, close)):
        raise ExportError("invalid_ohlc")
    if not (low <= min(open_, close) and max(open_, close) <= high):
        raise ExportError("bar_ordering_error")
    volume = _non_negative_decimal(row.get("volume"))
    open_oi = _non_negative_decimal(row.get("open_oi"))
    close_oi = _non_negative_decimal(row.get("close_oi"))
    imported_at_nano = _datetime_to_nano(row.get("imported_at"))
    trading_day = row.get("trading_day")
    trading_day = trading_day if isinstance(trading_day, date) else None
    product_sessions = tuple(session for session in sessions if session.product == product)

    reasons: tuple[str, ...] = ()
    if duration_seconds == 60:
        start_nano = datetime_nano
        end_nano = start_nano + _MINUTE_NANO
        session_id: str | None = None
        for session in product_sessions:
            if session.start_nano <= start_nano < session.end_nano:
                session_id = session.session_id
                if trading_day is None:
                    trading_day = session.trading_day
                break
        if session_id is None:
            reasons = ("outside_verified_calendar",)
        expected_minutes = 1
    else:
        day_sessions = tuple(s for s in product_sessions if s.trading_day == trading_day)
        if day_sessions:
            start_nano = min(s.start_nano for s in day_sessions)
            end_nano = max(s.end_nano for s in day_sessions)
            expected_minutes = sum(
                (s.end_nano - s.start_nano) // _MINUTE_NANO for s in day_sessions)
        else:
            start_nano = datetime_nano
            end_nano = start_nano + 86_400_000_000_000
            expected_minutes = 0
            reasons = ("calendar_coverage_missing",)
        session_id = None

    # Availability (01 §5.3): only a declared PRICE_RECONSTRUCTED policy
    # F07/F02(r3): no reconstruction at the frozen boundary. The imported-at
    # audit bound applies; bars stay non-READY so the consumer boundary
    # (visible_bars) refuses them.
    available_at_nano = imported_at_nano
    temporal_basis = "unknown"
    reasons = (*reasons, "availability_unknown_not_reconstructed")
    # A forming bar (its interval not closed at the export clock) must never
    # be claimed complete; its actual minutes count planned slots elapsed
    # by the export clock so far.
    complete = end_nano <= as_of_nano
    if complete:
        actual_minutes = expected_minutes
    elif duration_seconds == 60:
        actual_minutes = 1 if end_nano <= as_of_nano else 0
    else:
        actual_minutes = sum(
            1 for session in product_sessions
            if session.trading_day == trading_day
            for slot_end in range(session.start_nano + _MINUTE_NANO,
                                  session.end_nano + 1, _MINUTE_NANO)
            if slot_end <= as_of_nano)
    if not complete:
        reasons = (*reasons, "bar_not_closed_at_export_clock")

    row_identity = (f"{raw_symbol}|{duration_seconds}|{datetime_nano}|"
                    f"{imported_at_nano}|{row.get('source')}")
    quality = DataQuality(
        status="READY" if not reasons else "DEGRADED",
        reasons=reasons,
        temporal_basis=temporal_basis,
        input_hash=sha256_hex(row_identity.encode("utf-8")),
    )
    return MarketBar(
        bar_id=stable_id("bar", f"{contract_id}|{duration_seconds}|{datetime_nano}"),
        contract_id=contract_id,
        raw_symbol=raw_symbol,
        product=product,
        duration_seconds=duration_seconds,
        datetime_nano=datetime_nano,
        start_nano=start_nano,
        end_nano=end_nano,
        trading_day=trading_day,
        session_id=session_id,
        available_at_nano=available_at_nano,
        imported_at_nano=imported_at_nano,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        open_oi=open_oi,
        close_oi=close_oi,
        complete=complete,
        expected_minutes=expected_minutes,
        actual_minutes=actual_minutes,
        source=str(row.get("source") or "database"),
        quality=quality,
    )


def export_frozen_input(connection: Connection, request: ExportRequestV2,
                        output: Path) -> FrozenInput:
    """Run one bounded verified READ ONLY / REPEATABLE READ export and publish.

    Over-limit requests fail with no final manifest; duplicate identical rows
    are idempotent while conflicting duplicates are INVALID; conflicting
    orderings are audited and re-sorted by bar identity. The injected
    connection belongs to the caller and is never closed here. Availability
    uses the conservative imported-at audit bound (temporal basis "unknown")
    — the keyword-only reconstruction extension is reverted to the frozen
    3-parameter interface (F02 r3).
    """
    _validate_request(request, output)
    if begin_read_only_snapshot is None or select_contract_bars is None:
        raise ExportError("persistence_futures_research_unavailable")
    contract_by_symbol = {
        instrument.raw_symbol: instrument for instrument in request.instruments
    }
    evidence = begin_read_only_snapshot(connection)
    if not evidence.verified:
        raise ExportError(f"read_only_snapshot_unverified:{evidence.not_executed_reason}")

    bars_by_key: dict[tuple[str, int, int], MarketBar] = {}
    try:
        for duration in FIXED_DURATIONS:  # noqa: B007
            for page in select_contract_bars(
                connection,
                symbols=list(request.raw_symbols),
                duration_seconds=duration,
                start_nano=request.start_nano,
                end_nano=request.end_nano,
                page_size=request.page_size,
            ):
                for row in page:
                    symbol = str(row["symbol"])
                    instrument = contract_by_symbol.get(symbol)
                    if instrument is None:
                        raise ExportError("unrequested_symbol_returned")
                    bar = _row_to_market_bar(row, instrument.contract_id, instrument.product,
                                             request.sessions, request.as_of_nano)
                    key = (bar.contract_id, bar.duration_seconds, bar.datetime_nano)
                    previous = bars_by_key.get(key)
                    if previous is not None:
                        if previous != bar:
                            raise ExportError("conflicting_duplicate_bar")
                        continue
                    bars_by_key[key] = bar
                if len(bars_by_key) > request.maximum_rows:
                    raise ExportError("maximum_rows_exceeded")
    except ExportError:
        _rollback_quietly(connection)
        raise
    except Exception:
        _rollback_quietly(connection)
        raise ExportError("database_read_failed") from None

    bars = tuple(
        bars_by_key[key] for key in sorted(bars_by_key, key=lambda k: (k[0], k[1], k[2]))
    )
    if len(bars) > request.maximum_rows:
        raise ExportError("maximum_rows_exceeded")

    rows_per_contract: dict[str, int] = {contract_id: 0 for contract_id in request.contract_ids}
    for bar in bars:
        rows_per_contract[bar.contract_id] += 1

    payloads: dict[str, bytes] = {
        "bars.csv": encode_csv_bytes(MarketBar, bars),
        "instruments.json": encode_record_file(
            "Instrument", INSTRUMENTS_SCHEMA_ID, request.instruments),
        "sessions.json": encode_record_file(
            "SessionWindow", SESSIONS_SCHEMA_ID, request.sessions),
        "macro_items.json": encode_record_file("MacroItem", MACRO_ITEMS_SCHEMA_ID, []),
        "rules.json": encode_record_file(
            "TradingRule", RULES_SCHEMA_ID, request.rules),
        "availability.json": canonical_json_bytes({
            "schema_version": AVAILABILITY_SCHEMA_ID,
            "input_manifest_sha256": None,
            "contract_status": {
                contract_id: ("EXPORTED" if rows_per_contract[contract_id] else "NO_ROWS")
                for contract_id in request.contract_ids
            },
            "minute_history_status": "EXPORTED" if any(
                bar.duration_seconds == 60 for bar in bars) else "NO_MINUTE_ROWS",
            "realtime_status": "NOT_ACCEPTED",
            "reasons": [
                "realtime_link_not_accepted",
                "availability_unknown_not_reconstructed",
                "macro_items_not_exported",
            ],
        }),
    }

    request_manifest = {
        "contract_ids": list(request.contract_ids),
        "raw_symbols": list(request.raw_symbols),
        "start_nano": request.start_nano,
        "end_nano": request.end_nano,
        "as_of_nano": request.as_of_nano,
        "durations": list(request.durations),
        "page_size": request.page_size,
        "maximum_rows": request.maximum_rows,
        "maximum_bytes": request.maximum_bytes,
    }
    limitations = [
        "realtime_link_not_accepted",
        "synthetic_or_missing_calendars_marked_in_quality",
        "bar_availability_unknown_not_reconstructed_ready",
        "availability_temporal_basis_unknown_not_first_publication_pit",
    ]
    provenance = {
        "schema_version": INPUT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "dataset_kind": "HISTORICAL_RECONSTRUCTED",
        "created_at_nano": request.as_of_nano,
        "request": request_manifest,
        "read_only_transaction": {
            "isolation": "REPEATABLE READ",
            "read_only": True,
            "verified": True,
        },
        "source_hashes": {
            "request": sha256_hex(canonical_json_bytes(request_manifest)),
        },
        "limitations": limitations,
    }
    # F08: the byte limit bounds the ACTUAL published bundle, manifest
    # included; failures happen before any directory is created.
    staged_files = {name: sha256_hex(payload) for name, payload in payloads.items()}
    staged_manifest = canonical_json_bytes({**dict(provenance), "files": staged_files})
    total_bytes = sum(len(payload) for payload in payloads.values()) + len(staged_manifest)
    if total_bytes > request.maximum_bytes:
        _rollback_quietly(connection)
        raise ExportError("maximum_bytes_exceeded")
    connection.commit()
    publish_bundle(output, payloads, provenance)
    return read_frozen_input(output)
