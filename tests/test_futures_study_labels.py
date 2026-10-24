from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from ak_trading.futures.spec import ContractBar
from ak_trading.futures.study_labels import label_study_outcomes, summarize_study_outcomes
from ak_trading.futures.study_spec import StudyContract, StudySignal


DAY_NANO = 86_400_000_000_000
BASE_NANO = 1_700_000_000_000_000_000
OUTCOME_KEYS = {
    "contract_instance_id", "raw_symbol", "product", "strategy_id", "index", "horizon",
    "partition", "partition_start", "partition_end", "entry_index", "exit_index",
    "decision_datetime_nano", "entry_datetime_nano", "exit_datetime_nano",
    "candidate_direction", "feature_status", "label_status", "reason", "entry_open",
    "exit_close", "raw_change", "directional_change", "eligible_directional",
    "elapsed_calendar_days", "gap_status",
}


def contract(n=20, instance_id="instance-a", raw_symbol="SHFE.rb2501", product="SHFE.RB"):
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = tuple(
        ContractBar(
            symbol=raw_symbol, duration_seconds=86400,
            datetime_nano=BASE_NANO + i * DAY_NANO,
            ts=ts + timedelta(days=i), source="offline_fixture",
            open=Decimal("100"), high=Decimal("101"), low=Decimal("99"), close=Decimal("100"),
        )
        for i in range(n)
    )
    return StudyContract(instance_id, raw_symbol, product, ts + timedelta(days=365), bars)


def signal(item, index=0, direction=1, status="ready", strategy="test_strategy"):
    return StudySignal(
        item.contract_instance_id, item.raw_symbol, item.product, strategy,
        index, item.bars[index].datetime_nano, direction, status, "fixture_reason",
    )


def changed_bar(item, index, **changes):
    bars = list(item.bars)
    bars[index] = replace(bars[index], **changes)
    return replace(item, bars=tuple(bars))


def closing(item, index, value):
    value = Decimal(value)
    return changed_bar(item, index, close=value, high=max(Decimal(101), value), low=min(Decimal(99), value))


def product_group(summary):
    return next(group for group in summary["groups"] if group["level"] == "product")


def test_horizon_one_is_next_open_to_same_next_close_and_exact_columns():
    item = closing(contract(), 1, "125")
    item = closing(item, 2, "170")
    row, = label_study_outcomes([item], [signal(item)], horizons=(1,))
    assert set(row) == OUTCOME_KEYS
    assert (row["index"], row["entry_index"], row["exit_index"]) == (0, 1, 1)
    assert row["entry_datetime_nano"] == row["exit_datetime_nano"] == item.bars[1].datetime_nano
    assert row["decision_datetime_nano"] == item.bars[0].datetime_nano
    assert row["entry_open"] == "100"
    assert row["exit_close"] == "125"
    assert row["raw_change"] == row["directional_change"] == 0.25
    assert row["eligible_directional"] is True
    assert row["elapsed_calendar_days"] == 1.0
    assert row["gap_status"] == "no_calendar_gap"
    json.dumps(row, allow_nan=False)


@pytest.mark.parametrize("index,partition,start,end,status", [
    (0, "train", 0, 6, "valid"),
    (4, "train", 0, 6, "valid"),
    (5, "train", 0, 6, "boundary"),
    (6, "development", 6, 8, "valid"),
    (7, "development", 6, 8, "boundary"),
    (8, "holdout", 8, 10, "valid"),
    (9, "holdout", 8, 10, "unmatured"),
])
def test_half_open_partitions_require_decision_entry_exit_together(index, partition, start, end, status):
    item = contract(10)
    row, = label_study_outcomes([item], [signal(item, index)], horizons=(1,))
    assert (row["partition"], row["partition_start"], row["partition_end"]) == (partition, start, end)
    assert row["label_status"] == status
    if status != "valid":
        assert row["entry_open"] is row["exit_close"] is None
        assert row["raw_change"] is row["directional_change"] is row["elapsed_calendar_days"] is None
        assert row["eligible_directional"] is False
    if status == "boundary":
        assert row["reason"] == "cross_partition"
    if status == "unmatured":
        assert row["entry_index"] == row["exit_index"] == 10
        assert row["entry_datetime_nano"] is row["exit_datetime_nano"] is None


def test_integer_partition_cuts_and_empty_partitions():
    item = contract(7)
    row, = label_study_outcomes([item], [signal(item, 4)], horizons=(1,))
    assert (row["partition"], row["partition_start"], row["partition_end"]) == ("development", 4, 5)
    assert row["label_status"] == "boundary"
    item = contract(1)
    row, = label_study_outcomes([item], [signal(item)], horizons=(1,))
    assert (row["partition"], row["partition_start"], row["partition_end"]) == ("holdout", 0, 1)
    assert row["label_status"] == "unmatured"


def test_every_signal_horizon_retained_and_original_indexes_never_compressed():
    item = changed_bar(contract(), 3, close=None)
    signals = [signal(item, i) for i in range(20)]
    rows = label_study_outcomes([item], signals)
    assert isinstance(rows, tuple)
    assert len(rows) == 60
    assert [(r["index"], r["horizon"]) for r in rows] == [(i, h) for i in range(20) for h in (1, 5, 20)]
    lookup = {(r["index"], r["horizon"]): r for r in rows}
    assert lookup[0, 1]["label_status"] == "valid"
    assert lookup[0, 5]["label_status"] == "invalid"
    assert lookup[0, 5]["exit_index"] == 5
    assert lookup[2, 1]["label_status"] == lookup[3, 1]["label_status"] == "invalid"
    assert lookup[4, 1]["label_status"] == "valid"
    assert lookup[4, 1]["entry_index"] == lookup[4, 1]["exit_index"] == 5
    assert lookup[19, 20]["exit_index"] == 39
    assert lookup[19, 20]["label_status"] == "unmatured"


@pytest.mark.parametrize("path_index", range(6))
@pytest.mark.parametrize("changes,reason", [
    ({"high": None}, "missing_ohlc"),
    ({"low": Decimal("NaN")}, "non_finite_ohlc"),
    ({"open": float("inf")}, "non_finite_ohlc"),
    ({"close": Decimal("-Infinity")}, "non_finite_ohlc"),
    ({"high": Decimal("99")}, "invalid_ohlc_order"),
    ({"low": Decimal("102")}, "invalid_ohlc_order"),
    ({"open": True}, "invalid_ohlc_type"),
])
def test_entire_inclusive_path_requires_valid_ohlc(path_index, changes, reason):
    item = changed_bar(contract(), path_index, **changes)
    row, = label_study_outcomes([item], [signal(item)], horizons=(5,))
    assert row["label_status"] == "invalid"
    assert row["reason"] == reason
    assert row["raw_change"] is row["directional_change"] is None
    assert row["entry_open"] is row["exit_close"] is None
    assert row["eligible_directional"] is False
    json.dumps(row, allow_nan=False)


def test_consistent_negative_terminal_allowed_but_inconsistent_negative_rejected():
    item = closing(contract(), 1, "-25")
    row, = label_study_outcomes([item], [signal(item, direction=-1)], horizons=(1,))
    assert row["label_status"] == "valid"
    assert row["exit_close"] == "-25"
    assert row["raw_change"] == -1.25
    assert row["directional_change"] == 1.25
    bad = changed_bar(item, 1, low=Decimal("1"))
    bad_row, = label_study_outcomes([bad], [signal(bad)], horizons=(1,))
    assert bad_row["reason"] == "invalid_ohlc_order"
    assert bad_row["raw_change"] is None


@pytest.mark.parametrize("value", ["0", "-1"])
def test_entry_open_must_be_positive_even_when_ohlc_consistent(value):
    item = changed_bar(contract(), 1, open=Decimal(value), low=Decimal(value))
    row, = label_study_outcomes([item], [signal(item)], horizons=(1,))
    assert row["label_status"] == "invalid"
    assert row["reason"] == "non_positive_entry_open"
    assert row["raw_change"] is None


def test_nonfinite_change_is_null_and_extreme_finite_price_text_is_preserved():
    item = changed_bar(contract(), 1, open=Decimal("1e-400"), low=Decimal("1e-400"))
    row, = label_study_outcomes([item], [signal(item)], horizons=(1,))
    assert row["reason"] == "non_finite_change"
    assert row["raw_change"] is row["entry_open"] is None
    item = changed_bar(contract(), 1, open=Decimal("1e400"), low=Decimal("1e400"), high=Decimal("2e400"), close=Decimal("2e400"))
    row, = label_study_outcomes([item], [signal(item)], horizons=(1,))
    assert row["label_status"] == "valid"
    assert row["entry_open"] == "1E+400"
    assert row["raw_change"] == 1.0
    json.dumps(summarize_study_outcomes([row]), allow_nan=False)


def test_gap_is_timestamp_description_without_compressing_observations():
    item = contract()
    bars = tuple(replace(bar, datetime_nano=bar.datetime_nano + (2 * DAY_NANO if i >= 3 else 0)) for i, bar in enumerate(item.bars))
    item = replace(item, bars=bars)
    row, = label_study_outcomes([item], [signal(item)], horizons=(5,))
    assert row["label_status"] == "valid"
    assert row["gap_status"] == "calendar_gap"
    assert row["elapsed_calendar_days"] == 7.0
    assert row["exit_index"] == 5


def test_summary_eligible_denominator_same_event_baseline_flat_null_and_unready():
    item = contract()
    for index, close in enumerate(("120", "80", "200", "400", "0", "100"), 1):
        item = closing(item, index, close)
    signals = [
        signal(item, 0, 1), signal(item, 1, -1), signal(item, 2, 0),
        signal(item, 3, None, "warmup"), signal(item, 4, 1, "unavailable"),
        signal(item, 5, 1),
    ]
    rows = label_study_outcomes([item], signals, horizons=(1,))
    assert all(row["label_status"] == "valid" for row in rows)
    assert [row["eligible_directional"] for row in rows] == [True, True, False, False, False, True]
    assert all(rows[i]["directional_change"] is None for i in (2, 3, 4))
    summary = summarize_study_outcomes(rows)
    group = product_group(summary)
    counts = summary["counts"]
    assert counts["n_total"] == counts["n_valid"] == 6
    assert counts["n_eligible"] == group["eligible_n"] == 3
    assert counts["n_flat"] == counts["n_unavailable"] == 1
    assert counts["n_feature_not_ready"] == 2
    assert counts["label_status_counts"] == {"valid": 6}
    assert counts["reason_counts"] == {"ok": 6}
    assert group["groupkeys"] == {"product": "SHFE.RB", "strategy_id": "test_strategy", "horizon": 1, "partition": "train"}
    candidate, baseline = group["candidate"], group["buy_direction_baseline"]
    assert candidate["n"] == baseline["n"] == 3
    assert candidate["mean"] == pytest.approx(0.4 / 3)
    assert candidate["median"] == 0.2
    assert candidate["success_rate"] == 2 / 3
    assert (candidate["success_count"], candidate["tie_count"], candidate["loss_count"]) == (2, 1, 0)
    assert baseline["mean"] == baseline["median"] == 0.0
    assert (baseline["success_count"], baseline["tie_count"], baseline["loss_count"]) == (1, 1, 1)
    assert baseline["success_rate"] == baseline["tie_rate"] == baseline["loss_rate"] == 1 / 3
    assert group["paired_mean_difference"] == pytest.approx(0.4 / 3)
    assert group["contribution_contracts"] == [{"contract_instance_id": "instance-a", "raw_symbol": "SHFE.rb2501", "n_total": 6, "n_valid": 6, "n_eligible": 3}]
    assert summary["methodology"]["overlapping_labels"] is True
    json.dumps(summary, allow_nan=False)


def test_product_summary_weights_events_and_keeps_contract_instances_separate():
    a = closing(closing(contract(instance_id="a"), 1, "110"), 2, "110")
    b = closing(contract(instance_id="b"), 1, "50")
    rows = label_study_outcomes([a, b], [signal(a, 0), signal(a, 1), signal(b, 0)], horizons=(1,))
    summary = summarize_study_outcomes(rows)
    group = product_group(summary)
    assert group["candidate"]["mean"] == pytest.approx(-0.1)
    assert group["candidate"]["mean"] != pytest.approx(-0.2)
    assert group["counts"]["n_contributing_contracts"] == 2
    assert [r["n_eligible"] for r in group["contribution_contracts"]] == [2, 1]
    assert len([g for g in summary["groups"] if g["level"] == "contract"]) == 2
    assert summarize_study_outcomes(reversed(rows)) == summary


def test_partition_cuts_are_contract_local_even_with_same_raw_symbol():
    a, b = contract(10, "a"), contract(20, "b")
    a_row, b_row = label_study_outcomes([a, b], [signal(a, 5), signal(b, 5)], horizons=(1,))
    assert a_row["partition_end"] == 6 and a_row["label_status"] == "boundary"
    assert b_row["partition_end"] == 12 and b_row["label_status"] == "valid"


def test_empty_and_no_eligible_cohorts_have_null_statistics():
    assert label_study_outcomes([], []) == ()
    assert label_study_outcomes([contract(0)], []) == ()
    summary = summarize_study_outcomes([])
    assert summary["groups"] == []
    assert summary["counts"]["n_total"] == summary["counts"]["n_eligible"] == 0
    item = contract()
    rows = label_study_outcomes([item], [signal(item, direction=None)], horizons=(1,))
    group = product_group(summarize_study_outcomes(rows))
    for name in ("candidate", "buy_direction_baseline"):
        assert group[name]["n"] == 0
        assert all(group[name][key] is None for key in ("mean", "median", "success_rate", "tie_rate", "loss_rate"))
    assert group["paired_mean_difference"] is None
    assert group["counts"]["n_valid"] == 1
    assert group["counts"]["n_contributing_contracts"] == 0
    json.dumps(summary, allow_nan=False)
    json.dumps(group, allow_nan=False)


@pytest.mark.parametrize("horizon", [True, False, 1.0, Decimal(5), "1", 0, -1, 2, 21, None])
def test_only_fixed_integer_horizons_allowed(horizon):
    with pytest.raises(ValueError, match="horizons"):
        label_study_outcomes([], [], horizons=(horizon,))


@pytest.mark.parametrize("changes", [
    {"contract_instance_id": "unknown"}, {"raw_symbol": "SHFE.rb9999"},
    {"product": "DCE.I"}, {"index": -1}, {"index": 20}, {"index": True},
    {"index": 0.0}, {"datetime_nano": BASE_NANO + 1}, {"datetime_nano": True},
    {"datetime_nano": float(BASE_NANO)}, {"candidate_direction": True},
    {"candidate_direction": False}, {"candidate_direction": 1.0},
    {"candidate_direction": 2},
])
def test_signal_identity_indexes_nano_and_direction_are_validated(changes):
    item = contract()
    with pytest.raises(ValueError):
        label_study_outcomes([item], [replace(signal(item), **changes)])


def test_duplicate_identities_rejected_but_distinct_strategies_allowed():
    item = contract()
    with pytest.raises(ValueError, match="duplicate contract"):
        label_study_outcomes([item, item], [])
    with pytest.raises(ValueError, match="duplicate signal"):
        label_study_outcomes([item], [signal(item), signal(item)])
    with pytest.raises(ValueError, match="duplicate horizons"):
        label_study_outcomes([item], [signal(item)], horizons=(1, 1))
    rows = label_study_outcomes([item], [signal(item), signal(item, strategy="second")], horizons=(1,))
    assert len(rows) == 2
    with pytest.raises(ValueError, match="duplicate outcome"):
        summarize_study_outcomes([rows[0], rows[0]])
    duplicate_clock = changed_bar(item, 1, datetime_nano=item.bars[0].datetime_nano)
    with pytest.raises(ValueError, match="strictly increasing"):
        label_study_outcomes([duplicate_clock], [])
    wrong_symbol = changed_bar(item, 2, symbol="DCE.i2501")
    with pytest.raises(ValueError, match="raw_symbol"):
        label_study_outcomes([wrong_symbol], [])


def test_json_safe_summary_handles_large_finite_values_without_overflowing_mean():
    item = changed_bar(contract(), 1, high=Decimal("1e310"), close=Decimal("1e310"))
    item = changed_bar(item, 2, high=Decimal("1e310"), close=Decimal("1e310"))
    rows = label_study_outcomes([item], [signal(item, 0, -1), signal(item, 1, -1)], horizons=(1,))
    group = product_group(summarize_study_outcomes(rows))
    assert group["candidate"]["mean"] == group["candidate"]["median"] == -1e308
    assert group["paired_mean_difference"] is None
    assert group["paired_mean_difference_status"] == "non_finite"
    json.dumps(group, allow_nan=False)
