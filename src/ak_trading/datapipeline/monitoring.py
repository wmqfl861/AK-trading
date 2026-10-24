"""DP06 monitoring core: local alert engine with an independent monitor tick.

AUTHORITY
    docs/devplan-datapipeline-20260923-r1/work-orders/DP06-monitoring.md
    (H06.1-H06.8); quality semantics from 03-data-quality-contract.md (DQ-1);
    frozen value spaces from the DP01 contract freeze
    ``datapipeline_contracts_v1`` (docs/devplan-datapipeline-20260923-r1/
    DP01/r1, revision dp01-r1).

ROLE IN THE PIPELINE
    This module is the READ-ONLY observability consumer of pipeline facts
    (heartbeat files, status events, quality reports, calendar answers, run
    registry facts).  Per the 02 blueprint the monitor owns exactly
    ``R/alerts/<monitor_id>/**`` and ``R/publish-index/<monitor_id>.json``
    and never writes collector heartbeats or any other node's output.
    H06.3: alert detection runs on an INDEPENDENT monitor tick -- a dead
    collector still produces a missing-heartbeat alert because the monitor,
    not the monitored process, performs the staleness check.

FROZEN CONTRACT DEPENDENCY (hash-verified, read-only)
    DP01 r1 owns ``ak_trading.datapipeline.contracts``.  In the integrated
    candidate tree the module is importable directly.  While DP01 is still
    staged in its evidence package, the identical file is loaded from the
    pinned freeze location after verifying its SHA-256 against
    ``dp01-freeze-manifest.json`` (no hand-copied hashes; drift refuses to
    run).  Override the staged root with AK_DP01_FROZEN_ROOT.

IMPORT HYGIENE
    Importing this module performs no I/O: no .env, no socket, no database,
    no task registration, no filesystem access.  The frozen-contract loader
    and all stores run lazily inside explicit function/method calls.

LOCAL ONLY (H06.7)
    Alerts and notifications are local events/reports.  There is no e-mail,
    Slack, webhook or any other outbound delivery in this revision.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

__all__ = [
    "MONITOR_SCHEMA_VERSION",
    "CONTRACT_REVISION_PIN",
    "MonitoringError",
    "AlertReason",
    "AlertSeverity",
    "AlertTransition",
    "AlertStatus",
    "TaskHealth",
    "DP06_EVENT_REASON_CODES",
    "MonitoringThresholds",
    "default_thresholds",
    "thresholds_from_mapping",
    "AlertEvent",
    "AlertRecord",
    "MonitorState",
    "TickResult",
    "load_pipeline_facts",
    "evaluate_tick",
    "LocalMonitorStore",
    "assert_within_root",
    "validate_free_text",
    "frozen_contracts_module",
    "resolve_frozen_root",
]

MONITOR_SCHEMA_VERSION = "dp06-monitoring/1"
CONTRACT_REVISION_PIN = "dp01-r1"

_FROZEN_PACKAGE_REL = Path("src") / "ak_trading" / "datapipeline"
_FROZEN_CONTRACTS_NAME = "ak_trading.datapipeline.contracts"
_FROZEN_MANIFEST_NAME = "dp01-freeze-manifest.json"
_DEFAULT_FROZEN_ROOT_RELPATH = Path("docs") / "devplan-datapipeline-20260923-r1" / "DP01" / "r1"
_FROZEN_ROOT_ENV = "AK_DP01_FROZEN_ROOT"

_contracts_cache: MutableMapping[str, Any] = {}


class MonitoringError(RuntimeError):
    """DP06 local error with a stable machine-readable code.

    ``code`` is a DP06 monitoring error code (not a DP01 contract error
    code); ``detail`` never echoes credential-shaped or otherwise secret
    text (Q07/H06.8).
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Frozen DP01 contract access (read-only, hash-verified)
# ---------------------------------------------------------------------------


def resolve_frozen_root() -> Path:
    """Locate the pinned DP01 freeze root.

    Order: AK_DP01_FROZEN_ROOT env override, then the staged evidence path
    derived from this file's location (repo root = parents[3]).  Returned
    path is not checked here; :func:`frozen_contracts_module` verifies the
    manifest and file hashes before use.
    """
    env_root = os.environ.get(_FROZEN_ROOT_ENV)
    if env_root:
        return Path(env_root)
    return Path(__file__).resolve().parents[3] / _DEFAULT_FROZEN_ROOT_RELPATH


def _load_contracts_from_freeze(root: Path) -> Any:
    manifest_path = root / _FROZEN_MANIFEST_NAME
    if not manifest_path.is_file():
        raise MonitoringError(
            "E_DP06_FROZEN_MANIFEST_MISSING",
            f"DP01 freeze manifest not found at {manifest_path}; set {_FROZEN_ROOT_ENV}",
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MonitoringError(
            "E_DP06_FROZEN_MANIFEST_UNREADABLE",
            f"cannot read DP01 freeze manifest ({type(exc).__name__})",
        ) from None
    suffix = _FROZEN_PACKAGE_REL / "contracts.py"
    pinned = None
    for entry in manifest.get("files", []):
        entry_path = entry.get("path", "")
        if entry_path.endswith(str(suffix).replace("\\", "/")) or entry_path.endswith(str(suffix)):
            pinned = entry
            break
    if pinned is None:
        raise MonitoringError(
            "E_DP06_FROZEN_MANIFEST_MISSING",
            "manifest does not pin the frozen contracts.py entry",
        )
    contracts_path = root / _FROZEN_PACKAGE_REL / "contracts.py"
    if not contracts_path.is_file():
        raise MonitoringError(
            "E_DP06_FROZEN_FILE_MISSING",
            f"frozen contracts.py not found at {contracts_path}",
        )
    actual = hashlib.sha256(contracts_path.read_bytes()).hexdigest()
    if actual != pinned.get("sha256"):
        raise MonitoringError(
            "E_DP06_FROZEN_HASH_DRIFT",
            "frozen contracts.py sha256 does not match dp01-freeze-manifest pin; refusing",
        )
    spec = importlib.util.spec_from_file_location(_FROZEN_CONTRACTS_NAME, contracts_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise MonitoringError("E_DP06_FROZEN_LOAD_FAILED", "importlib produced no loader")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_FROZEN_CONTRACTS_NAME] = module
    spec.loader.exec_module(module)
    return module


def frozen_contracts_module() -> Any:
    """Return the DP01 frozen ``contracts`` module.

    Resolution order:
      1. ``import ak_trading.datapipeline.contracts`` -- the integrated
         candidate tree where DP01's files sit inside the package;
      2. hash-verified load from the pinned DP01 freeze evidence path.

    Both resolutions yield byte-identical semantics (the staged file is the
    same artifact DP01 froze); option 2 refuses on any hash drift.
    """
    cached = _contracts_cache.get("contracts")
    if cached is not None:
        return cached
    try:
        from ak_trading.datapipeline import contracts as _integrated  # type: ignore
    except ImportError:
        module = _load_contracts_from_freeze(resolve_frozen_root())
    else:
        if getattr(_integrated, "CONTRACT_SCHEMA_VERSION", "") != "datapipeline_contracts_v1":
            raise MonitoringError(
                "E_DP06_FROZEN_IDENTITY_MISMATCH",
                "integrated contracts module does not carry schema datapipeline_contracts_v1",
            )
        module = _integrated
    _contracts_cache["contracts"] = module
    return module


# ---------------------------------------------------------------------------
# Frozen DP06 r1 alert taxonomy (H06.2 coverage; H06.4 states)
# ---------------------------------------------------------------------------


class AlertReason(str, Enum):
    """Local alert reasons, each with an explicit cause payload (H06.2)."""

    HEARTBEAT_EXPIRED = "HEARTBEAT_EXPIRED"            # worker heartbeat stale/absent (monitor-detected)
    RUN_LOST = "RUN_LOST"                              # run says RUNNING, no liveness evidence
    SESSION_DISCONNECTED = "SESSION_DISCONNECTED"      # disconnect events beyond threshold in window
    PERMISSION_DENIED = "PERMISSION_DENIED"            # ACCOUNT_DENIED / PERMISSION_DENIED observed
    DB_COMMIT_FAILED = "DB_COMMIT_FAILED"              # commit failures beyond threshold
    PERSISTENT_EMPTY_SOURCE = "PERSISTENT_EMPTY_SOURCE"  # consecutive SOURCE_EMPTY_UNEXPLAINED
    WINDOW_LOST = "WINDOW_LOST"                        # window slid past unobserved in a VERIFIED session
    CALENDAR_UNKNOWN = "CALENDAR_UNKNOWN"              # calendar UNKNOWN over a monitored window
    CALENDAR_CONFLICT = "CALENDAR_CONFLICT"            # conflicting calendar evidence
    BACKLOG = "BACKLOG"                                # queued/retry backlog beyond threshold
    DISK_OUTPUT_FAILED = "DISK_OUTPUT_FAILED"          # monitor/report local output write failure


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AlertTransition(str, Enum):
    """Alert lifecycle events (H06.4 event chain)."""

    RAISED = "RAISED"
    RENOTIFIED = "RENOTIFIED"      # same reason persisted, cooldown elapsed
    SUPPRESSED = "SUPPRESSED"      # same reason persisted, inside cooldown
    ACKNOWLEDGED = "ACKNOWLEDGED"  # operator ACK; does NOT change data quality
    RESOLVED = "RESOLVED"          # condition cleared; recovery notification appended
    REOPENED = "REOPENED"          # condition returned after RESOLVED


class AlertStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class TaskHealth(str, Enum):
    """Per symbol x period health.  UNKNOWN is never displayed as OK (H06.3)."""

    OK = "OK"
    STALE = "STALE"
    DEGRADED = "DEGRADED"
    FAILING = "FAILING"
    UNKNOWN = "UNKNOWN"


#: Status-event reason codes the monitor understands (reading layer
#: vocabulary; DP03 emits DP01 ``StatusEvent`` records whose
#: ``reason_sanitized`` starts with one of these codes).  Unknown codes are
#: retained as facts but never silently reinterpreted.
DP06_EVENT_REASON_CODES: Tuple[str, ...] = (
    "TASK_QUEUED",
    "TASK_RUNNING",
    "TASK_RETRY_SCHEDULED",
    "TASK_ACCEPTED",
    "SESSION_DISCONNECTED",
    "PERMISSION_DENIED",
    "COMMIT_FAILED",
    "SOURCE_EMPTY",
    "OUTSIDE_FREE_WINDOW",
    "RUN_STARTED",
    "RUN_FINALIZED",
)

#: Frozen DP01 TaskState values that mean a hard task failure (mirrors
#: contracts.HARD_FAILURE_TASK_STATES; validated in tests against the
#: frozen module).
HARD_FAILURE_TASK_STATES: Tuple[str, ...] = (
    "RETRYABLE_FAILURE",
    "PERMISSION_DENIED",
    "UNSUPPORTED",
    "QUARANTINED",
)

_FAILURE_CLASS_ACCOUNT_DENIED = "ACCOUNT_DENIED"


# ---------------------------------------------------------------------------
# Thresholds (H06.2: per period; frozen defaults, explicit config only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonitoringThresholds:
    """Frozen r1 alert thresholds.  Times are integer nanoseconds.

    Window-loss thresholds are per ``duration_seconds``: a 60s stream that
    has not observed a bar for ``window_loss_after_nano[60]`` inside a
    VERIFIED session has likely lost the sliding free window; a daily bar
    gets until the next evidenced session end plus grace.  Market closure
    evidenced by the calendar (NO_TRADING_EVIDENCED) never fabricates a
    no-data fault (H06.2).
    """

    heartbeat_timeout_nano: int = 120 * 1_000_000_000
    run_lost_after_nano: int = 300 * 1_000_000_000
    disconnect_window_nano: int = 3600 * 1_000_000_000
    disconnect_threshold: int = 3
    commit_failure_window_nano: int = 1800 * 1_000_000_000
    commit_failure_threshold: int = 1
    empty_source_consecutive: int = 3
    backlog_retry_threshold: int = 24
    renotify_cooldown_nano: int = 1800 * 1_000_000_000
    window_loss_after_nano: Tuple[Tuple[int, int], ...] = (
        (60, 5 * 60 * 1_000_000_000),
        (86400, 36 * 3600 * 1_000_000_000),
    )

    def window_loss_after(self, duration_seconds: int) -> Optional[int]:
        for duration, after in self.window_loss_after_nano:
            if duration == duration_seconds:
                return after
        return None


def default_thresholds() -> MonitoringThresholds:
    return MonitoringThresholds()


def thresholds_from_mapping(mapping: Mapping[str, Any]) -> MonitoringThresholds:
    """Build thresholds from an explicit config mapping; unknown keys and
    non-positive times are rejected (never silently defaulted)."""
    valid = {f for f in MonitoringThresholds.__dataclass_fields__}
    unknown = sorted(set(mapping) - valid)
    if unknown:
        raise MonitoringError("E_DP06_THRESHOLD_UNKNOWN_KEY", f"unknown threshold keys {unknown}")
    kwargs: Dict[str, Any] = {}
    for key, value in mapping.items():
        if key == "window_loss_after_nano":
            pairs = []
            for item in value:
                duration, after = item
                if duration not in (60, 86400):
                    raise MonitoringError(
                        "E_DP06_THRESHOLD_BAD_PERIOD",
                        f"window_loss period {duration} not in frozen r1 durations (60, 86400)",
                    )
                pairs.append((int(duration), int(after)))
            kwargs[key] = tuple(sorted(pairs))
        else:
            kwargs[key] = int(value)
    for name in (
        "heartbeat_timeout_nano",
        "run_lost_after_nano",
        "disconnect_window_nano",
        "commit_failure_window_nano",
        "renotify_cooldown_nano",
    ):
        if name in kwargs and kwargs[name] <= 0:
            raise MonitoringError("E_DP06_THRESHOLD_NON_POSITIVE", f"{name} must be positive")
    for name in ("disconnect_threshold", "commit_failure_threshold", "empty_source_consecutive"):
        if name in kwargs and kwargs[name] < 1:
            raise MonitoringError("E_DP06_THRESHOLD_NON_POSITIVE", f"{name} must be >= 1")
    return MonitoringThresholds(**kwargs)


# ---------------------------------------------------------------------------
# Alert records / monitor state (JSON round-trip, never overwriting history)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlertEvent:
    """One transition in an alert's lifecycle (append-only event chain)."""

    event_id: str
    alert_key: str
    transition: str            # AlertTransition value
    at_nano: int
    reason: str                # AlertReason value
    severity: str              # AlertSeverity value at event time
    cause: str                 # explicit, sanitized, human-readable cause (H06.2)
    evidence_refs: Tuple[str, ...] = ()
    notified: bool = False     # local notification emitted for this event

    def to_dict(self) -> dict:
        return {
            "alert_key": self.alert_key,
            "at_nano": str(self.at_nano),
            "cause": self.cause,
            "event_id": self.event_id,
            "evidence_refs": list(self.evidence_refs),
            "notified": self.notified,
            "reason": self.reason,
            "severity": self.severity,
            "transition": self.transition,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AlertEvent":
        return cls(
            event_id=str(payload["event_id"]),
            alert_key=str(payload["alert_key"]),
            transition=str(payload["transition"]),
            at_nano=int(payload["at_nano"]),
            reason=str(payload["reason"]),
            severity=str(payload["severity"]),
            cause=str(payload["cause"]),
            evidence_refs=tuple(str(r) for r in payload.get("evidence_refs", ())),
            notified=bool(payload.get("notified", False)),
        )


@dataclass
class AlertRecord:
    """Live state of one alert key (H06.4: key, severity, first/last, count,
    cooldown, ACK/RESOLVED chain)."""

    alert_key: str
    reason: str
    severity: str
    first_raised_nano: int
    last_raised_nano: int
    occurrence_count: int
    last_notified_nano: Optional[int]
    status: str                              # AlertStatus value
    suppressed_count: int = 0
    acked_at_nano: Optional[int] = None
    resolved_at_nano: Optional[int] = None
    cause: str = ""
    evidence_refs: Tuple[str, ...] = ()
    events: Tuple[AlertEvent, ...] = ()

    def to_dict(self) -> dict:
        return {
            "alert_key": self.alert_key,
            "cause": self.cause,
            "evidence_refs": list(self.evidence_refs),
            "events": [event.to_dict() for event in self.events],
            "first_raised_nano": str(self.first_raised_nano),
            "last_notified_nano": (str(self.last_notified_nano) if self.last_notified_nano is not None else None),
            "last_raised_nano": str(self.last_raised_nano),
            "occurrence_count": self.occurrence_count,
            "reason": self.reason,
            "severity": self.severity,
            "status": self.status,
            "suppressed_count": self.suppressed_count,
            "acked_at_nano": (str(self.acked_at_nano) if self.acked_at_nano is not None else None),
            "resolved_at_nano": (str(self.resolved_at_nano) if self.resolved_at_nano is not None else None),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AlertRecord":
        return cls(
            alert_key=str(payload["alert_key"]),
            reason=str(payload["reason"]),
            severity=str(payload["severity"]),
            first_raised_nano=int(payload["first_raised_nano"]),
            last_raised_nano=int(payload["last_raised_nano"]),
            occurrence_count=int(payload["occurrence_count"]),
            last_notified_nano=(int(payload["last_notified_nano"]) if payload["last_notified_nano"] is not None else None),
            status=str(payload["status"]),
            suppressed_count=int(payload.get("suppressed_count", 0)),
            acked_at_nano=(int(payload["acked_at_nano"]) if payload.get("acked_at_nano") is not None else None),
            resolved_at_nano=(int(payload["resolved_at_nano"]) if payload.get("resolved_at_nano") is not None else None),
            cause=str(payload.get("cause", "")),
            evidence_refs=tuple(str(r) for r in payload.get("evidence_refs", ())),
            events=tuple(AlertEvent.from_dict(e) for e in payload.get("events", ())),
        )


@dataclass
class MonitorState:
    """Whole monitor state; serialized as new revisions, never overwritten."""

    monitor_id: str
    schema_version: str = MONITOR_SCHEMA_VERSION
    tick_count: int = 0
    event_seq: int = 0
    alerts: Dict[str, AlertRecord] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "alerts": {key: record.to_dict() for key, record in sorted(self.alerts.items())},
            "event_seq": self.event_seq,
            "monitor_id": self.monitor_id,
            "schema_version": self.schema_version,
            "tick_count": self.tick_count,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MonitorState":
        return cls(
            monitor_id=str(payload["monitor_id"]),
            schema_version=str(payload.get("schema_version", MONITOR_SCHEMA_VERSION)),
            tick_count=int(payload.get("tick_count", 0)),
            event_seq=int(payload.get("event_seq", 0)),
            alerts={
                str(key): AlertRecord.from_dict(value)
                for key, value in payload.get("alerts", {}).items()
            },
        )


@dataclass(frozen=True)
class TickResult:
    """Outcome of one monitor tick (pure evaluation + emitted events)."""

    state: MonitorState
    events: Tuple[AlertEvent, ...]
    notifications: Tuple[AlertEvent, ...]
    health: Dict[str, str]          # task_key -> TaskHealth value
    health_reasons: Dict[str, Tuple[str, ...]]
    as_of_nano: int
    disk_output_failures: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Fact loading (explicit input roots only; SYNTHETIC fixtures carry markers)
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MonitoringError(
            "E_DP06_INPUT_UNREADABLE",
            f"cannot read {path.name} ({type(exc).__name__})",
        ) from None


def _iter_jsonl(path: Path) -> List[dict]:
    records: List[dict] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            records.append(json.loads(text))
        except ValueError:
            raise MonitoringError(
                "E_DP06_INPUT_UNREADABLE",
                f"{path.name}:{line_no} is not valid JSON",
            ) from None
    return records


def load_pipeline_facts(
    *,
    heartbeats_root: Optional[Path] = None,
    events_root: Optional[Path] = None,
    quality_root: Optional[Path] = None,
    tasks_file: Optional[Path] = None,
    calendar_file: Optional[Path] = None,
    runs_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load pipeline facts from explicitly injected input roots.

    Every root must exist; nothing is inferred from the process CWD and no
    network/DB access happens here.  Returns a snapshot dict consumed by
    :func:`evaluate_tick` and ``reporting.build_report``.
    """
    snapshot: Dict[str, Any] = {
        "heartbeats": [],
        "status_events": [],
        "quality_reports": [],
        "tasks": [],
        "calendar_answers": [],
        "runs": [],
        "sources_read": [],
    }

    def note(path: Path) -> Path:
        snapshot["sources_read"].append(str(path))
        return path

    if heartbeats_root is not None:
        root = Path(heartbeats_root)
        if not root.is_dir():
            raise MonitoringError("E_DP06_INPUT_ROOT_MISSING", f"heartbeats root missing: {root}")
        for path in sorted(root.glob("*.json")):
            snapshot["heartbeats"].append(_read_json(note(path)))

    if events_root is not None:
        root = Path(events_root)
        if not root.is_dir():
            raise MonitoringError("E_DP06_INPUT_ROOT_MISSING", f"events root missing: {root}")
        for path in sorted(root.rglob("*.jsonl")):
            snapshot["status_events"].extend(_iter_jsonl(note(path)))
    snapshot["status_events"].sort(key=lambda e: (int(e.get("occurred_at_nano", 0)), str(e.get("event_id", ""))))

    if quality_root is not None:
        root = Path(quality_root)
        if not root.is_dir():
            raise MonitoringError("E_DP06_INPUT_ROOT_MISSING", f"quality root missing: {root}")
        for path in sorted(root.rglob("*.json")):
            payload = _read_json(note(path))
            if isinstance(payload, list):          # one file may carry many reports
                snapshot["quality_reports"].extend(payload)
            else:
                snapshot["quality_reports"].append(payload)

    if tasks_file is not None:
        payload = _read_json(Path(tasks_file))
        snapshot["tasks"] = payload.get("tasks", payload) if isinstance(payload, dict) else payload
        note(Path(tasks_file))

    if calendar_file is not None:
        payload = _read_json(Path(calendar_file))
        snapshot["calendar_answers"] = (
            payload.get("calendar_answers", payload) if isinstance(payload, dict) else payload
        )
        note(Path(calendar_file))

    if runs_file is not None:
        payload = _read_json(Path(runs_file))
        snapshot["runs"] = payload.get("runs", payload) if isinstance(payload, dict) else payload
        note(Path(runs_file))

    return snapshot


# ---------------------------------------------------------------------------
# Sanitization gate (H06.8 / Q07)
# ---------------------------------------------------------------------------


def validate_free_text(value: str, field_path: str) -> str:
    """Reject credential-shaped free text BEFORE it can enter any report.

    Uses the frozen DP01 ``detect_credential_shape`` (single source of the
    pattern semantics).  The offending VALUE is never echoed into the error
    (only the field path), so no secret-derived string enters evidence.
    """
    contracts = frozen_contracts_module()
    matched = contracts.detect_credential_shape(value)
    if matched is not None:
        raise MonitoringError(
            "E_DP06_CREDENTIAL_SHAPE_BLOCKED",
            f"field {field_path} matched credential shape {matched}; publish blocked",
        )
    return value


# ---------------------------------------------------------------------------
# Pure tick evaluation (H06.2/H06.3/H06.4; fake-clock friendly)
# ---------------------------------------------------------------------------


def _calendar_status_at(answer_list: Sequence[Mapping[str, Any]], task: Mapping[str, Any], instant_nano: int) -> Tuple[str, Optional[Mapping[str, Any]]]:
    """Resolve the calendar status covering ``instant_nano`` for one task.

    Returns (status, answer) with status a frozen CalendarAnswerStatus
    value; no covering evidence resolves to UNKNOWN (never VERIFIED)."""
    symbol = str(task.get("source_symbol", ""))
    duration = int(task.get("duration_seconds", 0))
    for answer in answer_list:
        if str(answer.get("source_symbol", "")) != symbol:
            continue
        if int(answer.get("duration_seconds", -1)) != duration:
            continue
        start = int(answer.get("window_start_nano", 0))
        end = int(answer.get("window_end_nano", 0))
        if start <= instant_nano < end:
            return str(answer.get("status", "UNKNOWN")), answer
    return "UNKNOWN", None


def _events_for_task(events: Sequence[Mapping[str, Any]], task_key: str) -> List[Mapping[str, Any]]:
    return [e for e in events if str(e.get("task_key", "")) == task_key]


def _task_key(task: Mapping[str, Any]) -> str:
    return str(
        task.get(
            "task_key",
            f"{task.get('source_id', '')}|{task.get('source_symbol', '')}|{task.get('duration_seconds', '')}",
        )
    )


def _emit(
    state: MonitorState,
    *,
    alert_key: str,
    reason: str,
    severity: str,
    now_nano: int,
    cause: str,
    evidence_refs: Sequence[str],
    cooldown_nano: int,
    events: List[AlertEvent],
    notifications: List[AlertEvent],
) -> None:
    """Apply the H06.4 alert state machine for one detection at one tick."""
    for text, label in ((cause, "cause"),):
        validate_free_text(text, f"alert[{alert_key}].{label}")
    for ref in evidence_refs:
        validate_free_text(ref, f"alert[{alert_key}].evidence_ref")

    existing = state.alerts.get(alert_key)
    if existing is None or existing.status == "RESOLVED":
        record = AlertRecord(
            alert_key=alert_key,
            reason=reason,
            severity=severity,
            first_raised_nano=now_nano,
            last_raised_nano=now_nano,
            occurrence_count=1,
            last_notified_nano=now_nano,
            status="OPEN",
            cause=cause,
            evidence_refs=tuple(evidence_refs),
        )
        event = _new_event(state, record, "RAISED" if existing is None else "REOPENED", now_nano, cause, evidence_refs, notified=True)
        record.events = record.events + (event,)
        state.alerts[alert_key] = record
        events.append(event)
        notifications.append(event)
        return

    # Existing open/acknowledged alert, same reason persists (H06.4 cooldown).
    record = existing
    record.last_raised_nano = now_nano
    record.occurrence_count += 1
    record.cause = cause
    record.evidence_refs = tuple(evidence_refs)
    last_notified = record.last_notified_nano if record.last_notified_nano is not None else record.first_raised_nano
    if now_nano - last_notified >= cooldown_nano:
        transition = "RENOTIFIED"
        record.last_notified_nano = now_nano
        notified = True
    else:
        transition = "SUPPRESSED"
        record.suppressed_count += 1
        notified = False
    event = _new_event(state, record, transition, now_nano, cause, evidence_refs, notified=notified)
    record.events = record.events + (event,)
    events.append(event)
    if notified:
        notifications.append(event)


def _resolve(
    state: MonitorState,
    *,
    alert_key: str,
    now_nano: int,
    cause: str,
    evidence_refs: Sequence[str],
    events: List[AlertEvent],
    notifications: List[AlertEvent],
) -> None:
    """Mark a previously open/acknowledged alert RESOLVED (recovery
    notification appended, H06.4).  Resolving an already-resolved alert is a
    no-op."""
    record = state.alerts.get(alert_key)
    if record is None or record.status == "RESOLVED":
        return
    validate_free_text(cause, f"alert[{alert_key}].cause")
    record.status = "RESOLVED"
    record.resolved_at_nano = now_nano
    event = _new_event(state, record, "RESOLVED", now_nano, cause, evidence_refs, notified=True)
    record.events = record.events + (event,)
    events.append(event)
    notifications.append(event)


def _new_event(
    state: MonitorState,
    record: AlertRecord,
    transition: str,
    now_nano: int,
    cause: str,
    evidence_refs: Sequence[str],
    *,
    notified: bool,
) -> AlertEvent:
    state.event_seq += 1
    return AlertEvent(
        event_id=f"{state.monitor_id}-evt-{state.event_seq:08d}",
        alert_key=record.alert_key,
        transition=transition,
        at_nano=now_nano,
        reason=record.reason,
        severity=record.severity,
        cause=cause,
        evidence_refs=tuple(evidence_refs),
        notified=notified,
    )


def acknowledge_alert(state: MonitorState, alert_key: str, now_nano: int, ack_note: str = "operator ack") -> AlertEvent:
    """Record an operator ACK (H06.4).

    ACK changes only the alert's presentation state.  It does NOT change
    any data-quality number, coverage conclusion or gap count, and never
    counts as backfill; the caller's reports keep counting acknowledged
    alerts as active.
    """
    record = state.alerts.get(alert_key)
    if record is None:
        raise MonitoringError("E_DP06_ACK_UNKNOWN_ALERT", f"no alert with key {alert_key}")
    if record.status not in ("OPEN", "ACKNOWLEDGED"):
        raise MonitoringError("E_DP06_ACK_INVALID_STATE", f"alert {alert_key} is {record.status}")
    validate_free_text(ack_note, f"ack[{alert_key}].note")
    record.status = "ACKNOWLEDGED"
    record.acked_at_nano = now_nano
    state.event_seq += 1
    event = AlertEvent(
        event_id=f"{state.monitor_id}-evt-{state.event_seq:08d}",
        alert_key=alert_key,
        transition="ACKNOWLEDGED",
        at_nano=now_nano,
        reason=record.reason,
        severity=record.severity,
        cause=ack_note,
        evidence_refs=record.evidence_refs,
        notified=False,
    )
    record.events = record.events + (event,)
    return event


def evaluate_tick(
    state: MonitorState,
    snapshot: Mapping[str, Any],
    now_nano: int,
    thresholds: Optional[MonitoringThresholds] = None,
    *,
    disk_output_failures: Sequence[str] = (),
) -> TickResult:
    """One independent monitor tick (H06.3).

    Pure function of (state, snapshot, now, thresholds): the fake clock is
    injected, no wall clock is read, no I/O happens.  Every detection
    carries an explicit cause and evidence references; evidenced market
    closure suppresses window-loss fabrication (H06.2); UNKNOWN calendar
    windows surface as CALENDAR_UNKNOWN, never as OK.
    """
    contracts = frozen_contracts_module()
    thresholds = thresholds or default_thresholds()
    state.tick_count += 1
    events: List[AlertEvent] = []
    notifications: List[AlertEvent] = []

    # --- disk output failure (self-observed; H06.2 last item) -------------
    disk_key = f"DISK_OUTPUT_FAILED|monitor={state.monitor_id}"
    if disk_output_failures:
        _emit(
            state,
            alert_key=disk_key,
            reason=AlertReason.DISK_OUTPUT_FAILED.value,
            severity=AlertSeverity.CRITICAL.value,
            now_nano=now_nano,
            cause="local report/alert output write failed: " + "; ".join(disk_output_failures),
            evidence_refs=[],
            cooldown_nano=thresholds.renotify_cooldown_nano,
            events=events,
            notifications=notifications,
        )
    else:
        _resolve(
            state,
            alert_key=disk_key,
            now_nano=now_nano,
            cause="local outputs writable again",
            evidence_refs=[],
            events=events,
            notifications=notifications,
        )

    heartbeats = list(snapshot.get("heartbeats", []))
    status_events = list(snapshot.get("status_events", []))
    tasks = list(snapshot.get("tasks", []))
    calendar_answers = list(snapshot.get("calendar_answers", []))
    runs = list(snapshot.get("runs", []))

    # --- worker heartbeat liveness (H06.3: monitor-side detection) --------
    for beat in heartbeats:
        worker_id = str(beat.get("worker_id", ""))
        if not worker_id:
            continue
        key = f"HEARTBEAT_EXPIRED|worker={worker_id}"
        beat_at = int(beat.get("heartbeat_at_nano", 0))
        age = now_nano - beat_at
        if age > thresholds.heartbeat_timeout_nano:
            _emit(
                state,
                alert_key=key,
                reason=AlertReason.HEARTBEAT_EXPIRED.value,
                severity=AlertSeverity.CRITICAL.value,
                now_nano=now_nano,
                cause=(
                    f"worker {worker_id} heartbeat stale by {age // 1_000_000_000}s "
                    f"(threshold {thresholds.heartbeat_timeout_nano // 1_000_000_000}s); "
                    "detected by the independent monitor tick"
                ),
                evidence_refs=[str(beat.get("heartbeat_source", f"heartbeats/{worker_id}.json"))],
                cooldown_nano=thresholds.renotify_cooldown_nano,
                events=events,
                notifications=notifications,
            )
        else:
            _resolve(
                state,
                alert_key=key,
                now_nano=now_nano,
                cause=f"worker {worker_id} heartbeat fresh",
                evidence_refs=[str(beat.get("heartbeat_source", f"heartbeats/{worker_id}.json"))],
                events=events,
                notifications=notifications,
            )

    # --- run liveness (RUN_LOST; distinct from worker heartbeat) ---------
    latest_run_beat: Dict[str, int] = {}
    for event in status_events:
        run_id = str(event.get("run_id", ""))
        if not run_id:
            continue
        occurred = int(event.get("occurred_at_nano", 0))
        if run_id not in latest_run_beat or occurred > latest_run_beat[run_id]:
            latest_run_beat[run_id] = occurred
    for run in runs:
        run_id = str(run.get("run_id", ""))
        run_state = str(run.get("state", ""))
        if run_state != "RUNNING":
            continue
        key = f"RUN_LOST|run={run_id}"
        last_seen = max(
            latest_run_beat.get(run_id, 0),
            max((int(b.get("heartbeat_at_nano", 0)) for b in heartbeats if str(b.get("run_id", "")) == run_id), default=0),
        )
        silent_for = now_nano - last_seen
        if silent_for > thresholds.run_lost_after_nano:
            _emit(
                state,
                alert_key=key,
                reason=AlertReason.RUN_LOST.value,
                severity=AlertSeverity.CRITICAL.value,
                now_nano=now_nano,
                cause=(
                    f"run {run_id} still RUNNING but no heartbeat/status event for "
                    f"{silent_for // 1_000_000_000}s (threshold "
                    f"{thresholds.run_lost_after_nano // 1_000_000_000}s)"
                ),
                evidence_refs=[str(run.get("run_source", f"runs/{run_id}.json"))],
                cooldown_nano=thresholds.renotify_cooldown_nano,
                events=events,
                notifications=notifications,
            )
        else:
            _resolve(
                state,
                alert_key=key,
                now_nano=now_nano,
                cause=f"run {run_id} has fresh liveness evidence",
                evidence_refs=[],
                events=events,
                notifications=notifications,
            )

    # --- per task detections ----------------------------------------------
    health: Dict[str, str] = {}
    health_reasons: Dict[str, Tuple[str, ...]] = {}

    for task in tasks:
        task_key = _task_key(task)
        task_events = _events_for_task(status_events, task_key)
        duration = int(task.get("duration_seconds", 0))
        reasons: List[str] = []

        # PERMISSION_DENIED: immediate CRITICAL (H06.2).
        denied = [
            e for e in task_events
            if str(e.get("reason_sanitized", "")).startswith("PERMISSION_DENIED")
            or str(e.get("failure_class", "")) == _FAILURE_CLASS_ACCOUNT_DENIED
            or str(e.get("task_state", "")) == "PERMISSION_DENIED"
        ]
        key = f"PERMISSION_DENIED|task={task_key}"
        if denied:
            latest = denied[-1]
            _emit(
                state,
                alert_key=key,
                reason=AlertReason.PERMISSION_DENIED.value,
                severity=AlertSeverity.CRITICAL.value,
                now_nano=now_nano,
                cause=f"source permission denied for {task_key} at event {latest.get('event_id', '?')}",
                evidence_refs=[str(latest.get("evidence_ref", "")) or str(latest.get("event_id", ""))],
                cooldown_nano=thresholds.renotify_cooldown_nano,
                events=events,
                notifications=notifications,
            )
            reasons.append("PERMISSION_DENIED")
        else:
            _resolve(state, alert_key=key, now_nano=now_nano, cause="no permission denial observed", evidence_refs=[], events=events, notifications=notifications)

        # DB_COMMIT_FAILED beyond threshold inside window.
        commit_failures = [
            e for e in task_events
            if str(e.get("reason_sanitized", "")).startswith("COMMIT_FAILED")
            and now_nano - int(e.get("occurred_at_nano", 0)) <= thresholds.commit_failure_window_nano
        ]
        key = f"DB_COMMIT_FAILED|task={task_key}"
        if len(commit_failures) >= thresholds.commit_failure_threshold:
            _emit(
                state,
                alert_key=key,
                reason=AlertReason.DB_COMMIT_FAILED.value,
                severity=AlertSeverity.CRITICAL.value,
                now_nano=now_nano,
                cause=(
                    f"{len(commit_failures)} commit failure(s) for {task_key} within "
                    f"{thresholds.commit_failure_window_nano // 1_000_000_000}s"
                ),
                evidence_refs=[str(e.get("event_id", "")) for e in commit_failures[-3:]],
                cooldown_nano=thresholds.renotify_cooldown_nano,
                events=events,
                notifications=notifications,
            )
            reasons.append("DB_COMMIT_FAILED")
        else:
            _resolve(state, alert_key=key, now_nano=now_nano, cause="commit failures within threshold", evidence_refs=[], events=events, notifications=notifications)

        # PERSISTENT_EMPTY_SOURCE: consecutive empties at the tail.
        empties = [e for e in task_events if str(e.get("reason_sanitized", "")).startswith("SOURCE_EMPTY")]
        consecutive = 0
        ordered = sorted(task_events, key=lambda e: int(e.get("occurred_at_nano", 0)))
        for event in reversed(ordered):
            if str(event.get("reason_sanitized", "")).startswith("SOURCE_EMPTY"):
                consecutive += 1
            else:
                break
        key = f"PERSISTENT_EMPTY_SOURCE|task={task_key}"
        if consecutive >= thresholds.empty_source_consecutive:
            _emit(
                state,
                alert_key=key,
                reason=AlertReason.PERSISTENT_EMPTY_SOURCE.value,
                severity=AlertSeverity.WARNING.value,
                now_nano=now_nano,
                cause=(
                    f"{consecutive} consecutive unexplained empty returns for {task_key} "
                    f"without an activity-rule evidence (threshold {thresholds.empty_source_consecutive})"
                ),
                evidence_refs=[str(e.get("event_id", "")) for e in empties[-3:]],
                cooldown_nano=thresholds.renotify_cooldown_nano,
                events=events,
                notifications=notifications,
            )
            reasons.append("PERSISTENT_EMPTY_SOURCE")
        else:
            _resolve(state, alert_key=key, now_nano=now_nano, cause="empty-source streak below threshold", evidence_refs=[], events=events, notifications=notifications)

        # SESSION_DISCONNECTED beyond threshold in window.
        disconnects = [
            e for e in task_events
            if str(e.get("reason_sanitized", "")).startswith("SESSION_DISCONNECTED")
            and now_nano - int(e.get("occurred_at_nano", 0)) <= thresholds.disconnect_window_nano
        ]
        key = f"SESSION_DISCONNECTED|task={task_key}"
        if len(disconnects) >= thresholds.disconnect_threshold:
            _emit(
                state,
                alert_key=key,
                reason=AlertReason.SESSION_DISCONNECTED.value,
                severity=AlertSeverity.WARNING.value,
                now_nano=now_nano,
                cause=(
                    f"{len(disconnects)} session disconnect(s) for {task_key} within "
                    f"{thresholds.disconnect_window_nano // 1_000_000_000}s "
                    f"(threshold {thresholds.disconnect_threshold})"
                ),
                evidence_refs=[str(e.get("event_id", "")) for e in disconnects[-3:]],
                cooldown_nano=thresholds.renotify_cooldown_nano,
                events=events,
                notifications=notifications,
            )
            reasons.append("SESSION_DISCONNECTED")
        else:
            _resolve(state, alert_key=key, now_nano=now_nano, cause="disconnect count within threshold", evidence_refs=[], events=events, notifications=notifications)

        # BACKLOG: retry pressure beyond the frozen per-run attempt budget.
        retry_counts = [int(e.get("retry_count", 0) or 0) for e in task_events]
        queued = sum(1 for e in task_events if str(e.get("reason_sanitized", "")).startswith("TASK_QUEUED"))
        key = f"BACKLOG|task={task_key}"
        if (retry_counts and max(retry_counts) > thresholds.backlog_retry_threshold) or queued > thresholds.backlog_retry_threshold:
            _emit(
                state,
                alert_key=key,
                reason=AlertReason.BACKLOG.value,
                severity=AlertSeverity.WARNING.value,
                now_nano=now_nano,
                cause=(
                    f"retry/queue backlog for {task_key}: max_retry={max(retry_counts) if retry_counts else 0}, "
                    f"queued={queued} (threshold {thresholds.backlog_retry_threshold})"
                ),
                evidence_refs=[],
                cooldown_nano=thresholds.renotify_cooldown_nano,
                events=events,
                notifications=notifications,
            )
            reasons.append("BACKLOG")
        else:
            _resolve(state, alert_key=key, now_nano=now_nano, cause="backlog within threshold", evidence_refs=[], events=events, notifications=notifications)

        # Calendar status over the task's monitored window (H06.2: no
        # fabrication during evidenced closure; UNKNOWN surfaces honestly).
        calendar_status, calendar_answer = _calendar_status_at(calendar_answers, task, now_nano)
        if calendar_status == "UNKNOWN":
            # Only raise when there is collection activity to judge.
            if task_events:
                _emit(
                    state,
                    alert_key=f"CALENDAR_UNKNOWN|task={task_key}",
                    reason=AlertReason.CALENDAR_UNKNOWN.value,
                    severity=AlertSeverity.WARNING.value,
                    now_nano=now_nano,
                    cause=(
                        f"calendar UNKNOWN over the monitored window for {task_key}; "
                        "data absence cannot be judged as fault or closure"
                    ),
                    evidence_refs=[],
                    cooldown_nano=thresholds.renotify_cooldown_nano,
                    events=events,
                    notifications=notifications,
                )
                reasons.append("CALENDAR_UNKNOWN")
            else:
                _resolve(state, alert_key=f"CALENDAR_UNKNOWN|task={task_key}", now_nano=now_nano, cause="no activity to judge", evidence_refs=[], events=events, notifications=notifications)
        elif calendar_status == "CONFLICT":
            _emit(
                state,
                alert_key=f"CALENDAR_CONFLICT|task={task_key}",
                reason=AlertReason.CALENDAR_CONFLICT.value,
                severity=AlertSeverity.WARNING.value,
                now_nano=now_nano,
                cause=f"conflicting calendar evidence for {task_key}",
                evidence_refs=[str(r) for r in (calendar_answer or {}).get("conflict_refs", [])],
                cooldown_nano=thresholds.renotify_cooldown_nano,
                events=events,
                notifications=notifications,
            )
            reasons.append("CALENDAR_CONFLICT")
        else:
            for stale_key in (f"CALENDAR_UNKNOWN|task={task_key}", f"CALENDAR_CONFLICT|task={task_key}"):
                _resolve(state, alert_key=stale_key, now_nano=now_nano, cause="calendar status resolved", evidence_refs=[], events=events, notifications=notifications)

        # WINDOW_LOST: requires a VERIFIED session covering now and a last
        # observation older than the per-period threshold.  NO_TRADING_
        # EVIDENCED closure never fabricates a no-data fault (H06.2).
        last_observed = max(
            [int(e.get("last_observed_nano", 0) or 0) for e in task_events] + [0]
        )
        window_loss_after = thresholds.window_loss_after(duration)
        key = f"WINDOW_LOST|task={task_key}"
        if calendar_status == "VERIFIED_SESSIONS" and window_loss_after is not None and last_observed > 0:
            lag = now_nano - last_observed
            if lag > window_loss_after:
                _emit(
                    state,
                    alert_key=key,
                    reason=AlertReason.WINDOW_LOST.value,
                    severity=AlertSeverity.CRITICAL.value,
                    now_nano=now_nano,
                    cause=(
                        f"no observation for {task_key} in a VERIFIED session for "
                        f"{lag // 1_000_000_000}s (period threshold "
                        f"{window_loss_after // 1_000_000_000}s); the free 8192-bar "
                        "window may have slid past"
                    ),
                    evidence_refs=[],
                    cooldown_nano=thresholds.renotify_cooldown_nano,
                    events=events,
                    notifications=notifications,
                )
                reasons.append("WINDOW_LOST")
            else:
                _resolve(state, alert_key=key, now_nano=now_nano, cause="observations fresh within threshold", evidence_refs=[], events=events, notifications=notifications)
        else:
            # No verified session (closure evidenced or calendar unknown):
            # never fabricate a window-loss fault.  Resolve any prior alert
            # only when closure is evidenced; UNKNOWN keeps prior state.
            if calendar_status == "NO_TRADING_EVIDENCED":
                _resolve(
                    state,
                    alert_key=key,
                    now_nano=now_nano,
                    cause="market closure evidenced by calendar; no trading window to lose",
                    evidence_refs=[],
                    events=events,
                    notifications=notifications,
                )

        # --- health derivation (H06.3: UNKNOWN never displays OK) ---------
        task_state = str(task.get("acquisition_state", ""))
        if task_state in HARD_FAILURE_TASK_STATES:
            reasons.append(f"TASK_STATE_{task_state}")   # manifest terminal state is a fact
        if not task_events and task_state not in HARD_FAILURE_TASK_STATES:
            # Zero task events: a RUNNING manifest alone is not evidence that
            # this task is flowing; only a known terminal failure is a fact.
            health[task_key] = TaskHealth.UNKNOWN.value
            health_reasons[task_key] = ("no execution evidence; check NOT_RUN",)
            continue
        hard_fail_states = set(HARD_FAILURE_TASK_STATES)
        active = tuple(reasons)
        if any(r in ("PERMISSION_DENIED", "DB_COMMIT_FAILED", "WINDOW_LOST", "PERSISTENT_EMPTY_SOURCE") for r in active) or task_state in hard_fail_states:
            health[task_key] = TaskHealth.FAILING.value
        elif any(r in ("SESSION_DISCONNECTED", "BACKLOG", "CALENDAR_UNKNOWN", "CALENDAR_CONFLICT") for r in active):
            health[task_key] = TaskHealth.DEGRADED.value
        else:
            worker_ids = {str(b.get("worker_id", "")) for b in heartbeats if str(b.get("run_id", "")) == str(task.get("run_id", ""))}
            fresh_beat = any(
                b for b in heartbeats
                if str(b.get("worker_id", "")) in worker_ids
                and now_nano - int(b.get("heartbeat_at_nano", 0)) <= thresholds.heartbeat_timeout_nano
            ) if worker_ids else False
            if fresh_beat or last_observed > 0:
                health[task_key] = TaskHealth.OK.value
            else:
                health[task_key] = TaskHealth.UNKNOWN.value
        health_reasons[task_key] = tuple(reasons) or ("no adverse conditions observed",)

    _ = contracts  # frozen module validated on first call; values used above
    return TickResult(
        state=state,
        events=tuple(events),
        notifications=tuple(notifications),
        health=health,
        health_reasons=health_reasons,
        as_of_nano=now_nano,
        disk_output_failures=tuple(disk_output_failures),
    )


# ---------------------------------------------------------------------------
# Output containment + local store (02 blueprint output whitelist)
# ---------------------------------------------------------------------------


def assert_within_root(root: Path, target: Path) -> Path:
    """Refuse path escapes and junction/symlink components BEFORE any access
    (V12 discipline: the refusal happens before touching the forbidden
    face)."""
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    if target_resolved != root_resolved and root_resolved not in target_resolved.parents:
        raise MonitoringError("E_DP06_OUTPUT_OUTSIDE_ROOT", f"path escapes the injected output root: {target}")
    checked = Path(target.anchor) if target.is_absolute() else Path()
    for part in target.parts[1:] if target.is_absolute() else target.parts:
        checked = checked / part
        if checked.exists() and checked.is_symlink():
            raise MonitoringError("E_DP06_OUTPUT_SYMLINK_REFUSED", f"symlink/junction component refused: {checked}")
    return target_resolved


class LocalMonitorStore:
    """DP06-owned output tree: ``alerts/<monitor_id>/**`` and
    ``publish-index/<monitor_id>.json`` (02 blueprint).

    Writes are append-mostly: every tick publishes a NEW state revision and
    a NEW ordered event log; the publish index is atomically replaced (the
    one derived pointer the blueprint allows its owner to swap).  Original
    records are never overwritten (H06.5).
    """

    def __init__(self, root: Path, monitor_id: str) -> None:
        sanitized = monitor_id.replace("/", "_").replace("\\", "_").replace("..", "_")
        if not sanitized or sanitized.startswith("."):
            raise MonitoringError("E_DP06_MONITOR_ID_INVALID", "monitor_id must be a non-empty safe name")
        self.root = Path(root)
        self.monitor_id = sanitized
        self.alerts_dir = self.root / "alerts" / sanitized
        self.index_path = self.root / "publish-index" / f"{sanitized}.json"

    def _next_revision(self) -> int:
        highest = 0
        if self.alerts_dir.is_dir():
            for child in self.alerts_dir.iterdir():
                name = child.name
                if name.startswith("state-r") and name.endswith(".json"):
                    try:
                        highest = max(highest, int(name[len("state-r"):-len(".json")]))
                    except ValueError:
                        continue
        return highest + 1

    def read_state(self) -> MonitorState:
        latest = self._latest_state_path()
        if latest is None:
            return MonitorState(monitor_id=self.monitor_id)
        return MonitorState.from_dict(_read_json(latest))

    def _latest_state_path(self) -> Optional[Path]:
        if not self.alerts_dir.is_dir():
            return None
        candidates = sorted(
            (p for p in self.alerts_dir.glob("state-r*.json")),
            key=lambda p: p.name,
        )
        return candidates[-1] if candidates else None

    def publish_tick(self, result: TickResult) -> Dict[str, Any]:
        """Persist one tick: ordered event log (new file), new state
        revision, atomic publish-index swap.  Raises MonitoringError with
        code E_DP06_DISK_OUTPUT_FAILED on write failure; the caller turns
        that into a DISK_OUTPUT_FAILED alert / exit code."""
        assert_within_root(self.root, self.alerts_dir)
        revision = self._next_revision()
        try:
            self.alerts_dir.mkdir(parents=True, exist_ok=True)
            events_path = self.alerts_dir / f"events-r{revision:06d}.jsonl"
            if events_path.exists():
                raise MonitoringError("E_DP06_DISK_OUTPUT_FAILED", f"event log revision collision {events_path.name}")
            lines = [json.dumps(event.to_dict(), sort_keys=True, ensure_ascii=False) for event in result.events]
            events_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

            state_path = self.alerts_dir / f"state-r{revision:06d}.json"
            if state_path.exists():
                raise MonitoringError("E_DP06_DISK_OUTPUT_FAILED", f"state revision collision {state_path.name}")
            state_path.write_text(
                json.dumps(result.state.to_dict(), sort_keys=True, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            index_payload = {
                "alert_events_log": str(events_path.relative_to(self.root)).replace("\\", "/"),
                "latest_state_path": str(state_path.relative_to(self.root)).replace("\\", "/"),
                "latest_state_sha256": hashlib.sha256(state_path.read_bytes()).hexdigest(),
                "monitor_id": self.monitor_id,
                "revision": revision,
                "schema_version": MONITOR_SCHEMA_VERSION,
                "tick_count": result.state.tick_count,
            }
            self._atomic_write_json(self.index_path, index_payload)
        except MonitoringError:
            raise
        except OSError as exc:
            raise MonitoringError("E_DP06_DISK_OUTPUT_FAILED", f"tick publication failed ({type(exc).__name__})") from None
        return index_payload

    def _atomic_write_json(self, path: Path, payload: Mapping[str, Any]) -> None:
        assert_within_root(self.root, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=1)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except OSError:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
