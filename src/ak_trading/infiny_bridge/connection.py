"""Pure connection timing and correlated hold-reply evidence; no native imports."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import threading
import time
from typing import Any, Mapping


@dataclass(frozen=True)
class TimingPolicy:
    link_ttl: float = 15.0
    packet_ttl: float = 10.0
    quote_ttl: float = 3.0
    account_ttl: float = 5.0
    position_ttl: float = 5.0
    contract_ttl: float = 3600.0
    future_skew: float = 2.0

    def __post_init__(self):
        for value in vars(self).values():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("invalid timing policy")


def utc_time(value: Any) -> datetime | None:
    """Require an explicit calendar time and timezone, never infer trading dates."""
    try:
        if isinstance(value, str) and len(value) <= 64:
            text = value.strip()
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            value = datetime.fromisoformat(text)
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def clock_sample(clock: Any) -> tuple[datetime | None, float | None]:
    try:
        wall = utc_time(clock.now())
    except Exception:
        wall = None
    try:
        raw = clock.monotonic()
        mono = float(raw) if not isinstance(raw, bool) else None
        if mono is not None and (not math.isfinite(mono) or mono < 0):
            mono = None
    except Exception:
        mono = None
    return wall, mono


def time_freshness(at: Any, now: Any, ttl: float, future_skew: float = 2.0) -> dict[str, Any]:
    observed, current = utc_time(at), utc_time(now)
    age = (current - observed).total_seconds() if observed is not None and current is not None else None
    status = "unknown" if age is None else "future" if age < -future_skew else "stale" if age > ttl else "fresh"
    return {"status": status, "observed_at": observed.isoformat().replace("+00:00", "Z") if observed else None,
            "age_seconds": age, "stale_after_seconds": ttl}


class _Clock:
    def now(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()


class ConnectionMonitor:
    """Bounded local request correlation, independent of snapshot readiness.

    Call start on a lifecycle change; note_request also recognizes a new epoch.
    A reply needs the original local request context, not fields supplied by a
    remote peer. No result here proves a native account or enables execution.
    """

    def __init__(self, *, clock=None, timing: TimingPolicy | None = None, capacity=64):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 256:
            raise ValueError("invalid connection capacity")
        self.clock = clock or _Clock()
        self.timing = timing or TimingPolicy()
        self.capacity = capacity
        self._lock = threading.RLock()
        self._identity = None
        self._requests = OrderedDict()
        self._latest = 0
        self._accepted = 0
        self._success = None
        self._active = True

    def start(self, instance_id, session_epoch):
        with self._lock:
            self._identity = (instance_id, session_epoch)
            self._requests.clear()
            self._latest = self._accepted = 0
            self._success = None
            self._active = True

    def stop(self):
        with self._lock:
            self._active = False
            self._requests.clear()
            self._success = None

    def note_request(self, meta: Mapping[str, Any]) -> bool:
        with self._lock:
            identity = (meta.get("instance_id"), meta.get("session_epoch"))
            sequence, request_id = meta.get("sequence"), meta.get("request_id")
            if not self._active or not all(isinstance(item, str) and 0 < len(item) <= 128 for item in (*identity, request_id)):
                return False
            if type(sequence) is not int or not 1 <= sequence <= 2 ** 53 - 1:
                return False
            if identity != self._identity:
                # Lifecycle switching is explicit start() only; a late request
                # from a superseded epoch must never revive that epoch.
                return False
            if sequence <= self._latest:
                return False
            self._latest = sequence
            self._requests[request_id] = (dict(meta), clock_sample(self.clock))
            while len(self._requests) > self.capacity:
                self._requests.popitem(last=False)
            return True

    def note_response(self, meta: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
        with self._lock:
            if not self._active or (meta.get("instance_id"), meta.get("session_epoch")) != self._identity:
                return False
            request = self._requests.get(meta.get("request_id"))
            if request is None or request[0] != dict(meta) or meta.get("sequence", 0) <= self._accepted:
                return False
            if (not isinstance(result, Mapping) or set(result) != {"signal", "reason"}
                    or result["signal"] != "hold" or not isinstance(result["reason"], str)
                    or not 0 < len(result["reason"]) <= 20 or any(ord(c) < 32 for c in result["reason"])):
                return False
            wall, mono = clock_sample(self.clock)
            request_wall, request_mono = request[1]
            if wall is None or mono is None or request_wall is None or request_mono is None:
                return False
            elapsed = mono - request_mono
            wall_age = (wall - request_wall).total_seconds()
            packet = time_freshness(meta.get("sent_at"), wall, self.timing.packet_ttl, self.timing.future_skew)
            if not 0 <= elapsed <= self.timing.packet_ttl or wall_age < 0 or packet["status"] != "fresh":
                return False
            self._accepted = meta["sequence"]
            self._success = (wall, mono)
            self._requests.pop(meta["request_id"], None)
            return True

    def status(self, now=None) -> dict[str, Any]:
        with self._lock:
            wall, mono = clock_sample(self.clock)
            if now is not None:
                wall = utc_time(now)
            age = None
            state = "offline" if not self._active or self._identity is None else "service_only"
            if self._active and self._success is not None:
                at, received_mono = self._success
                wall_age = (wall - at).total_seconds() if wall is not None else None
                mono_age = mono - received_mono if mono is not None else None
                valid = wall_age is not None and mono_age is not None and min(wall_age, mono_age) >= 0
                age = max(wall_age, mono_age) if valid else None
                state = "roundtrip" if age is not None and age <= self.timing.link_ttl else "stale"
            return {"connection": state, "age_seconds": age, "stale_after_seconds": self.timing.link_ttl,
                    "instance_id": self._identity[0] if self._identity else None,
                    "session_epoch": self._identity[1] if self._identity else None,
                    "sequence": self._accepted, "client_verified": False, "execution_enabled": False}
