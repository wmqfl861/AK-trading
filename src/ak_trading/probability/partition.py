"""Global raw-time splits with explicit full-window purging."""
from __future__ import annotations

import math
from typing import Iterable

from ak_trading.probability.contracts import FactorRow, PartitionPlan


def build_partition_plan(
    timestamps: Iterable[int], *, train_fraction: float = 0.6, calibration_fraction: float = 0.2
) -> PartitionPlan:
    for value in (train_fraction, calibration_fraction):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value < 1:
            raise ValueError("partition fractions must be finite in (0, 1)")
    if train_fraction + calibration_fraction >= 1:
        raise ValueError("partitions must retain a test window")
    raw = list(timestamps)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in raw):
        raise ValueError("timestamps must be integer raw nanoseconds")
    ordered = sorted(set(raw))
    a, b = int(len(ordered) * train_fraction), int(len(ordered) * (train_fraction + calibration_fraction))
    if not 0 < a < b < len(ordered):
        raise ValueError("not enough distinct observations for three time partitions")
    return PartitionPlan(ordered[a], ordered[b], ordered[-1], (train_fraction, calibration_fraction))


def partition_event(event: FactorRow, terminal_nano: int | None, plan: PartitionPlan) -> str:
    if terminal_nano is None:
        return "unmatured"
    if isinstance(terminal_nano, bool) or not isinstance(terminal_nano, int) or terminal_nano <= event.timestamp_nano:
        raise ValueError("terminal time must follow the event")
    if terminal_nano > plan.maximum_nano or event.timestamp_nano > plan.maximum_nano:
        return "outside_span"
    if event.timestamp_nano < plan.train_end_nano:
        return "train" if terminal_nano < plan.train_end_nano else "purged"
    if event.timestamp_nano < plan.calibration_end_nano:
        return "calibration" if terminal_nano < plan.calibration_end_nano else "purged"
    return "test"
