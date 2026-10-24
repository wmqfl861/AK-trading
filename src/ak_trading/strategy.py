from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Protocol


@dataclass(frozen=True)
class SignalDecision:
    target_position: int | None
    reason: str
    fast_ma: float | None = None
    slow_ma: float | None = None


class CompletedBarStrategy(Protocol):
    """Runtime-neutral structural protocol for strategies that consume completed closes.

    Exposes required_bars (positive int) and evaluate(completed_closes) returning SignalDecision.
    """

    @property
    def required_bars(self) -> int:
        ...

    def evaluate(self, completed_closes: Iterable[object]) -> SignalDecision:
        ...


@dataclass(frozen=True)
class DualMAStrategy:
    """Long/flat dual-MA signal over completed close prices only."""

    fast_window: int = 5
    slow_window: int = 20

    def __post_init__(self) -> None:
        if isinstance(self.fast_window, bool) or not isinstance(self.fast_window, int):
            raise ValueError("fast_window must be a positive integer")
        if isinstance(self.slow_window, bool) or not isinstance(self.slow_window, int):
            raise ValueError("slow_window must be a positive integer")
        if self.fast_window <= 0 or self.slow_window <= 0:
            raise ValueError("MA windows must be positive")
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be smaller than slow_window")

    @property
    def required_bars(self) -> int:
        return self.slow_window

    def evaluate(self, completed_closes: Iterable[object]) -> SignalDecision:
        """Return a target using only closes from bars that have already closed."""
        raw_values = list(completed_closes)
        if len(raw_values) < self.required_bars:
            return SignalDecision(None, "insufficient_history")

        window = raw_values[-self.slow_window :]
        try:
            closes = [float(value) for value in window]
        except (TypeError, ValueError):
            return SignalDecision(None, "invalid_close")
        if not all(math.isfinite(value) for value in closes):
            return SignalDecision(None, "invalid_close")

        fast_ma = math.fsum(closes[-self.fast_window :]) / self.fast_window
        slow_ma = math.fsum(closes) / self.slow_window
        if fast_ma > slow_ma:
            return SignalDecision(1, "fast_above_slow", fast_ma, slow_ma)
        return SignalDecision(0, "fast_not_above_slow", fast_ma, slow_ma)
