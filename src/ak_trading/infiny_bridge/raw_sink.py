"""Bounded asynchronous raw-batch sink for the observation bridge.

Implements data/maintenance_artifacts/m5-real-feed-prep/observer-sink-miniplan.md
(§2 receipt-boundary capture, §3 journal contract ak_observer_raw_batch_v1,
§4 resource bounds, §5 restart semantics). The receipt hook performs bounded
in-memory admission only; a single dedicated daemon writer serializes,
flushes, fsyncs, rotates and seals segments.

r2 repair notes (observer-sink-r1 hard review):
- F01: the writer's exception path never re-enters the sink lock and never
  requeues an ambiguous record; any sink-internal failure degrades to
  "record dropped/uncertain + counted" and can never block offer()/status().
- F02: the periodic fsync is TIME driven (>= 1 s since the last fsync) so a
  sustained arrival stream cannot starve durability.
- F03: start() seals any leftover active file (including an unterminated
  crash tail) as immutable incomplete evidence before a fresh run opens it.
- F04: the segment cap counts sealed + active segments and is enforced BEFORE
  rotation; MAX_TOTAL_BYTES is enforced; only hash-verified archive acks make
  a sealed segment removable, and removal actually deletes the data file.
- F05: durable accounting advances only after a successful fsync; the
  append index is committed after a successful write.
- F06: an oversize serialized row is an admitted-record capture failure
  (single counting), never also a drop.
- M1-M5: launcher startup line, rate-limited diagnostics emission, bounded
  close, all counter mutations under the lock, crash-tail sealing also fixes
  mixed-run segment accounting.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA = "ak_observer_raw_batch_v1"
EVENT = "receipt_batch"

MAX_PENDING_DESCRIPTORS = 256
MAX_PENDING_BODY_BYTES = 32 * 1024 * 1024
MAX_ROW_BYTES = 4 * 1024 * 1024
MAX_SEGMENT_BYTES = 64 * 1024 * 1024
MAX_SEGMENTS = 8
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
DISK_HEADROOM_BYTES = 1024 * 1024 * 1024
FSYNC_INTERVAL_SECONDS = 1.0
RETRY_BACKOFF_MIN = 0.1
RETRY_BACKOFF_MAX = 1.0
DRAIN_SECONDS = 2.0
STATUS_SAMPLE_LIMIT = 100
RATE_LIMIT_SECONDS = 60.0
DIAGNOSTICS_LIMIT = 32


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _safe_size(path: Path) -> int | None:
    """m2: an externally vanished file must not kill the writer thread."""
    try:
        return path.stat().st_size
    except OSError:
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class SinkDescriptor:
    """Immutable receipt-boundary descriptor; ownership-transferred facts."""

    __slots__ = ("endpoint", "batch_id", "exchange", "instrument", "synthetic",
                 "source_header", "received_at", "receipt_at", "clock_valid",
                 "decision", "blocked_reason", "tick_count", "payload_hash",
                 "instance_id", "sequence", "payload", "body_bytes", "body_kind",
                 "admitted_at")

    def __init__(self, *, endpoint: str, batch_id: str | None, exchange: str | None,
                 instrument: str | None, synthetic: bool, source_header: str | None,
                 received_at: str | None, receipt_at: str | None, clock_valid: bool,
                 decision: str, blocked_reason: str | None, tick_count: int,
                 payload_hash: str, instance_id: str, sequence: int,
                 payload: Mapping[str, Any], body_bytes: bytes | None) -> None:
        self.endpoint = endpoint
        self.batch_id = batch_id
        self.exchange = exchange
        self.instrument = instrument
        self.synthetic = synthetic
        self.source_header = source_header
        self.received_at = received_at
        self.receipt_at = receipt_at
        self.clock_valid = clock_valid
        self.decision = decision
        self.blocked_reason = blocked_reason
        self.tick_count = tick_count
        self.payload_hash = payload_hash
        self.instance_id = instance_id
        self.sequence = sequence
        self.payload = payload
        self.body_kind = "HTTP_VERBATIM" if body_bytes is not None else "NO_HTTP_BODY"
        self.body_bytes = body_bytes
        self.admitted_at = time.monotonic()


class _Stats:
    def __init__(self) -> None:
        self.receipt_total = 0
        self.admitted_total = 0
        self.persisted_total = 0
        self.durable_total = 0
        self.dropped = {"queue_full": 0, "byte_limit": 0, "writer_faulted": 0,
                        "retention_full": 0, "disabled": 0, "low_disk_space": 0}
        # post-admission capture failures partition INSIDE admitted (H06);
        # never also counted in dropped (F06 single counting).
        self.capture_failures = 0
        self.abandoned_total = 0
        self.uncertain_total = 0
        self.tick_total = 0
        self.direct_call_rows = 0
        self.write_failures = 0
        self.flush_failures = 0
        self.fsync_failures = 0
        self.rotation_failures = 0
        self.rows_oversize = 0
        self.retention_refusals = 0
        self.segments_removed = 0
        self.resumes = 0
        self.last_success_at: str | None = None
        self.last_error_at: str | None = None
        self.last_error_reason: str | None = None
        self.failed_keys: deque = deque(maxlen=STATUS_SAMPLE_LIMIT)

    def drop(self, reason: str, key: dict[str, Any]) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1
        self.failed_keys.append({**key, "reason": reason})
        self.last_error_at = _iso(_now_utc())
        self.last_error_reason = reason


class RawBatchSink:
    """Receipt-boundary admission + one dedicated writer thread."""

    def __init__(self, directory: str | Path, *, disk_free: Callable[[Path], int] | None = None,
                 sleeper: Callable[[float], None] | None = None,
                 monotime: Callable[[], float] | None = None,
                 fsync: Callable[[int], None] | None = None,
                 max_segment_bytes: int = MAX_SEGMENT_BYTES,
                 max_segments: int = MAX_SEGMENTS,
                 max_total_bytes: int = MAX_TOTAL_BYTES,
                 disk_headroom_bytes: int = DISK_HEADROOM_BYTES) -> None:
        self.directory = Path(directory)
        # Plan defaults; overridable only at construction for scaled tests.
        self.max_segment_bytes = max_segment_bytes
        self.max_segments = max_segments
        self.max_total_bytes = max_total_bytes
        self.disk_headroom_bytes = disk_headroom_bytes
        self._disk_free = disk_free or self._default_disk_free
        self._sleep = sleeper or time.sleep
        self._monotime = monotime or time.monotonic
        # Production uses os.fsync; tests may inject a no-op/failing callable.
        self._fsync = fsync or os.fsync
        self._last_arrival_mono = 0.0
        self.capture_run_id = "run-" + uuid.uuid4().hex
        self.state = "disabled"  # disabled|running|suspended|retention_full|faulted|closed
        self.capture_incomplete = False
        self._stats = _Stats()
        # RLock: belt-and-braces; the writer fault path additionally never
        # re-enters (F01).
        self._lock = threading.RLock()
        self._queue: deque[SinkDescriptor] = deque()
        self._pending_body_bytes = 0
        self._queue_high_water = 0
        self._pending_bytes_high_water = 0
        self._append_index = 0            # last successfully committed row index
        self._visible_append_index = 0
        self._durable_append_index = 0
        self._durable_locked_index: int | None = None   # permanent prefix cap (N2)
        self._segment_durable_through = 0               # fsynced prefix inside the active segment
        self._last_fsync_mono = 0.0
        self._fsync_retry_at = 0.0
        self._resume_probe_at = 0.0
        self._resume_backoff = RETRY_BACKOFF_MIN
        self._segment_id = "seg-" + uuid.uuid4().hex[:12]
        self._segment_start_index = 1     # index of the next committed row
        self._segment_rows = 0
        self._segment_bytes = 0
        self._segment_min_sequence: int | None = None
        self._segment_max_sequence: int | None = None
        self._retained_bytes = 0
        self._rotations = 0
        self._segment_ticks = 0
        self._writer: threading.Thread | None = None
        self._writer_busy = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._file = None
        self._rate_limited: dict[str, float] = {}
        self._diagnostics: deque = deque(maxlen=DIAGNOSTICS_LIMIT)
        self._oldest_pending_mono: float | None = None
        self._last_flush_lag = 0.0
        self._last_fsync_lag = 0.0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Open the journal directory and start the single writer thread.

        Any leftover active file from a previous (possibly crashed) run is
        sealed as immutable incomplete evidence first (§5/H05, F03/M5): the
        new capture run never appends onto prior bytes.
        """
        with self._lock:
            if self._writer is not None and self._writer.is_alive():
                return True
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._seal_leftover_active()
                if not self._disk_headroom_ok():
                    self.state = "suspended"
                    self._stats.last_error_reason = "low_disk_space"
                    self._note_diagnostic_locked("low_disk_space")
                    return False
                self._retained_bytes = self._sealed_bytes()
                self._segment_durable_through = 0
                self._file = open(self._active_path(), "ab", buffering=0)
            except OSError:
                self.state = "faulted"
                self.capture_incomplete = True
                self._stats.last_error_reason = "start_failed"
                return False
            self.state = "running"
        self._stop.clear()
        self._writer = threading.Thread(target=self._writer_loop,
                                        name=f"raw-sink-{self.capture_run_id[:12]}",
                                        daemon=True)
        self._writer.start()
        return True

    def _seal_leftover_active(self) -> None:
        """Seal a pre-existing active file (crash tail) as prior-run evidence.

        Must be called before the new run opens its active file. A torn final
        line is recorded (reason + byte count) and never repaired; the file is
        renamed away. When a foreign same-process handle pins the file
        (Windows rename refusal), a hash-verified byte-identical copy becomes
        the sealed evidence and the original is reset only after the copy is
        verified; the manifest records the relocation.
        """
        active = self._active_path()
        if not active.exists():
            return
        raw = active.read_bytes()
        if not raw:
            return
        segment_id = "seg-" + uuid.uuid4().hex[:12]
        sealed = self._sealed_path(segment_id)
        digest = hashlib.sha256(raw).hexdigest()
        relocation = None
        try:
            os.replace(active, sealed)
        except PermissionError:
            sealed.write_bytes(raw)
            if hashlib.sha256(sealed.read_bytes()).hexdigest() != digest:
                raise OSError("crash_tail_copy_hash_mismatch")
            with active.open("r+b") as handle:
                handle.truncate(0)
            relocation = "copy_relocated_foreign_handle"
        incomplete_tail = None
        torn_bytes = 0
        if not raw.endswith(b"\n"):
            torn_bytes = len(raw.rsplit(b"\n", 1)[-1])
            incomplete_tail = "unterminated_tail"
        rows = ticks = 0
        prior_run = None
        interval: list = [None, None]
        for line in raw.split(b"\n"):
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("schema") == SCHEMA:
                rows += 1
                ticks += obj.get("tick_count") or 0
                prior_run = obj.get("capture_run_id") or prior_run
                index = obj.get("append_index")
                if isinstance(index, int):
                    interval = [index if interval[0] is None else min(interval[0], index),
                                index if interval[1] is None else max(interval[1], index)]
        manifest = {
            "schema": SCHEMA,
            "manifest_kind": "segment_manifest",
            "capture_run_id": prior_run or "unknown_prior_run",
            "sealed_by_run": self.capture_run_id,
            "segment_id": segment_id,
            "cause": "restart_crash_tail",
            "file": sealed.name,
            "file_bytes": len(raw),
            "file_sha256": digest,
            "row_count": rows,
            "tick_count": ticks,
            "append_index_interval": interval,
            "sequence_interval": [None, None],
            "durability": "unknown_prior_run",
            "incomplete_tail": incomplete_tail,
            "torn_bytes": torn_bytes,
            "relocation": relocation,
            "sealed_at": _iso(_now_utc()),
        }
        self._write_manifest(manifest)
        self._retained_bytes = len(raw)

    def close(self) -> None:
        """Best-effort drain+fsync bounded by DRAIN_SECONDS; never blocks long.

        The fsync-on-close belongs to the writer's own seal path (writer
        thread); if the writer does not exit within the budget, close()
        returns without touching the file (M3) — the daemon writer finishes
        its bounded seal afterwards.
        """
        self._stop.set()
        self._wake.set()
        writer = self._writer
        if writer is not None and writer.is_alive():
            writer.join(timeout=DRAIN_SECONDS)
        with self._lock:
            handle, self._file = self._file, None
            if handle is not None and (writer is None or not writer.is_alive()):
                # writer exited without sealing it (e.g. seal disabled)
                try:
                    handle.close()
                except OSError:
                    pass
            if self.state not in {"faulted", "retention_full"}:
                self.state = "closed"
            abandoned = len(self._queue)
            if abandoned:
                self._stats.abandoned_total += abandoned
                self.capture_incomplete = True
            self._queue.clear()
            self._pending_body_bytes = 0

    # -- receipt hook: bounded, non-blocking -------------------------------

    def offer(self, descriptor: SinkDescriptor) -> bool:
        """Admit one descriptor; drop the incoming copy on any bound/fault.

        Never raises, never waits for capacity or the writer (H03): the only
        lock held is the sink's own, for bounded in-memory accounting.
        """
        try:
            with self._lock:  # M4: every counter mutation under the lock
                self._stats.receipt_total += 1
                self._stats.tick_total += descriptor.tick_count
                key = {"instance_id": descriptor.instance_id,
                       "sequence": descriptor.sequence}
                if self.state in {"disabled", "closed"}:
                    self._stats.drop("disabled", key)
                    self._note_diagnostic_locked("disabled")
                    return False
                if self.state == "retention_full":
                    self._stats.drop("retention_full", key)
                    self._note_diagnostic_locked("retention_full")
                    return False
                if self.state in {"suspended", "faulted"}:
                    reason = "low_disk_space" if self.state == "suspended" else "writer_faulted"
                    self._stats.drop(reason, key)
                    self._note_diagnostic_locked(reason)
                    return False
                if len(self._queue) >= MAX_PENDING_DESCRIPTORS:
                    self._stats.drop("queue_full", key)
                    self._note_diagnostic_locked("queue_full")
                    return False
                body_len = len(descriptor.body_bytes) if descriptor.body_bytes is not None else 0
                if self._pending_body_bytes + body_len > MAX_PENDING_BODY_BYTES:
                    self._stats.drop("byte_limit", key)
                    self._note_diagnostic_locked("byte_limit")
                    return False
                self._queue.append(descriptor)
                self._last_arrival_mono = self._monotime()
                if self._oldest_pending_mono is None:
                    self._oldest_pending_mono = self._monotime()
                self._pending_body_bytes += body_len
                if descriptor.body_kind == "NO_HTTP_BODY":
                    self._stats.direct_call_rows += 1
                self._queue_high_water = max(self._queue_high_water, len(self._queue))
                self._pending_bytes_high_water = max(self._pending_bytes_high_water,
                                                     self._pending_body_bytes)
                self._stats.admitted_total += 1
        except Exception:  # absolute isolation of the receipt path (H03)
            return False
        self._wake.set()
        return True

    # -- status (additive surface) ------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            pending = len(self._queue)
            in_flight = 1 if self._writer_busy else 0
            oldest = None
            if pending and self._oldest_pending_mono is not None:
                oldest = max(0.0, self._monotime() - self._oldest_pending_mono)
            return {
                "enabled": self.state != "disabled",
                "state": self.state,
                "capture_run_id": self.capture_run_id,
                "capture_incomplete": self.capture_incomplete,
                "limits": {
                    "max_pending_descriptors": MAX_PENDING_DESCRIPTORS,
                    "max_pending_body_bytes": MAX_PENDING_BODY_BYTES,
                    "max_row_bytes": MAX_ROW_BYTES,
                    "max_segment_bytes": self.max_segment_bytes,
                    "max_segments": self.max_segments,
                    "max_total_bytes": self.max_total_bytes,
                    "disk_headroom_bytes": self.disk_headroom_bytes,
                    "fsync_interval_seconds": FSYNC_INTERVAL_SECONDS,
                },
                "writer_alive": bool(self._writer is not None and self._writer.is_alive()),
                "queue_depth": pending,
                "pending_body_bytes": self._pending_body_bytes,
                "high_water": {"queue_depth": self._queue_high_water,
                               "pending_body_bytes": self._pending_bytes_high_water},
                "queued_in_flight": pending + in_flight,
                "oldest_pending_age_seconds": oldest,
                "visible_append_index": self._visible_append_index,
                "durable_append_index": self._durable_append_index,
                "flush_lag_seconds": self._last_flush_lag,
                "fsync_lag_seconds": self._last_fsync_lag,
                "rotations": self._rotations,
                "retained_bytes": self._retained_bytes,
                "last_success_at": self._stats.last_success_at,
                "last_error_at": self._stats.last_error_at,
                "last_error_reason": self._stats.last_error_reason,
                "diagnostics": list(self._diagnostics),
                "counts": {
                    "receipt_total": self._stats.receipt_total,
                    "admitted_total": self._stats.admitted_total,
                    "persisted_total": self._stats.persisted_total,
                    "durable_total": self._stats.durable_total,
                    "dropped_total": sum(self._stats.dropped.values()),
                    "dropped_by_reason": dict(self._stats.dropped),
                    "capture_failures": self._stats.capture_failures,
                    "abandoned_total": self._stats.abandoned_total,
                    "uncertain_total": self._stats.uncertain_total,
                    "tick_total": self._stats.tick_total,
                    "direct_call_rows": self._stats.direct_call_rows,
                    "write_failures": self._stats.write_failures,
                    "flush_failures": self._stats.flush_failures,
                    "fsync_failures": self._stats.fsync_failures,
                    "rotation_failures": self._stats.rotation_failures,
                    "rows_oversize": self._stats.rows_oversize,
                    "retention_refusals": self._stats.retention_refusals,
                    "segments_removed": self._stats.segments_removed,
                    "resumes": self._stats.resumes,
                },
                "failed_receipt_keys": list(self._stats.failed_keys),
            }

    # -- writer internals ----------------------------------------------------

    def _default_disk_free(self, path: Path) -> int:
        try:
            usage = os.statvfs(str(path))
            return usage.f_bavail * usage.f_frsize
        except (OSError, AttributeError):
            return self.disk_headroom_bytes + 1  # unknown: do not suspend on it

    def _disk_headroom_ok(self) -> bool:
        return self._disk_free(self.directory) >= self.disk_headroom_bytes

    def _active_path(self) -> Path:
        return self.directory / "raw-batches.jsonl"

    def _sealed_path(self, segment_id: str) -> Path:
        return self.directory / f"raw-batches.{segment_id}.jsonl"

    def _sealed_files(self) -> list[Path]:
        # m2: an externally vanished sealed file must not kill the writer
        return [path for path in self.directory.glob("raw-batches.*.jsonl")
                if _safe_size(path) is not None]

    def _sealed_bytes(self) -> int:
        return sum(size for size in (_safe_size(path) for path in
                                     self.directory.glob("raw-batches.*.jsonl"))
                   if size is not None)

    def _note_diagnostic_locked(self, reason: str) -> None:
        """Rate-limited reason-code emission (<= once per reason per 60 s)."""
        now = self._monotime()
        if now - self._rate_limited.get(reason, float("-inf")) < RATE_LIMIT_SECONDS:
            return
        self._rate_limited[reason] = now
        self._diagnostics.append({"reason": reason, "at": _iso(_now_utc())})

    def _writer_loop(self) -> None:
        backoff = RETRY_BACKOFF_MIN
        last_space_check = 0.0
        while not self._stop.is_set():
            self._wake.wait(0.05)
            self._wake.clear()
            # drain continuously: one wait per idle gap, not per record
            while not self._stop.is_set():
                if self.state == "retention_full":
                    break  # wait for an archive ack to free capacity
                with self._lock:
                    descriptor = self._queue.popleft() if self._queue else None
                    if descriptor is not None:
                        if not self._queue:
                            self._oldest_pending_mono = None
                        body_len = len(descriptor.body_bytes) if descriptor.body_bytes is not None else 0
                        self._pending_body_bytes -= body_len
                if descriptor is None:
                    break
                self._writer_busy = True
                try:
                    written = self._write_row(descriptor)
                    if written:
                        backoff = RETRY_BACKOFF_MIN
                        # F02: TIME-driven durability — >= 1 s since the last
                        # fsync forces one even mid-stream.
                        self._maybe_fsync(force=False)
                except Exception as exc:
                    # F01: no nested locking, no requeue. The attempted
                    # record's outcome is ambiguous -> uncertain; later
                    # receipts may resume after bounded backoff (§4).
                    try:
                        if isinstance(exc, OSError) and getattr(exc, "errno", None) in (28, 13):
                            new_state = "suspended"
                            reason = "low_disk_space"
                        else:
                            new_state = "faulted"
                            reason = f"writer_io:{type(exc).__name__}"
                        with self._lock:
                            self._stats.write_failures += 1
                            self._stats.last_error_at = _iso(_now_utc())
                            self._stats.last_error_reason = reason
                            self._stats.uncertain_total += 1
                            self._stats.capture_failures += 1
                            self.capture_incomplete = True
                            self.state = new_state
                            self._note_diagnostic_locked(reason)
                    except Exception:
                        pass
                    self._sleep(backoff)
                    backoff = min(backoff * 2, RETRY_BACKOFF_MAX)
                    self._resume_backoff = backoff
                    self._resume_probe_at = self._monotime() + backoff
                    break
                finally:
                    self._writer_busy = False
            now = self._monotime()
            if now - last_space_check >= 1.0:
                last_space_check = now
                try:
                    self._check_disk_or_retention()
                except Exception:
                    pass  # disk/retention probing must never kill the writer
            # N1: bounded re-probe — a faulted writer periodically retries a
            # zero-cost write probe; success resumes capture (§4 transient
            # recovery). Disk-full/permission faults stay suspended until the
            # disk check observes headroom; the status surface reports both.
            self._maybe_resume_from_fault(now)
            # tail durability for the idle queue: fsync within the 1 s bound
            self._maybe_fsync(force=False)
        self._writer_busy = False
        self._seal_active("clean_close")

    def _maybe_resume_from_fault(self, now: float) -> None:
        """Recover from a transient writer fault by probing writability.

        O1: the rotation-reopen window is the only mid-run moment with
        ``_file is None``; a single transient failure of that reopen must not
        permanently disable capture. When faulted with no live handle the
        probe performs a BOUNDED reopen of the active file (same 100 ms -> 1 s
        backoff); success resumes capture with the resumes counter. With a
        live handle the probe is the zero-cost flush. Persistent failure keeps
        the sink faulted and retries every cycle.
        """
        with self._lock:
            if self.state != "faulted":
                return
            if now < self._resume_probe_at:
                return
            handle = self._file
        if handle is None:
            # rotation-reopen window: bounded reopen attempt
            try:
                reopened = open(self._active_path(), "ab", buffering=0)
            except Exception:
                with self._lock:
                    self._resume_probe_at = self._monotime() + self._resume_backoff
                    self._resume_backoff = min(self._resume_backoff * 2, RETRY_BACKOFF_MAX)
                return
            with self._lock:
                if self._file is None and self.state == "faulted":
                    self._file = reopened
                    self.state = "running"
                    self._stats.resumes += 1
                    self._note_diagnostic_locked("writer_reopened")
                    self._resume_backoff = RETRY_BACKOFF_MIN
                    self._resume_probe_at = 0.0
                else:  # a concurrent recovery already restored state
                    try:
                        reopened.close()
                    except OSError:
                        pass
            return
        try:
            # zero-cost writability probe: flush only touches a healthy handle
            handle.flush()
        except Exception:
            with self._lock:
                self._resume_probe_at = self._monotime() + self._resume_backoff
                self._resume_backoff = min(self._resume_backoff * 2, RETRY_BACKOFF_MAX)
            return
        with self._lock:
            self.state = "running"
            self._stats.resumes += 1
            self._note_diagnostic_locked("writer_resumed")
            self._resume_backoff = RETRY_BACKOFF_MIN
            self._resume_probe_at = 0.0

    def _maybe_fsync(self, *, force: bool) -> bool:
        """Time-driven fsync; durable advances ONLY on success (F05)."""
        now = self._monotime()
        if self._file is None:
            return False
        with self._lock:
            pending_rows = self._durable_append_index < self._visible_append_index
            due = now - self._last_fsync_mono >= FSYNC_INTERVAL_SECONDS
            retry_ok = now >= self._fsync_retry_at
        if not pending_rows or not (due or force) or not retry_ok:
            return False
        try:
            self._fsync(self._file.fileno())
        except OSError:
            with self._lock:
                self._stats.fsync_failures += 1
                self._stats.last_error_reason = "fsync_failed"
                self._note_diagnostic_locked("fsync_failed")
            self._fsync_retry_at = now + RETRY_BACKOFF_MIN
            return False
        with self._lock:
            self._last_fsync_mono = now
            self._last_fsync_lag = now - self._last_arrival_mono
            # N2: this fsync covers the CURRENT file, so its rows become
            # durable; the contiguous prefix cannot extend past a previously
            # sealed segment whose fsyncs failed (permanent lock below).
            self._segment_durable_through = self._visible_append_index
            if self._durable_locked_index is None:
                self._durable_append_index = self._visible_append_index
            else:
                self._durable_append_index = min(self._visible_append_index,
                                                 self._durable_locked_index)
            self._stats.durable_total = self._durable_append_index
        return True

    def _check_disk_or_retention(self) -> None:
        with self._lock:
            if self.state == "closed":
                return
            if not self._disk_headroom_ok():
                if self.state != "suspended":
                    self.state = "suspended"
                    self._stats.last_error_reason = "low_disk_space"
                    self._note_diagnostic_locked("low_disk_space")
                return
            if self.state == "suspended":
                self.state = "running"
            if self._enforce_retention(cap_rotation=False):
                if self.state == "retention_full":
                    self.state = "running"  # capacity freed by verified acks
            else:
                self.state = "retention_full"
                self._stats.last_error_reason = "retention_full"
                self._note_diagnostic_locked("retention_full")

    def _archive_ack_matches(self, ack_path: Path) -> str | None:
        """Return the segment_id whose sealed bytes are hash-verified by ack."""
        try:
            record = json.loads(ack_path.read_text(encoding="utf-8"))
            segment_id = record.get("segment_id")
            archive_sha = record.get("archive_sha256")
            if not isinstance(segment_id, str) or not isinstance(archive_sha, str):
                return None
            manifest_path = self.directory / f"raw-batches.{segment_id}.jsonl.manifest"
            if not manifest_path.exists():
                return None
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("file_sha256") != archive_sha:
                return None  # F04: ack must identify a verified byte-identical copy
            return segment_id
        except (OSError, ValueError):
            return None

    def _remove_eligible_segments(self) -> int:
        """Delete data files whose archive ack hash-verifies against the
        segment manifest. The manifest+ack remain as the removal record."""
        removed = 0
        for ack_path in list(self.directory.glob("raw-batches.*.jsonl.ack")):
            segment_id = self._archive_ack_matches(ack_path)
            if segment_id is None:
                continue
            data_file = self._sealed_path(segment_id)
            size = _safe_size(data_file)
            if size is None:
                continue
            try:
                data_file.unlink()
            except OSError:
                continue
            with self._lock:
                self._retained_bytes = max(0, self._retained_bytes - size)
                self._stats.segments_removed += 1
            removed += 1
        return removed

    def _enforce_retention(self, *, cap_rotation: bool) -> bool:
        """Return True when writing may continue under the segment/byte caps.

        cap_rotation=True blocks sealing a further segment when the total
        (sealed + the active file to be reopened) would exceed max_segments
        or max_total_bytes (F04); removable ack segments are deleted first.
        """
        self._remove_eligible_segments()
        sealed = self._sealed_files()
        sealed_bytes = self._sealed_bytes()
        with self._lock:
            active_bytes = self._segment_bytes
        if cap_rotation:
            # sealing adds 1 sealed file (the current active bytes) and we then
            # reopen a fresh active one: total files = sealed + 2
            if len(sealed) + 2 > self.max_segments:
                return False
            if sealed_bytes + active_bytes > self.max_total_bytes:
                return False
        else:
            if len(sealed) + 1 > self.max_segments:
                # admission check: sealed + active file on disk
                return False
            if sealed_bytes + active_bytes > self.max_total_bytes:
                return False
        return True

    def _write_row(self, descriptor: SinkDescriptor) -> bool:
        candidate_index = self._append_index + 1  # committed only on success (F05)
        row = {
            "schema": SCHEMA,
            "event": EVENT,
            "capture_run_id": self.capture_run_id,
            "segment_id": self._segment_id,
            "append_index": candidate_index,
            "instance_id": descriptor.instance_id,
            "sequence": descriptor.sequence,
            "endpoint": descriptor.endpoint,
            "batch_id": descriptor.batch_id,
            "exchange": descriptor.exchange,
            "instrument": descriptor.instrument,
            "synthetic": descriptor.synthetic,
            "source_header": descriptor.source_header,
            "received_at": descriptor.received_at,
            "receipt_at": descriptor.receipt_at,
            "clock_valid": descriptor.clock_valid,
            "decision": descriptor.decision,
            "blocked_reason": descriptor.blocked_reason,
            "tick_count": descriptor.tick_count,
            "payload_hash": descriptor.payload_hash,
            "body_kind": descriptor.body_kind,
            "body_bytes": len(descriptor.body_bytes) if descriptor.body_bytes is not None else None,
            "body_sha256": (hashlib.sha256(descriptor.body_bytes).hexdigest()
                            if descriptor.body_bytes is not None else None),
            "body_base64": (base64.b64encode(descriptor.body_bytes).decode("ascii")
                            if descriptor.body_bytes is not None else None),
            "payload": dict(descriptor.payload),
            "written_at": _iso(_now_utc()),
        }
        encoded = (json.dumps(row, ensure_ascii=True, allow_nan=False,
                              separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_ROW_BYTES:
            # F06: admitted-record capture failure; never also a drop, never
            # truncated, and no append index is consumed.
            with self._lock:
                self._stats.rows_oversize += 1
                self._stats.capture_failures += 1
                self._stats.failed_keys.append(
                    {"instance_id": descriptor.instance_id, "sequence": descriptor.sequence,
                     "reason": "row_oversize"})
                self._stats.last_error_reason = "row_oversize"
                self._note_diagnostic_locked("row_oversize")
            return False
        # rotation BEFORE the line would cross the segment cap, gated by the
        # total segment/byte caps (F04)
        if self._segment_bytes and self._segment_bytes + len(encoded) > self.max_segment_bytes:
            if not self._rotate():
                with self._lock:
                    self._stats.retention_refusals += 1
                    self._stats.capture_failures += 1
                    self._stats.failed_keys.append(
                        {"instance_id": descriptor.instance_id, "sequence": descriptor.sequence,
                         "reason": "retention_full"})
                    self.state = "retention_full"
                    self._note_diagnostic_locked("retention_full")
                return False
        if self._file is None:
            # rotation-reopen window: retry the reopen here so a single
            # transient failure cannot cascade into repeated fault-backoff
            # pushes (O1); a persistent failure still faults via the handler.
            self._file = open(self._active_path(), "ab", buffering=0)
        if self._file is None:
            raise OSError("writer file closed")
        written = self._file.write(encoded)
        if written is not None and written < len(encoded):
            raise OSError("short_write")
        self._file.flush()
        now = self._monotime()
        with self._lock:
            self._append_index = candidate_index
            self._visible_append_index = candidate_index
            self._stats.persisted_total += 1
            self._stats.last_success_at = _iso(_now_utc())
            self._segment_rows += 1
            self._segment_bytes += len(encoded)
            self._segment_ticks += descriptor.tick_count
            if self._segment_min_sequence is None or descriptor.sequence < self._segment_min_sequence:
                self._segment_min_sequence = descriptor.sequence
            if self._segment_max_sequence is None or descriptor.sequence > self._segment_max_sequence:
                self._segment_max_sequence = descriptor.sequence
            self._last_flush_lag = now - descriptor.admitted_at
            if self.state == "faulted":
                self.state = "running"  # transient recovery for later receipts
        return True

    def _rotate(self) -> bool:
        """Seal the active segment and open a fresh one under the caps.

        N3: when the seal's rename is refused (Windows sharing etc.) the
        rotation does NOT silently reset accounting -- bounded rename retries
        run, and on persistent refusal the sink keeps appending to the SAME
        physical file with the SAME segment identity/counters, so the eventual
        manifest describes every row in that file. The deviation is counted
        (rotation_failures) and latches capture_incomplete.
        """
        try:
            if not self._enforce_retention(cap_rotation=True):
                return False
            renamed = self._seal_active("rotation")
            if renamed:
                self._rotations += 1
                with self._lock:
                    self._segment_id = "seg-" + uuid.uuid4().hex[:12]
                    self._segment_start_index = self._append_index + 1
                    self._segment_rows = 0
                    self._segment_bytes = 0
                    self._segment_min_sequence = None
                    self._segment_max_sequence = None
                    self._segment_ticks = 0
                    self._segment_durable_through = self._durable_append_index
            else:
                # rename refused after retries: continue the SAME segment on
                # the SAME file; accounting is preserved (N3), deviation recorded
                with self._lock:
                    self._stats.rotation_failures += 1
                    self.capture_incomplete = True
                    self._note_diagnostic_locked("rotation_rename_refused")
            if not self._disk_headroom_ok():
                self.state = "suspended"
                return False
            self._file = open(self._active_path(), "ab", buffering=0)
            return True
        except OSError:
            with self._lock:
                self._stats.rotation_failures += 1
                self.state = "faulted"
                self._note_diagnostic_locked("rotation_failed")
            raise

    def _seal_active(self, cause: str) -> bool:
        """Flush, fsync, close and publish the active segment manifest.

        Returns True when the file was renamed to its sealed name; False when
        the rename was refused after bounded retries (the caller must then
        keep the same physical file and accounting, N3). Durable accounting
        advances ONLY on a successful fsync of THIS file (F05); a failed seal
        fsync permanently locks the durable prefix at this segment's last
        successful fsync (N2) -- rows beyond it can never be claimed durable
        by a later fsync of a different file.
        """
        with self._lock:
            file_handle = self._file
            segment_id = self._segment_id
            rows = self._segment_rows
            ticks = self._segment_ticks
            seg_bytes = self._segment_bytes
            start_index = self._segment_start_index
            end_index = self._append_index
            min_seq = self._segment_min_sequence
            max_seq = self._segment_max_sequence
            durable_through = self._segment_durable_through
            self._file = None
        if file_handle is None:
            return True
        incomplete_tail = None
        seal_fsynced = False
        try:
            file_handle.flush()
            self._fsync(file_handle.fileno())
            seal_fsynced = True
        except OSError:
            incomplete_tail = "seal_fsync_failed"
            with self._lock:
                self._stats.fsync_failures += 1
                self._note_diagnostic_locked("seal_fsync_failed")
        finally:
            try:
                file_handle.close()
            except OSError:
                pass
        # N2: durable prefix bookkeeping for the closing segment
        with self._lock:
            if seal_fsynced:
                self._segment_durable_through = end_index
                if self._durable_locked_index is None:
                    self._durable_append_index = max(self._durable_append_index, end_index)
                else:
                    self._durable_append_index = min(max(self._durable_append_index,
                                                         durable_through),
                                                      self._durable_locked_index)
            else:
                # permanently cap the contiguous durable prefix at this
                # segment's last successful fsync; later fsyncs of other
                # files can never cover these rows (H06 / N2)
                self._durable_locked_index = max(
                    self._durable_locked_index or 0, durable_through)
                self._durable_append_index = min(self._durable_append_index,
                                                 self._durable_locked_index)
            self._stats.durable_total = self._durable_append_index
        try:
            source = self._active_path()
            digest = hashlib.sha256()
            total = 0
            with open(source, "rb") as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
            # N3: bounded rename retries before giving the caller a refusal
            renamed = False
            last_error = None
            for attempt in range(3):
                try:
                    os.replace(source, self._sealed_path(segment_id))
                    renamed = True
                    break
                except PermissionError as exc:
                    last_error = exc
                    time.sleep(0.02 * (attempt + 1))
            if not renamed:
                with self._lock:
                    self._stats.rotation_failures += 1
                    self.capture_incomplete = True
                    self._note_diagnostic_locked("seal_rename_refused")
                return False
            with self._lock:
                self._retained_bytes += total
            manifest = {
                "schema": SCHEMA,
                "manifest_kind": "segment_manifest",
                "capture_run_id": self.capture_run_id,
                "segment_id": segment_id,
                "cause": cause,
                "file": self._sealed_path(segment_id).name,
                "file_bytes": total,
                "file_sha256": digest.hexdigest(),
                "row_count": rows,
                "tick_count": ticks,
                "append_index_interval": [start_index, end_index],
                "sequence_interval": [min_seq, max_seq],
                "durability": "fsynced" if seal_fsynced else "uncertain",
                "durable_through_append_index": durable_through,
                "incomplete_tail": incomplete_tail,
                "sealed_at": _iso(_now_utc()),
            }
            self._write_manifest(manifest)
            return True
        except OSError:
            with self._lock:
                self._stats.rotation_failures += 1
                self.capture_incomplete = True
                self._note_diagnostic_locked("seal_failed")
            return False

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        manifest_path = self.directory / f"{manifest['file']}.manifest"
        text = json.dumps(manifest, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if len(text) > MAX_MANIFEST_BYTES:
            text = json.dumps({**manifest, "truncated_fields": "manifest_too_large"},
                              ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        with open(manifest_path, "wb") as handle:
            handle.write(text)

def archive_acknowledge(directory: str | Path, segment_id: str, *, archive_sha256: str,
                        archive_location: str) -> bool:
    """Record an archive acknowledgment that permits future segment removal.

    Removal additionally requires the ack's archive_sha256 to equal the sealed
    segment manifest's file_sha256 (verified byte-identical copy, §4/F04).
    """
    directory = Path(directory)
    ack_path = directory / f"raw-batches.{segment_id}.jsonl.ack"
    record = {"schema": SCHEMA, "ack_kind": "archive",
              "segment_id": segment_id, "archive_sha256": archive_sha256,
              "archive_location": archive_location[:256],
              "acknowledged_at": _iso(_now_utc())}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        ack_path.write_text(json.dumps(record, ensure_ascii=True, separators=(",", ":")),
                            encoding="utf-8")
    except OSError:
        return False
    return True


__all__ = ["RawBatchSink", "SinkDescriptor", "SCHEMA", "EVENT", "archive_acknowledge"]
