from __future__ import annotations

from dataclasses import replace

from ak_trading.probability.contracts import FactorRow
from ak_trading.probability.partition import build_partition_plan, partition_event


def event(timestamp):
    return FactorRow(str(timestamp), "contract", "SHFE.rb2101", "SHFE.RB", 0, timestamp, {}, "ready", "", "middle", "range")


def test_global_time_partition_purges_labels_crossing_boundary():
    plan = build_partition_plan(range(100), train_fraction=0.6, calibration_fraction=0.2)
    assert (plan.train_end_nano, plan.calibration_end_nano) == (60, 80)
    assert partition_event(event(58), 59, plan) == "train"
    assert partition_event(event(59), 60, plan) == "purged"
    assert partition_event(event(60), 79, plan) == "calibration"
    assert partition_event(event(79), 80, plan) == "purged"
    assert partition_event(event(80), 99, plan) == "test"
    assert partition_event(event(99), None, plan) == "unmatured"


def test_same_market_time_has_same_partition_across_contracts():
    plan = build_partition_plan([*range(100), *range(100)])
    other = replace(event(61), contract_id="other", symbol="SHFE.rb2201", index=500)
    assert partition_event(event(61), 70, plan) == partition_event(other, 70, plan) == "calibration"
