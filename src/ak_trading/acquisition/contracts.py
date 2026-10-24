"""C0 public contracts: policy validation, immutable records, canonical hashing.

Every normative number, prohibition and time window comes from
``execution-policy.json`` (arbitration ACQ-EXEC-20260919-r1). The CLI and all
other modules consume the policy exclusively through :class:`Policy`; direct
bypassing of these limits is a contract violation.

Time representation: all instants are integer nanoseconds since the Unix epoch
(UTC). Conversion helpers below never round-trip a timestamp through binary
floating point (H07). SDK kline ``datetime`` floats are converted once via
``int(value)`` after an integrality check; that single conversion is performed
in :mod:`ak_acquisition_w2.capture`, not here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

SHANGHAI_TZ = timezone(timedelta(hours=8))
NANOSECOND = 1_000_000_000
MAX_SIGNED_INT64 = 9_223_372_036_854_775_807
SUPPORTED_DURATIONS = (60, 86400)
REQUEST_DATA_LENGTH_V1 = 8192  # H06: fixed request length for the first version
CLOSE_BUFFER_SECONDS = 60  # H07: conservative close watermark buffer

# Byte-identical frozen copy of the arbitration policy kept in config/.
FROZEN_POLICY_SHA256 = (
    "cd2a5c314394ef52a8e7b2b3f8374ce0c76c18d06a5f2f5bb2259ab1441eed83"
)

CANONICAL_SERIALIZER_VERSION = "python-json-utf8-v1"


class ContractError(ValueError):
    """Raised when a record or policy violates the frozen contracts."""


# ---------------------------------------------------------------------------
# Canonical serialization and hashing
# ---------------------------------------------------------------------------

def canonical_json_bytes(obj: Any) -> bytes:
    """Deterministic JSON: sorted keys, no spaces, no NaN/Infinity."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(obj: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(obj)).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Integer nanosecond time (no float round-trips)
# ---------------------------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def iso_instant_to_nano(value: str) -> int:
    """Parse an ISO-8601 instant (``+08:00`` offsets allowed) to integer ns.

    Uses only integer arithmetic on whole seconds and microseconds; never
    multiplies a float by 1e9.
    """
    if not isinstance(value, str) or not value.strip():
        raise ContractError("iso_instant_to_nano requires a non-empty string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContractError(f"invalid ISO-8601 instant: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"instant must carry an explicit offset: {value!r}")
    delta = parsed - _EPOCH
    total_seconds = delta.days * 86400 + delta.seconds
    return total_seconds * NANOSECOND + delta.microseconds * 1000


def nano_to_iso(nano: int, tz: timezone = SHANGHAI_TZ) -> str:
    """Render integer ns as an ISO-8601 instant string (default +08:00)."""
    validate_nano_int(nano)
    seconds, nanos = divmod(nano, NANOSECOND)
    moment = _EPOCH + timedelta(seconds=seconds)
    shifted = moment.astimezone(tz)
    base = shifted.strftime("%Y-%m-%dT%H:%M:%S")
    offset = shifted.strftime("%z")
    rendered_offset = f"{offset[:3]}:{offset[3:]}" if offset else "+00:00"
    if nanos:
        return f"{base}.{nanos:09d}{rendered_offset}"
    return f"{base}{rendered_offset}"


def civil_date_to_nano(value: date, tz: timezone = SHANGHAI_TZ) -> int:
    """Midnight (tz-local) of a civil date as integer ns."""
    local_midnight = datetime(value.year, value.month, value.day, tzinfo=tz)
    delta = local_midnight - _EPOCH
    total_seconds = delta.days * 86400 + delta.seconds
    assert delta.microseconds == 0
    return total_seconds * NANOSECOND


def validate_nano_int(value: Any) -> int:
    """Accept only integral nanosecond integers; reject bool/float/str."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(
            f"nanosecond values must be integers, got {type(value).__name__}"
        )
    if not 0 <= value <= MAX_SIGNED_INT64:
        raise ContractError(f"nanosecond value out of range: {value}")
    return value


# ---------------------------------------------------------------------------
# Enums (values must stay in sync with execution-policy.json)
# ---------------------------------------------------------------------------

class TargetKind(str, Enum):
    A = "TARGET_A_TRADING_DAY_SESSIONS"
    B = "TARGET_B_CIVIL_INTERVAL"
    CONTINUOUS = "CONTINUOUS_INCREMENT"


class TaskState(str, Enum):
    # H11 progress states
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    RAW_VERIFIED = "RAW_VERIFIED"
    INGEST_VERIFIED = "INGEST_VERIFIED"
    ACCEPTED = "ACCEPTED"
    # H11 terminal/intermediate failure states
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    UNSUPPORTED = "UNSUPPORTED"
    SOURCE_EMPTY_UNEXPLAINED = "SOURCE_EMPTY_UNEXPLAINED"
    QUARANTINED = "QUARANTINED"
    OUTSIDE_FREE_WINDOW = "OUTSIDE_FREE_WINDOW"
    DEFERRED_BUDGET = "DEFERRED_BUDGET"


class CoverageStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"
    UNKNOWN = "UNKNOWN"


class FramePartition(str, Enum):
    """Mutually exclusive top-level frame row partition (dev-plan section 2)."""

    PADDING = "PADDING"
    INVALID = "INVALID"
    VALID = "VALID"


class ValidRowStatus(str, Enum):
    CLOSED = "CLOSED"
    PROVISIONAL = "PROVISIONAL"
    UNKNOWN = "UNKNOWN"


class InstrumentKind(str, Enum):
    CONCRETE_FUTURE = "CONCRETE_FUTURE"
    MAIN_CONTINUOUS_REFERENCE = "MAIN_CONTINUOUS_REFERENCE"
    INDEX_REFERENCE = "INDEX_REFERENCE"
    F_SUFFIX_QUARANTINED = "F_SUFFIX_QUARANTINED"  # H05
    UNKNOWN_UNCLASSIFIED = "UNKNOWN_UNCLASSIFIED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class LifecycleStatus(str, Enum):
    ACTIVE = "ACTIVE"
    EXPIRED_IN_WINDOW = "EXPIRED_IN_WINDOW"
    LIFECYCLE_UNKNOWN = "LIFECYCLE_UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class GapReason(str, Enum):
    SOURCE_EMPTY_UNEXPLAINED = "SOURCE_EMPTY_UNEXPLAINED"
    SOURCE_CONFIRMED_NO_DATA = "SOURCE_CONFIRMED_NO_DATA"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    OUTSIDE_FREE_WINDOW = "OUTSIDE_FREE_WINDOW"
    CALENDAR_UNKNOWN = "CALENDAR_UNKNOWN"
    LIFECYCLE_UNKNOWN = "LIFECYCLE_UNKNOWN"
    INVALID_SOURCE_ROW = "INVALID_SOURCE_ROW"
    CONFLICT = "CONFLICT"
    PROVISIONAL = "PROVISIONAL"
    FAILED = "FAILED"
    NOT_RUN = "NOT_RUN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class FailureClass(str, Enum):
    TRANSIENT = "TRANSIENT"
    RATE_LIMITED = "RATE_LIMITED"
    ACCOUNT_DENIED = "ACCOUNT_DENIED"
    SYMBOL_UNSUPPORTED = "SYMBOL_UNSUPPORTED"
    EMPTY_UNEXPLAINED = "EMPTY_UNEXPLAINED"
    BUDGET = "BUDGET"
    LOCAL_ERROR = "LOCAL_ERROR"


# ---------------------------------------------------------------------------
# Exact decimal strings for OHLCV/OI values
# ---------------------------------------------------------------------------

def decimal_string(value: Any) -> str:
    """Traceable exact-string form of a source value.

    Integers render exactly. Floats render via ``repr`` (shortest round-trip
    decimal for the float64 the SDK delivered). This is a traceable
    conversion of the post-SDK value; the package never claims recovery of
    precision the source had before the SDK decoded it into float64
    (dev-plan section 2). Non-finite values and bools are rejected.
    """
    if isinstance(value, bool):
        raise ContractError("boolean is not a market value")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ContractError("non-finite market value")
        return repr(value)
    if isinstance(value, str):
        # Already an exact string from a fixture/export; keep verbatim.
        return value
    raise ContractError(f"unsupported value type: {type(value).__name__}")


BAR_VALUE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_oi",
    "close_oi",
)


def canonical_bar_content(
    *,
    duration_seconds: int,
    datetime_nano: int,
    bar_start_nano: int,
    bar_end_nano: int,
    trading_day: Optional[str],
    values: Mapping[str, str],
) -> str:
    """SHA-256 over canonical market content only.

    Excludes observed_at / imported_at / attempt ids / log data by
    construction: only the fields below enter the digest, so the same values
    re-fetched later hash identically (dev-plan section 2). ``row_status``
    is deliberately excluded here; status/classification evolution is tracked
    by the version uniqueness key in storage, not by this digest.
    """
    if duration_seconds not in SUPPORTED_DURATIONS:
        raise ContractError(f"unsupported duration: {duration_seconds}")
    validate_nano_int(datetime_nano)
    validate_nano_int(bar_start_nano)
    validate_nano_int(bar_end_nano)
    if bar_end_nano <= bar_start_nano:
        raise ContractError("bar_end must be strictly after bar_start")
    payload = {
        "bar_end_nano": str(bar_end_nano),
        "bar_start_nano": str(bar_start_nano),
        "datetime_nano": str(datetime_nano),
        "duration_seconds": duration_seconds,
        "trading_day": trading_day,
        "values": {name: str(values[name]) for name in BAR_VALUE_FIELDS},
    }
    return canonical_sha256(payload)


# ---------------------------------------------------------------------------
# Policy (execution-policy.json) loading and validation
# ---------------------------------------------------------------------------

_BUDGET_INT_FIELDS = (
    "network_workers_max",
    "active_tqapi_connections_max",
    "active_symbols_max",
    "active_serials_max",
    "metadata_explicit_symbols_per_call_max",
    "metadata_calls_per_run_max",
    "run_wall_seconds_max",
    "serial_request_attempts_per_run_max",
    "api_connection_starts_per_rolling_600_seconds_max",
    "serial_ready_timeout_seconds",
    "wait_update_deadline_seconds_max",
    "safe_finalize_reserve_seconds",
    "database_connections_held_max",
    "database_workers_max",
    "database_chunk_rows_max",
    "database_connect_timeout_seconds",
    "database_statement_timeout_seconds",
    "database_lock_timeout_seconds",
    "process_rss_bytes_max",
    "uncommitted_raw_snapshots_max",
    "raw_file_bytes_max",
    "batch_new_artifact_bytes_max",
    "minimum_batch_peak_reservation_bytes_including_database_wal",
    "free_space_floor_bytes_after_reservations",
)

# Codex-fixed values from ACQ-EXEC-20260919-r1 execution-policy.json#/budget.
# Any change is a new arbitration revision, never an in-place edit.
_FROZEN_BUDGET = {
    "network_workers_max": 1,
    "active_tqapi_connections_max": 1,
    "active_symbols_max": 4,
    "active_serials_max": 8,
    "metadata_explicit_symbols_per_call_max": 100,
    "metadata_calls_per_run_max": 12,
    "run_wall_seconds_max": 1800,
    "serial_request_attempts_per_run_max": 24,
    "api_connection_starts_per_rolling_600_seconds_max": 3,
    "serial_ready_timeout_seconds": 90,
    "wait_update_deadline_seconds_max": 5,
    "safe_finalize_reserve_seconds": 60,
    "database_connections_held_max": 1,
    "database_workers_max": 1,
    "database_chunk_rows_max": 5000,
    "database_connect_timeout_seconds": 5,
    "database_statement_timeout_seconds": 30,
    "database_lock_timeout_seconds": 2,
    "process_rss_bytes_max": 1_073_741_824,
    "uncommitted_raw_snapshots_max": 2,
    "raw_file_bytes_max": 268_435_456,
    "batch_new_artifact_bytes_max": 536_870_912,
    "minimum_batch_peak_reservation_bytes_including_database_wal": 2_147_483_648,
    "free_space_floor_bytes_after_reservations": 64_424_509_440,
}


@dataclass(frozen=True)
class Policy:
    """Validated view over execution-policy.json. All limits are read-only."""

    policy_id: str
    sha256: str
    raw: Mapping[str, Any]

    # -- lifecycle ---------------------------------------------------------
    @classmethod
    def from_file(cls, path: Path, *, expected_sha256: Optional[str] = None) -> "Policy":
        payload = Path(path).read_bytes()
        digest = sha256_bytes(payload)
        if expected_sha256 is not None and digest != expected_sha256:
            raise ContractError(
                "policy file hash mismatch: file is not the frozen policy "
                "referenced by this workflow"
            )
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("policy file is not valid UTF-8 JSON") from exc
        return cls(policy_id=str(raw["policy_id"]), sha256=digest, raw=raw)

    def validate(self) -> "Policy":
        raw = self.raw
        if raw.get("schema_version") != "acquisition_execution_policy_v1":
            raise ContractError("unsupported policy schema_version")
        if raw.get("status") != "DEVELOPMENT_AUTHORIZED_RUNTIME_NOT_AUTHORIZED":
            raise ContractError(
                "policy status must remain DEVELOPMENT_AUTHORIZED_RUNTIME_NOT_AUTHORIZED; "
                "a runtime-authorized policy requires a new arbitration revision"
            )
        source = raw.get("source_policy", {})
        scope_block = raw.get("scope", {})
        if tuple(scope_block.get("duration_seconds", ())) != SUPPORTED_DURATIONS:
            raise ContractError("policy durations must be exactly [60, 86400]")
        # H06: first version request length is fixed at 8192.
        if source.get("request_data_length") != REQUEST_DATA_LENGTH_V1:
            raise ContractError(
                f"policy request_data_length must be {REQUEST_DATA_LENGTH_V1} in v1"
            )
        if source.get("network_channel") != "TQSDK_FREE_REALTIME_GET_KLINE_SERIAL":
            raise ContractError("policy network channel changed")
        if source.get("raw_kind") != "RAW_SDK_SNAPSHOT":
            raise ContractError("raw kind must be RAW_SDK_SNAPSHOT")
        if source.get("beyond_reachable_window_status") != "OUTSIDE_FREE_WINDOW":
            raise ContractError("out-of-window status must stay OUTSIDE_FREE_WINDOW")

        safety = raw.get("safety", {})
        if safety.get("DataDownloader_allowed") is not False:
            raise ContractError("DataDownloader must stay excluded")
        if safety.get("paid_permission_probes_allowed") is not False:
            raise ContractError("paid probes must stay excluded")
        if safety.get("enable_trading") is not False:
            raise ContractError("policy must fix enable_trading=false")
        if safety.get("expected_order_submit_calls") != 0:
            raise ContractError("order submit calls must be 0")
        if safety.get("expected_order_cancel_calls") != 0:
            raise ContractError("order cancel calls must be 0")
        if safety.get("paid_channels_permanently_excluded_from_workflow") is not True:
            raise ContractError("paid channels must stay permanently excluded")
        if safety.get("dotenv_reads_allowed") is not False:
            raise ContractError("dotenv reads must stay disallowed")
        if safety.get("raw_sensitive_exception_logging_allowed") is not False:
            raise ContractError("raw sensitive exception logging must stay disallowed")

        targets = raw.get("targets", {})
        target_a = targets.get("A", {})
        if target_a.get("trading_day") != "2026-09-18":
            raise ContractError("target A trading day must be 2026-09-18")
        target_b = targets.get("B", {})
        expected_b_start = "2026-09-18T00:00:00+08:00"
        expected_b_end = "2026-09-20T00:00:00+08:00"
        if target_b.get("start_inclusive") != expected_b_start:
            raise ContractError("target B start must be 2026-09-18T00:00:00+08:00")
        if target_b.get("end_exclusive") != expected_b_end:
            raise ContractError("target B end must be 2026-09-20T00:00:00+08:00")
        if targets.get("scenario_generation") is not False:
            raise ContractError("scenario generation must stay disabled")

        bar = raw.get("bar_policy", {})
        if list(bar.get("authority_key", [])) != [
            "source_symbol",
            "duration_seconds",
            "datetime_nano",
        ]:
            raise ContractError("authority key must be (symbol, duration, nano)")
        if bar.get("nanoseconds_type") != "INTEGER_NO_FLOAT_ROUNDTRIP":
            raise ContractError("nanoseconds must be integer-typed")
        if bar.get("minute_to_daily_substitution") is not False:
            raise ContractError("minute-to-daily substitution is forbidden")
        if bar.get("daily_start_plus_86400_rule_allowed") is not False:
            raise ContractError("start+86400 daily close rule is forbidden")
        if bar.get("conservative_close_buffer_seconds") != CLOSE_BUFFER_SECONDS:
            raise ContractError("close buffer must be 60 seconds")
        if list(bar.get("raw_row_partition", [])) != [p.value for p in FramePartition]:
            raise ContractError("raw row partition must be PADDING/INVALID/VALID")
        if list(bar.get("valid_row_partition", [])) != [s.value for s in ValidRowStatus]:
            raise ContractError("valid row partition must be CLOSED/PROVISIONAL/UNKNOWN")

        storage = raw.get("storage", {})
        if storage.get("write_schema") != "acquisition_w2":
            raise ContractError("write schema must be acquisition_w2")
        if list(storage.get("write_tables", [])) != [
            "bar_versions",
            "ingest_batches",
            "batch_rows",
        ]:
            raise ContractError("write tables must be the three acquisition_w2 tables")
        if storage.get("legacy_import_market_bars_allowed") is not False:
            raise ContractError("legacy importer must stay excluded")
        if storage.get("legacy_store_reference_snapshot_allowed") is not False:
            raise ContractError("legacy reference writer must stay excluded")
        if storage.get("write_existing_ingestion_or_reference_tables") is not False:
            raise ContractError("existing ingestion/reference tables stay unwritable")
        if storage.get("last_write_wins") is not False:
            raise ContractError("last-write-wins is forbidden")

        retry = raw.get("retry", {})
        if retry.get("attempts_max_including_initial") != 4:
            raise ContractError("attempts_max_including_initial must be 4")
        if list(retry.get("backoff_seconds", [])) != [60, 300, 900]:
            raise ContractError("backoff schedule must be 60/300/900")
        if retry.get("repeated_account_rate_limit_stop_threshold") != 2:
            raise ContractError("rate-limit stop threshold must be 2")

        acceptance = raw.get("acceptance", {})
        if list(acceptance.get("coverage_statuses", [])) != [
            s.value for s in CoverageStatus
        ]:
            raise ContractError("coverage statuses must be the five exclusive states")
        if acceptance.get("conditional_pass") is not False:
            raise ContractError("conditional pass is forbidden")
        if acceptance.get("software_pass_implies_data_complete") is not False:
            raise ContractError("software pass must not imply data completeness")
        if acceptance.get("owner_can_review_own_work") is not False:
            raise ContractError("owner cannot review own work")

        budget = raw.get("budget", {})
        for name in _BUDGET_INT_FIELDS:
            value = budget.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ContractError(f"budget field {name} must be a positive integer")
            # The budget numbers are Codex-fixed by the ruling; changing any
            # limit requires a new policy revision, not an edit to this file.
            if value != _FROZEN_BUDGET[name]:
                raise ContractError(
                    f"budget field {name} deviates from the frozen ruling value"
                )
        if budget.get("auto_increase_limits") is not False:
            raise ContractError("budget auto-increase is forbidden")
        if budget.get("heavy_database_and_hash_io_overlap_allowed") is not False:
            raise ContractError("heavy DB/hash I/O overlap is forbidden")

        scope = raw.get("scope", {})
        if scope.get("historical_baseline_products") != 81:
            raise ContractError("baseline must keep 81 products")
        expected_venues = {"SHFE": 19, "INE": 5, "DCE": 20, "CZCE": 24, "CFFEX": 8, "GFEX": 5}
        if dict(scope.get("historical_baseline_by_venue", {})) != expected_venues:
            raise ContractError("baseline venue split changed")
        f_suffix = scope.get("F_suffix", {})
        if f_suffix.get("cached_count") != 42:
            raise ContractError("F-suffix cached count must remain 42")
        if f_suffix.get("by_product") != {"DCE.l": 14, "DCE.pp": 14, "DCE.v": 14}:
            raise ContractError("F-suffix product split changed")
        if f_suffix.get("status") != "QUARANTINED_PENDING_CLASSIFICATION":
            raise ContractError("F-suffix must stay QUARANTINED_PENDING_CLASSIFICATION")
        if f_suffix.get("strip_suffix") is not False:
            raise ContractError("stripping F suffix is forbidden")
        return self

    # -- typed accessors ----------------------------------------------------
    @property
    def budget(self) -> Mapping[str, int]:
        return {name: int(self.raw["budget"][name]) for name in _BUDGET_INT_FIELDS}

    @property
    def target_b_start_nano(self) -> int:
        return iso_instant_to_nano(self.raw["targets"]["B"]["start_inclusive"])

    @property
    def target_b_end_nano(self) -> int:
        return iso_instant_to_nano(self.raw["targets"]["B"]["end_exclusive"])

    @property
    def target_a_trading_day(self) -> str:
        return str(self.raw["targets"]["A"]["trading_day"])

    @property
    def write_schema(self) -> str:
        return str(self.raw["storage"]["write_schema"])

    @property
    def write_tables(self) -> tuple[str, ...]:
        return tuple(str(t) for t in self.raw["storage"]["write_tables"])

    @property
    def request_data_length(self) -> int:
        return int(self.raw["source_policy"]["request_data_length"])

    @property
    def durations(self) -> tuple[int, ...]:
        return SUPPORTED_DURATIONS


# ---------------------------------------------------------------------------
# Immutable records (dev-plan section 2 table)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceIdentity:
    exchange: str
    product_key: str  # normalized key, never rewrites the raw symbol
    source_product_code: str  # preserves source case
    source_symbol: str  # preserves source case
    instrument_kind: InstrumentKind
    delivery_yyyymm: Optional[str]  # None when not source-proven
    delivery_provenance: str  # source_expire_evidence | derived_from_code | unknown
    delivery_unknown_reason: Optional[str]
    lifecycle_status: LifecycleStatus
    classification_evidence: tuple[str, ...]
    observed_at_nano: int

    def __post_init__(self) -> None:
        if not self.source_symbol or not self.exchange:
            raise ContractError("identity requires exchange and source symbol")
        validate_nano_int(self.observed_at_nano)
        if self.instrument_kind == InstrumentKind.F_SUFFIX_QUARANTINED:
            if not self.source_symbol.endswith("F"):
                raise ContractError("F-quarantined symbol must retain its F suffix")
            if self.delivery_yyyymm is not None:
                raise ContractError(
                    "F-suffix delivery month is not affirmable; must stay unknown"
                )
        if (
            self.delivery_yyyymm is None
            and self.delivery_provenance != "unknown"
            and self.instrument_kind == InstrumentKind.CONCRETE_FUTURE
        ):
            raise ContractError("concrete future without delivery month needs reason")

    def canonical(self) -> dict[str, Any]:
        return {
            "classification_evidence": list(self.classification_evidence),
            "delivery_provenance": self.delivery_provenance,
            "delivery_unknown_reason": self.delivery_unknown_reason,
            "delivery_yyyymm": self.delivery_yyyymm,
            "exchange": self.exchange,
            "instrument_kind": self.instrument_kind.value,
            "lifecycle_status": self.lifecycle_status.value,
            "observed_at_nano": str(self.observed_at_nano),
            "product_key": self.product_key,
            "source_product_code": self.source_product_code,
            "source_symbol": self.source_symbol,
        }

    def content_sha256(self) -> str:
        return canonical_sha256(self.canonical())


@dataclass(frozen=True)
class CaptureTask:
    task_id: str
    logical_task_key: str
    target_kind: TargetKind
    source_symbol: str
    duration_seconds: int
    window_start_nano: Optional[int]  # logical window (A/B), may be None for continuous
    window_end_nano: Optional[int]
    universe_sha256: str
    policy_sha256: str
    source_sha256: str  # identity evidence bundle hash
    calendar_sha256: str  # 'NONE_UNKNOWN' when no calendar evidence is bound
    attempt_id: Optional[str] = None
    lease_holder: Optional[str] = None
    reuse_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.duration_seconds not in SUPPORTED_DURATIONS:
            raise ContractError(f"unsupported duration {self.duration_seconds}")
        for name in (
            self.task_id,
            self.logical_task_key,
            self.universe_sha256,
            self.policy_sha256,
            self.source_sha256,
            self.calendar_sha256,
        ):
            if not isinstance(name, str) or not name:
                raise ContractError("hash/identity fields must be non-empty strings")
        for bound in (self.window_start_nano, self.window_end_nano):
            if bound is not None:
                validate_nano_int(bound)
        if (
            self.window_start_nano is not None
            and self.window_end_nano is not None
            and self.window_end_nano <= self.window_start_nano
        ):
            raise ContractError("logical window end must exceed start")

    def logical_identity_sha256(self) -> str:
        payload = {
            "calendar_sha256": self.calendar_sha256,
            "duration_seconds": self.duration_seconds,
            "policy_sha256": self.policy_sha256,
            "source_sha256": self.source_sha256,
            "source_symbol": self.source_symbol,
            "target_kind": self.target_kind.value,
            "universe_sha256": self.universe_sha256,
            "window_end_nano": (
                str(self.window_end_nano) if self.window_end_nano is not None else None
            ),
            "window_start_nano": (
                str(self.window_start_nano)
                if self.window_start_nano is not None
                else None
            ),
        }
        return canonical_sha256(payload)


@dataclass(frozen=True)
class RawSnapshotMeta:
    task_id: str
    attempt_id: str
    source_symbol: str
    duration_seconds: int
    requested_length: int
    returned_length: int  # actual frame rows incl. padding; never assumed == requested
    frame_rows: int
    padding_rows: int
    invalid_rows: int
    valid_rows: int
    observed_at_nano: int
    sdk_version: str
    serializer_version: str
    raw_path: str
    raw_sha256: str
    raw_bytes: int
    first_valid_nano: Optional[int]
    last_valid_nano: Optional[int]

    def __post_init__(self) -> None:
        if self.requested_length != REQUEST_DATA_LENGTH_V1:
            raise ContractError("v1 snapshots must request exactly 8192 rows")
        validate_nano_int(self.observed_at_nano)
        if self.first_valid_nano is not None:
            validate_nano_int(self.first_valid_nano)
        if self.last_valid_nano is not None:
            validate_nano_int(self.last_valid_nano)
        counts = self.padding_rows + self.invalid_rows + self.valid_rows
        if counts != self.frame_rows:
            raise ContractError(
                "frame partition must be conserved: "
                f"{self.padding_rows}+{self.invalid_rows}+{self.valid_rows} != {self.frame_rows}"
            )
        if self.returned_length != self.frame_rows:
            raise ContractError("returned_length must equal total frame rows")


@dataclass(frozen=True)
class ClassifiedRow:
    raw_row_ordinal: int
    datetime_nano: Optional[int]  # None for padding rows
    source_symbol: str
    duration_seconds: int
    bar_start_nano: Optional[int]
    bar_end_nano: Optional[int]
    trading_day: Optional[str]
    trading_day_source: str  # session_evidence | none
    frame_partition: FramePartition
    valid_status: Optional[ValidRowStatus]
    values: Mapping[str, str]  # exact strings for the 7 bar value fields
    content_sha256: Optional[str]
    evidence_version: str
    reason: Optional[str]
    target_refs: tuple[TargetKind, ...] = ()

    def __post_init__(self) -> None:
        if self.frame_partition == FramePartition.VALID:
            if self.datetime_nano is None or self.valid_status is None:
                raise ContractError("valid rows need datetime and status")
            validate_nano_int(self.datetime_nano)
            if set(self.values) != set(BAR_VALUE_FIELDS):
                raise ContractError("valid rows carry all seven value fields")
            if not self.content_sha256:
                raise ContractError("valid rows need a content hash")
        else:
            if self.valid_status is not None:
                raise ContractError("non-valid rows carry no valid_status")
            if self.frame_partition == FramePartition.PADDING and self.datetime_nano is not None:
                raise ContractError("padding rows carry no datetime")


@dataclass(frozen=True)
class CoverageResult:
    target_kind: TargetKind
    product_key: str
    duration_seconds: int
    expected_slots: Optional[int]  # None means UNKNOWN denominator, never 0
    actual_slots: int
    missing_slots: Optional[tuple[str, ...]]
    unknown_slot_count: Optional[int]
    conflicts: int
    raw_watermark: str  # NONE | RAW_VERIFIED | ...
    db_watermark: str  # NONE | INGEST_VERIFIED | ...
    source_availability: str
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.duration_seconds not in SUPPORTED_DURATIONS:
            raise ContractError("unsupported duration in coverage")
        if self.expected_slots is None:
            # Unknown denominators may never masquerade as zero (H13/H14).
            if self.missing_slots is not None or self.unknown_slot_count is None:
                raise ContractError(
                    "unknown expected slots must keep missing=None and unknown>=0"
                )
        else:
            if self.expected_slots < 0 or self.actual_slots < 0:
                raise ContractError("slot counts cannot be negative")
            if self.missing_slots is None:
                raise ContractError("known denominator requires a missing slot list")

    def coverage_status(self) -> CoverageStatus:
        if self.expected_slots is None:
            return CoverageStatus.UNKNOWN
        if self.raw_watermark != "RAW_VERIFIED" or self.db_watermark != "INGEST_VERIFIED":
            if self.actual_slots > 0 or self.source_availability == "ATTEMPTED":
                return CoverageStatus.PARTIAL
            return CoverageStatus.NOT_RUN
        if self.conflicts > 0 or self.missing_slots:
            return CoverageStatus.PARTIAL
        if self.actual_slots >= self.expected_slots:
            return CoverageStatus.COMPLETE
        return CoverageStatus.PARTIAL


@dataclass(frozen=True)
class RunAuthorization:
    """H01: user run confirmation record. This program never self-signs one."""

    confirmation_ref: str  # pointer to the user's original confirmation text
    confirmed_at_nano: int
    approver: str  # must be HUMAN_USER
    scope_policy_sha256: str
    scope_review_seal_sha256: str
    scope_code_manifest_sha256: str
    scope_plan_sha256: str
    output_root: str
    db_target_non_sensitive: str  # e.g. host:port/dbname, never a credential
    session_window_start_nano: int
    session_window_end_nano: int
    max_serial_attempts: int
    max_wall_seconds: int
    allow_subsequent_batches: bool
    protected_paths: tuple[str, ...]
    protected_db_objects: tuple[str, ...]
    expires_at_nano: Optional[int] = None

    def __post_init__(self) -> None:
        if self.approver != "HUMAN_USER":
            raise ContractError(
                "run authorization must be approved by HUMAN_USER; this program "
                "refuses to self-sign or accept agent-signed confirmations"
            )
        validate_nano_int(self.confirmed_at_nano)
        validate_nano_int(self.session_window_start_nano)
        validate_nano_int(self.session_window_end_nano)
        if self.session_window_end_nano <= self.session_window_start_nano:
            raise ContractError("authorization session window is empty")
        if self.expires_at_nano is not None:
            validate_nano_int(self.expires_at_nano)
        for name in (
            self.scope_policy_sha256,
            self.scope_review_seal_sha256,
            self.scope_code_manifest_sha256,
            self.scope_plan_sha256,
        ):
            if not isinstance(name, str) or len(name) != 64:
                raise ContractError("scope hashes must be 64-hex sha256 strings")
        if not self.protected_paths or not self.protected_db_objects:
            raise ContractError(
                "H12: authorization must carry an explicit protection list"
            )

    def expired(self, now_nano: int) -> bool:
        return (
            self.expires_at_nano is not None and now_nano >= self.expires_at_nano
        )

    def within_budget(self, policy: Policy) -> list[str]:
        """Authorization caps may tighten but never exceed policy (H10)."""
        budget = policy.budget
        problems: list[str] = []
        if self.max_serial_attempts > budget["serial_request_attempts_per_run_max"]:
            problems.append("authorization serial attempts exceed policy cap")
        if self.max_wall_seconds > budget["run_wall_seconds_max"]:
            problems.append("authorization wall seconds exceed policy cap")
        return problems


@dataclass(frozen=True)
class AcceptanceRecord:
    """H13/H15 acceptance record; independence is declared, not self-certified."""

    author: str
    independence_declaration: str  # must affirm no participation in implementation
    policy_sha256: str
    code_manifest_sha256: str
    input_hashes: tuple[tuple[str, str], ...]
    db_verification_snapshot_sha256: str
    evidence_integrity_verdict: str  # VERIFIED | INSUFFICIENT | NOT_RUN
    coverage_status: CoverageStatus
    scope_statement: str
    gaps: tuple[str, ...]
    next_steps: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.independence_declaration or "no part" not in self.independence_declaration:
            raise ContractError(
                "acceptance records must declare non-participation in the "
                "reviewed implementation"
            )
        if self.evidence_integrity_verdict == "VERIFIED" and self.author.startswith(
            "COLLECTOR_"
        ):
            raise ContractError(
                "automated pre-acceptance may not claim VERIFIED evidence integrity"
            )


__all__ = [
    "BAR_VALUE_FIELDS",
    "CLOSE_BUFFER_SECONDS",
    "CANONICAL_SERIALIZER_VERSION",
    "ContractError",
    "CoverageResult",
    "CoverageStatus",
    "CaptureTask",
    "ClassifiedRow",
    "FailureClass",
    "FROZEN_POLICY_SHA256",
    "FramePartition",
    "GapReason",
    "InstrumentKind",
    "LifecycleStatus",
    "MAX_SIGNED_INT64",
    "NANOSECOND",
    "Policy",
    "RawSnapshotMeta",
    "REQUEST_DATA_LENGTH_V1",
    "RunAuthorization",
    "SHANGHAI_TZ",
    "SUPPORTED_DURATIONS",
    "SourceIdentity",
    "AcceptanceRecord",
    "TargetKind",
    "TaskState",
    "ValidRowStatus",
    "canonical_bar_content",
    "canonical_json_bytes",
    "canonical_sha256",
    "civil_date_to_nano",
    "decimal_string",
    "iso_instant_to_nano",
    "nano_to_iso",
    "sha256_bytes",
    "sha256_file",
    "validate_nano_int",
]
