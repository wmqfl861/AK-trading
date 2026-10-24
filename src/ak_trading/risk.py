from __future__ import annotations

from dataclasses import dataclass
from numbers import Integral


@dataclass(frozen=True)
class RiskDecision:
    requested_target: int | None
    target_position: int | None
    reason: str


def require_positive_position_limit(value: object, *, name: str = "max_abs_position") -> int:
    """Return a validated integer position limit for CLI and runner boundaries."""
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} must be a positive integer")
    limit = int(value)
    if limit <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return limit


def limit_target_position(
    requested_target: int | None,
    *,
    max_abs_position: int = 1,
    allow_short: bool = False,
) -> RiskDecision:
    """Apply the MVP position boundary without inventing account sizing rules."""
    limit = require_positive_position_limit(max_abs_position)

    if requested_target is None:
        return RiskDecision(None, None, "no_signal")
    if isinstance(requested_target, bool) or not isinstance(requested_target, Integral):
        raise ValueError("requested_target must be an integer or None")

    requested = int(requested_target)
    lower_bound = -limit if allow_short else 0
    target = min(limit, max(lower_bound, requested))
    if target == requested:
        return RiskDecision(requested, target, "allowed")
    if requested < lower_bound:
        reason = "short_blocked_to_flat" if not allow_short else "clamped_min"
    else:
        reason = "clamped_max"
    return RiskDecision(requested, target, reason)
