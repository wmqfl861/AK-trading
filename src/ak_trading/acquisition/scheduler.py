"""C4 scheduler: budgets, retries, leases, isolation, H03 ordering (H10-H12).

Everything numeric comes from the validated policy. The scheduler:

- meters serial attempts (<= 24 incl. retries), wall clock (<= 1800 s), API
  connection starts (<= 3 per rolling 600 s), active symbol/serial group
  sizes, and uncommitted raw snapshots (<= 2);
- classifies failures with the frozen backoff ladder 60/300/900 s and at
  most 4 attempts including the initial; attempt counters and
  ``next_eligible_at`` persist in the run directory and are NOT reset by
  restarts (H11);
- stops the whole account run on auth/permission denial, pauses the worker
  on rate limits, honors longer vendor Retry-After, and stops new requests
  after 2 consecutive account rate limits; single-symbol failures quarantine
  that symbol only;
- enforces H12 isolation: below-normal process priority, protected path/DB
  lists, junction/symlink refusal, disk reservation floor, RSS cap, and
  refusal to start when cold-chain health signals are missing. It never
  kills, restarts, or reconfigures any existing service.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .contracts import (
    ContractError,
    FailureClass,
    Policy,
    TaskState,
    canonical_sha256,
    nano_to_iso,
)
from . import tq_adapter as tq

BACKOFF_LADDER_SECONDS = (60, 300, 900)
ATTEMPTS_MAX_INCLUDING_INITIAL = 4
RATE_LIMIT_STOP_THRESHOLD = 2


class BudgetExhausted(RuntimeError):
    """Raised when the run's policy budget is spent -> DEFERRED_BUDGET."""


class IsolationRefused(RuntimeError):
    """Raised when H12 isolation checks fail; nothing has been started."""


def _observe_rss_bytes(max_rss_bytes: int) -> int:
    """One fail-closed observation shared by the preflight and run ledger."""
    if type(max_rss_bytes) is not int or max_rss_bytes <= 0:
        raise IsolationRefused("invalid RSS policy cap (H12)")
    try:
        import psutil

        rss = psutil.Process().memory_info().rss
    except Exception:
        raise IsolationRefused("RSS cannot be observed; run refused (H12)") from None
    if type(rss) is not int or rss <= 0:
        raise IsolationRefused("RSS observation unknown; run refused (H12)")
    if rss > max_rss_bytes:
        raise IsolationRefused("process RSS exceeds policy cap; stopping new tasks (H12)")
    return rss


def canonical_task_key(source_symbol: str, duration_seconds: int) -> str:
    """Execution state identity; queue phase affects ordering only."""
    return f"{source_symbol}|{duration_seconds}"


# ---------------------------------------------------------------------------
# Budget ledger (H10)
# ---------------------------------------------------------------------------

@dataclass
class BudgetLedger:
    policy: Policy
    started_monotonic: float = field(default_factory=time.monotonic)
    serial_attempts: int = 0
    connection_starts: list[float] = field(default_factory=list)
    uncommitted_snapshots: int = 0
    stopped: bool = False

    def wall_elapsed(self) -> float:
        return time.monotonic() - self.started_monotonic

    def wall_remaining(self) -> float:
        budget = self.policy.budget
        remaining = budget["run_wall_seconds_max"] - self.wall_elapsed()
        # Always reserve the safe finalize window (H10 session boundary).
        remaining -= budget["safe_finalize_reserve_seconds"]
        return remaining

    def charge_serial_attempt(self) -> None:
        if self.stopped:
            raise BudgetExhausted("run stopped")
        if self.serial_attempts >= self.policy.budget["serial_request_attempts_per_run_max"]:
            raise BudgetExhausted("serial attempt budget exhausted (DEFERRED_BUDGET)")
        self.serial_attempts += 1

    def charge_connection_start(self, now_monotonic: Optional[float] = None) -> None:
        now = now_monotonic if now_monotonic is not None else time.monotonic()
        budget = self.policy.budget
        window = 600.0
        cap = budget["api_connection_starts_per_rolling_600_seconds_max"]
        self.connection_starts = [
            t for t in self.connection_starts if now - t < window
        ]
        if len(self.connection_starts) >= cap:
            raise BudgetExhausted(
                "api connection start rate budget exhausted (rolling 600 s)"
            )
        self.connection_starts.append(now)

    def charge_uncommitted_snapshot(self) -> None:
        cap = self.policy.budget["uncommitted_raw_snapshots_max"]
        if self.uncommitted_snapshots + 1 > cap:
            raise BudgetExhausted("uncommitted raw snapshot cap reached")
        self.uncommitted_snapshots += 1

    def release_uncommitted_snapshot(self) -> None:
        self.uncommitted_snapshots = max(0, self.uncommitted_snapshots - 1)

    def check_rss_bytes(self) -> int:
        return _observe_rss_bytes(self.policy.budget["process_rss_bytes_max"])

    def stop(self) -> None:
        self.stopped = True


# ---------------------------------------------------------------------------
# Persistent attempt state (H11)
# ---------------------------------------------------------------------------

@dataclass
class TaskAttemptState:
    attempts: int = 0
    next_eligible_at_nano: int = 0
    last_failure_class: Optional[str] = None
    state: TaskState = TaskState.PLANNED

    def to_json(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "last_failure_class": self.last_failure_class,
            "next_eligible_at_nano": str(self.next_eligible_at_nano),
            "state": self.state.value,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "TaskAttemptState":
        return cls(
            attempts=int(payload.get("attempts", 0)),
            next_eligible_at_nano=int(payload.get("next_eligible_at_nano", 0)),
            last_failure_class=payload.get("last_failure_class"),
            state=TaskState(payload.get("state", "PLANNED")),
        )


class RunStateStore:
    """Crash-safe run state (attempts/backoff/checkpoints) in the run dir."""

    def __init__(self, run_dir: Path) -> None:
        self.path = Path(run_dir) / "run-state.json"
        self._state: dict[str, Any] = {"tasks": {}, "checkpoints": {}}
        if self.path.exists():
            try:
                self._state = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ContractError("run state unreadable; refusing recovery") from None
            if (
                not isinstance(self._state, dict)
                or not isinstance(self._state.get("tasks"), dict)
                or not isinstance(self._state.get("checkpoints"), dict)
            ):
                raise ContractError("invalid run state; refusing recovery")

    def bind_plan(self, plan: Mapping[str, Any]) -> None:
        """Call under the run lease, before factories or state mutations."""
        binding = {"canonical_plan_sha256": canonical_sha256(plan)}
        name = "immutable_plan_identity"
        checkpoints = self._state["checkpoints"]
        if name in checkpoints:
            if checkpoints[name] != binding:
                raise ContractError("run directory is bound to a different plan")
            return
        if self._state != {"tasks": {}, "checkpoints": {}}:
            raise ContractError("unbound nonempty run state requires explicit migration")
        self.checkpoint(name, binding)

    def task_state(self, logical_key: str) -> TaskAttemptState:
        raw = self._state["tasks"].get(logical_key)
        return TaskAttemptState.from_json(raw) if raw else TaskAttemptState()

    def save_task_state(self, logical_key: str, state: TaskAttemptState) -> None:
        self._state["tasks"][logical_key] = state.to_json()
        self._flush()

    def checkpoint(self, name: str, payload: Mapping[str, Any]) -> None:
        self._state["checkpoints"][name] = dict(payload)
        self._flush()

    def checkpoints(self) -> dict[str, Any]:
        return dict(self._state.get("checkpoints", {}))

    def _flush(self) -> None:
        import tempfile

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".run-state-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, sort_keys=True, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


class RetryGate:
    """Backoff 60/300/900, max 4 attempts, persisted across restarts."""

    def __init__(self, store: RunStateStore, clock_nano: Callable[[], int]) -> None:
        self._store = store
        self._clock = clock_nano

    def can_attempt_now(self, logical_key: str) -> bool:
        state = self._store.task_state(logical_key)
        if state.attempts >= ATTEMPTS_MAX_INCLUDING_INITIAL or state.state in {
            TaskState.INGEST_VERIFIED, TaskState.ACCEPTED,
        }:
            return False
        return self._clock() >= state.next_eligible_at_nano

    def record_success(self, logical_key: str, state: TaskState) -> None:
        current = self._store.task_state(logical_key)
        current.state = state
        self._store.save_task_state(logical_key, current)

    def start_attempt(self, logical_key: str) -> TaskAttemptState:
        """Reserve an attempt durably BEFORE a request, including on crashes."""
        if not self.can_attempt_now(logical_key):
            raise ContractError("attempt not eligible")
        state = self._store.task_state(logical_key)
        state.attempts += 1
        state.state = TaskState.RUNNING
        state.next_eligible_at_nano = self._next_eligible(state.attempts)
        self._store.save_task_state(logical_key, state)
        return state

    def _next_eligible(self, attempts: int) -> int:
        if attempts >= ATTEMPTS_MAX_INCLUDING_INITIAL:
            return 0
        return self._clock() + BACKOFF_LADDER_SECONDS[attempts - 1] * 1_000_000_000

    def record_failure(
        self, logical_key: str, failure: FailureClass, *, attempt_started: bool = False,
    ) -> TaskAttemptState:
        state = self._store.task_state(logical_key)
        if not attempt_started:
            state.attempts += 1  # compatibility for standalone failure recording
        elif state.attempts < 1:
            raise ContractError("failure has no persisted attempt")
        state.last_failure_class = failure.value
        state.state = TaskState.RETRYABLE_FAILURE
        state.next_eligible_at_nano = self._next_eligible(state.attempts)
        self._store.save_task_state(logical_key, state)
        return state


class RateLimitCoordinator:
    """Pause on rate limit; 2 consecutive account rate limits stop new requests."""

    def __init__(self) -> None:
        self.paused_until_nano = 0
        self.consecutive_account_rate_limits = 0
        self.stop_new_requests = False

    def observe(self, exc: RateLimitErrorT, now_nano: int) -> None:
        retry_after = max(
            exc.retry_after_seconds, 60.0
        )
        self.paused_until_nano = max(
            self.paused_until_nano, now_nano + int(retry_after * 1_000_000_000)
        )
        self.consecutive_account_rate_limits += 1
        if self.consecutive_account_rate_limits >= RATE_LIMIT_STOP_THRESHOLD:
            self.stop_new_requests = True

    def observe_non_rate_limit(self) -> None:
        self.consecutive_account_rate_limits = 0

    def paused(self, now_nano: int) -> bool:
        return now_nano < self.paused_until_nano


RateLimitErrorT = tq.RateLimitError


def classify_exception(exc: BaseException) -> tuple[FailureClass, TaskState]:
    """Failure taxonomy (H11): distinct states for distinct causes."""
    if isinstance(exc, tq.RateLimitError):
        return FailureClass.RATE_LIMITED, TaskState.RETRYABLE_FAILURE
    if isinstance(exc, tq.AccountDeniedError):
        return FailureClass.ACCOUNT_DENIED, TaskState.PERMISSION_DENIED
    if isinstance(exc, tq.SymbolUnsupportedError):
        return FailureClass.SYMBOL_UNSUPPORTED, TaskState.UNSUPPORTED
    if isinstance(exc, tq.NotReadyTimeoutError):
        return FailureClass.TRANSIENT, TaskState.RETRYABLE_FAILURE
    if isinstance(exc, BudgetExhausted):
        return FailureClass.BUDGET, TaskState.DEFERRED_BUDGET
    if isinstance(exc, tq.TqAdapterError):
        return exc.failure_class, TaskState.RETRYABLE_FAILURE
    return FailureClass.LOCAL_ERROR, TaskState.RETRYABLE_FAILURE


# ---------------------------------------------------------------------------
# Lease (H11: single active holder)
# ---------------------------------------------------------------------------

class TaskLease:
    """One stable OS lock held throughout ownership, with durable identity."""

    def __init__(self, run_dir: Path, holder_name: str) -> None:
        self.path = Path(run_dir) / "task-lease.json"
        self.lock_path = Path(run_dir) / "task-lease.lock"
        self.holder_name = holder_name
        self.owner_token = uuid.uuid4().hex
        self._handle: Any = None
        self._locked = False
        self._holder_pid: Optional[int] = None
        self._process_pid: Optional[int] = None

    def _lock(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # O_CREAT without truncation: the lock inode/path is never replaced or
        # unlinked. Windows permits locking byte zero even on a new empty file;
        # initialize that byte only AFTER winning the exclusive lock.
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self._handle = os.fdopen(fd, "r+b", buffering=0)
        except BaseException:
            os.close(fd)
            raise
        if os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._locked = True
        if os.fstat(self._handle.fileno()).st_size == 0:
            self._handle.write(b"0")
            self._handle.flush()
            os.fsync(self._handle.fileno())

    def _close_lock(self) -> None:
        handle, self._handle = self._handle, None
        locked, self._locked = self._locked, False
        self._holder_pid = None
        self._process_pid = None
        if handle is None:
            return
        try:
            if locked and not handle.closed:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass  # Closing the descriptor releases the kernel lock too.
        finally:
            handle.close()

    def _record(self) -> Optional[dict[str, Any]]:
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(record, dict):
            raise ContractError("invalid lease metadata")
        pid, heartbeat = record.get("holder_pid"), record.get("heartbeat_nano")
        if isinstance(pid, bool) or not isinstance(pid, (int, str)) or int(pid) <= 0:
            raise ContractError("invalid lease PID")
        if (
            isinstance(heartbeat, bool) or not isinstance(heartbeat, (int, str))
            or int(heartbeat) < 0
        ):
            raise ContractError("invalid lease heartbeat")
        if "owner_token" in record and (
            not isinstance(record["owner_token"], str) or not record["owner_token"]
        ):
            raise ContractError("invalid lease token")
        return record

    def _owns(self, pid: int, record: Optional[Mapping[str, Any]]) -> bool:
        if (
            not self._locked or self._handle is None or self._handle.closed
            or self._holder_pid != pid or self._process_pid != os.getpid()
        ):
            return False
        # A closed/replaced descriptor or changed lock path is not ownership.
        current = os.fstat(self._handle.fileno())
        path_stat = self.lock_path.stat()
        if (current.st_dev, current.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            return False
        return (
            record is not None and int(record["holder_pid"]) == pid
            and record.get("owner_token") == self.owner_token
        )

    def acquire(self, pid: int, now_nano: int) -> bool:
        if type(pid) is not int or pid <= 0 or type(now_nano) is not int or now_nano < 0:
            return False
        if self._handle is not None:
            try:
                self.heartbeat(pid, now_nano)
                return True
            except ContractError:
                return False
        acquired = False
        try:
            self._lock()
            record = self._record()
            if record is not None:
                expired = now_nano - int(record["heartbeat_nano"]) >= 300 * 1_000_000_000
                # This includes same-PID and legacy records: only confirmed
                # dead AND expired metadata can be recovered.
                if not expired or _pid_alive(int(record["holder_pid"])) is not False:
                    return False
            self._holder_pid = pid
            self._process_pid = os.getpid()
            self._write(pid, now_nano)
            acquired = True
            return True
        except (OSError, ValueError, TypeError, ImportError):
            return False
        finally:
            if not acquired:
                self._close_lock()

    def heartbeat(self, pid: int, now_nano: int) -> None:
        try:
            if not self._owns(pid, self._record()):
                raise ContractError("lease lost")
            self._write(pid, now_nano)
        except (OSError, ValueError, TypeError, ImportError):
            self._close_lock()
            raise ContractError("lease lost; refusing to continue work") from None

    def release(self, pid: int) -> None:
        # An old object without a lock never reads or removes a successor.
        if self._handle is None:
            return
        try:
            if self._owns(pid, self._record()):
                self.path.unlink()
        except (OSError, ValueError, TypeError):
            pass  # Leave uncertain metadata intact; it cannot be stolen.
        finally:
            self._close_lock()

    def _write(self, pid: int, now_nano: int) -> None:
        if not self._locked or self._handle is None or self._handle.closed:
            raise ContractError("lease lost; refusing metadata write")
        payload = {
            "heartbeat_nano": str(now_nano),
            "holder_name": self.holder_name,
            "holder_pid": pid,
            "owner_token": self.owner_token,
        }
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".task-lease-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def _pid_alive(pid: int) -> Optional[bool]:
    """True=live, False=confirmed dead, None=UNKNOWN (never recoverable)."""
    if pid <= 0:
        return None
    try:
        import psutil

        observed = psutil.pid_exists(pid)
        return observed if type(observed) is bool else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Isolation guard (H12)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProtectionList:
    paths: tuple[str, ...]
    db_objects: tuple[str, ...]

    @classmethod
    def from_authorization(cls, auth: Any) -> "ProtectionList":
        return cls(
            paths=tuple(str(p) for p in auth.protected_paths),
            db_objects=tuple(str(o) for o in auth.protected_db_objects),
        )


@dataclass(frozen=True)
class HealthCheckDescriptor:
    name: str
    kind: str  # file_exists | process_alive
    target: str


class IsolationGuard:
    """H12 checks; refuses formal start when signals are missing/unsafe."""

    def __init__(
        self,
        protection: ProtectionList,
        *,
        minimum_free_bytes: int,
        batch_peak_bytes: int,
    ) -> None:
        self.protection = protection
        self.minimum_free_bytes = minimum_free_bytes
        self.batch_peak_bytes = batch_peak_bytes

    def validate_output_root(self, output_root: Path) -> Path:
        from .artifacts import ArtifactError, ensure_no_reparse_chain

        root = Path(output_root).absolute()
        ensure_no_reparse_chain(root)
        resolved = root.resolve(strict=False)
        for protected in self.protection.paths:
            protected_path = Path(protected).resolve(strict=False)
            try:
                resolved.relative_to(protected_path)
                raise IsolationRefused(
                    f"output root inside protected path: {protected}"
                )
            except ValueError:
                pass
            try:
                protected_path.relative_to(resolved)
                raise IsolationRefused(
                    f"output root would contain protected path: {protected}"
                )
            except ValueError:
                pass
        return resolved

    def check_disk(self, output_root: Path) -> int:
        import shutil

        usage = shutil.disk_usage(str(output_root))
        free_after = (
            usage.free
            - max(self.batch_peak_bytes, 2 * 1024**3)
        )
        if free_after < self.minimum_free_bytes:
            raise IsolationRefused(
                "disk reservation floor violated (H12): free "
                f"{usage.free} below floor {self.minimum_free_bytes + self.batch_peak_bytes}"
            )
        return usage.free

    def check_rss_bytes(self, max_rss_bytes: int) -> int:
        return _observe_rss_bytes(max_rss_bytes)

    def check_health_signals(
        self, descriptors: Sequence[HealthCheckDescriptor]
    ) -> list[str]:
        """Run non-invasive checks; missing signals refuse the formal run."""
        failures: list[str] = []
        for descriptor in descriptors:
            if descriptor.kind == "file_exists":
                if not Path(descriptor.target).exists():
                    failures.append(f"health signal missing: {descriptor.name}")
            elif descriptor.kind == "process_alive":
                try:
                    pid = int(descriptor.target)
                except ValueError:
                    failures.append(f"health signal invalid: {descriptor.name}")
                    continue
                if not _pid_alive(pid):
                    failures.append(f"health signal dead: {descriptor.name}")
            else:
                failures.append(f"health signal unknown kind: {descriptor.name}")
        return failures

    def set_below_normal_priority(self) -> str:
        try:
            import psutil

            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            return "BELOW_NORMAL_SET"
        except Exception:
            return "BELOW_NORMAL_UNAVAILABLE_RECORDED"


# ---------------------------------------------------------------------------
# H03 execution ordering
# ---------------------------------------------------------------------------

DEFAULT_PILOT_PRODUCTS = (
    "SHFE.rb",
    "DCE.i",
    "CZCE.SF",
    "INE.sc",
    "CFFEX.IF",
    "GFEX.si",
    "SHFE.au",
    "CFFEX.TL",  # treasury candidate; identity confirmation required at runtime
)


@dataclass(frozen=True)
class QueueItem:
    logical_key: str
    phase: str
    product_key: str
    source_symbol: str
    duration_seconds: int


def build_execution_queue(
    concrete_by_product: Mapping[str, Sequence[str]],
    *,
    pilot_products: Sequence[str] = DEFAULT_PILOT_PRODUCTS,
    priorities: Optional[Mapping[str, str]] = None,
    max_consecutive_per_product: int = 2,
) -> list[QueueItem]:
    """H03 ordering: pilot sample first, then recent-window round-robin, then
    historical P0..P5; no single product may monopolize the queue."""
    queue: list[QueueItem] = []

    def product_items(product: str, phase: str) -> list[QueueItem]:
        items = []
        for symbol in sorted(concrete_by_product.get(product, ())):
            for duration in (60, 86400):
                items.append(
                    QueueItem(
                        logical_key=f"{phase}|{symbol}|{duration}",
                        phase=phase,
                        product_key=product,
                        source_symbol=symbol,
                        duration_seconds=duration,
                    )
                )
        return items

    # Phase 1: bounded cross-venue pilot (H03 order 2).
    for product in pilot_products:
        queue.extend(product_items(product, "PILOT_SAMPLE"))

    # Phase 2: every product gets one pass before any gets a second
    # (H03 order 3/4: cover unattempted products first; fair interleave).
    remaining: dict[str, list[QueueItem]] = {
        product: product_items(product, "RECENT_WINDOW")
        for product in sorted(concrete_by_product)
        if product not in pilot_products
    }
    while remaining:
        served = 0
        for product in list(remaining):
            for _ in range(max_consecutive_per_product):
                if remaining[product]:
                    queue.append(remaining[product].pop(0))
                    served += 1
            if not remaining[product]:
                del remaining[product]
        if served == 0:
            break

    # Phase 3: historical gaps by P0..P5 (H03 order 5).
    priorities = priorities or {}
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4, "P5": 5}
    for product in sorted(
        concrete_by_product,
        key=lambda p: order.get(priorities.get(p, "P5"), 5),
    ):
        queue.extend(product_items(product, "HISTORICAL_GAP"))
    return queue


__all__ = [
    "ATTEMPTS_MAX_INCLUDING_INITIAL",
    "BACKOFF_LADDER_SECONDS",
    "BudgetExhausted",
    "BudgetLedger",
    "DEFAULT_PILOT_PRODUCTS",
    "HealthCheckDescriptor",
    "IsolationGuard",
    "IsolationRefused",
    "ProtectionList",
    "QueueItem",
    "RATE_LIMIT_STOP_THRESHOLD",
    "RateLimitCoordinator",
    "RetryGate",
    "RunStateStore",
    "TaskAttemptState",
    "TaskLease",
    "build_execution_queue",
    "classify_exception",
]
