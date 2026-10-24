"""Hand-priced public episode paths; no live account or external data."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext

import pytest

from ak_trading.futures.scenario_backtest import ScenarioConfig
from ak_trading.futures.spec import ContractBar
from ak_trading.probability.contracts import ACTIONS, ActionSpec, FactorRow
from ak_trading.probability.episodes import action_specs, simulate_action

D = Decimal
SYMBOL = "SHFE.rb2610"
CONTRACT = SYMBOL + "|2026-10-15T00:00:00+00:00"


def bar(index, open=100, high=None, low=None, close=None):
    opening = D(str(open))
    closing = opening if close is None else D(str(close))
    return ContractBar(
        symbol=SYMBOL, duration_seconds=86400, datetime_nano=1000 + index,
        ts=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index),
        source="hand_path", open=opening, close=closing,
        high=max(opening, closing) if high is None else D(str(high)),
        low=min(opening, closing) if low is None else D(str(low)),
    )


def observation(bars, *, index=0, regime="uptrend", **values):
    factors = {
        "close": float(bars[index].close), "atr14": 10.0,
        "prior_high20": 99.0, "prior_low20": 80.0,
        "er20": 0.7, "ma20": 95.0, "ma60": 90.0,
        "ma_spread_atr": 0.5, "zscore20": 0.0,
        "distance_high_atr": -0.1, "distance_low_atr": 2.0,
    }
    factors.update(values)
    return FactorRow(
        event_id=f"{CONTRACT}:{index}", contract_id=CONTRACT,
        symbol=SYMBOL, product="SHFE.RB", index=index,
        timestamp_nano=bars[index].datetime_nano, values=factors,
        status="ready", reason="", context="middle", regime=regime,
    )


def economics(**changes):
    return ScenarioConfig(**dict({
        "initial_cash": D(10000), "lots": 1, "multiplier": D(10),
        "price_tick": D(1), "fee_per_lot": D(0), "fee_rate": D(0),
        "slippage_ticks": D(0), "margin_rate": D("0.1"),
        "max_margin_ratio": D("0.5"),
    }, **changes))


def test_fixed_action_catalog_and_cash_keep_the_complete_event_window():
    specs = action_specs()
    assert isinstance(specs, tuple)
    assert tuple(spec.name for spec in specs) == ACTIONS
    assert specs == tuple(ActionSpec(name) for name in ACTIONS)
    assert tuple(spec.horizon for spec in action_specs(3)) == (3,) * 5
    bars = [bar(i) for i in range(7)]
    result = simulate_action(observation(bars, index=1), bars, specs[-1], economics())
    assert (result.event_id, result.contract_id, result.index) == (f"{CONTRACT}:1", CONTRACT, 1)
    assert (result.status, result.reason, result.terminal_nano) == ("complete", "cash", 1006)
    assert (result.traded, result.direction, result.fills, result.ambiguous_bars) == (False, 0, (), 0)
    assert (result.gross_pnl, result.fees, result.slippage, result.net_pnl, result.max_margin) == (D(0),) * 5
    with pytest.raises(ValueError):
        action_specs(True)


@pytest.mark.parametrize("name", ACTIONS)
def test_unavailable_windows_are_never_scored_as_legitimate_zero_trades(name):
    bars = [bar(i) for i in range(4)]
    spec = ActionSpec(name, horizon=3)
    row = observation(bars)
    for supplied, event, status, reason in (
        (bars[:-1], row, "unmatured", "insufficient_future_bars"),
        (bars, replace(row, status="warmup", reason="insufficient_history"), "invalid", "observation_not_ready:insufficient_history"),
        (bars[:2] + [replace(bars[2], high=None)] + bars[3:], row, "invalid", "invalid_bar:2"),
        (bars[:2] + [replace(bars[2], symbol="DCE.m2609")] + bars[3:], row, "invalid", "contract_mismatch:2"),
        (bars[:2] + [replace(bars[2], datetime_nano=1001)] + bars[3:], row, "invalid", "invalid_timestamp:2"),
        (bars, replace(row, timestamp_nano=99), "invalid", "observation_identity_mismatch"),
        (bars, replace(row, index=-1), "invalid", "invalid_observation_index"),
    ):
        result = simulate_action(event, supplied, spec, economics())
        assert (result.status, result.reason, result.terminal_nano) == (status, reason, None)
        assert not result.traded and result.fills == ()
        assert result.net_pnl == result.gross_pnl == result.fees == result.slippage == D(0)


def test_breakout_actual_next_open_bracket_costs_and_terminal_cash_reconcile():
    # Long 2 @103, exit 2 @113: gross 200; fees 6.06+6.26;
    # two ticks * 0.5 tick size * 10 multiplier * 2 lots each way = 40.
    bars = [bar(0), bar(1, 103, 114, 102, 112), bar(2, 111)]
    config = economics(lots=2, fee_per_lot=D(2), fee_rate=D("0.001"),
                       price_tick=D("0.5"), slippage_ticks=D(2))
    result = simulate_action(observation(bars), bars, ActionSpec("breakout", horizon=2), config)
    assert (result.status, result.reason, result.direction, result.traded) == ("complete", "target", 1, True)
    assert result.terminal_nano == 1002  # Full opportunity end, not first exit.
    assert (result.gross_pnl, result.fees, result.slippage, result.net_pnl) == (D(200), D("12.32"), D(40), D("147.68"))
    assert result.max_margin == D(226)
    opening, closing = result.fills
    assert (opening["step"], opening["index"], opening["signal_index"], opening["phase"]) == (1, 1, 0, "next_open")
    assert opening["raw_ts"] == bars[1].ts.isoformat()
    assert opening["datetime_nano"] == 1001
    assert (opening["offset"], opening["side"], opening["lots"], opening["ideal_price"]) == ("open", 1, 2, D(103))
    assert (closing["phase"], closing["offset"], closing["side"], closing["ideal_price"]) == ("target", "close", -1, D(113))
    assert (opening["cash_after"], opening["position_after"]) == (D("9973.94"), 2)
    assert (closing["gross_pnl"], closing["fee"], closing["slippage"], closing["cash_after"], closing["position_after"]) == (D(200), D("6.26"), D(20), D("10147.68"), 0)
    assert result.gross_pnl - result.fees - result.slippage == result.net_pnl


@pytest.mark.parametrize("direction,second,exit_price,reason,ambiguous,gross", [
    (1, (80, 115, 75, 100), 80, "stop_gap", 0, -200),
    (-1, (120, 125, 85, 100), 120, "stop_gap", 0, -200),
    (1, (115, 120, 80, 100), 110, "target_gap", 0, 100),
    (-1, (85, 120, 80, 100), 90, "target_gap", 0, 100),
    (1, (100, 115, 85, 105), 90, "stop", 1, -100),
    (-1, (100, 115, 85, 95), 110, "stop", 1, -100),
    (1, (102, 105, 98, 104), 104, "terminal_close", 0, 40),
    (-1, (98, 102, 95, 96), 96, "terminal_close", 0, 40),
])
def test_brackets_check_gap_open_before_extremes_and_stop_first_when_ambiguous(
    direction, second, exit_price, reason, ambiguous, gross,
):
    bars = [bar(0), bar(1, 100, 101, 99, 100), bar(2, *second)]
    row = observation(bars) if direction == 1 else observation(
        bars, regime="downtrend", prior_high20=120, prior_low20=101, ma20=105, ma60=110,
    )
    result = simulate_action(row, bars, ActionSpec("breakout", horizon=2), economics())
    assert (result.status, result.reason, result.direction) == ("complete", reason, direction)
    assert result.fills[-1]["ideal_price"] == D(exit_price)
    assert result.fills[-1]["position_after"] == 0
    assert (result.gross_pnl, result.ambiguous_bars) == (D(gross), ambiguous)


def test_opening_margin_admission_reduces_integer_lots_after_all_estimated_costs():
    bars = [bar(0), bar(1)]
    # 3 lots require 300; after 45 costs equity*0.5=277.5 -> only 2 fit.
    config = economics(initial_cash=D(600), lots=3, fee_per_lot=D(5), slippage_ticks=D(1))
    result = simulate_action(observation(bars), bars, ActionSpec("breakout", horizon=1), config)
    assert (result.status, result.reason) == ("complete", "terminal_close:margin_limited")
    assert [fill["lots"] for fill in result.fills] == [2, 2]
    assert result.max_margin == D(200)
    assert (result.fees, result.slippage, result.net_pnl) == (D(20), D(40), D(-60))
    blocked = simulate_action(observation(bars), bars, ActionSpec("breakout", horizon=1),
                              replace(config, initial_cash=D(100)))
    assert (blocked.status, blocked.reason, blocked.traded, blocked.direction) == ("complete", "margin_blocked", False, 1)
    assert blocked.net_pnl == blocked.max_margin == 0


def test_a_bankrupt_gap_retains_the_entire_loss_and_closes_the_position():
    bars = [bar(0), bar(1), bar(2, 200), bar(3, 100)]
    row = observation(bars, regime="downtrend", prior_low20=101, ma20=105, ma60=110)
    result = simulate_action(row, bars, ActionSpec("breakout", horizon=3),
                             economics(initial_cash=D(150), max_margin_ratio=D(1)))
    assert (result.status, result.reason, result.traded) == ("complete", "insolvent:stop_gap", True)
    assert result.net_pnl == D(-1000)
    assert result.fills[-1]["cash_after"] == D(-850)
    assert result.fills[-1]["position_after"] == 0
    assert len(result.fills) == 2 and result.terminal_nano == 1003


@pytest.mark.parametrize("name,regime,changes,direction,reason", [
    ("breakout", "range", {"prior_high20": 100}, 0, "no_trade:breakout_not_confirmed"),
    ("breakout", "uptrend", {"ma20": 90}, 0, "no_trade:breakout_not_confirmed"),
    ("breakout", "range", {"prior_low20": 100, "ma20": 85}, 0, "no_trade:breakout_not_confirmed"),
    ("mean_reversion", "range", {"zscore20": -2}, 1, "terminal_close"),
    ("mean_reversion", "transition", {"zscore20": 2}, -1, "terminal_close"),
    ("mean_reversion", "range", {"zscore20": 1.999}, 0, "no_trade:zscore_inside_threshold"),
    ("mean_reversion", "uptrend", {"zscore20": -3}, 0, "no_trade:regime_not_range_or_transition"),
    ("mean_reversion", "downtrend", {"zscore20": 3}, 0, "no_trade:regime_not_range_or_transition"),
])
def test_action_eligibility_uses_only_current_factors(name, regime, changes, direction, reason):
    bars = [bar(0), bar(1, 100, 101, 99, 100)]
    row = observation(bars, regime=regime, **changes)
    result = simulate_action(row, bars, ActionSpec(name, horizon=1), economics())
    assert (result.status, result.reason, result.direction, result.traded) == ("complete", reason, direction, bool(direction))
    assert result.net_pnl == 0
    if direction:
        assert (result.fills[0]["index"], result.fills[0]["side"]) == (1, direction)
    else:
        assert not result.fills


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), True, 0, -1])
def test_missing_or_nonpositive_atr_is_unavailable_not_a_no_trade(value):
    bars = [bar(0), bar(1)]
    result = simulate_action(observation(bars, atr14=value), bars, ActionSpec("breakout", horizon=1), economics())
    assert (result.status, result.reason, result.traded) == ("invalid", "invalid_factor:atr14", False)
    assert result.net_pnl == 0 and result.terminal_nano is None


def test_unused_nullable_factors_do_not_disable_an_otherwise_valid_trade():
    bars = [bar(0), bar(1)]
    row = observation(bars, zscore20=None, volume_ratio20=None)
    result = simulate_action(row, bars, ActionSpec("breakout", horizon=1), economics())
    assert result.traded and result.status == "complete"
    missing = simulate_action(row, bars, ActionSpec("mean_reversion", horizon=1), economics())
    assert (missing.status, missing.reason) == ("invalid", "invalid_factor:zscore20")


@pytest.mark.parametrize("direction,ma,closes,entry,exit_price,expected", [
    (1, 95, (96, 95), 97, 101, 40),
    (-1, 105, (104, 105), 103, 99, 40),
    (1, None, (96, 95), 97, 101, 40),
    (-1, 95, (104, 105), 103, 99, 40),
])
def test_pullback_freezes_current_level_and_waits_for_completed_close_then_next_open(
    direction, ma, closes, entry, exit_price, expected,
):
    # Intrabar crosses on bar 1, close confirms only on bar 2; enter bar 3.
    bars = [bar(0), bar(1, 100, 106, 94, closes[0]),
            bar(2, closes[0], close=closes[1]),
            bar(3, entry, close=exit_price)]
    row = observation(bars, regime="uptrend" if direction == 1 else "downtrend", ma20=ma)
    result = simulate_action(row, bars, ActionSpec("pullback", horizon=3), economics())
    assert (result.status, result.reason, result.direction) == ("complete", "terminal_close", direction)
    assert result.net_pnl == D(expected)
    assert (result.fills[0]["index"], result.fills[0]["signal_index"], result.fills[0]["ideal_price"]) == (3, 2, D(entry))
    assert result.fills[-1]["position_after"] == 0


@pytest.mark.parametrize("close,reason", [
    (95, "no_trade:pullback_confirmed_at_terminal"),
    (96, "no_trade:pullback_not_reached"),
])
def test_pullback_never_fills_from_intrabar_touch_or_a_final_close_signal(close, reason):
    bars = [bar(0), bar(1, 100, 102, 90, 96), bar(2, 96, close=close)]
    result = simulate_action(observation(bars), bars, ActionSpec("pullback", horizon=2), economics())
    assert (result.status, result.reason, result.traded, result.direction) == ("complete", reason, False, 1)
    assert result.net_pnl == 0 and result.fills == ()
    blocked = simulate_action(observation(bars, regime="transition"), bars,
                              ActionSpec("pullback", horizon=2), economics())
    assert (blocked.reason, blocked.direction) == ("no_trade:regime_not_trend", 0)


def test_grid_favorable_round_trips_wait_until_next_open_and_pay_each_leg():
    bars = [bar(0), bar(1, 100, 102, 93, 94), bar(2, 94, 105, 93, 104),
            bar(3, 104, close=106), bar(4, 106, close=100), bar(5)]
    result = simulate_action(observation(bars, regime="range"), bars, ActionSpec("grid"),
                             economics(fee_per_lot=D(1), slippage_ticks=D(1)))
    assert (result.status, result.reason, result.direction) == ("complete", "terminal_close", 0)
    assert [(f["index"], f["offset"], f["side"], f["lots"], f["ideal_price"]) for f in result.fills] == [
        (2, "open", 1, 1, D(94)), (3, "close", -1, 1, D(104)),
        (4, "open", -1, 1, D(106)), (5, "close", 1, 1, D(100)),
    ]
    assert all(f["signal_index"] == f["index"] - 1 for f in result.fills)
    assert (result.gross_pnl, result.fees, result.slippage, result.net_pnl) == (D(160), D(4), D(40), D(116))
    assert result.max_margin == D(106)
    assert result.fills[-1]["cash_after"] == D(10116)
    assert result.fills[-1]["position_after"] == 0


def test_grid_adverse_path_keeps_integer_cap_without_doubling():
    bars = [bar(0), bar(1, 100, close=94), bar(2, 94, close=89),
            bar(3, 89, close=84), bar(4, 84, close=79), bar(5, 79)]
    result = simulate_action(observation(bars), bars, ActionSpec("grid"), economics(lots=2))
    assert result.status == "complete" and result.net_pnl == D(-500)
    assert [(f["index"], f["lots"], f["position_after"]) for f in result.fills] == [(2, 2, 2), (3, 2, 4), (5, 4, 0)]
    assert all(type(f["position_after"]) is int and abs(f["position_after"]) <= 4 for f in result.fills)
    assert result.max_margin == D(356)


def test_grid_reversal_closes_before_opening_the_opposite_inventory():
    bars = [bar(0), bar(1, 100, close=89), bar(2, 90, close=106),
            bar(3, 105, close=100), bar(4)]
    result = simulate_action(observation(bars), bars, ActionSpec("grid", horizon=4),
                             economics(fee_per_lot=D(5)))
    assert [(f["index"], f["offset"], f["lots"], f["position_after"]) for f in result.fills] == [
        (2, "open", 2, 2), (3, "close", 2, 0), (3, "open", 1, -1), (4, "close", 1, 0),
    ]
    assert (result.gross_pnl, result.fees, result.net_pnl) == (D(350), D(30), D(320))


@pytest.mark.parametrize("direction,second,third,price,gross,reason,ambiguous", [
    (1, 94, (94, 95, 65, 80), 70, -240, "grid_stop", 0),
    (1, 94, (60, 100, 55, 80), 60, -340, "grid_stop_gap", 0),
    (-1, 106, (106, 135, 100, 120), 130, -240, "grid_stop", 0),
    (-1, 106, (140, 145, 100, 120), 140, -340, "grid_stop_gap", 0),
    (1, 94, (94, 135, 65, 100), 70, -240, "grid_stop", 1),
    (-1, 106, (106, 135, 65, 100), 130, -240, "grid_stop", 1),
])
def test_grid_global_stop_closes_in_same_bar_and_never_reenters(
    direction, second, third, price, gross, reason, ambiguous,
):
    bars = [bar(0), bar(1, 100, close=second), bar(2, second),
            bar(3, *third), bar(4, 100, close=90), bar(5, 90, close=100)]
    result = simulate_action(observation(bars), bars, ActionSpec("grid"), economics())
    assert (result.status, result.reason, result.net_pnl, result.ambiguous_bars) == ("complete", reason, D(gross), ambiguous)
    assert len(result.fills) == 2
    assert (result.fills[-1]["index"], result.fills[-1]["phase"], result.fills[-1]["ideal_price"]) == (3, reason, D(price))
    assert result.fills[-1]["side"] == -direction
    assert result.fills[-1]["position_after"] == 0
    assert result.terminal_nano == 1005


def test_grid_gap_stop_cancels_pending_open_before_admission_or_charging_costs():
    bars = [bar(0), bar(1, 100, close=94), bar(2, 60), bar(3, 100)]
    result = simulate_action(observation(bars), bars, ActionSpec("grid", horizon=3),
                             economics(fee_per_lot=D(5)))
    assert (result.status, result.reason, result.traded) == ("complete", "no_trade:grid_stop_gap", False)
    assert result.fills == () and result.net_pnl == result.fees == result.max_margin == 0


def test_grid_new_open_can_stop_intrabar_but_completed_close_cannot_fill_itself():
    bars = [bar(0), bar(1, 100, close=94), bar(2, 94, 95, 60, 80)]
    result = simulate_action(observation(bars), bars, ActionSpec("grid", horizon=2), economics())
    assert [(f["index"], f["phase"], f["ideal_price"]) for f in result.fills] == [
        (2, "next_open", D(94)), (2, "grid_stop", D(70)),
    ]
    assert result.net_pnl == D(-240)
    final_only = [bar(0), bar(1), bar(2, 100, close=94)]
    no_fill = simulate_action(observation(final_only), final_only, ActionSpec("grid", horizon=2), economics())
    assert (no_fill.status, no_fill.reason, no_fill.fills) == ("complete", "no_trade:grid_confirmed_at_terminal", ())


def test_insolvent_open_cannot_wait_for_a_later_intrabar_winning_target():
    bars = [bar(0), bar(1), bar(2, 107, 108, 89, 100)]
    row = observation(bars, regime="downtrend", prior_low20=101, ma20=105, ma60=110)
    result = simulate_action(row, bars, ActionSpec("breakout", horizon=2),
                             economics(initial_cash=D(50), margin_rate=D(0)))
    assert (result.status, result.reason, result.net_pnl) == ("complete", "insolvent:insolvency_open", D(-70))
    assert result.fills[-1]["ideal_price"] == D(107)
    assert result.fills[-1]["position_after"] == 0


def test_grid_margin_uses_unrealized_equity_and_reduces_available_new_lots():
    # First 1 @94; next close requests 2. At next open 79, held loss=150,
    # equity=310 and two lots' margin=158 >155 -> no second lot admitted.
    bars = [bar(0), bar(1, 100, close=94), bar(2, 94, close=89), bar(3, 79)]
    result = simulate_action(observation(bars), bars, ActionSpec("grid", horizon=3),
                             economics(initial_cash=D(460)))
    assert result.reason == "terminal_close:margin_limited"
    assert [(f["offset"], f["lots"]) for f in result.fills] == [("open", 1), ("close", 1)]
    assert (result.net_pnl, result.max_margin) == (D(-150), D(94))
    blocked = simulate_action(observation(bars), bars, ActionSpec("grid", horizon=3),
                              economics(initial_cash=D(100)))
    assert (blocked.status, blocked.reason, blocked.net_pnl) == ("complete", "margin_blocked", D(0))


def test_grid_bankruptcy_is_an_observed_loss_and_prevents_future_entries():
    bars = [bar(0), bar(1, 100, close=106), bar(2, 106), bar(3, 125), bar(4, 100)]
    result = simulate_action(observation(bars), bars, ActionSpec("grid", horizon=4),
                             economics(initial_cash=D(110), max_margin_ratio=D(1)))
    assert (result.status, result.reason, result.net_pnl) == ("complete", "insolvent:insolvency_open", D(-190))
    assert len(result.fills) == 2
    assert (result.fills[-1]["index"], result.fills[-1]["cash_after"], result.fills[-1]["position_after"]) == (3, D(-80), 0)


@pytest.mark.parametrize("name", ["mean_reversion", "pullback"])
def test_unavailable_current_regime_is_not_a_valid_strategy_rejection(name):
    bars = [bar(0), bar(1)]
    result = simulate_action(observation(bars, regime="unavailable", zscore20=-3),
                             bars, ActionSpec(name, horizon=1), economics())
    assert (result.status, result.reason, result.terminal_nano) == ("invalid", "invalid_factor:regime", None)
    assert not result.traded and result.net_pnl == 0


def test_grid_partial_inventory_reduction_preserves_fifo_pnl_and_per_leg_costs():
    # Buy 2 @90; reduce 1 @94; close the remaining 1 @100.
    bars = [bar(0), bar(1, 100, close=89), bar(2, 90, close=94),
            bar(3, 94, close=100), bar(4)]
    result = simulate_action(observation(bars), bars, ActionSpec("grid", horizon=4),
                             economics(fee_per_lot=D(1), fee_rate=D("0.001")))
    assert [(f["index"], f["lots"], f["position_after"]) for f in result.fills] == [(2, 2, 2), (3, 1, 1), (4, 1, 0)]
    assert (result.gross_pnl, result.fees, result.net_pnl) == (D(140), D("7.74"), D("132.26"))


def test_all_action_outcomes_are_independent_of_suffix_history_and_decimal_context():
    bars = [bar(0), bar(1, 100, close=95), bar(2, 96, close=100), bar(3, 101, close=100)]
    row = observation(bars, regime="uptrend", zscore20=-2)
    config = economics(lots=3, initial_cash=D("5000.123456789012"),
                       multiplier=D("10.000000000003"), fee_per_lot=D("0.987654321012"),
                       fee_rate=D("0.001234567891"), price_tick=D("0.123456789012"),
                       slippage_ticks=D("1.012345678912"))
    specs = action_specs(3)
    expected = [simulate_action(row, bars, spec, config) for spec in specs]
    # Malformed price rows outside this opportunity cannot affect its result.
    changed_suffix = bars + [replace(bar(4, 500), high=None)]
    with localcontext() as ambient:
        ambient.prec = 4
        ambient.traps[Inexact] = True
        actual = [simulate_action(row, changed_suffix, spec, config) for spec in specs]
        assert actual == expected
        assert ambient.prec == 4 and ambient.traps[Inexact]
    with localcontext() as audit:
        audit.prec = 128
        for result in actual:
            assert result.status == "complete" and result.terminal_nano == 1003
            assert result.gross_pnl - result.fees - result.slippage == result.net_pnl
            assert sum((f["gross_pnl"] for f in result.fills), D(0)) == result.gross_pnl
            assert sum((f["fee"] for f in result.fills), D(0)) == result.fees
            assert sum((f["slippage"] for f in result.fills), D(0)) == result.slippage
            if result.fills:
                assert result.fills[-1]["cash_after"] == config.initial_cash + result.net_pnl
                assert result.fills[-1]["position_after"] == 0
    shifted = [replace(bar(-1), high=None)] + bars
    shifted_row = replace(row, index=1)
    shifted_result = simulate_action(shifted_row, shifted, specs[0], config)
    assert shifted_result.net_pnl == expected[0].net_pnl
    assert shifted_result.fills[0]["index"] == 2 and shifted_result.fills[0]["step"] == 1


@pytest.mark.parametrize("name", ACTIONS)
def test_invalid_tail_is_retained_even_when_trade_could_have_exited_earlier(name):
    bars = [bar(0), bar(1, 100, 111, 99, 105), bar(2)]
    bars[-1] = replace(bars[-1], close=D("NaN"))
    result = simulate_action(observation(bars), bars, ActionSpec(name, horizon=2), economics())
    assert (result.status, result.reason, result.fills) == ("invalid", "invalid_bar:2", ())
    assert result.net_pnl == 0 and result.terminal_nano is None


def test_grid_reversal_uses_cash_after_closing_fee_before_opening_admission():
    bars = [bar(0), bar(1, 100, close=94), bar(2, 94, close=106),
            bar(3, 94), bar(4, 94)]
    result = simulate_action(observation(bars), bars, ActionSpec("grid", horizon=4),
                             economics(initial_cash=D(120), fee_per_lot=D(10), max_margin_ratio=D(1)))
    # Opening 1 @94 costs 10. Closing costs another 10, so a new opening
    # would leave only 90 equity against 94 margin; neither direction fits.
    assert [(f["index"], f["offset"], f["position_after"]) for f in result.fills] == [(2, "open", 1), (3, "close", 0)]
    assert result.reason == "terminal_close:margin_limited"
    assert result.net_pnl == D(-20) and result.fills[-1]["cash_after"] == D(100)


def test_grid_fifo_reduction_of_different_entry_prices_is_exact():
    bars = [bar(0), bar(1, 100, close=94), bar(2, 94, close=89),
            bar(3, 89, close=94), bar(4, 95, close=100), bar(5)]
    result = simulate_action(observation(bars), bars, ActionSpec("grid"), economics())
    assert [(f["index"], f["offset"], f["lots"], f["gross_pnl"]) for f in result.fills] == [
        (2, "open", 1, D(0)), (3, "open", 1, D(0)),
        (4, "close", 1, D(10)), (5, "close", 1, D(110)),
    ]
    assert result.net_pnl == D(120)
