from __future__ import annotations

import math
import time
from typing import Any, Callable


def is_valid_price(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _require_positive_finite(name: str, value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return number


def wait_until(
    api: Any,
    predicate: Callable[[], bool],
    *,
    timeout: float,
    poll: float = 0.5,
    on_update: Callable[[], None] | None = None,
) -> bool:
    """Drive live updates to a wall-clock deadline; never use as a backtest loop."""
    timeout = _require_positive_finite("timeout", timeout)
    poll = _require_positive_finite("poll", poll)
    deadline_mono = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        remaining = deadline_mono - time.monotonic()
        if remaining <= 0:
            return False
        step = min(poll, remaining)
        # TqSdk wait_update deadline uses wall-clock time.time().
        api.wait_update(deadline=time.time() + step)
        if on_update is not None:
            on_update()
        if predicate():
            return True
        if time.monotonic() >= deadline_mono:
            return False
