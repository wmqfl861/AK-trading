"""DP03 fair rotating scheduler (work order H03.1/H03.2/H03.5/H03.9).

Semantics inherited read-only from the pinned acquisition scheduler (DP00
baseline-manifest source_files) and re-implemented for the datapipeline:
backoff ladder 60/300/900 s, at most 4 attempts including the initial,
rolling 600 s connection-start window, conservative rate-limit pause with a
2-strike stop.  All numeric caps come from the frozen DP01 budget
(policy.DP01_BUDGET_FROZEN - single worker, single connection, 4 symbols /
8 serials per subscription group, 24 serial attempts per run, 1800 s wall).

Fairness model (V06):

- the universe expands to tasks (942 symbols x {60, 86400} = 1884 in r1);
- tasks are chunked into subscription groups of at most
  ``active_symbols_max`` distinct symbols (x all durations -> at most
  ``active_serials_max`` serials);
- a persistent rotation pointer selects the next group; a group skipped for
  retry-ineligibility does NOT advance the pointer, so every eligible group
  is served within one full rotation - no task can starve while others make
  progress (proof hook: :meth:`FairRotatingScheduler.service_stats`);
- retry timing (``next_eligible_at_nano``), service priority (rotation with
  rounds-unserved aging) and wait deadlines (engine-side, clamped by budget)
  are three separate mechanisms (H03.1 "deadline/优先级/重试分离").

Restart discipline (H03.9): the rotation pointer and per-task progress
persist atomically in the run root; restart NEVER uses ``max(ts)`` to skip
work - suppression requires the committed window plus a hole-free verified
range, and any registered gap keeps the task scheduled.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .contracts import (
    NANOSECOND,
    ErrorCode,
    FailureClass,
    SourceCoordinate,
    TaskState,
    _fail,
    validate_duration_seconds,
)
from .policy import DP01_BUDGET_FROZEN
from .ports import SubscriptionKey

__all__ = [
    "BACKOFF_LADDER_SECONDS",
    "ATTEMPTS_MAX_INCLUDING_INITIAL",
    "RATE_LIMIT_STOP_THRESHOLD",
    "ROLLING_CONNECTION_WINDOW_SECONDS",
    "BudgetExhausted",
    "BudgetLedger",
    "TaskProgress",
    "SubscriptionGroup",
    "FairRotatingScheduler",
    "SchedulerStateFile",
]


#: Frozen r1 collector retry policy (ladder semantics inherited read-only
#: from the pinned acquisition scheduler; values identical).
BACKOFF_LADDER_SECONDS: Tuple[int, ...] = (60, 300, 900)
ATTEMPTS_MAX_INCLUDING_INITIAL = 4
RATE_LIMIT_STOP_THRESHOLD = 2
ROLLING_CONNECTION_WINDOW_SECONDS = 600.0

_TERMINAL_OK_STATES = (TaskState.ACCEPTED,)


class BudgetExhausted(RuntimeError):
    """The frozen run budget is spent -> tasks defer with DEFERRED_BUDGET."""


# ---------------------------------------------------------------------------
# Budget ledger (H03.5: rate limit / cross-run budget respected)
# ---------------------------------------------------------------------------


class BudgetLedger:
    """Meters wall clock, serial attempts, connection starts and the
    uncommitted-snapshot depth against the frozen DP01 budget numbers."""

    def __init__(
        self,
        budget: Optional[Mapping[str, int]] = None,
        *,
        clock_mono: Optional[Callable[[], float]] = None,
    ) -> None:
        import time

        self.budget = dict(budget or DP01_BUDGET_FROZEN)
        self._clock_mono = clock_mono or time.monotonic
        self.started_monotonic = self._clock_mono()
        self.serial_attempts = 0
        self.connection_starts: List[float] = []
        self.uncommitted_snapshots = 0
        self.stopped = False

    # -- wall ---------------------------------------------------------------

    def wall_elapsed(self) -> float:
        return self._clock_mono() - self.started_monotonic

    def wall_remaining(self) -> float:
        remaining = (
            self.budget["run_wall_seconds_max"]
            - self.wall_elapsed()
            - self.budget["safe_finalize_reserve_seconds"]
        )
        return remaining

    # -- charges --------------------------------------------------------------

    def charge_serial_attempt(self) -> None:
        if self.stopped:
            raise BudgetExhausted("run stopped")
        if self.serial_attempts >= self.budget["serial_request_attempts_per_run_max"]:
            raise BudgetExhausted("serial attempt budget exhausted (DEFERRED_BUDGET)")
        self.serial_attempts += 1

    def charge_connection_start(self, now_mono: Optional[float] = None) -> None:
        now = self._clock_mono() if now_mono is None else now_mono
        cap = self.budget["api_connection_starts_per_rolling_600_seconds_max"]
        self.connection_starts = [t for t in self.connection_starts if now - t < ROLLING_CONNECTION_WINDOW_SECONDS]
        if len(self.connection_starts) >= cap:
            raise BudgetExhausted(
                "api connection start rate budget exhausted (rolling 600 s)"
            )
        self.connection_starts.append(now)

    def charge_uncommitted_snapshot(self) -> None:
        if self.uncommitted_snapshots + 1 > self.budget["uncommitted_raw_snapshots_max"]:
            raise BudgetExhausted("uncommitted raw snapshot cap reached (backpressure)")
        self.uncommitted_snapshots += 1

    def release_uncommitted_snapshot(self) -> None:
        self.uncommitted_snapshots = max(0, self.uncommitted_snapshots - 1)

    def stop(self) -> None:
        self.stopped = True

    def snapshot(self) -> Dict[str, Any]:
        return {
            "wall_elapsed_seconds": self.wall_elapsed(),
            "wall_remaining_seconds": self.wall_remaining(),
            "serial_attempts": self.serial_attempts,
            "connection_starts": len(self.connection_starts),
            "uncommitted_snapshots": self.uncommitted_snapshots,
            "stopped": self.stopped,
        }


# ---------------------------------------------------------------------------
# Persistent scheduler state (H03.9 restart discipline)
# ---------------------------------------------------------------------------


class SchedulerStateFile:
    """Crash-safe rotation pointer + task progress in the run root.

    Writes are serialized with a lock and the atomic replace retried a few
    times: on Windows, ``os.replace`` onto a target that another thread just
    replaced can fail transiently (WinError 5), and the collector worker and
    storage thread both advance task state concurrently.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {"rotation_pointer": 0, "progress": {}, "windows": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise BudgetExhausted("scheduler state unreadable; refusing restart") from None
            if not isinstance(loaded, dict) or "progress" not in loaded or "rotation_pointer" not in loaded:
                raise BudgetExhausted("invalid scheduler state; refusing restart") from None
            self._state = loaded

    def load(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def save(self, state: Mapping[str, Any]) -> None:
        import time as _time

        with self._lock:
            self._state = json.loads(json.dumps(dict(state)))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".sched-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self._state, handle, sort_keys=True, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                last_error: Optional[BaseException] = None
                for attempt in range(5):
                    try:
                        os.replace(tmp, self.path)
                        return
                    except PermissionError as exc:  # transient Windows replace race
                        last_error = exc
                        _time.sleep(0.01 * (attempt + 1))
                raise last_error if last_error else RuntimeError("state replace failed")
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass


# ---------------------------------------------------------------------------
# Task progress and groups
# ---------------------------------------------------------------------------


@dataclass
class TaskProgress:
    """Retry/eligibility/window bookkeeping for one task (H03.1/H03.6)."""

    source_symbol: str
    duration_seconds: int
    product_key: str = ""
    instrument_kind: str = ""
    attempts: int = 0
    next_eligible_at_nano: int = 0
    last_failure_class: Optional[str] = None
    state: TaskState = TaskState.PLANNED
    rounds_unserved: int = 0
    served_count: int = 0
    #: actual observed returned window (recorded per attempt)
    window_first_nano: Optional[int] = None
    window_last_nano: Optional[int] = None
    #: earliest first-nano EVER returned for this task; never decreases
    #: (H03.6: repeated connections never claim earlier history)
    min_window_first_nano: Optional[int] = None
    #: committed coverage from the store view (restart hint, H03.9)
    committed_through_nano: Optional[int] = None
    registered_gap_nanos: List[int] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "source_symbol": self.source_symbol,
            "duration_seconds": self.duration_seconds,
            "product_key": self.product_key,
            "instrument_kind": self.instrument_kind,
            "attempts": self.attempts,
            "next_eligible_at_nano": str(self.next_eligible_at_nano),
            "last_failure_class": self.last_failure_class,
            "state": self.state.value,
            "rounds_unserved": self.rounds_unserved,
            "served_count": self.served_count,
            "window_first_nano": (str(self.window_first_nano) if self.window_first_nano is not None else None),
            "window_last_nano": (str(self.window_last_nano) if self.window_last_nano is not None else None),
            "min_window_first_nano": (
                str(self.min_window_first_nano) if self.min_window_first_nano is not None else None
            ),
            "committed_through_nano": (
                str(self.committed_through_nano) if self.committed_through_nano is not None else None
            ),
            "registered_gap_nanos": [str(n) for n in self.registered_gap_nanos],
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "TaskProgress":
        return cls(
            source_symbol=str(payload["source_symbol"]),
            duration_seconds=int(payload["duration_seconds"]),
            product_key=str(payload.get("product_key", "")),
            instrument_kind=str(payload.get("instrument_kind", "")),
            attempts=int(payload.get("attempts", 0)),
            next_eligible_at_nano=int(payload.get("next_eligible_at_nano", "0")),
            last_failure_class=payload.get("last_failure_class"),
            state=TaskState(payload.get("state", "PLANNED")),
            rounds_unserved=int(payload.get("rounds_unserved", 0)),
            served_count=int(payload.get("served_count", 0)),
            window_first_nano=(int(payload["window_first_nano"]) if payload.get("window_first_nano") else None),
            window_last_nano=(int(payload["window_last_nano"]) if payload.get("window_last_nano") else None),
            min_window_first_nano=(
                int(payload["min_window_first_nano"]) if payload.get("min_window_first_nano") else None
            ),
            committed_through_nano=(
                int(payload["committed_through_nano"]) if payload.get("committed_through_nano") else None
            ),
            registered_gap_nanos=[int(n) for n in payload.get("registered_gap_nanos", [])],
        )


@dataclass(frozen=True)
class SubscriptionGroup:
    """One rotation slot: <= active_symbols_max symbols x all durations."""

    group_index: int
    keys: Tuple[SubscriptionKey, ...]

    @property
    def task_keys(self) -> Tuple[str, ...]:
        return tuple(
            f"{key.coordinate.source_id}|{key.coordinate.source_symbol}|{key.coordinate.duration_seconds}"
            for key in self.keys
        )

    @property
    def symbols(self) -> Tuple[str, ...]:
        seen: List[str] = []
        for key in self.keys:
            if key.coordinate.source_symbol not in seen:
                seen.append(key.coordinate.source_symbol)
        return tuple(seen)


# ---------------------------------------------------------------------------
# The fair rotating scheduler
# ---------------------------------------------------------------------------


class FairRotatingScheduler:
    """rotating_incremental scheduler over the frozen universe (H03.2)."""

    def __init__(
        self,
        *,
        source_id: str,
        symbols: Sequence[Tuple[str, str, str]],
        durations_seconds: Sequence[int] = (60, 86400),
        budget: Optional[Mapping[str, int]] = None,
        clock_nano: Optional[Callable[[], int]] = None,
        state_file: Optional[SchedulerStateFile] = None,
    ) -> None:
        import time

        self.source_id = source_id
        self._clock_nano = clock_nano or time.time_ns
        self._budget = dict(budget or DP01_BUDGET_FROZEN)
        self._state_file = state_file
        self._tasks: Dict[str, TaskProgress] = {}
        self._rotation_pointer = 0
        self._rounds_completed = 0

        # H03.1: validate EVERY task period BEFORE any network request; an
        # invalid period marks that single task UNSUPPORTED and never breaks
        # the group (no subscribe is ever issued for it).
        seen_symbols: set = set()
        for source_symbol, instrument_kind, product_key in symbols:
            if not source_symbol:
                _fail(ErrorCode.E_UNIVERSE_SYMBOL_MISSING, "scheduler task needs source_symbol")
            if source_symbol in seen_symbols:
                _fail(ErrorCode.E_UNIVERSE_DUPLICATE_SYMBOL, f"duplicate universe symbol {source_symbol}")
            seen_symbols.add(source_symbol)
            for duration in durations_seconds:
                task_key = f"{source_id}|{source_symbol}|{duration}"
                product = str(product_key)
                kind = str(instrument_kind)
                if self._is_supported_duration(duration):
                    self._tasks[task_key] = TaskProgress(
                        source_symbol=source_symbol,
                        duration_seconds=int(duration),
                        product_key=product,
                        instrument_kind=kind,
                    )
                else:
                    self._tasks[task_key] = TaskProgress(
                        source_symbol=source_symbol,
                        duration_seconds=int(duration) if isinstance(duration, int) and not isinstance(duration, bool) else -1,
                        product_key=product,
                        instrument_kind=kind,
                        state=TaskState.UNSUPPORTED,
                        last_failure_class=FailureClass.SYMBOL_UNSUPPORTED.value,
                    )

        self._group_symbols = self._build_group_symbol_order()
        self._groups = self._build_groups()
        if state_file is not None:
            self._load_persisted_state()

    # -- plan construction ------------------------------------------------------

    @staticmethod
    def _is_supported_duration(duration: Any) -> bool:
        try:
            validate_duration_seconds(duration)
            return True
        except Exception:
            return False

    def _build_group_symbol_order(self) -> List[str]:
        products: Dict[str, List[str]] = {}
        for task in self._tasks.values():
            product = getattr(task, "product_key", "")
            products.setdefault(product, [])
            if task.source_symbol not in products[product]:
                products[product].append(task.source_symbol)
        ordered: List[str] = []
        for product in sorted(products):
            ordered.extend(sorted(products[product]))
        return ordered

    def _build_groups(self) -> List[SubscriptionGroup]:
        cap_symbols = self._budget["active_symbols_max"]
        cap_serials = self._budget["active_serials_max"]
        durations = sorted({t.duration_seconds for t in self._tasks.values() if t.state is not TaskState.UNSUPPORTED})
        groups: List[SubscriptionGroup] = []
        for start in range(0, len(self._group_symbols), cap_symbols):
            chunk = self._group_symbols[start : start + cap_symbols]
            keys: List[SubscriptionKey] = []
            for symbol in chunk:
                for duration in durations:
                    progress = self._tasks.get(f"{self.source_id}|{symbol}|{duration}")
                    if progress is None or progress.state is TaskState.UNSUPPORTED:
                        continue
                    keys.append(
                        SubscriptionKey(
                            coordinate=SourceCoordinate(
                                source_id=self.source_id,
                                source_symbol=symbol,
                                duration_seconds=duration,
                            )
                        )
                    )
            if not keys:
                continue
            if len({k.coordinate.source_symbol for k in keys}) * len(durations) > cap_serials:
                # Defensive: never exceed the serial cap (H03.2 budget).
                _fail(
                    ErrorCode.E_RUN_STATE_INVALID,
                    "subscription group exceeds active_serials_max budget",
                )
            groups.append(SubscriptionGroup(group_index=len(groups), keys=tuple(keys)))
        return groups

    # -- persistence (H03.9) -----------------------------------------------------

    def _load_persisted_state(self) -> None:
        assert self._state_file is not None
        state = self._state_file.load()
        pointer = state.get("rotation_pointer", 0)
        if not isinstance(pointer, int) or not 0 <= pointer <= max(len(self._groups), 0):
            pointer = 0
        self._rotation_pointer = pointer
        for task_key, payload in (state.get("progress") or {}).items():
            existing = self._tasks.get(task_key)
            if existing is None or existing.state is TaskState.UNSUPPORTED:
                continue
            self._tasks[task_key] = TaskProgress.from_json(payload)

    def commit_pointer(self) -> None:
        """Persist rotation pointer + progress atomically (restart safety)."""
        if self._state_file is None:
            return
        self._state_file.save(
            {
                "rotation_pointer": self._rotation_pointer,
                "rounds_completed": self._rounds_completed,
                "progress": {k: v.to_json() for k, v in sorted(self._tasks.items())},
            }
        )

    # -- eligibility / fairness (V06) --------------------------------------------

    def _task_eligible(self, task: TaskProgress, now_nano: int) -> bool:
        if task.state in _TERMINAL_OK_STATES:
            return False
        if task.attempts >= ATTEMPTS_MAX_INCLUDING_INITIAL:
            return False
        return now_nano >= task.next_eligible_at_nano

    def _group_eligible(self, group: SubscriptionGroup, now_nano: int) -> bool:
        return any(
            self._task_eligible(self._tasks[key], now_nano)
            for key in group.task_keys
            if key in self._tasks
        )

    def next_group(self, now_nano: Optional[int] = None) -> Optional[SubscriptionGroup]:
        """Next group in rotation order; None when nothing is eligible."""
        now = self._clock_nano() if now_nano is None else now_nano
        if not self._groups:
            return None
        total = len(self._groups)
        for offset in range(total):
            index = (self._rotation_pointer + offset) % total
            group = self._groups[index]
            if self._group_eligible(group, now):
                return group
        return None

    def record_group_started(self, group: SubscriptionGroup) -> None:
        """Advance the rotation pointer past the served group and age the
        rest (one full rotation => every eligible group served => no
        starvation)."""
        total = len(self._groups)
        index = group.group_index
        for task_key in group.task_keys:
            task = self._tasks.get(task_key)
            if task is not None:
                task.rounds_unserved = 0
                task.served_count += 1
        for task in self._tasks.values():
            if task.state is not TaskState.UNSUPPORTED:
                task.rounds_unserved += 1
        for task_key in group.task_keys:
            task = self._tasks.get(task_key)
            if task is not None:
                task.rounds_unserved = 0
        self._rotation_pointer = (index + 1) % total
        if self._rotation_pointer == 0:
            self._rounds_completed += 1
        self.commit_pointer()

    def restart_hint(
        self,
        *,
        committed_windows: Mapping[str, Tuple[int, int]],
        known_gaps: Mapping[str, Sequence[int]],
    ) -> None:
        """Restart planning input from the STORE view - never max(ts).

        A task is suppressed (kept ACCEPTED, attempts capped) only when its
        committed window covers the verified range AND no registered gap lies
        inside that window; any hole keeps the task scheduled with a FRESH
        attempt budget (H03.9: restart cannot skip mid-window gaps).
        """
        for task_key, (start_nano, end_nano) in committed_windows.items():
            task = self._tasks.get(task_key)
            if task is None:
                continue
            task.committed_through_nano = int(end_nano)
            task.window_first_nano = int(start_nano)
            task.min_window_first_nano = (
                int(start_nano) if task.min_window_first_nano is None else task.min_window_first_nano
            )
            gaps = [int(g) for g in known_gaps.get(task_key, ())]
            task.registered_gap_nanos = sorted(set(task.registered_gap_nanos) | set(gaps))
            if not gaps:
                task.state = TaskState.ACCEPTED
                task.attempts = ATTEMPTS_MAX_INCLUDING_INITIAL  # fully done
            else:
                task.state = TaskState.PLANNED
                task.attempts = 0
                task.next_eligible_at_nano = 0

    def record_task_started(self, task_key: str) -> TaskProgress:
        """Reserve one attempt durably BEFORE the request (crash-safe)."""
        task = self._tasks.get(task_key)
        if task is None:
            _fail(ErrorCode.E_RUN_STATE_INVALID, f"unknown task {task_key!r}")
        task.attempts += 1
        task.state = TaskState.RUNNING
        # Pre-arm the backoff so a crash mid-attempt still backs off.
        task.next_eligible_at_nano = self._clock_nano() + self._backoff_seconds(task.attempts) * NANOSECOND
        self.commit_pointer()
        return task

    def record_task_result(
        self,
        task_key: str,
        state: TaskState,
        *,
        failure_class: Optional[FailureClass] = None,
    ) -> TaskProgress:
        """Record a terminal (or intermediate) task outcome.

        Attempts were already reserved by :meth:`record_task_started`; a
        RETRYABLE_FAILURE keeps the pre-armed backoff deadline.  Failures
        never advance a success checkpoint (H03.5).
        """
        task = self._tasks.get(task_key)
        if task is None:
            _fail(ErrorCode.E_RUN_STATE_INVALID, f"unknown task {task_key!r}")
        task.state = state
        if failure_class is not None:
            task.last_failure_class = failure_class.value
        if state is TaskState.RETRYABLE_FAILURE and task.next_eligible_at_nano <= self._clock_nano():
            task.next_eligible_at_nano = (
                self._clock_nano() + self._backoff_seconds(max(task.attempts, 1)) * NANOSECOND
            )
        self.commit_pointer()
        return task

    def record_window(self, task_key: str, first_nano: Optional[int], last_nano: Optional[int]) -> None:
        """Record the ACTUAL returned window; the earliest first-nano ever
        seen never decreases (H03.6 - reconnects claim no earlier history)."""
        task = self._tasks.get(task_key)
        if task is None:
            return
        if first_nano is not None:
            task.window_first_nano = first_nano if task.window_first_nano is None else min(task.window_first_nano, first_nano)
            task.min_window_first_nano = (
                first_nano if task.min_window_first_nano is None else min(task.min_window_first_nano, first_nano)
            )
        if last_nano is not None:
            task.window_last_nano = last_nano if task.window_last_nano is None else max(task.window_last_nano, last_nano)

    def outside_free_window(
        self, task_key: str, requested_from_nano: int
    ) -> Optional[int]:
        """H03.6: nanoseconds of the requested window that lie BELOW the
        earliest row the free channel has ever returned for this task."""
        task = self._tasks.get(task_key)
        if task is None or task.min_window_first_nano is None:
            return None
        if requested_from_nano >= task.min_window_first_nano:
            return None
        return task.min_window_first_nano - requested_from_nano

    @staticmethod
    def _backoff_seconds(attempts: int) -> int:
        if attempts <= 0:
            return 0
        ladder_index = min(attempts, len(BACKOFF_LADDER_SECONDS)) - 1
        return BACKOFF_LADDER_SECONDS[ladder_index]

    # -- continuous mode gate (H03.2) ---------------------------------------------

    def continuous_eligible(self) -> bool:
        """Continuous mode only when the WHOLE target set fits the
        subscription budget at once (with 1884 r1 tasks this is always
        False; the realtime coverage display stays rotating)."""
        symbols = {t.source_symbol for t in self._tasks.values() if t.state is not TaskState.UNSUPPORTED}
        serials = sum(
            1
            for t in self._tasks.values()
            if t.state is not TaskState.UNSUPPORTED
        )
        return (
            len(symbols) <= self._budget["active_symbols_max"]
            and serials <= self._budget["active_serials_max"]
        )

    # -- introspection ---------------------------------------------------------

    @property
    def groups(self) -> Tuple[SubscriptionGroup, ...]:
        return tuple(self._groups)

    @property
    def rotation_pointer(self) -> int:
        return self._rotation_pointer

    def task_progress(self, task_key: str) -> TaskProgress:
        task = self._tasks.get(task_key)
        if task is None:
            _fail(ErrorCode.E_RUN_STATE_INVALID, f"unknown task {task_key!r}")
        return task

    def all_task_states(self) -> Dict[str, str]:
        return {key: task.state.value for key, task in sorted(self._tasks.items())}

    def has_pending_retries(self, now_nano: Optional[int] = None) -> bool:
        """True while some task is only waiting out its retry backoff (the
        worker must not treat 'nothing eligible NOW' as 'done')."""
        now = self._clock_nano() if now_nano is None else now_nano
        return any(
            task.state is TaskState.RETRYABLE_FAILURE
            and task.attempts < ATTEMPTS_MAX_INCLUDING_INITIAL
            and task.next_eligible_at_nano > now
            for task in self._tasks.values()
        )

    def service_stats(self) -> Dict[str, Any]:
        """Fairness proof hook: served counts, max rounds unserved, pointer."""
        eligible_states = [
            task for task in self._tasks.values() if task.state is not TaskState.UNSUPPORTED
        ]
        served = [task.served_count for task in eligible_states]
        return {
            "tasks": len(self._tasks),
            "groups": len(self._groups),
            "rotation_pointer": self._rotation_pointer,
            "rounds_completed": self._rounds_completed,
            "max_served": max(served) if served else 0,
            "min_served": min(served) if served else 0,
            "max_rounds_unserved": max((t.rounds_unserved for t in eligible_states), default=0),
            "budget": dict(self._budget),
        }
