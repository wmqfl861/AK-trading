"""DP03 collection engine (work order H03.3-H03.10).

Single-owned worker isolation (H03.4): one collection worker thread + one
storage worker thread, both created and cancellable by the engine.  The SDK
session lives ONLY inside the collection worker; frames are deep-copied
there (:class:`SnapshotCopy`, ``detached=True`` enforced by the frozen
ports) and cross to the storage worker through a BOUNDED queue.  A full
queue pauses new tasks (backpressure evidence) - unconfirmed data is never
dropped (H03.3).

Observation chains (H03.6/Q02): every connection epoch records a
subscribe-ready baseline; first-sight and reconnect CLOSED bars are
SESSION_BACKFILL; an effective PROVISIONAL observation followed by a CLOSED
observation on the same connection_epoch/continuity_id with unbroken health
evidence passes the frozen deterministic REALTIME gate
(contracts.classify_acquisition_mode).  Disconnects, group switches and
unexplained no-data gaps reset continuity immediately.

Failure model (H03.5): transport errors reconnect with bounded backoff +
jitter and resubscribe; rate limits pause conservatively (2-strike stop);
permission denial circuit-breaks the run without touching accounts or
channels; failures never advance a success checkpoint.  Every task outcome
lands in the frozen TaskState taxonomy and the run terminal state is
DERIVED (contracts.derive_run_terminal_state) - exit 0 is unreachable while
anything failed, gapped, is unknown or unpublished (H03.10).

Daily bars (H03.8): a daily row is CLOSED only with a qualified calendar
session-end for that trading day; without calendar evidence the row stays
row_status UNKNOWN (never natural-day start+86400).
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import random
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .contracts import (
    BAR_VALUE_FIELDS,
    CANONICAL_SERIALIZER_VERSION,
    NANOSECOND,
    REQUEST_DATA_LENGTH_R1,
    AcquisitionMode,
    AvailabilityBasis,
    DataOrigin,
    FailureClass,
    FramePartition,
    IntervalNano,
    QualityState,
    RealtimeGateInputs,
    RealtimeVerdict,
    RowObservation,
    RowStatus,
    RawSnapshotMetaV1,
    SnapshotEnvelope,
    SourceCoordinate,
    TaskState,
    canonical_bar_content,
    canonical_sha256,
    classify_acquisition_mode,
    derive_run_terminal_state,
    exit_code_for_run,
    make_bar_key,
    normalize_decimal_string,
    realtime_verdict,
    sanitize_status_reason,
    validate_bar_values,
    validate_duration_seconds,
)
from .policy import PipelinePolicy, default_policy
from .ports import (
    CalendarAnswer,
    CalendarAnswerStatus,
    CalendarProvider,
    CommitOutcome,
    CommitReceipt,
    FencingToken,
    Lease,
    PipelineStore,
    RunRegistry,
    SerialRef,
    SnapshotCopy,
    StatusEvent,
    StatusSink,
    SubscriptionKey,
    TaskRegistry,
)
from .scheduler import (
    BACKOFF_LADDER_SECONDS,
    BudgetExhausted,
    BudgetLedger,
    FairRotatingScheduler,
    RATE_LIMIT_STOP_THRESHOLD,
)
from .session_factory import (
    PermissionDeniedError,
    RateLimitError,
    SessionError,
    SymbolUnsupportedError,
)

__all__ = [
    "FrameStats",
    "SnapshotClassifier",
    "ObservationChain",
    "QueueItem",
    "RunSummary",
    "CollectionEngine",
    "serialize_raw_rows",
]

_SOURCE_BUNDLE_FALLBACK: Dict[str, Any] = {
    "source_id": "tqsdk-free-realtime-get_kline-serial",
    "network_channel": "TQSDK_FREE_REALTIME_GET_KLINE_SERIAL",
    "method": "get_kline_serial(symbol, duration_seconds, data_length=8192) advanced by wait_update(deadline=...)",
    "request_data_length": REQUEST_DATA_LENGTH_R1,
}

#: bounded store-delegation unwrap (X-08-1): the physical store owning the
#: pinned acquisition module may sit behind diagnostic/retry wrappers.
_STORE_DELEGATION_MAX = 4


# ---------------------------------------------------------------------------
# Raw serialization (traceable; NaN -> null so canonical JSON stays finite)
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


def serialize_raw_rows(rows: Sequence[Mapping[str, Any]]) -> bytes:
    payload = [
        {column: _jsonable(row.get(column)) for column in ("datetime",) + BAR_VALUE_FIELDS}
        for row in rows
    ]
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Row classification (H03.7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameStats:
    frame_rows: int
    padding_rows: int
    invalid_rows: int
    valid_rows: int
    closed_rows: int
    provisional_rows: int
    unknown_rows: int
    first_valid_nano: Optional[int]
    last_valid_nano: Optional[int]
    duplicate_same_value_rows: int
    conflicting_rows: int
    out_of_order_rows: int = 0
    invalid_reasons: Tuple[str, ...] = ()


class SnapshotClassifier:
    """Classify copied SDK rows into the frozen frame partition (Q01/Q02).

    - NaN datetime with all-NaN values -> PADDING (SDK padding head);
    - missing/lossy datetime, NaN values, broken OHLC, negative quantities
      -> INVALID with per-row evidence reasons;
    - unclosed bar (event_end > observed_at) -> PROVISIONAL;
    - daily bars need a qualified calendar session end to be CLOSED,
      otherwise they stay UNKNOWN (H03.8);
    - same-key duplicates: same content counted idempotent, different content
      flagged CONFLICT (evidence only - storage arbitrates).
    """

    @staticmethod
    def _datetime_nano(row: Mapping[str, Any], reasons: List[str]) -> Optional[int]:
        value = row.get("datetime")
        if isinstance(value, bool) or value is None:
            reasons.append("datetime-missing")
            return None
        if isinstance(value, float):
            if value != value:
                return None  # NaN: padding or invalid, decided by caller
            if not value.is_integer():
                reasons.append("datetime-sub-nanosecond")
                return None
            # Traceable conversion of a post-SDK value (never a precision
            # claim); the raw float is preserved in the raw frame evidence.
            return int(value)
        if isinstance(value, int):
            return value
        reasons.append("datetime-type")
        return None

    @classmethod
    def classify(
        cls,
        *,
        coordinate: SourceCoordinate,
        rows: Sequence[Mapping[str, Any]],
        observed_at_nano: int,
        daily_session_end_nano: Optional[int] = None,
    ) -> Tuple[List[RowObservation], FrameStats]:
        duration = validate_duration_seconds(coordinate.duration_seconds)
        observations: List[RowObservation] = []
        reasons_collector: List[str] = []
        seen_keys: Dict[int, str] = {}
        duplicate_same = 0
        conflicts = 0
        out_of_order = 0
        prev_dt: Optional[int] = None
        first_valid: Optional[int] = None
        last_valid: Optional[int] = None

        def _partition_row(
            ordinal: int, partition: FramePartition, reasons: Sequence[str]
        ) -> RowObservation:
            reasons_collector.extend(f"{ordinal}:{r}" for r in reasons)
            return RowObservation(
                raw_row_ordinal=ordinal,
                frame_partition=partition,
                row_status=None,
                data_origin=DataOrigin.SYNTHETIC,  # engine overrides per session
                acquisition_mode=AcquisitionMode.SESSION_BACKFILL,
                observed_at_nano=observed_at_nano,
                first_observed_at_nano=observed_at_nano,
                connection_epoch=0,  # engine overrides
                continuity_id="pending",  # engine overrides
                update_seq=0,  # engine overrides
                key=None,
                content_sha256=None,
                event_start_nano=None,
                event_end_nano=None,
                availability_basis=AvailabilityBasis.UNKNOWN,
            )

        for ordinal, row in enumerate(rows):
            reasons: List[str] = []
            dt_nano = cls._datetime_nano(row, reasons)
            values_raw = {name: row.get(name) for name in BAR_VALUE_FIELDS}
            nan_values = [v for v in values_raw.values() if isinstance(v, float) and v != v]

            if dt_nano is None and not reasons:
                if len(nan_values) == len(values_raw):
                    observations.append(_partition_row(ordinal, FramePartition.PADDING, ()))
                    continue
                reasons.append("datetime-nan-with-values")

            event_start: Optional[int] = None
            event_end: Optional[int] = None
            normalized: Optional[Tuple[str, ...]] = None
            if not reasons:
                event_start = dt_nano
                if duration == 86400:
                    event_end = (
                        int(daily_session_end_nano)
                        if daily_session_end_nano is not None
                        else event_start + duration * NANOSECOND  # provisional bound
                    )
                else:
                    event_end = event_start + duration * NANOSECOND
                try:
                    normalized = tuple(normalize_decimal_string(v) for v in values_raw.values())
                    validate_bar_values(values_raw)
                except Exception:
                    reasons.append("values-invalid")
            if not reasons and dt_nano is not None and dt_nano > observed_at_nano:
                # Q03: a bar whose interval has not begun cannot have been
                # observed; that is a row disposition, never event-time
                # backdating (E_OBSERVED_BEFORE_EVENT guarded upstream).
                reasons.append("observed-before-event-start")
            if reasons:
                observations.append(_partition_row(ordinal, FramePartition.INVALID, reasons))
                continue

            assert event_start is not None and event_end is not None and normalized is not None
            if prev_dt is not None and event_start < prev_dt:
                out_of_order += 1  # H03.7: 错序 evidence (kept, counted)
            prev_dt = event_start
            key = make_bar_key(
                coordinate.source_id, coordinate.source_symbol, duration, event_start
            )
            content_sha = canonical_bar_content(
                duration_seconds=duration,
                datetime_nano=event_start,
                bar_start_nano=event_start,
                bar_end_nano=event_end,
                trading_day=None,
                values={name: text for name, text in zip(BAR_VALUE_FIELDS, normalized)},
            )
            prior = seen_keys.get(event_start)
            duplicate_same_value = prior == content_sha
            if prior is None:
                seen_keys[event_start] = content_sha
            elif prior == content_sha:
                duplicate_same += 1
            else:
                conflicts += 1

            if event_end > observed_at_nano:
                row_status: Optional[RowStatus] = RowStatus.PROVISIONAL
            elif duration == 86400 and daily_session_end_nano is None:
                row_status = RowStatus.UNKNOWN
            else:
                row_status = RowStatus.CLOSED
            first_valid = event_start if first_valid is None else min(first_valid, event_start)
            last_valid = event_start if last_valid is None else max(last_valid, event_start)
            observations.append(
                RowObservation(
                    raw_row_ordinal=ordinal,
                    frame_partition=FramePartition.VALID,
                    row_status=row_status,
                    data_origin=DataOrigin.SYNTHETIC,  # engine overrides
                    acquisition_mode=AcquisitionMode.SESSION_BACKFILL,  # engine overrides
                    observed_at_nano=observed_at_nano,
                    first_observed_at_nano=observed_at_nano,
                    connection_epoch=0,
                    continuity_id="pending",
                    update_seq=0,
                    key=key,
                    content_sha256=content_sha,
                    event_start_nano=event_start,
                    event_end_nano=event_end,
                    availability_basis=AvailabilityBasis.UNKNOWN,
                    quality_state=(
                        QualityState.CONFLICT
                        if prior is not None and prior != content_sha
                        else QualityState.UNVERIFIED
                    ),
                )
            )

        valid = [o for o in observations if o.frame_partition is FramePartition.VALID]
        padding = sum(1 for o in observations if o.frame_partition is FramePartition.PADDING)
        invalid = sum(1 for o in observations if o.frame_partition is FramePartition.INVALID)
        closed = sum(1 for o in valid if o.row_status is RowStatus.CLOSED)
        provisional = sum(1 for o in valid if o.row_status is RowStatus.PROVISIONAL)
        unknown = sum(1 for o in valid if o.row_status is RowStatus.UNKNOWN)
        stats = FrameStats(
            frame_rows=len(observations),
            padding_rows=padding,
            invalid_rows=invalid,
            valid_rows=len(valid),
            closed_rows=closed,
            provisional_rows=provisional,
            unknown_rows=unknown,
            first_valid_nano=first_valid,
            last_valid_nano=last_valid,
            duplicate_same_value_rows=duplicate_same,
            conflicting_rows=conflicts,
            out_of_order_rows=out_of_order,
            invalid_reasons=tuple(reasons_collector),
        )
        return observations, stats


# ---------------------------------------------------------------------------
# Observation chain (H03.6 / Q02)
# ---------------------------------------------------------------------------


class ObservationChain:
    """Per-task observation memory enforcing the frozen REALTIME gate.

    One chain activation per (connection_epoch, group service).  A
    disconnect, group switch or unexplained health gap calls :meth:`reset`;
    bars formed before the reset can never inherit REALTIME from the new
    chain (their CLOSED sighting after reconnect is SESSION_BACKFILL).
    first_observed memory persists across resets within the run (Q03).
    """

    def __init__(self, task_key: str) -> None:
        self.task_key = task_key
        self.connection_epoch = 0
        self.continuity_id = ""
        self.healthy = False
        self.baseline_keys: set = set()
        self.baseline_recorded = False
        self.first_observed: Dict[int, int] = {}
        self.provisional_at: Dict[int, int] = {}
        self.closed_at: Dict[int, int] = {}
        self.reset_count = 0
        self.last_reset_reason = ""

    def activate(self, connection_epoch: int, baseline_keys: set) -> None:
        self.connection_epoch = connection_epoch
        self.continuity_id = uuid.uuid4().hex
        self.healthy = True
        self.baseline_keys = set(baseline_keys)
        self.baseline_recorded = True

    def reset(self, reason: str) -> None:
        """Continuity reset: disconnect / group switch / health gap (Q02)."""
        self.healthy = False
        self.continuity_id = uuid.uuid4().hex
        self.baseline_keys = set()
        self.baseline_recorded = False
        self.reset_count += 1
        self.last_reset_reason = sanitize_status_reason(reason)

    def observe(
        self,
        row: RowObservation,
        *,
        duration_seconds: int,
        daily_session_end_proven: Optional[bool],
    ) -> Tuple[AcquisitionMode, RealtimeVerdict, AvailabilityBasis]:
        key_nano = row.key.datetime_nano if row.key is not None else -1
        observed = row.observed_at_nano
        if key_nano not in self.first_observed:
            self.first_observed[key_nano] = observed
        if row.row_status is RowStatus.PROVISIONAL and key_nano not in self.provisional_at:
            self.provisional_at[key_nano] = observed
        if row.row_status is RowStatus.CLOSED and key_nano not in self.closed_at:
            self.closed_at[key_nano] = observed
        event_start = row.event_start_nano if row.event_start_nano is not None else key_nano
        event_end = (
            row.event_end_nano
            if row.event_end_nano is not None
            else key_nano + duration_seconds * NANOSECOND
        )
        inputs = RealtimeGateInputs(
            duration_seconds=duration_seconds,
            event_start_nano=max(event_start, 0),
            proven_event_end_nano=max(event_end, max(event_start, 0) + 1),
            in_baseline=key_nano in self.baseline_keys,
            provisional_observed_at=self.provisional_at.get(key_nano),
            closed_observed_at=self.closed_at.get(key_nano),
            same_epoch_and_continuity=self.healthy and self.baseline_recorded,
            health_continuity=self.healthy,
            daily_session_end_proven=daily_session_end_proven,
        )
        mode = classify_acquisition_mode(inputs)
        verdict = realtime_verdict(inputs)
        if mode is AcquisitionMode.REALTIME:
            basis = AvailabilityBasis.LIVE_CAPTURE_EVIDENCE
        elif row.row_status in (RowStatus.CLOSED, RowStatus.UNKNOWN):
            basis = AvailabilityBasis.RETROSPECTIVE_CAPTURE
        else:
            basis = AvailabilityBasis.UNKNOWN
        return mode, verdict, basis


# ---------------------------------------------------------------------------
# Queue item and run summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueItem:
    envelope: SnapshotEnvelope
    classified_rows: Tuple[RowObservation, ...]
    task_key: str
    attempt_id: str
    fence: Optional[FencingToken]
    raw_bytes: bytes
    #: per-raw-ordinal normalized bar value strings (7-tuple in
    #: BAR_VALUE_FIELDS order; None for non-VALID rows) - the exact strings
    #: the classifier folded into content_sha256, carried so the storage
    #: worker can rebuild acquisition-pinned ClassifiedRow objects without
    #: re-parsing raw evidence (X-08-1).
    bar_values: Optional[Tuple[Optional[Tuple[str, ...]], ...]] = None


@dataclass
class RunSummary:
    run_id: str
    task_states: Dict[str, str] = field(default_factory=dict)
    terminal_state: Optional[str] = None
    exit_code: Optional[int] = None
    snapshot_count: int = 0
    requested_length_total: int = 0
    returned_rows_total: int = 0
    reconnects: int = 0
    rate_limit_pauses: int = 0
    backpressure_events: int = 0
    permission_denied: bool = False
    worker_abandoned: bool = False
    stale_events: int = 0
    market_closed_events: int = 0
    outside_window_gap_nanos: int = 0
    pending_uncommitted: int = 0
    conflicts: int = 0
    duplicate_same_value_rows: int = 0
    notes: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_states": dict(self.task_states),
            "terminal_state": self.terminal_state,
            "exit_code": self.exit_code,
            "snapshot_count": self.snapshot_count,
            "requested_length_total": self.requested_length_total,
            "returned_rows_total": self.returned_rows_total,
            "reconnects": self.reconnects,
            "rate_limit_pauses": self.rate_limit_pauses,
            "backpressure_events": self.backpressure_events,
            "permission_denied": self.permission_denied,
            "worker_abandoned": self.worker_abandoned,
            "stale_events": self.stale_events,
            "market_closed_events": self.market_closed_events,
            "outside_window_gap_nanos": self.outside_window_gap_nanos,
            "pending_uncommitted": self.pending_uncommitted,
            "conflicts": self.conflicts,
            "duplicate_same_value_rows": self.duplicate_same_value_rows,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# The collection engine
# ---------------------------------------------------------------------------


class CollectionEngine:
    """Session-collecting engine: rotating groups -> detached snapshots ->
    bounded queue -> storage worker -> derived run terminal state."""

    def __init__(
        self,
        *,
        worker_id: str,
        dataset: str,
        scheduler: FairRotatingScheduler,
        session_factory: Any,
        store: Optional[PipelineStore],
        status_sink: Optional[StatusSink],
        task_registry: Optional[TaskRegistry] = None,
        run_registry: Optional[RunRegistry] = None,
        calendar: Optional[CalendarProvider] = None,
        policy: Optional[PipelinePolicy] = None,
        run_root: Path,
        universe_sha256: str,
        source_bundle: Optional[Mapping[str, Any]] = None,
        clock_nano: Optional[Callable[[], int]] = None,
        clock_mono: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        rng: Optional[random.Random] = None,
        queue_bound: Optional[int] = None,
        run_deadline_seconds: Optional[float] = None,
        group_service_seconds: Optional[float] = None,
        stale_after_seconds: Optional[float] = None,
        store_retries: int = 2,
        finalize_wait_seconds: Optional[float] = None,
        lease_ttl_nano: int = 300 * NANOSECOND,
    ) -> None:
        self.worker_id = worker_id
        self.dataset = dataset
        self.scheduler = scheduler
        self.session_factory = session_factory
        self.store = store
        self.status_sink = status_sink
        self.task_registry = task_registry
        self.run_registry = run_registry
        self.calendar = calendar
        self.policy = policy or default_policy()
        self.run_root = Path(run_root)
        self._universe_sha256 = universe_sha256
        self._source_bundle = dict(source_bundle or _SOURCE_BUNDLE_FALLBACK)
        self._clock_nano = clock_nano or time.time_ns
        self._clock_mono = clock_mono or time.monotonic
        self._sleep = sleep or time.sleep
        self._rng = rng or random.Random(0)
        self._queue_bound = int(
            queue_bound if queue_bound is not None else self.policy.budget["uncommitted_raw_snapshots_max"]
        )
        self.budget = BudgetLedger(self.policy.budget, clock_mono=self._clock_mono)
        self._run_deadline_seconds = run_deadline_seconds
        self._group_service_seconds = group_service_seconds
        #: no-data staleness bound; None => frozen budget
        #: serial_ready_timeout_seconds (tests inject a smaller bound).
        self._stale_after = stale_after_seconds
        self._store_retries = int(store_retries)
        self._finalize_wait_seconds = finalize_wait_seconds
        self._lease_ttl_nano = int(lease_ttl_nano)

        self._validate_run_root()
        (self.run_root / "raw").mkdir(parents=True, exist_ok=True)
        (self.run_root / "spool" / self.worker_id).mkdir(parents=True, exist_ok=True)
        (self.run_root / "collector-events" / self.worker_id).mkdir(parents=True, exist_ok=True)
        (self.run_root / "heartbeats").mkdir(parents=True, exist_ok=True)

        self._queue: "queue.Queue[QueueItem]" = queue.Queue(maxsize=max(self._queue_bound, 1))
        self._chains: Dict[str, ObservationChain] = {}
        self._leases: Dict[str, Lease] = {}
        self._session: Any = None
        self._cancel = threading.Event()
        self._worker_done = threading.Event()
        self._storage_done = threading.Event()
        self._storage_thread: Optional[threading.Thread] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._summary = RunSummary(run_id="")
        self._run_id = ""
        self._attempt_counter = 0
        self._rate_limit_strikes = 0
        self._paused_until_mono = 0.0
        self._committed_windows: Dict[str, Tuple[int, int]] = {}
        self._confirmed_commits: List[str] = []
        self._pending: List[QueueItem] = []
        self._last_calendar_sha: Optional[str] = None
        self._run_started = False
        #: X-08-1 pinned-acquisition resolution cache (see
        #: _store_acquisition_module); storage-worker thread only.
        self._store_acquisition: Optional[Any] = None
        self._store_acquisition_resolved = False

    # -- run-root validation (V12 aspect: refuse BEFORE filesystem use) ----

    def _validate_run_root(self) -> None:
        from .policy import FORBIDDEN_FACE_MARKERS, validate_output_root

        if not self.run_root.is_absolute():
            raise SessionError("run root must be an explicitly injected absolute path")
        text = str(self.run_root)
        validate_output_root(text)
        for part in [p for p in text.replace("\\", "/").split("/") if p]:
            for marker in FORBIDDEN_FACE_MARKERS:
                if marker.lower() in part.lower():
                    raise SessionError(
                        f"run root touches forbidden face marker {marker!r}"
                    )

    # -- public API ------------------------------------------------------------

    def run(
        self, run_id: Optional[str] = None, *, stop_event: Optional[threading.Event] = None
    ) -> RunSummary:
        if self._run_started:
            raise SessionError("engine already ran (single-run engine)")
        self._run_started = True
        self._run_id = run_id or f"dp03-{uuid.uuid4().hex[:12]}"
        self._summary = RunSummary(run_id=self._run_id)
        external_stop = stop_event or threading.Event()

        if self.run_registry is not None:
            registry_run_id = self.run_registry.start_run(
                policy_sha256=canonical_sha256(self.policy.raw_document),
                universe_sha256=self._universe_sha256,
                baseline_manifest_sha256="",
                mode=(
                    "continuous" if self.scheduler.continuous_eligible() else "rotating_incremental"
                ),
                generation_evidence_sha256=None,
            )
            # X-08-2: the registry MINTS the authoritative run identity; the
            # engine ADOPTS the returned id so status events, attempt ids,
            # heartbeats and the final transition all carry ONE id (the old
            # code discarded it and finalized under its own id, which the
            # registry refuses as unknown).
            if isinstance(registry_run_id, str) and registry_run_id:
                self._run_id = registry_run_id
                self._summary = RunSummary(run_id=self._run_id)
        self._emit("", "RUN_EVENT", "run started (SYNTHETIC/offline default)")

        # H03.4: single owned collection worker with a TOTAL deadline; both
        # workers are daemon threads owned by this engine only.
        self._storage_thread = threading.Thread(
            target=self._storage_worker,
            name=f"dp03-storage-{self.worker_id}",
            daemon=True,
        )
        self._storage_thread.start()
        self._worker_thread = threading.Thread(
            target=self._collection_worker,
            args=(external_stop,),
            name=f"dp03-collect-{self.worker_id}",
            daemon=True,
        )
        self._worker_thread.start()

        total_deadline = max(self._total_deadline_seconds(), 0.1)
        if not self._worker_done.wait(timeout=total_deadline):
            self._cancel.set()
            self._summary.worker_abandoned = True
            self._summary.notes.append(
                "collection worker did not finish inside the total deadline; "
                "cancelled and abandoned - engine-owned threads only, no other "
                "process affected (H03.4)"
            )
            self._worker_thread.join(timeout=5.0)
        else:
            self._cancel.set()
        self._finalize()
        return self._summary

    def stop(self) -> None:
        """Graceful stop request (H03.9): no new tasks; drain and persist."""
        self._cancel.set()

    # -- deadlines ---------------------------------------------------------------

    def _total_deadline_seconds(self) -> float:
        if self._run_deadline_seconds is not None:
            return float(self._run_deadline_seconds)
        return (
            self.policy.budget["run_wall_seconds_max"]
            - self.policy.budget["safe_finalize_reserve_seconds"]
        )

    def _group_deadline_seconds(self) -> float:
        base = (
            self._group_service_seconds
            if self._group_service_seconds is not None
            else self.policy.budget["serial_ready_timeout_seconds"] + 30.0
        )
        return max(min(base, self.budget.wall_remaining()), 0.0)

    def _finalize_wait(self) -> float:
        if self._finalize_wait_seconds is not None:
            return float(self._finalize_wait_seconds)
        return min(self.policy.budget["safe_finalize_reserve_seconds"], 30.0)

    def _stale_after_seconds(self) -> float:
        if self._stale_after is not None:
            return float(self._stale_after)
        return float(self.policy.budget["serial_ready_timeout_seconds"])

    # -- collection worker --------------------------------------------------------

    def _collection_worker(self, external_stop: threading.Event) -> None:
        try:
            while not self._cancel.is_set() and not external_stop.is_set():
                if self.budget.wall_remaining() <= 0 or self.budget.stopped:
                    self._summary.notes.append("budget stopped; ending collection")
                    break
                if self._paused_until_mono > self._clock_mono():
                    self._sleep(0.01)
                    continue
                if self._queue.full():
                    # H03.3 backpressure: pause NEW tasks; never drop data.
                    self._summary.backpressure_events += 1
                    self._emit(
                        "", "ALERT_EVENT", "bounded queue full; pausing new tasks (backpressure)"
                    )
                    self._sleep(0.01)
                    continue
                group = self.scheduler.next_group()
                if group is None:
                    if self.scheduler.has_pending_retries(self._clock_nano()):
                        # Retry-waiting tasks are not done: hold the worker
                        # until eligibility or budget end (no false finish).
                        self._sleep(0.01)
                        continue
                    break
                self.scheduler.record_group_started(group)
                self._serve_group(group)
                self._heartbeat()
        except PermissionDeniedError as exc:
            self._permission_circuit_breaker(exc)
        except Exception as exc:  # engine-local failure: surfaced, never swallowed
            self._summary.notes.append(f"collection worker error: {type(exc).__name__}")
            self._emit("", "ALERT_EVENT", f"collection worker failed: {type(exc).__name__}")
        finally:
            self._worker_done.set()

    def _serve_group(self, group: Any) -> None:
        task_keys = list(group.task_keys)
        session = self._open_session()
        if session is None:
            return
        serials: Dict[str, SerialRef] = {}
        group_deadline_mono = self._clock_mono() + self._group_deadline_seconds()

        for key in group.keys:
            task_key = self._task_key_of(key)
            try:
                self.budget.charge_serial_attempt()
                self.scheduler.record_task_started(task_key)
                self._record_attempt(task_key)
                serials[task_key] = session.subscribe(key)
            except SymbolUnsupportedError as exc:
                self.scheduler.record_task_result(
                    task_key, TaskState.UNSUPPORTED, failure_class=FailureClass.SYMBOL_UNSUPPORTED
                )
                self._emit(task_key, "TASK_EVENT", f"symbol unsupported: {exc.reason_sanitized}")
            except PermissionDeniedError:
                self.scheduler.record_task_result(
                    task_key, TaskState.PERMISSION_DENIED, failure_class=FailureClass.ACCOUNT_DENIED
                )
                raise
            except RateLimitError as exc:
                self._observe_rate_limit(exc)
                self.scheduler.record_task_result(
                    task_key, TaskState.RETRYABLE_FAILURE, failure_class=FailureClass.RATE_LIMITED
                )
            except (SessionError, BudgetExhausted) as exc:
                self.scheduler.record_task_result(
                    task_key,
                    TaskState.RETRYABLE_FAILURE,
                    failure_class=getattr(exc, "failure_class", FailureClass.BUDGET),
                )
                self._emit(task_key, "TASK_EVENT", "subscribe failed; retry scheduled")

        # H03.2/H03.6: switching group resets continuity for these tasks.
        for task_key in task_keys:
            self._chain(task_key).reset("group switch")

        # -- readiness window (bounded; permanent not-ready cannot block) ----
        ready: Dict[str, SerialRef] = {}
        ready_deadline_mono = min(
            self._clock_mono() + self.policy.budget["serial_ready_timeout_seconds"],
            group_deadline_mono,
        )
        while (
            self._clock_mono() < ready_deadline_mono
            and len(ready) < len(serials)
            and not self._cancel.is_set()
        ):
            if self._paused_until_mono > self._clock_mono():
                break
            try:
                got = session.wait_update(
                    min(self.policy.budget["wait_update_deadline_seconds_max"], 1.0)
                )
            except RateLimitError as exc:
                self._observe_rate_limit(exc)
                continue
            except SessionError as exc:
                self._handle_transport_error(session, task_keys, exc)
                return
            # Readiness is checked on EVERY pass, including wait-deadline
            # returns (False = deadline, not disconnect - H03.3): a serial
            # that is already ready must not wait for a further update.
            for task_key, serial in serials.items():
                if task_key in ready:
                    continue
                try:
                    if session.ready(serial):
                        ready[task_key] = serial
                except SessionError as exc:
                    self._handle_transport_error(session, [task_key], exc)
                    return
            if len(ready) == len(serials):
                break
        for task_key, serial in serials.items():
            if task_key not in ready:
                self.scheduler.record_task_result(
                    task_key,
                    TaskState.RETRYABLE_FAILURE,
                    failure_class=FailureClass.TRANSIENT,
                )
                self._emit(task_key, "TASK_EVENT", "serial not ready within bounded budget")
                self._record_failure(task_key, FailureClass.TRANSIENT, "serial not ready (bounded)")

        if not ready:
            self._close_session(session)
            return

        # -- baseline recording (Q02) ------------------------------------------
        session.record_baseline(f"baseline-{uuid.uuid4().hex[:12]}")
        for task_key, serial in ready.items():
            snapshot = session.copy_snapshot(serial)
            baseline_keys = {
                int(row["datetime"])
                for row in snapshot.rows
                if isinstance(row.get("datetime"), (int, float))
                and not isinstance(row.get("datetime"), bool)
                and float(row["datetime"]) == float(row["datetime"])
                and float(row["datetime"]).is_integer()
            }
            self._chain(task_key).activate(session.connection_epoch, baseline_keys)
            self._handle_snapshot(task_key, serial, snapshot, session)

        # -- update loop ---------------------------------------------------------
        last_data_mono = self._clock_mono()
        while not self._cancel.is_set() and self._clock_mono() < group_deadline_mono:
            if self.budget.wall_remaining() <= 0 or self._paused_until_mono > self._clock_mono():
                break
            try:
                got = session.wait_update(self.policy.budget["wait_update_deadline_seconds_max"])
            except RateLimitError as exc:
                self._observe_rate_limit(exc)
                continue
            except SessionError as exc:
                self._handle_transport_error(session, list(ready), exc)
                return
            if got:
                last_data_mono = self._clock_mono()
                for task_key, serial in ready.items():
                    try:
                        if session.ready(serial):
                            self._handle_snapshot(
                                task_key, serial, session.copy_snapshot(serial), session
                            )
                    except SessionError as exc:
                        self._handle_transport_error(session, list(ready), exc)
                        return
                continue
            # H03.3: False means the WAIT DEADLINE passed, not a disconnect.
            if self._clock_mono() - last_data_mono >= self._stale_after_seconds():
                self._classify_no_data(list(ready))
                break

        # H03.2: close the owned session at group switch; serials recycled.
        self._close_session(session)

    # -- session lifecycle ---------------------------------------------------------

    def _open_session(self) -> Any:
        """Open the single owned session (rate-limit + budget guarded)."""
        while not self._cancel.is_set():
            if self.budget.wall_remaining() <= 0:
                self._defer_remaining("wall budget exhausted before session open")
                return None
            try:
                self.budget.charge_connection_start()
            except BudgetExhausted:
                self._defer_remaining("connection start budget exhausted")
                return None
            try:
                session = self.session_factory.create()
            except PermissionDeniedError:
                raise
            except SessionError as exc:
                self._summary.reconnects += 1
                backoff = self._backoff_seconds()
                self._emit(
                    "",
                    "TASK_EVENT",
                    f"session create failed ({exc.reason_sanitized}); backoff {backoff:.1f}s",
                )
                self._sleep(backoff)
                continue
            if self._session is not None:
                raise SessionError(
                    "single-connection budget violated (active_tqapi_connections_max=1)"
                )
            self._session = session
            return session
        return None

    def _close_session(self, session: Any) -> None:
        try:
            session.close()
        except Exception:
            pass
        if self._session is session:
            self._session = None

    def _backoff_seconds(self) -> float:
        """Frozen ladder 60/300/900 + bounded jitter <= 25% (H03.5)."""
        index = min(max(self._summary.reconnects - 1, 0), len(BACKOFF_LADDER_SECONDS) - 1)
        base = float(BACKOFF_LADDER_SECONDS[index])
        return base + self._rng.uniform(0.0, base * 0.25)

    def _handle_transport_error(
        self, session: Any, task_keys: Sequence[str], exc: SessionError
    ) -> None:
        """Bounded backoff + jitter -> rebuild session -> resubscribe (H03.5)."""
        self._summary.reconnects += 1
        backoff = self._backoff_seconds()
        self._emit(
            "",
            "ALERT_EVENT",
            f"SESSION_DISCONNECTED: transport error; reconnect "
            f"#{self._summary.reconnects} after {backoff:.1f}s",
        )
        self._close_session(session)
        for task_key in task_keys:
            self._chain(task_key).reset("disconnect")
            if self.scheduler.task_progress(task_key).state not in (
                TaskState.ACCEPTED,
                TaskState.UNSUPPORTED,
            ):
                self.scheduler.record_task_result(
                    task_key,
                    TaskState.RETRYABLE_FAILURE,
                    failure_class=FailureClass.TRANSIENT,
                )
                self._record_failure(task_key, FailureClass.TRANSIENT, "transport error")
        self._sleep(backoff)
        # The next scheduler.next_group() retries (attempts already charged);
        # no checkpoint advanced by this failure (H03.5).

    def _observe_rate_limit(self, exc: RateLimitError) -> None:
        self._summary.rate_limit_pauses += 1
        self._rate_limit_strikes += 1
        pause = max(exc.retry_after_seconds, 60.0)
        self._paused_until_mono = max(self._paused_until_mono, self._clock_mono() + pause)
        self._emit("", "ALERT_EVENT", f"rate limited; conservative pause {pause:.0f}s")
        if self._rate_limit_strikes >= RATE_LIMIT_STOP_THRESHOLD:
            self._defer_remaining("consecutive account rate limits; stopping new requests")

    def _permission_circuit_breaker(self, exc: PermissionDeniedError) -> None:
        self._summary.permission_denied = True
        self._emit(
            "",
            "ALERT_EVENT",
            "PERMISSION_DENIED: circuit breaker engaged (no account/channel change)",
        )
        for task_key, state in self.scheduler.all_task_states().items():
            if state in (TaskState.PLANNED.value, TaskState.RUNNING.value):
                self.scheduler.record_task_result(
                    task_key, TaskState.PERMISSION_DENIED, failure_class=FailureClass.ACCOUNT_DENIED
                )
        self.budget.stop()

    def _defer_remaining(self, reason: str) -> None:
        for task_key, state in self.scheduler.all_task_states().items():
            if state in (TaskState.PLANNED.value, TaskState.RUNNING.value):
                self.scheduler.record_task_result(
                    task_key, TaskState.DEFERRED_BUDGET, failure_class=FailureClass.BUDGET
                )
        self._emit("", "RUN_EVENT", sanitize_status_reason(reason))
        self.budget.stop()

    # -- snapshot handling ------------------------------------------------------

    def _handle_snapshot(
        self, task_key: str, serial: SerialRef, snapshot: SnapshotCopy, session: Any
    ) -> None:
        coordinate = serial.coordinate
        observed_at = self._clock_nano()
        duration = coordinate.duration_seconds
        calendar_sha: Optional[str] = None
        daily_session_end: Optional[int] = None
        daily_proven: Optional[bool] = None
        if duration == 86400:
            answer = self._calendar_answer(coordinate.source_symbol, duration, observed_at)
            if answer is not None:
                calendar_sha = answer.evidence_sha256
                if answer.sessions:
                    daily_session_end = answer.sessions[-1][1].end_nano
                    daily_proven = True
                elif answer.status is CalendarAnswerStatus.NO_TRADING_EVIDENCED:
                    daily_proven = False
        rows, stats = SnapshotClassifier.classify(
            coordinate=coordinate,
            rows=snapshot.rows,
            observed_at_nano=observed_at,
            daily_session_end_nano=daily_session_end,
        )
        data_origin = (
            DataOrigin.SYNTHETIC if getattr(session, "is_synthetic", False) else DataOrigin.REAL_SOURCE
        )
        chain = self._chain(task_key)
        classified: List[RowObservation] = []
        for row in rows:
            mode, verdict, basis = chain.observe(
                row,
                duration_seconds=duration,
                daily_session_end_proven=daily_proven if duration == 86400 else None,
            )
            object.__setattr__(row, "acquisition_mode", mode)
            object.__setattr__(row, "evidence_class", verdict.value)
            object.__setattr__(row, "availability_basis", basis)
            object.__setattr__(row, "data_origin", data_origin)
            object.__setattr__(row, "connection_epoch", session.connection_epoch)
            object.__setattr__(row, "continuity_id", chain.continuity_id or "reset")
            object.__setattr__(row, "update_seq", snapshot.copied_at_update_seq)
            key_nano = row.key.datetime_nano if row.key is not None else -1
            object.__setattr__(
                row,
                "first_observed_at_nano",
                chain.first_observed.get(key_nano, row.observed_at_nano),
            )
            classified.append(row)

        raw_bytes = serialize_raw_rows(snapshot.rows)
        raw_sha = hashlib.sha256(raw_bytes).hexdigest()
        self._attempt_counter += 1
        attempt_id = f"{self._run_id}-{self._attempt_counter:04d}"
        raw_dir = self.run_root / "raw" / attempt_id
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "frame.jsonl").write_bytes(raw_bytes)

        meta = RawSnapshotMetaV1(
            task_id=task_key,
            attempt_id=attempt_id,
            source_symbol=coordinate.source_symbol,
            duration_seconds=duration,
            requested_length=REQUEST_DATA_LENGTH_R1,
            returned_length=stats.frame_rows,
            frame_rows=stats.frame_rows,
            padding_rows=stats.padding_rows,
            invalid_rows=stats.invalid_rows,
            valid_rows=stats.valid_rows,
            observed_at_nano=observed_at,
            sdk_version=getattr(session, "sdk_version", "unknown"),
            serializer_version=CANONICAL_SERIALIZER_VERSION,
            raw_path=str(raw_dir / "frame.jsonl"),
            raw_sha256=raw_sha,
            raw_bytes=len(raw_bytes),
            first_valid_nano=stats.first_valid_nano,
            last_valid_nano=stats.last_valid_nano,
        )
        envelope = SnapshotEnvelope(
            snapshot_id=f"snap-{uuid.uuid4().hex[:12]}",
            task_id=task_key,
            attempt_id=attempt_id,
            coordinate=coordinate,
            connection_baseline=(
                session.baseline
                if session.baseline is not None
                else session.record_baseline(f"baseline-late-{uuid.uuid4().hex[:8]}")
            ),
            meta=meta,
            rows=tuple(classified),
            raw_bytes_sha256=raw_sha,
            policy_sha256=canonical_sha256(self.policy.raw_document),
            source_bundle_sha256=canonical_sha256(self._source_bundle),
            calendar_evidence_sha256=calendar_sha,
            generation_evidence_sha256=None,
            universe_sha256=self._universe_sha256,
            data_origin=data_origin,
            synthetic_marker=(
                "SYNTHETIC-DP03-OFFLINE-RUN" if data_origin is DataOrigin.SYNTHETIC else None
            ),
        )
        (raw_dir / "envelope.json").write_bytes(
            json.dumps(
                envelope.frozen_content(), sort_keys=True, ensure_ascii=False, indent=1
            ).encode("utf-8")
        )

        # Requested vs returned counted independently (H03.4/D02).
        self._summary.snapshot_count += 1
        self._summary.requested_length_total += REQUEST_DATA_LENGTH_R1
        self._summary.returned_rows_total += stats.frame_rows
        self._summary.conflicts += stats.conflicting_rows
        self._summary.duplicate_same_value_rows += stats.duplicate_same_value_rows
        self.scheduler.record_window(task_key, stats.first_valid_nano, stats.last_valid_nano)

        # H03.6: gap evidence for the part of the requested window that slid
        # below the earliest row the free channel has ever returned.
        requested_from = observed_at - REQUEST_DATA_LENGTH_R1 * duration * NANOSECOND
        gap_nanos = self.scheduler.outside_free_window(task_key, requested_from)
        if gap_nanos:
            self._summary.outside_window_gap_nanos += gap_nanos
            self._emit(
                task_key,
                "TASK_EVENT",
                f"OUTSIDE_FREE_WINDOW: {gap_nanos} ns of requested window below earliest returned row",
            )

        # X-08-1: carry the classifier's exact normalized value strings per
        # raw ordinal (None for non-VALID rows) so the storage worker can
        # rebuild the store's pinned acquisition ClassifiedRow shape without
        # re-parsing raw evidence.  VALID rows are exactly the rows whose
        # values the classifier already normalized successfully, so the same
        # pure conversion is deterministic here.
        values_by_ordinal: List[Optional[Tuple[str, ...]]] = [None] * len(snapshot.rows)
        for observation in classified:
            if observation.frame_partition is not FramePartition.VALID:
                continue
            raw_row = snapshot.rows[observation.raw_row_ordinal]
            values_by_ordinal[observation.raw_row_ordinal] = tuple(
                normalize_decimal_string(raw_row.get(name)) for name in BAR_VALUE_FIELDS
            )

        item = QueueItem(
            envelope=envelope,
            classified_rows=tuple(classified),
            task_key=task_key,
            attempt_id=attempt_id,
            fence=self._fence_for(task_key),
            raw_bytes=raw_bytes,
            bar_values=tuple(values_by_ordinal),
        )
        try:
            self.budget.charge_uncommitted_snapshot()
            self._queue.put(item, timeout=0.05)
        except queue.Full:
            self.budget.release_uncommitted_snapshot()
            self._summary.backpressure_events += 1
            self._emit(
                task_key,
                "ALERT_EVENT",
                "queue full at enqueue; snapshot held pending (never dropped)",
            )
            self._pending.append(item)
        except BudgetExhausted:
            self._pending.append(item)
        if self.scheduler.task_progress(task_key).state is not TaskState.UNSUPPORTED:
            self.scheduler.record_task_result(task_key, TaskState.RAW_VERIFIED)

    # -- no-data / stale classification (H03.3) ------------------------------------

    def _calendar_answer(
        self, source_symbol: str, duration: int, now_nano: int
    ) -> Optional[CalendarAnswer]:
        if self.calendar is None:
            return None
        try:
            answer = self.calendar.sessions(
                dataset=self.dataset,
                source_symbol=source_symbol,
                duration_seconds=duration,
                window=IntervalNano(
                    start_nano=max(now_nano - 2 * duration * NANOSECOND, 0),
                    end_nano=now_nano + duration * NANOSECOND,
                ),
                as_of_nano=now_nano,
            )
        except Exception:
            return None
        self._last_calendar_sha = answer.evidence_sha256
        return answer

    def _classify_no_data(self, task_keys: Sequence[str]) -> None:
        now_nano = self._clock_nano()
        for task_key in task_keys:
            progress = self.scheduler.task_progress(task_key)
            answer = self._calendar_answer(progress.source_symbol, progress.duration_seconds, now_nano)
            if answer is None or answer.status in (
                CalendarAnswerStatus.UNKNOWN,
                CalendarAnswerStatus.CONFLICT,
            ):
                if progress.window_last_nano is None:
                    self.scheduler.record_task_result(
                        task_key,
                        TaskState.SOURCE_EMPTY_UNEXPLAINED,
                        failure_class=FailureClass.EMPTY_UNEXPLAINED,
                    )
                    self._emit(
                        task_key,
                        "TASK_EVENT",
                        "SOURCE_EMPTY_UNEXPLAINED: no data and calendar UNKNOWN",
                    )
                else:
                    self._summary.stale_events += 1
                    self._emit(
                        task_key, "ALERT_EVENT", "stale data with UNKNOWN calendar; conservatively stale"
                    )
            elif answer.status is CalendarAnswerStatus.NO_TRADING_EVIDENCED:
                self._summary.market_closed_events += 1
                self._emit(
                    task_key,
                    "TASK_EVENT",
                    "no trading evidenced by calendar; market closed (not a failure)",
                )
            else:
                self._summary.stale_events += 1
                self._emit(task_key, "ALERT_EVENT", "stale data inside a verified session window")

    # -- storage worker ----------------------------------------------------------

    def _storage_worker(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.02)
            except queue.Empty:
                if self._worker_done.is_set() or self._cancel.is_set():
                    break
                continue
            self._commit_item(item)
        # Drain anything still queued at shutdown.
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            self._commit_item(item)
        self._storage_done.set()

    def _commit_item(self, item: QueueItem) -> None:
        if self.store is None:
            self.budget.release_uncommitted_snapshot()
            return
        last_error: Optional[BaseException] = None
        for _retry in range(self._store_retries + 1):
            try:
                receipt = self.store.commit(
                    item.envelope, self._rows_for_store(item), item.fence
                )
                self._apply_receipt(item, receipt)
                self.budget.release_uncommitted_snapshot()
                return
            except Exception as exc:
                last_error = exc
                self._sleep(0.01)
        # H03.3/H03.9: never drop unconfirmed data; persist and surface.
        self._pending.append(item)
        self._emit(
            item.task_key,
            "ALERT_EVENT",
            "COMMIT_FAILED: store commit failed after retries ("
            f"{type(last_error).__name__ if last_error else 'unknown'}); "
            "snapshot persisted pending; checkpoint NOT advanced",
        )

    # -- store row-shape reconciliation (X-08-1) -------------------------------

    def _store_acquisition_module(self) -> Optional[Any]:
        """Resolve the pinned acquisition module the wired store commits under.

        DP02's SqlPipelineStore exposes the DP00-pinned acquisition module it
        was constructed with as ``store.acquisition``; wrappers around it are
        followed through ``.inner`` (bounded, ``_STORE_DELEGATION_MAX``
        levels).  The result is cached for the run (single storage worker
        thread).  ``None`` means the store makes no pinned-shape declaration
        - the DP01 port types ``classified_rows`` as ``Sequence[Any]`` and
        such a store keeps receiving the port-native DP01 RowObservation
        rows verbatim; no shape is ever guessed.
        """
        if self._store_acquisition_resolved:
            return self._store_acquisition
        candidate: Any = self.store
        for _ in range(_STORE_DELEGATION_MAX):
            if candidate is None:
                break
            module = getattr(candidate, "acquisition", None)
            if module is not None and hasattr(module, "ClassifiedRow"):
                self._store_acquisition = module
                self._store_acquisition_resolved = True
                return module
            candidate = getattr(candidate, "inner", None)
        self._store_acquisition = None
        self._store_acquisition_resolved = True
        return None

    def _rows_for_store(self, item: QueueItem) -> Sequence[Any]:
        """Rows in the shape the wired store declares (X-08-1 reconciliation).

        With a resolvable pinned acquisition module the engine's DP01
        RowObservation rows are converted into that module's ClassifiedRow
        objects - the documented, frozen r1 input shape of
        SqlPipelineStore.commit - using only facts already frozen on the
        observation rows plus the classifier's own normalized value strings
        (carried on the queue item).  trading_day stays None/none: the r1
        classifier derives no trading day and none is fabricated here.
        """
        module = self._store_acquisition_module()
        if module is None or item.bar_values is None:
            return item.classified_rows
        coordinate = item.envelope.coordinate
        converted = []
        for observation, values in zip(item.classified_rows, item.bar_values):
            converted.append(
                module.ClassifiedRow(
                    raw_row_ordinal=observation.raw_row_ordinal,
                    datetime_nano=(
                        observation.key.datetime_nano if observation.key is not None else None
                    ),
                    source_symbol=coordinate.source_symbol,
                    duration_seconds=coordinate.duration_seconds,
                    bar_start_nano=observation.event_start_nano,
                    bar_end_nano=observation.event_end_nano,
                    trading_day=None,
                    trading_day_source="none",
                    frame_partition=module.FramePartition(observation.frame_partition.value),
                    valid_status=(
                        module.ValidRowStatus(observation.row_status.value)
                        if observation.row_status is not None
                        else None
                    ),
                    values=dict(zip(BAR_VALUE_FIELDS, values)) if values is not None else {},
                    content_sha256=observation.content_sha256,
                    evidence_version=CANONICAL_SERIALIZER_VERSION,
                    reason=None,
                )
            )
        return tuple(converted)

    def _apply_receipt(self, item: QueueItem, receipt: CommitReceipt) -> None:
        task_key = item.task_key
        outcome = CommitOutcome(receipt.outcome)  # str-enum tolerant
        if outcome in (CommitOutcome.REJECTED_FENCE, CommitOutcome.REJECTED_INVALID):
            self.scheduler.record_task_result(
                task_key, TaskState.RETRYABLE_FAILURE, failure_class=FailureClass.LOCAL_ERROR
            )
            self._emit(task_key, "TASK_EVENT", f"commit rejected ({outcome.value})")
            return
        if outcome is CommitOutcome.CONFLICT:
            self._summary.conflicts += 1
            self.scheduler.record_task_result(
                task_key, TaskState.QUARANTINED, failure_class=FailureClass.EMPTY_UNEXPLAINED
            )
            self._emit(
                task_key,
                "ALERT_EVENT",
                "conflicting CLOSED content for same K; both versions kept, "
                "default consumption blocked",
            )
            return
        if receipt.raw_verified and receipt.chunk_written and receipt.reconciled:
            if item.envelope.meta.first_valid_nano is None:
                # F-HARD-01 (DP-HARD-r1) commit-decision determinism: a
                # snapshot with ZERO valid rows evidences no data coverage.
                # Its clean commit (raw evidence + empty chunk + reconcile)
                # must never produce the green task state ACCEPTED, whatever
                # the worker-vs-storage thread ordering is - the task
                # terminal state stays with the no-data classification
                # (_classify_no_data: SOURCE_EMPTY_UNEXPLAINED / market
                # closed / stale alert) or the non-green RAW_VERIFIED hold
                # (H01.5: unexplained-empty inputs can never reach a green
                # terminal state or exit 0).
                self._emit(
                    task_key,
                    "TASK_EVENT",
                    "empty snapshot committed (zero valid rows); "
                    "ACCEPTED withheld - no data coverage evidenced (H01.5)",
                )
                return
            self.scheduler.record_task_result(task_key, TaskState.ACCEPTED)
            self._confirmed_commits.append(item.attempt_id)
            first_nano = item.envelope.meta.first_valid_nano
            last_nano = item.envelope.meta.last_valid_nano
            if first_nano is not None and last_nano is not None:
                window = self._committed_windows.get(task_key)
                self._committed_windows[task_key] = (
                    (min(window[0], first_nano), max(window[1], last_nano))
                    if window
                    else (first_nano, last_nano)
                )
            self._emit(task_key, "TASK_EVENT", f"commit accepted ({outcome.value})")
        else:
            self.scheduler.record_task_result(task_key, TaskState.INGEST_VERIFIED)
            self._emit(
                task_key, "TASK_EVENT", "commit stored but unverified; no consumable checkpoint"
            )

    # -- finalize ----------------------------------------------------------------

    def _finalize(self) -> None:
        # Drain the queue within the bounded finalize window (H03.9).
        finalize_deadline = self._clock_mono() + self._finalize_wait()
        while self._storage_thread is not None and self._storage_thread.is_alive():
            if self._clock_mono() > finalize_deadline:
                self._summary.notes.append("storage worker finalize deadline hit; pending persisted")
                break
            self._sleep(0.005)
        if self._storage_thread is not None:
            self._storage_thread.join(timeout=1.0)

        # Persist pending (unconfirmed) items - never dropped (H03.9).
        if self._pending:
            pending_path = self.run_root / "spool" / self.worker_id / "pending.jsonl"
            with pending_path.open("ab") as handle:
                for item in self._pending:
                    handle.write(
                        json.dumps(
                            {
                                "attempt_id": item.attempt_id,
                                "task_key": item.task_key,
                                "raw_sha256": item.envelope.raw_bytes_sha256,
                                "envelope_path": str(
                                    self.run_root / "raw" / item.attempt_id / "envelope.json"
                                ),
                            },
                            sort_keys=True,
                        ).encode("utf-8")
                        + b"\n"
                    )
        self._summary.pending_uncommitted = len(self._pending)

        if self._session is not None:
            self._close_session(self._session)
        self.scheduler.commit_pointer()
        task_states = self.scheduler.all_task_states()
        self._summary.task_states = dict(task_states)
        terminal = derive_run_terminal_state(
            task_states,
            unresolved_known_gaps=0,
            unknown_coverage_remaining=False,
            unpublished_batches=len(self._pending) + (1 if self._queue.qsize() else 0),
            nonterminal_planned_remaining=sum(
                1 for state in task_states.values() if state == TaskState.PLANNED.value
            ),
        )
        self._summary.terminal_state = terminal.value
        self._summary.exit_code = int(exit_code_for_run(terminal).value)
        if self.run_registry is not None:
            self.run_registry.finalize_run(self._run_id, task_states)
        summary_path = self.run_root / "spool" / self.worker_id / "run-summary.json"
        summary_path.write_text(
            json.dumps(self._summary.to_json(), sort_keys=True, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        self._emit("", "RUN_EVENT", f"run finalized: {terminal.value}")

    # -- helpers -------------------------------------------------------------------

    def _chain(self, task_key: str) -> ObservationChain:
        chain = self._chains.get(task_key)
        if chain is None:
            chain = ObservationChain(task_key)
            self._chains[task_key] = chain
        return chain

    @staticmethod
    def _task_key_of(key: SubscriptionKey) -> str:
        return (
            f"{key.coordinate.source_id}|{key.coordinate.source_symbol}|"
            f"{key.coordinate.duration_seconds}"
        )

    def _record_attempt(self, task_key: str) -> None:
        if self.task_registry is None:
            return
        lease = self.task_registry.acquire_lease(
            self.dataset, task_key, self.worker_id, self._lease_ttl_nano
        )
        self._leases[task_key] = lease
        self.task_registry.record_attempt(
            lease, f"{self._run_id}-lease-{uuid.uuid4().hex[:8]}", self._clock_nano()
        )

    def _record_failure(self, task_key: str, failure_class: FailureClass, reason: str) -> None:
        if self.task_registry is None:
            return
        lease = self._leases.get(task_key)
        if lease is not None:
            self.task_registry.record_failure(
                lease, "attempt", failure_class.value, sanitize_status_reason(reason)
            )

    def _fence_for(self, task_key: str) -> Optional[FencingToken]:
        lease = self._leases.get(task_key)
        if lease is None:
            return None
        return FencingToken(
            dataset=self.dataset,
            task_key=task_key,
            lease_id=lease.lease_id,
            fencing_epoch=lease.fencing_epoch,
        )

    def _heartbeat(self) -> None:
        # X-08-3(b): field name and run correlation follow the monitoring
        # input vocabulary (heartbeat_at_nano + run_id, integer nanoseconds
        # carried as an exact string); a fresh heartbeat must read as fresh.
        payload = {
            "worker_id": self.worker_id,
            "run_id": self._run_id,
            "heartbeat_at_nano": str(self._clock_nano()),
            "queue_depth": self._queue.qsize(),
            "pending": len(self._pending),
        }
        path = self.run_root / "heartbeats" / f"{self.worker_id}.json"
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".hb-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
            last_error: Optional[BaseException] = None
            for attempt in range(5):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError as exc:  # transient Windows replace race
                    last_error = exc
                    self._sleep(0.01 * (attempt + 1))
            else:
                if last_error is not None:
                    raise last_error
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if self.task_registry is not None and self._leases:
            for task_key, lease in list(self._leases.items()):
                try:
                    self._leases[task_key] = self.task_registry.heartbeat_lease(lease)
                except Exception:
                    self._leases.pop(task_key, None)

    def _emit(self, task_key: str, kind: str, reason: str) -> None:
        if self.status_sink is None:
            return
        self.status_sink.append(
            StatusEvent(
                event_id=f"evt-{uuid.uuid4().hex[:16]}",
                run_id=self._run_id,
                task_key=task_key or "run",
                attempt_id=None,
                occurred_at_nano=self._clock_nano(),
                reason_sanitized=sanitize_status_reason(reason),
                kind=kind,
            )
        )

    # -- introspection for tests/reports ----------------------------------------

    @property
    def committed_windows(self) -> Dict[str, Tuple[int, int]]:
        return dict(self._committed_windows)

    @property
    def confirmed_commits(self) -> Tuple[str, ...]:
        return tuple(self._confirmed_commits)
