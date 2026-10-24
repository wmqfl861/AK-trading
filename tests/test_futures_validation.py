"""S2 focused tests: folds/holdout, splits document, metrics, replay read.

Finalized-plan §十二.14 (minimum 8 cases) and §十一 fold/metric rules.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ak_trading.futures.data import day_sequence_digest
from ak_trading.futures.validation import (
    DEV_WINDOW,
    HOLDOUT_DAYS,
    MIN_FOLD_HISTORY,
    TRAIN_WINDOW,
    aggregate_unit_index,
    annualized_turnover,
    build_folds,
    build_splits_document,
    daily_returns,
    max_drawdown,
    sharpe_ratio,
    summarize_run,
    trade_statistics,
)


def weekdays(count: int, start: date = date(2024, 1, 1)) -> list[date]:
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def test_insufficient_history_below_441_days() -> None:
    folds = build_folds(weekdays(440))
    assert folds.status == "insufficient_history"
    assert folds.reason == "n=440<441"
    assert folds.folds == () and folds.final_holdout == ()
    assert folds.k_all == ()


def test_folds_exact_layout_at_441_days() -> None:
    days = weekdays(441)
    folds = build_folds(days)
    assert folds.status == "available"
    n = len(days)
    h = n - HOLDOUT_DAYS  # 315
    assert folds.holdout_start_index == h
    j_count = (h - TRAIN_WINDOW) // DEV_WINDOW
    assert j_count == 1
    assert folds.k_all == (h - DEV_WINDOW,)  # k = 252
    assert folds.retained_k == folds.k_all and folds.dropped_k == ()
    fold = folds.folds[0]
    assert fold.kind == "development" and fold.index == 1 and fold.retained
    assert fold.train == tuple(days[0:252])
    assert fold.oos == tuple(days[252:315])
    assert folds.final_holdout == tuple(days[315:441])
    assert folds.final_holdout_train_context == tuple(days[63:315])
    # Right alignment: the last dev OOS block ends exactly where the holdout begins.
    assert fold.oos[-1] < folds.final_holdout[0]


def test_folds_retention_keeps_only_last_eight() -> None:
    days = weekdays(1260)  # h = 1134, J_count = 14
    folds = build_folds(days)
    assert folds.status == "available"
    assert len(folds.k_all) == 14
    assert folds.retained_k == folds.k_all[-8:]
    assert folds.dropped_k == folds.k_all[:6]
    retained_oos = [f for f in folds.folds if f.retained]
    assert len(retained_oos) == 8
    # Non-overlapping OOS blocks.
    for earlier, later in zip(retained_oos, retained_oos[1:]):
        assert earlier.oos[-1] < later.oos[0]
    # Fixed train length of 252 for every fold.
    assert all(len(fold.train) == TRAIN_WINDOW for fold in folds.folds)


def test_splits_document_carries_full_traceability() -> None:
    days = weekdays(500)
    folds = build_folds(days)
    document = build_splits_document(
        days,
        folds,
        universe=["SHFE.RB", "CFFEX.IF"],
        span=("2024-01-01T00:00:00+08:00", "2026-01-01T00:00:00+08:00"),
        construction_sha256="abc123",
        terminal_schedule={"SHFE.RB": days[-1].isoformat()},
    )
    assert document["day_sequence_sha256"] == day_sequence_digest(days)
    assert document["k_all"] == list(folds.k_all)
    assert document["retained_k"] == list(folds.retained_k)
    assert document["dropped_k"] == list(folds.dropped_k)
    assert document["final_holdout"][0] == days[len(days) - HOLDOUT_DAYS].isoformat()
    assert document["final_holdout"][-1] == days[-1].isoformat()
    first_k = folds.retained_k[0]
    assert document["folds"][0]["train"][0] == days[first_k - 252].isoformat()
    assert document["retention_note"]
    assert document["terminal_schedule"]["SHFE.RB"] == days[-1].isoformat()
    # Canonical JSON round-trip stays NaN-free and key-stable.
    import json

    encoded = json.dumps(document, sort_keys=True, allow_nan=False)
    assert json.loads(encoded)["n_days"] == 500


def test_daily_returns_and_sharpe_hand_computed() -> None:
    returns = daily_returns(
        [Decimal("110"), Decimal("99")], Decimal("100")
    )
    assert returns[0] == pytest.approx(0.10)
    assert returns[1] == pytest.approx(99 / 110 - 1.0)
    sharpe, reason = sharpe_ratio([0.10, -0.10])
    assert sharpe == pytest.approx(0.0)
    assert reason is None
    zero_var, zero_reason = sharpe_ratio([0.05, 0.05])
    assert zero_var is None and zero_reason == "zero_variance"
    short, short_reason = sharpe_ratio([0.05])
    assert short is None and short_reason == "insufficient_observations"
    # Hand-computed positive Sharpe: mean 0.01, sample std 0.01 -> sqrt(252).
    up, _ = sharpe_ratio([0.0, 0.02])
    assert up == pytest.approx((252 ** 0.5) * 0.01 / 0.0141421356, rel=1e-3)


def test_max_drawdown_includes_initial_nav_high_water() -> None:
    days = weekdays(4)
    navs = [Decimal("90"), Decimal("120"), Decimal("60"), Decimal("70")]
    result = max_drawdown(navs, Decimal("100"), days)
    # Peak stays at the initial 100 until day2's 120; worst trough 60.
    assert result["max_drawdown"] == pytest.approx(0.5)
    assert result["peak_day"] == days[1].isoformat()
    assert result["trough_day"] == days[2].isoformat()
    assert result["drawdown_duration_calendar_days"] == (days[2] - days[1]).days
    assert result["drawdown_duration_d_days"] == 2  # peak and trough days inclusive


def test_turnover_rules_use_all_fold_days_mean_nav() -> None:
    days = weekdays(63)
    navs = [Decimal("400000000")] * 63
    value, reason = annualized_turnover(
        turnover_notional=Decimal("1000000"),
        nav_close_by_day=navs,
        fold_days=days,
        incomplete=False,
        insolvency=False,
    )
    assert reason is None
    assert value == pytest.approx(1_000_000 / 400_000_000 * 252 / 63)
    # Incomplete or insolvent folds never publish an annualized figure.
    blocked, blocked_reason = annualized_turnover(
        turnover_notional=Decimal("1000000"), nav_close_by_day=navs,
        fold_days=days, incomplete=True, insolvency=False,
    )
    assert blocked is None and blocked_reason == "incomplete_or_insolved_ledger"
    # A missing NAV day blocks the figure; days never shrink to fit.
    missing, missing_reason = annualized_turnover(
        turnover_notional=Decimal("1000000"), nav_close_by_day=navs[:-1],
        fold_days=days, incomplete=False, insolvency=False,
    )
    assert missing is None and missing_reason == "nav_missing_for_fold_days"
    negative, negative_reason = annualized_turnover(
        turnover_notional=Decimal("1000000"),
        nav_close_by_day=[Decimal("0")] * 63,
        fold_days=days, incomplete=False, insolvency=False,
    )
    assert negative is None and negative_reason == "non_positive_mean_nav"


def test_trade_statistics_null_semantics() -> None:
    empty = trade_statistics([])
    assert empty["closed_trade_count"] == 0
    assert empty["win_rate"] is None and empty["reason"] == "no_closed_trades"

    class _T:
        def __init__(self, net):
            self.net_pnl = Decimal(net)

    no_losses = trade_statistics([_T("10"), _T("20")])
    assert no_losses["win_rate"] == 1.0
    assert no_losses["profit_factor"] is None
    assert no_losses["profit_factor_reason"] == "no_losses"
    mixed = trade_statistics([_T("30"), _T("-10"), _T("-5")])
    assert mixed["closed_trade_count"] == 3
    assert mixed["win_rate"] == pytest.approx(1 / 3)
    assert mixed["profit_factor"] == pytest.approx(2.0)


def test_summarize_run_report_shape_and_status() -> None:
    days = weekdays(5)
    navs = [
        Decimal("400000000"), Decimal("404000000"), Decimal("402000000"),
        Decimal("406000000"), Decimal("405000000"),
    ]
    report = summarize_run(
        days=days,
        nav_close_by_day=navs,
        initial_nav=Decimal("400000000"),
        turnover_notional=Decimal("5000000"),
        trades=[],
        status="complete",
    )
    assert report["status"] == "complete"
    assert report["days"] == 5
    assert report["final_nav"] == "405000000"
    assert report["total_net_pnl"] == "5000000"
    assert report["total_return"] == pytest.approx(0.0125)
    assert report["sharpe"] is None or isinstance(report["sharpe"], float)
    assert report["trades"]["reason"] == "no_closed_trades"
    assert report["max_drawdown"] >= 0.0


def test_aggregate_unit_index_flags_non_continuous_path() -> None:
    result = aggregate_unit_index([[0.01, 0.01], [0.02, -0.01]])
    expected = 1.01 * 1.01 * 1.02 * 0.99
    assert result["unit_index_final"] == pytest.approx(expected)
    assert result["reset_initial_capital_per_fold"] is True
    assert result["non_continuous_account_path"] is True
    empty = aggregate_unit_index([])
    assert empty["status"] == "no_complete_folds" and empty["unit_index_final"] is None


def test_s1_frozen_real_input_bundle_still_readable() -> None:
    bundle_dir = Path("data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02")
    if not bundle_dir.exists():  # defensive: never fabricate a read
        pytest.skip("frozen real-input-02 bundle not present")
    from ak_trading.futures.io import read_input_bundle

    bundle = read_input_bundle(bundle_dir)
    assert len(bundle.bars) == 92461
    assert bundle.manifest["dataset_kind"] == "real_export"
    assert bundle.manifest["read_only_transaction"]["verified"] is True


# ---------------------------------------------------------------------------
# Fix-round additions: mandatory metric set (B11), null returns (A5)
# ---------------------------------------------------------------------------


def test_summarize_run_contains_full_mandatory_metric_set_b11() -> None:
    from decimal import Decimal as D

    from ak_trading.futures.backtest import EquityMark
    from ak_trading.futures.validation import summarize_run

    days = weekdays(6)
    marks = []
    navs = [D("400000000"), D("404000000"), D("402000000"), D("406000000"),
            D("405000000"), D("405000000")]
    for day, nav in zip(days, navs):
        marks.append(EquityMark(
            trading_day=day, cash=nav - D("1000"), equity=nav,
            required_margin=D("80000000"), status="active",
            unrealized_pnl=D("1000"), gross_notional=D("200000000"),
            invested=True,
        ))
    report = summarize_run(
        days=days, nav_close_by_day=navs, initial_nav=D("400000000"),
        turnover_notional=D("5000000"), trades=[], status="complete",
        marks=marks, blocked=[{"reason": "capacity_exhausted_no_open"}],
        fees_cny=D("100"), slippage_cny=D("50"),
    )
    mandatory = (
        "total_gross_pnl", "roll_costs_cny", "total_net_pnl", "total_return", "sharpe", "annualized_volatility",
        "max_drawdown", "drawdown_duration_d_days",
        "drawdown_duration_calendar_days", "turnover_notional",
        "annualized_turnover", "exposure_mean", "exposure_max",
        "invested_days", "margin_mean_ratio", "margin_max_ratio",
        "fees_cny", "slippage_cny", "final_unrealized_pnl",
        "blocked_action_counts", "trades",
    )
    for key in mandatory:
        assert key in report, key
    assert report["annualized_turnover"] is not None  # complete ledger
    assert report["exposure_max"] == pytest.approx(0.5, rel=1e-3)
    assert report["invested_days"] == 6
    assert report["blocked_action_counts"] == {"capacity_exhausted_no_open": 1}


def test_incomplete_ledger_nullifies_annualized_turnover_b11() -> None:
    from decimal import Decimal as D

    from ak_trading.futures.validation import summarize_run

    days = weekdays(4)
    navs = [D("400000000")] * 4
    report = summarize_run(
        days=days, nav_close_by_day=navs, initial_nav=D("400000000"),
        turnover_notional=D("1000000"), trades=[], status="incomplete",
    )
    assert report["annualized_turnover"] is None
    assert report["annualized_turnover_reason"] == "incomplete_or_insolved_ledger"


def test_r2_initial_nav_peak_has_duration_without_invented_prior_day():
    days = [date(2026, 1, 9), date(2026, 1, 12), date(2026, 1, 13)]
    drawdown = max_drawdown([Decimal("90"), Decimal("80"), Decimal("85")], Decimal("100"), days)
    assert drawdown["max_drawdown"] == pytest.approx(0.20)
    assert drawdown["peak_day"] == "2026-01-09"
    assert drawdown["peak_basis"] == "initial_nav_start_of_first_reporting_day"
    assert drawdown["trough_day"] == "2026-01-12"
    assert drawdown["drawdown_duration_d_days"] == 2
    assert drawdown["drawdown_duration_calendar_days"] == 3


def test_r2_zero_trade_schema_is_stable_and_unknown_counts_are_not_zero():
    from types import SimpleNamespace

    empty = trade_statistics([], open_trade_count=2)
    wins = trade_statistics([SimpleNamespace(net_pnl=Decimal("10"))], open_trade_count=0)
    assert set(empty) == set(wins)
    assert empty["open_trade_count"] == 2
    assert empty["open_trade_count_basis"] == "nonempty_fifo_open_batches"
    assert empty["closed_trade_count"] == 0
    for name in ("win_rate", "mean_net_pnl", "median_net_pnl", "mean_win", "mean_loss",
                 "mean_win_loss_ratio", "profit_factor"):
        assert empty[name] is None
        assert empty[name + "_reason"] == "no_closed_trades"
    assert wins["profit_factor"] is None and wins["profit_factor_reason"] == "no_losses"
    unknown = trade_statistics([])
    assert unknown["open_trade_count"] is None
    assert unknown["open_trade_count_reason"] == "open_fifo_batches_not_supplied"


def test_r2_summary_populates_gross_roll_cost_and_open_batch_count():
    report = summarize_run(
        days=weekdays(2), nav_close_by_day=[Decimal("100"), Decimal("125")],
        initial_nav=Decimal("100"), turnover_notional=Decimal("500"),
        trades=[], status="incomplete", total_gross_pnl=Decimal("30"),
        fees_cny=Decimal("2"), slippage_cny=Decimal("3"), roll_costs_cny=Decimal("4"),
        open_trade_count=2,
    )
    assert report["total_gross_pnl"] == "30"
    assert report["roll_costs_cny"] == "4"
    assert report["trades"]["open_trade_count"] == 2
    assert report["annualized_turnover"] is None
    assert report["annualized_turnover_reason"] == "incomplete_or_insolved_ledger"
    no_account = summarize_run(
        days=[], nav_close_by_day=[], initial_nav=Decimal("100"),
        turnover_notional=Decimal("0"), trades=[], status="incomplete",
    )
    for name in ("total_gross_pnl", "roll_costs_cny", "fees_cny", "slippage_cny"):
        assert no_account[name] is None and no_account[name + "_reason"]


def test_zero_previous_nav_yields_null_return_a5() -> None:
    from decimal import Decimal as D

    from ak_trading.futures.validation import daily_returns, sharpe_ratio

    returns = daily_returns([D("50"), D("0"), D("100")], D("100"))
    assert returns[0] == pytest.approx(-0.5)
    assert returns[1] == pytest.approx(-1.0)
    assert returns[2] is None  # zero previous NAV: undefined, never zero
    sharpe, reason = sharpe_ratio(returns)
    # The two DEFINED days drive the statistic; the undefined day is
    # skipped, not zero-filled.
    assert reason is None
    assert sharpe == pytest.approx(-33.67491648096547, rel=1e-9)
