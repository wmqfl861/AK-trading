"""DP03 SYNTHETIC test doubles for the DP01 frozen ports.

Everything here is SYNTHETIC and never touches network, database, SDK or
credentials.  The doubles implement the frozen Protocol surfaces
(PipelineStore / StatusSink / TaskRegistry / RunRegistry / CalendarProvider)
plus a hanging session factory for the H03.4 construction-hang vector and a
raw SDK-like api object (with forbidden trading methods attached) to prove
the facade never delegates them.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ak_trading.datapipeline.contracts import (
    NANOSECOND,
    DataOrigin,
    IntervalNano,
)
from ak_trading.datapipeline.ports import (
    CalendarAnswer,
    CalendarAnswerStatus,
    CommitOutcome,
    CommitReceipt,
    CommitOutcome as _CommitOutcome,
    Lease,
    LeaseGrant,
    RecoveryOutcome,
    RecoveryStatus,
    AvailableVersionView,
    RowCommitProvenance,
    StatusEvent,
)

SYNTHETIC_MARKER = "SYNTHETIC-DP03-TEST-DOUBLE"


class FakeStatusSink:
    def __init__(self) -> None:
        self.events: List[StatusEvent] = []
        self._lock = threading.Lock()

    def append(self, event: StatusEvent) -> None:
        with self._lock:
            self.events.append(event)

    def reasons(self) -> List[str]:
        with self._lock:
            return [e.reason_sanitized for e in self.events]

    def find(self, needle: str) -> List[str]:
        return [r for r in self.reasons() if needle in r]


class FakeRunRegistry:
    def __init__(self) -> None:
        self.runs: List[Dict[str, Any]] = []
        self.finalized: List[Tuple[str, Dict[str, str]]] = []

    def start_run(self, *, policy_sha256, universe_sha256, baseline_manifest_sha256, mode, generation_evidence_sha256) -> str:
        run_id = f"fake-run-{len(self.runs) + 1:03d}"
        self.runs.append(
            {
                "run_id": run_id,
                "policy_sha256": policy_sha256,
                "universe_sha256": universe_sha256,
                "mode": mode,
            }
        )
        return run_id

    def transition_run(self, run_id: str, note: str) -> None:
        return None

    def finalize_run(self, run_id: str, task_terminal_states, **flags) -> str:
        self.finalized.append((run_id, dict(task_terminal_states)))
        return "FINALIZED"


class FakeTaskRegistry:
    def __init__(self) -> None:
        self.leases: Dict[str, Lease] = {}
        self.attempts: List[str] = []
        self.failures: List[Tuple[str, str]] = []
        self.heartbeats: int = 0

    def acquire_lease(self, dataset: str, task_key: str, owner: str, ttl_nano: int) -> Lease:
        lease = Lease(
            dataset=dataset,
            task_key=task_key,
            lease_id=f"lease-{uuid.uuid4().hex[:8]}",
            owner=owner,
            fencing_epoch=1,
            granted=LeaseGrant.ACQUIRED,
            expires_at_nano=int(time.time_ns()) + int(ttl_nano),
        )
        self.leases[task_key] = lease
        return lease

    def heartbeat_lease(self, lease: Lease) -> Lease:
        self.heartbeats += 1
        return lease

    def record_attempt(self, lease: Lease, attempt_id: str, started_at_nano: int) -> None:
        self.attempts.append(lease.task_key)

    def record_failure(self, lease: Lease, attempt_id: str, failure_class: str, reason_sanitized: str) -> None:
        self.failures.append((lease.task_key, failure_class))


class FakeStore:
    """PipelineStore double with failure/latency/conflict scripting."""

    def __init__(
        self,
        *,
        fail_commits: int = 0,
        block_event: Optional[threading.Event] = None,
        conflict_tasks: Sequence[str] = (),
        unverified: bool = False,
        available: Optional[Dict[str, Tuple[int, int]]] = None,
    ) -> None:
        self.commits: List[Dict[str, Any]] = []
        self.captured: List[Tuple[Any, Tuple[Any, ...]]] = []
        self.commit_count = 0
        self._fail_commits = int(fail_commits)
        self._block_event = block_event
        self._conflict_tasks = set(conflict_tasks)
        self._unverified = unverified
        self._available = dict(available or {})
        self._content_seen: Dict[str, str] = {}

    def commit(self, envelope, classified_rows, fence) -> CommitReceipt:
        if self._block_event is not None:
            self._block_event.wait(timeout=30.0)
        self.commit_count += 1
        if self._fail_commits > 0:
            self._fail_commits -= 1
            raise RuntimeError("synthetic db failure (SYNTHETIC)")
        task_key = envelope.task_id
        # Idempotent content check (Q04): every VALID row's (K, content)
        # already present verbatim -> COMMITTED_SAME_CONTENT; same K with a
        # different CLOSED content -> CONFLICT (both versions kept).
        outcome = CommitOutcome.COMMITTED_NEW
        valid_rows = [
            row for row in classified_rows
            if row.key is not None and row.content_sha256 is not None
        ]
        if valid_rows:
            identities = {
                row.key.identity_sha256(): f"{row.key.identity_sha256()}|{row.content_sha256}"
                for row in valid_rows
            }
            all_seen = bool(identities) and all(
                self._content_seen.get(k) == v for k, v in identities.items()
            )
            any_conflict = any(
                k in self._content_seen and self._content_seen[k] != v
                for k, v in identities.items()
            )
            has_closed = any(
                row.row_status is not None and row.row_status.value == "CLOSED"
                for row in valid_rows
            )
            if any_conflict and has_closed:
                outcome = CommitOutcome.CONFLICT
            elif all_seen:
                outcome = CommitOutcome.COMMITTED_SAME_CONTENT
            for key_identity, identity in identities.items():
                self._content_seen[key_identity] = identity
        if task_key in self._conflict_tasks:
            outcome = CommitOutcome.CONFLICT
        now = time.time_ns()
        receipt = CommitReceipt(
            commit_id=f"commit-{self.commit_count:05d}",
            batch_id=f"batch-{envelope.attempt_id}",
            evidence_revision="r1",
            outcome=outcome,
            row_provenance=tuple(
                RowCommitProvenance(
                    raw_row_ordinal=row.raw_row_ordinal,
                    content_sha256=(row.content_sha256 or ""),
                    disposition="NEW_VERSION" if outcome is CommitOutcome.COMMITTED_NEW else "SAME_VERSION",
                )
                for row in classified_rows
                if row.frame_partition.value == "VALID"
            ),
            raw_verified=not self._unverified,
            chunk_written=not self._unverified,
            reconciled=not self._unverified,
            committed_at_nano=now,
            available_at_nano=(now if not self._unverified else None),
        )
        first_nano = envelope.meta.first_valid_nano
        last_nano = envelope.meta.last_valid_nano
        if receipt.available_at_nano is not None and first_nano is not None and last_nano is not None:
            window = self._available.get(task_key)
            self._available[task_key] = (
                (min(window[0], first_nano), max(window[1], last_nano)) if window else (first_nano, last_nano)
            )
        self.commits.append({"attempt_id": envelope.attempt_id, "task_key": task_key, "outcome": outcome.value})
        self.captured.append((envelope, tuple(classified_rows)))
        return receipt

    def recover(self, batch_id: str) -> RecoveryOutcome:
        return RecoveryOutcome(batch_id=batch_id, status=RecoveryStatus.NOTHING_TO_DO)

    def query_available(self, *, as_of_nano, dataset=None, source_symbol=None, duration_seconds=None, task_key=None):
        return tuple()


class FakeCalendar:
    """CalendarProvider double with an explicit status per symbol."""

    def __init__(self, status: CalendarAnswerStatus = CalendarAnswerStatus.VERIFIED_SESSIONS) -> None:
        self.status = status
        self.calls: List[str] = []

    def sessions(self, *, dataset, source_symbol, duration_seconds, window, as_of_nano) -> CalendarAnswer:
        self.calls.append(source_symbol)
        if self.status is CalendarAnswerStatus.VERIFIED_SESSIONS:
            # Session ends at as_of so a daily bar whose start precedes as_of
            # evidences an actual session-end close (H03.8 positive vector).
            return CalendarAnswer(
                status=self.status,
                dataset=dataset,
                source_symbol=source_symbol,
                duration_seconds=duration_seconds,
                window=window,
                as_of_nano=as_of_nano,
                sessions=(("2026-09-23", IntervalNano(start_nano=window.start_nano, end_nano=as_of_nano)),),
                evidence_sha256="synthetic-calendar-evidence-sha",
                evidence_version="synthetic-v1",
            )
        if self.status is CalendarAnswerStatus.NO_TRADING_EVIDENCED:
            return CalendarAnswer(
                status=self.status,
                dataset=dataset,
                source_symbol=source_symbol,
                duration_seconds=duration_seconds,
                window=window,
                as_of_nano=as_of_nano,
                evidence_sha256="synthetic-no-trading-sha",
            )
        return CalendarAnswer(
            status=self.status,
            dataset=dataset,
            source_symbol=source_symbol,
            duration_seconds=duration_seconds,
            window=window,
            as_of_nano=as_of_nano,
        )


class HangingSessionFactory:
    """create() never returns (H03.4 construction-hang vector)."""

    def __init__(self) -> None:
        self.create_calls = 0
        self._release = threading.Event()

    def create(self):
        self.create_calls += 1
        self._release.wait(timeout=300.0)
        raise RuntimeError("hanging factory released (SYNTHETIC)")

    def release(self) -> None:
        self._release.set()


class RawApiWithTradingMethods:
    """SDK-like api exposing the four market-data methods PLUS forbidden
    trading methods; the facade must not forward the forbidden ones."""

    is_synthetic = True

    def __init__(self, frames: Mapping[Tuple[str, int], Any], clock_nano) -> None:
        self._frames = dict(frames)
        self._clock_nano = clock_nano
        self.get_kline_serial_calls: List[Tuple[str, int, int]] = []
        self.trading_calls: List[str] = []

    def get_kline_serial(self, symbol: str, duration_seconds: int, data_length: int = 8192):
        self.get_kline_serial_calls.append((symbol, duration_seconds, data_length))
        return self._frames[(symbol, duration_seconds)]

    def wait_update(self, deadline=None) -> bool:
        return True

    def is_serial_ready(self, serial) -> bool:
        return True

    def close(self) -> None:
        return None

    # forbidden surface (must be unreachable through the facade)
    def insert_order(self, *args, **kwargs):
        self.trading_calls.append("insert_order")

    def cancel_order(self, *args, **kwargs):
        self.trading_calls.append("cancel_order")

    def get_account(self, *args, **kwargs):
        self.trading_calls.append("get_account")

    def get_position(self, *args, **kwargs):
        self.trading_calls.append("get_position")


class ClockNano:
    """Mutable injected clock (nanoseconds)."""

    def __init__(self, start_nano: int) -> None:
        self.now = int(start_nano)

    def __call__(self) -> int:
        return self.now


class AutoClock:
    """Injected clock that advances a fixed step on every read (used where
    retry eligibility must pass without real waiting)."""

    def __init__(self, start_nano: int, step_nano: int = 61 * 1_000_000_000) -> None:
        self.now = int(start_nano)
        self.step_nano = int(step_nano)

    def __call__(self) -> int:
        self.now += self.step_nano
        return self.now


class SleepRecorder:
    """No-op injected sleep capturing backoff values."""

    def __init__(self) -> None:
        self.calls: List[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))


class VirtualMonoClock:
    """Deterministic monotonic clock + sleep double (F-HARD-01 harness fix).

    The engine's wall-clock reads (BudgetLedger, group/ready deadlines,
    no-data staleness, rate-limit pause, finalize window) are served in
    VIRTUAL time that advances a fixed step per read, so deadline and
    staleness decisions depend on the engine's OWN control flow, never on
    real elapsed seconds or host load (busy-wait spins collapse to a
    handful of iterations).  ``sleep(x)`` advances virtual time by ``x``
    and additionally yields a small bounded slice of REAL time so the
    storage worker thread keeps making progress while the engine waits.
    Thread-safe: reads from the collector and storage threads are locked.
    """

    def __init__(
        self,
        step_seconds: float = 0.02,
        real_yield_max_seconds: float = 0.005,
    ) -> None:
        self._lock = threading.Lock()
        self._now = 0.0
        self._step = float(step_seconds)
        self._real_yield_max = float(real_yield_max_seconds)

    def mono(self) -> float:
        with self._lock:
            self._now += self._step
            return self._now

    def sleep(self, seconds: float) -> None:
        requested = max(float(seconds), 0.0)
        with self._lock:
            self._now += requested
        time.sleep(min(requested, self._real_yield_max))
