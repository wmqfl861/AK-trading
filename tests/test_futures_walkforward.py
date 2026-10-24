"""Offline tests for window boundaries, opaque replay results, and OOS aggregation."""
from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from ak_trading.futures.validation import build_folds
from ak_trading.futures.walkforward import (
    ReplayWindow,
    WalkForwardResult,
    WindowResult,
    aggregate_oos_ledgers,
    run_walk_forward,
)


def weekdays(count: int) -> list[date]:
    days: list[date] = []
    cursor = date(2020, 1, 1)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


@pytest.mark.parametrize("count", [0, 1, 440])
def test_insufficient_history_never_invokes_callback(count: int) -> None:
    called = []
    result = run_walk_forward(weekdays(count), called.append)
    assert result.status == "insufficient_history"
    assert result.reason == f"n={count}<441"
    assert result.folds == build_folds(weekdays(count))
    assert result.results == ()
    assert called == []


@pytest.mark.parametrize("invalid", [datetime(2020, 1, 1), "2020-01-01", None, 1])
def test_requires_dates_even_when_history_is_insufficient(invalid: object) -> None:
    called = []
    with pytest.raises(TypeError, match="must be a date"):
        run_walk_forward([invalid], called.append)
    assert called == []


@pytest.mark.parametrize("duplicate", [True, False])
def test_rejects_duplicate_or_unsorted_dates_before_any_callback(duplicate: bool) -> None:
    days = weekdays(441)
    if duplicate:
        days[-1] = days[-2]
    else:
        days[-2], days[-1] = days[-1], days[-2]
    called = []
    with pytest.raises(ValueError, match="strictly ascending and unique"):
        run_walk_forward(days, called.append)
    assert called == []


def test_exact_441_day_layout_and_callback_order() -> None:
    days = weekdays(441)
    called: list[ReplayWindow] = []

    def replay(window: ReplayWindow) -> str:
        called.append(window)
        return window.window_id

    result = run_walk_forward(days, replay)
    assert result.status == "executed" and result.reason is None
    assert [item.window for item in result.results] == called
    assert [item.payload for item in result.results] == [
        "development/fold-01/train", "development/fold-01/oos", "final_holdout",
    ]
    train, oos, holdout = called
    assert train == ReplayWindow("development_train", 1, tuple(days[:252]), ())
    assert oos == ReplayWindow("development_oos", 1, tuple(days[252:315]), tuple(days[:252]))
    assert holdout == ReplayWindow("final_holdout", None, tuple(days[315:]), tuple(days[63:315]))


@pytest.mark.parametrize("count", [442, 500, 630, 1260])
def test_right_alignment_retention_causal_prefix_and_holdout_separation(count: int) -> None:
    days = weekdays(count)
    result = run_walk_forward(days, lambda window: window.window_id)
    folds = build_folds(days)
    assert result.folds == folds
    retained = [fold for fold in folds.folds if fold.retained]
    assert len(result.results) == len(retained) * 2 + 1
    assert len(retained) <= 8
    assert len({item.window.window_id for item in result.results}) == len(result.results)
    holdout_start = days[-126]
    development_oos: list[date] = []
    for index, fold in enumerate(retained):
        train, oos = (item.window for item in result.results[index * 2:index * 2 + 2])
        assert train.kind == "development_train" and oos.kind == "development_oos"
        assert train.fold_index == oos.fold_index == fold.index
        assert train.days == fold.train and oos.days == fold.oos
        train_start = days.index(fold.train[0])
        assert train.warmup_days == tuple(days[max(0, train_start - 252):train_start])
        assert len(train.warmup_days) <= 252
        assert oos.warmup_days == fold.train
        development_oos.extend(oos.days)
        for window in (train, oos):
            assert all(day < window.days[0] for day in window.warmup_days)
            assert set(window.days).isdisjoint(window.warmup_days)
            assert all(day < holdout_start for day in window.days + window.warmup_days)
    assert development_oos == sorted(set(development_oos))
    assert development_oos[-1] == days[-127]
    holdout = result.results[-1].window
    assert holdout.kind == "final_holdout" and holdout.fold_index is None
    assert holdout.days == tuple(days[-126:])
    assert holdout.warmup_days == tuple(days[-378:-126])
    assert set(holdout.days).isdisjoint(holdout.warmup_days)
    assert sum(item.window.kind == "final_holdout" for item in result.results) == 1
    if count == 1260:
        # Dropped folds retain their original indices in the split document;
        # callbacks never execute or renumber them.
        assert folds.dropped_k
        assert result.results[0].window.fold_index == 7
        assert len(result.results[0].window.warmup_days) == 252


def test_each_callback_result_and_state_are_kept_separate_without_payload_mutation() -> None:
    payloads: list[dict] = []

    def replay(window: ReplayWindow) -> dict:
        state = {"cash": Decimal("100"), "fills": []}
        state["fills"].append(window.days[0])
        payload = {"status": "incomplete", "account": state}
        payloads.append(payload)
        return payload

    result = run_walk_forward(weekdays(630), replay)
    expected = copy.deepcopy(payloads)
    assert result.status == "executed"  # Does not claim ledger completion.
    assert all(item.payload is payload for item, payload in zip(result.results, payloads))
    assert len({id(payload["account"]) for payload in payloads}) == len(payloads)
    assert all(payload["account"]["cash"] == Decimal("100") for payload in payloads)
    assert all(len(payload["account"]["fills"]) == 1 for payload in payloads)
    assert payloads == expected
    payloads[0]["account"]["fills"].append("first account only")
    assert all(len(item.payload["account"]["fills"]) == 1 for item in result.results[1:])


def test_window_schedule_is_frozen_before_callback_and_preserves_inputs() -> None:
    days = weekdays(500)
    original = days.copy()
    run_walk_forward(days, lambda window: {"days": window.days})
    assert days == original
    invoked = []

    def replay(window: ReplayWindow) -> None:
        invoked.append(window)
        days.clear()  # A host-side mutation cannot alter scheduled windows.

    result = run_walk_forward(days, replay)
    assert result.folds == build_folds(original)
    assert result.results[-1].window.days == tuple(original[-126:])
    assert invoked == [item.window for item in result.results]
    assert all(isinstance(item.window.days, tuple) for item in result.results)


def test_window_and_result_records_are_frozen() -> None:
    result = run_walk_forward(weekdays(441), lambda window: {"id": window.window_id})
    assert isinstance(result, WalkForwardResult)
    assert isinstance(result.results[0], WindowResult)
    with pytest.raises(FrozenInstanceError):
        result.results[0].window.kind = "final_holdout"
    with pytest.raises(FrozenInstanceError):
        result.results[0].payload = {}
    with pytest.raises(FrozenInstanceError):
        result.status = "complete"


def test_callback_failure_propagates_without_running_holdout() -> None:
    invoked = []

    def replay(window: ReplayWindow) -> None:
        invoked.append(window.kind)
        if window.kind == "development_oos":
            raise RuntimeError("failed ledger")

    with pytest.raises(RuntimeError, match="failed ledger"):
        run_walk_forward(weekdays(441), replay)
    assert invoked == ["development_train", "development_oos"]


def record(window_id: str = "development/fold-01/oos", **changes: object) -> dict:
    return {
        "window_id": window_id,
        "status": "complete",
        "initial_nav": Decimal("100"),
        "nav_close": [Decimal("110"), Decimal("121")],
        **changes,
    }


def assert_no_metrics(result) -> None:
    assert result["unit_index_path"] is None
    assert result["unit_index_final"] is None
    assert result["total_return"] is None
    assert result["reasons"]
    json.dumps(result, allow_nan=False)


def test_aggregate_joins_daily_unit_path_with_independent_account_resets() -> None:
    records = [
        record(),
        record("development/fold-02/oos", initial_nav="1000", nav_close=["900", "1080"]),
    ]
    original = copy.deepcopy(records)
    result = aggregate_oos_ledgers(records)
    assert result["status"] == "available"
    assert result["reasons"] == []
    assert result["unit_index_path"] == pytest.approx([1.0, 1.1, 1.21, 1.089, 1.3068])
    assert result["unit_index_final"] == pytest.approx(1.3068)
    assert result["total_return"] == pytest.approx(0.3068)
    assert result["reset_initial_capital_per_fold"] is True
    assert result["non_continuous_account_path"] is True
    assert records == original
    json.dumps(result, allow_nan=False)


def test_aggregate_cash_and_loss_accounts_are_valid_complete_ledgers() -> None:
    result = aggregate_oos_ledgers([
        record(nav_close=("100", "100")),
        record("development/fold-02/oos", nav_close=["50"]),
    ])
    assert result["status"] == "available"
    assert result["unit_index_path"] == [1.0, 1.0, 1.0, 0.5]
    assert result["total_return"] == -0.5


def test_aggregate_empty_records_are_unavailable() -> None:
    result = aggregate_oos_ledgers([])
    assert result["status"] == "unavailable"
    assert result["reasons"] == [{"window_id": None, "reason": "no_oos_ledgers"}]
    assert_no_metrics(result)


@pytest.mark.parametrize("status", ["incomplete", "insolvent", "killed", "available", None])
def test_aggregate_does_not_filter_incomplete_fold_and_claim_success(status: str | None) -> None:
    records = [record(), record("development/fold-02/oos", status=status)]
    original = copy.deepcopy(records)
    result = aggregate_oos_ledgers(records)
    assert result["status"] == "incomplete"
    assert result["reasons"] == [{
        "record_index": 1,
        "window_id": "development/fold-02/oos",
        "reason": "ledger_not_complete",
        "status": str(status),
    }]
    assert records == original
    assert_no_metrics(result)


@pytest.mark.parametrize("value,reason", [
    ("0", "non_positive_nav"),
    (Decimal("-1"), "non_positive_nav"),
    (Decimal("-0"), "non_positive_nav"),
    ("NaN", "non_finite_nav"),
    (Decimal("sNaN"), "non_finite_nav"),
    ("Infinity", "non_finite_nav"),
    (Decimal("-Infinity"), "non_finite_nav"),
    ("not a number", "invalid_decimal"),
    (None, "invalid_nav_type"),
    (100, "invalid_nav_type"),
    (100.0, "invalid_nav_type"),
    (True, "invalid_nav_type"),
])
@pytest.mark.parametrize("field", ["initial_nav", "nav_close"])
def test_aggregate_invalid_initial_or_closing_nav_nulls_all_metrics(value, reason, field) -> None:
    change = {field: value if field == "initial_nav" else ["110", value]}
    result = aggregate_oos_ledgers([record(**change)])
    assert result["status"] == "incomplete"
    assert result["reasons"][0]["reason"] == reason
    assert result["reasons"][0]["field"] == field
    if field == "nav_close":
        assert result["reasons"][0]["nav_index"] == 1
    assert_no_metrics(result)


@pytest.mark.parametrize("nav_close,reason", [
    ([], "empty_nav_close"),
    (None, "invalid_nav_sequence"),
    ("110", "invalid_nav_sequence"),
    ({"nav": "110"}, "invalid_nav_sequence"),
])
def test_aggregate_requires_nonempty_nav_sequence(nav_close, reason) -> None:
    result = aggregate_oos_ledgers([record(nav_close=nav_close)])
    assert result["status"] == "incomplete"
    assert result["reasons"][0]["reason"] == reason
    assert_no_metrics(result)


def test_aggregate_retains_every_bad_record_and_every_nav_failure() -> None:
    result = aggregate_oos_ledgers([
        record(status="incomplete", initial_nav="0", nav_close=["NaN", "-1"]),
        record("development/fold-02/oos", status="insolvent", initial_nav="bad", nav_close=[]),
        record("development/fold-03/oos"),
    ])
    assert result["status"] == "incomplete"
    assert [entry["reason"] for entry in result["reasons"]] == [
        "ledger_not_complete", "non_positive_nav", "non_finite_nav", "non_positive_nav",
        "ledger_not_complete", "invalid_decimal", "empty_nav_close",
    ]
    assert [entry["record_index"] for entry in result["reasons"]] == [0, 0, 0, 0, 1, 1, 1]
    assert_no_metrics(result)


def test_aggregate_rejects_duplicate_window_ids_without_double_counting() -> None:
    result = aggregate_oos_ledgers([record(), record()])
    assert result["status"] == "incomplete"
    assert result["reasons"][0]["reason"] == "duplicate_window_id"
    assert_no_metrics(result)


@pytest.mark.parametrize("window_id", [None, "", 1])
def test_aggregate_requires_identifiable_windows(window_id) -> None:
    result = aggregate_oos_ledgers([record(window_id=window_id)])
    assert result["status"] == "incomplete"
    assert result["reasons"][0]["reason"] == "invalid_window_id"
    assert_no_metrics(result)


@pytest.mark.parametrize("nav_close", [["1e1000"], ["1e-1000"]])
def test_aggregate_unrepresentable_unit_path_is_json_safe(nav_close) -> None:
    result = aggregate_oos_ledgers([record(initial_nav="1", nav_close=nav_close)])
    assert result["status"] == "unavailable"
    assert result["reasons"][0]["reason"] == "unit_index_not_representable"
    assert_no_metrics(result)
