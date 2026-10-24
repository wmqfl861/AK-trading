"""M3B-04: frozen-range reversion, fixed grid, costs, inventory and range
destruction (04-M3B-策略库.md §6.4–§6.5, §10-3).

Shared synthetic builders come from this node's ``test_v2_m3b_matrix``
module (the M1 shared fixture is not published yet and ``tests/conftest.py``
must not be edited).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from ak_trading.v2 import policy as m1_policy
from ak_trading.v2.contracts import Horizon, PositionSnapshot, TradingRule
from ak_trading.v2.strategies.registry import build_strategy_spec
from ak_trading.v2.strategies.state import commit_observation

from test_v2_m3b_matrix import (
    BASE,
    MINUTE,
    drive_open,
    features,
    forecast,
    frozen_range,
    holding_account,
    holding_state,
    make_context,
    minute_bars,
    ordinary_evidence,
    range_bars_sequence,
    range_forecast_builder,
    range_forecasts,
    regime,
    rule,
)

HORIZON = Horizon("TRADING_MINUTE", 30)
L, M, U = Decimal("90"), Decimal("100"), Decimal("110")
W = U - L


def forecasts_with(snap, horizons=(30,)):
    out = []
    for value in horizons:
        horizon = Horizon("TRADING_MINUTE", value)
        items = ordinary_evidence(snap, frozen_range(), "ORDERED_RANGE", horizon)
        out.append(forecast(horizon, base_regime="ORDERED_RANGE",
                            persistence={"ORDERED_RANGE": 0.9}, frozen=frozen_range(),
                            evidence_items=items))
    return tuple(out)


def reversion_strategy():
    from ak_trading.v2.strategies.reversion import RangeReversion
    spec = build_strategy_spec("REVERSION", "MINUTE")
    return spec, RangeReversion(spec)


def grid_strategy():
    from ak_trading.v2.strategies.grid import FixedInventoryGrid
    spec = build_strategy_spec("GRID", "MINUTE")
    return spec, FixedInventoryGrid(spec)


# --- reversion hand-calc triggers (§10-3) ----------------------------------------


def test_reversion_long_trigger_level_and_recross():
    # L=90/M=100/U=110 -> lower trigger = 90 + 0.20*20 = 94; close 93 arms,
    # a later close 95 re-crosses while below M.
    bars, snaps = range_bars_sequence()  # ends 93.25 -> 96 -> 96.2
    proposal, _ = drive_open("REVERSION", regime_base="ORDERED_RANGE",
                             bars_sequence=bars, features_sequence=snaps,
                             frozen=frozen_range())
    assert proposal.action == "OPEN"
    assert proposal.trigger_price == L + Decimal("0.20") * W
    assert proposal.stop_price == Decimal("85")
    assert proposal.target_price == M
    assert proposal.next_state.direction == 1
    assert proposal.layer_index == 0


def test_reversion_target_spread_below_double_cost_blocked():
    bars, snaps = range_bars_sequence()
    spec, strategy = reversion_strategy()
    # RT cost 3 points * multiplier 10 = 30 CNY per lot; worst entry 96.3
    # gives gross (100-96.3)*10 = 37 < 60.
    expensive = rule(per_lot=Decimal("15"))
    state = None
    proposal = None
    for bars_i, snap_i in zip(bars[:2], snaps[:2]):
        proposal = strategy.evaluate(
            make_context(spec, bars=bars_i, forecasts=range_forecasts(snap_i),
                         regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                         features_snap=snap_i), state)
        state = commit_observation(state, proposal)
    proposal = strategy.evaluate(
        make_context(spec, bars=bars[-1], forecasts=range_forecasts(snaps[-1]),
                     regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                     features_snap=snaps[-1], rule_obj=expensive), state)
    assert proposal.action == "WAIT"
    assert "COST_COVERAGE_INSUFFICIENT" in proposal.reasons


def test_reversion_short_mirror_at_upper_edge():
    bars = (minute_bars([103 + index * 0.35 for index in range(20)]),        # drift to 109.65
            minute_bars([103 + index * 0.35 for index in range(20)] + [104]),  # re-cross down
            minute_bars([103 + index * 0.35 for index in range(20)] + [104, 103.8]))
    snaps = (features(Decimal("109.65"), Decimal("1")),
             features(Decimal("104"), Decimal("1")),
             features(Decimal("103.8"), Decimal("1")))
    proposal, _ = drive_open("REVERSION", regime_base="ORDERED_RANGE",
                             bars_sequence=bars, features_sequence=snaps,
                             frozen=frozen_range(),
                             forecast_builder=range_forecast_builder())
    assert proposal.action == "OPEN"
    assert proposal.trigger_price == U - Decimal("0.20") * W
    assert proposal.stop_price == Decimal("115")
    assert proposal.next_state.direction == -1
    # Short target carries the sign; the budget rides on the proposal.
    assert proposal.target_lots == -proposal.budget.layers[0].lots
    assert proposal.budget.side == -1


def test_reversion_one_direction_per_round_and_no_middle_fill():
    # Once armed long, a move to the upper band never flips the setup.
    bars, snaps = range_bars_sequence()
    spec, strategy = reversion_strategy()
    state = None
    proposal = None
    for bars_i, snap_i in zip(bars[:2], snaps[:2]):
        proposal = strategy.evaluate(
            make_context(spec, bars=bars_i, forecasts=range_forecasts(snap_i),
                         regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                         features_snap=snap_i), state)
        state = commit_observation(state, proposal)
    assert state is not None and state.setup is not None and state.setup.side == 1
    upper = minute_bars([100] * 21 + [107])
    snap_upper = features(Decimal("107"), Decimal("1"))
    proposal = strategy.evaluate(
        make_context(spec, bars=upper, forecasts=range_forecasts(snap_upper),
                     regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                     features_snap=snap_upper), state)
    assert proposal.action == "WAIT"
    assert proposal.next_state.setup is None or proposal.next_state.setup.side == 1


# --- grid (§6.5) -------------------------------------------------------------------


def test_grid_layer_triggers_are_frozen_from_range():
    bars, snaps = range_bars_sequence()
    proposal, budget = drive_open("GRID", regime_base="ORDERED_RANGE",
                                  bars_sequence=bars, features_sequence=snaps,
                                  frozen=frozen_range())
    assert proposal.action == "OPEN"
    assert proposal.budget is not None
    layers = proposal.budget.layers
    assert tuple(layer.trigger_price for layer in layers) == (
        L + Decimal("0.25") * W, L + Decimal("0.15") * W, L + Decimal("0.05") * W)
    base = layers[0].lots
    assert tuple(layer.lots for layer in layers) == (base, base, base)
    assert sum(layer.lots for layer in layers) <= 6


def test_grid_adds_only_on_adverse_close_cross_with_recheck():
    spec, strategy = grid_strategy()
    snap0 = features(Decimal("96"), Decimal("1"))
    # Evidence for the ADD decision must match that decision's own C bucket.
    forecasts = forecasts_with(features(Decimal("92.5"), Decimal("1")), (30, 15))
    holding = holding_state(spec, range_forecasts(snap0)[0].window_end_nano)
    assert holding.budget is not None
    # Floating loss between layers: close 94 sits between layer0 entry and
    # layer1 trigger 93 -> no ADD.
    between = make_context(
        spec, bars=minute_bars([96, 95, 94], reference_slot=21), forecasts=forecasts,
        regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
        features_snap=features(Decimal("94"), Decimal("1")),
        account_snap=holding_account(spec, holding),
        position=PositionSnapshot("SHFE.RB2605", spec.strategy_id,
                                  holding.episode_id or "", holding.budget.layers[0].lots,
                                  Decimal("96"), False))
    proposal = strategy.evaluate(between, holding)
    assert proposal.action == "HOLD"

    # Adverse completed close through 93 proposes exactly the next layer.
    crossed = make_context(
        spec, bars=minute_bars([96, 95, 92.5], reference_slot=21), forecasts=forecasts,
        regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
        features_snap=features(Decimal("92.5"), Decimal("1")),
        account_snap=holding_account(spec, holding),
        position=PositionSnapshot("SHFE.RB2605", spec.strategy_id,
                                  holding.episode_id or "", holding.budget.layers[0].lots,
                                  Decimal("96"), False))
    proposal = strategy.evaluate(crossed, holding)
    assert proposal.action == "ADD"
    assert proposal.layer_index == 1
    assert proposal.trigger_price == L + Decimal("0.15") * W


def test_grid_range_destruction_cancels_adds_and_exits():
    spec, strategy = grid_strategy()
    snap = features(Decimal("89"), Decimal("1"))
    forecasts = range_forecasts(snap)
    holding = holding_state(spec, forecasts[0].window_end_nano)
    context = make_context(
        spec, bars=minute_bars([96, 94, 89]), forecasts=forecasts,
        regime_obj=regime("ORDERED_RANGE", (), frozen_range()), features_snap=snap,
        account_snap=holding_account(spec, holding),
        position=PositionSnapshot("SHFE.RB2605", spec.strategy_id,
                                  holding.episode_id or "", holding.budget.layers[0].lots,
                                  Decimal("96"), False))
    proposal = strategy.evaluate(context, holding)
    assert proposal.action == "EXIT"
    assert "RANGE_BROKEN" in proposal.reasons
    assert proposal.next_state.pending_layer_index is None


def test_grid_whole_round_target_exit_at_center():
    spec, strategy = grid_strategy()
    snap = features(Decimal("100"), Decimal("1"))
    forecasts = range_forecasts(snap)
    holding = holding_state(spec, forecasts[0].window_end_nano)
    assert holding.budget is not None
    filled = _with_lots(holding, (holding.budget.layers[0].lots,
                                  holding.budget.layers[1].lots,
                                  holding.budget.layers[2].lots))
    context = make_context(
        spec, bars=minute_bars([92, 96, 100.2]), forecasts=forecasts,
        regime_obj=regime("ORDERED_RANGE", (), frozen_range()), features_snap=snap,
        account_snap=holding_account(spec, holding),
        position=PositionSnapshot("SHFE.RB2605", spec.strategy_id,
                                  holding.episode_id or "",
                                  sum(layer.lots for layer in holding.budget.layers),
                                  Decimal("95"), False))
    proposal = strategy.evaluate(context, filled)
    assert proposal.action == "EXIT"
    assert "TARGET_REACHED" in proposal.reasons
    assert proposal.target_lots == 0


def _with_lots(state, layer_lots):
    from ak_trading.v2.contracts import StrategyState
    budget = state.budget
    assert budget is not None
    return StrategyState(
        strategy_id=state.strategy_id, episode_id=state.episode_id,
        contract_id=state.contract_id, phase=state.phase, setup=state.setup,
        last_regime=state.last_regime, direction=state.direction,
        filled_layers=(0, 1, 2), layer_filled_lots=tuple(layer_lots),
        pending_layer_index=None, active_exit_nano=state.active_exit_nano,
        pending_exit_nano=None, pending_intent_ids=(), budget=budget,
        confirmation_count=state.confirmation_count,
        cooldown_until_nano=state.cooldown_until_nano,
        last_event_id=state.last_event_id)


def test_grid_wide_range_blocked_without_independent_validation():
    bars, snaps = range_bars_sequence()
    proposal, _ = drive_open("GRID", regime_base="WIDE_RANGE", bars_sequence=bars,
                             features_sequence=snaps, frozen=frozen_range())
    assert proposal.action == "WAIT"
    assert "REGIME_NOT_ALLOWED" in proposal.reasons


def test_grid_layer_cost_coverage_per_spacing():
    spec, strategy = grid_strategy()
    bars, snaps = range_bars_sequence()
    state = None
    proposal = None
    for bars_i, snap_i in zip(bars[:2], snaps[:2]):
        proposal = strategy.evaluate(
            make_context(spec, bars=bars_i, forecasts=range_forecasts(snap_i),
                         regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                         features_snap=snap_i), state)
        state = commit_observation(state, proposal)
    # Spacing to M from worst entry 96.3 is (100-96.3)*10=37 CNY per lot; a
    # 40 CNY round trip is not covered (grid ratio: cover >= 1x).
    blocked = rule(per_lot=Decimal("20"))
    proposal = strategy.evaluate(
        make_context(spec, bars=bars[-1], forecasts=range_forecasts(snaps[-1]),
                     regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                     features_snap=snaps[-1], rule_obj=blocked), state)
    assert proposal.action == "WAIT"
    assert "COST_COVERAGE_INSUFFICIENT" in proposal.reasons
    cheap = rule(per_lot=Decimal("7"))
    proposal = strategy.evaluate(
        make_context(spec, bars=bars[-1], forecasts=range_forecasts(snaps[-1]),
                     regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                     features_snap=snaps[-1], rule_obj=cheap), state)
    assert proposal.action == "OPEN"


def test_round_trip_cost_converts_slippage_points_with_multiplier():
    from ak_trading.v2.strategies.registry import round_trip_cost_per_lot
    from test_v2_m3b_matrix import FeeLeg
    # 1 slippage tick at tick=0.1 -> 0.1 price points per side; per-lot fees
    # 1 CNY each way. Multiplier 10: RT = 2 + 2*0.1*10 = 4 CNY.
    rule10 = TradingRule("r10", "SHFE.RB2605", 0, 10 ** 18, 0, Decimal("10"),
                         Decimal("0.1"), 1, 1, FeeLeg(Decimal("1"), Decimal("0"), False),
                         FeeLeg(Decimal("1"), Decimal("0"), False),
                         FeeLeg(Decimal("1"), Decimal("0"), False), Decimal("0.1"),
                         Decimal("0.1"), None, None, Decimal("1"), Decimal("0"),
                         Decimal("0.1"), "SCENARIO", "0" * 64)
    assert round_trip_cost_per_lot(rule10, Decimal("96"), Decimal("100")) == Decimal("4")
    # Multiplier 100: RT = 2 + 2*0.1*100 = 22 CNY (points are not CNY).
    rule100 = TradingRule("r100", "SHFE.RB2605", 0, 10 ** 18, 0, Decimal("100"),
                          Decimal("0.1"), 1, 1, FeeLeg(Decimal("1"), Decimal("0"), False),
                          FeeLeg(Decimal("1"), Decimal("0"), False),
                          FeeLeg(Decimal("1"), Decimal("0"), False), Decimal("0.1"),
                          Decimal("0.1"), None, None, Decimal("1"), Decimal("0"),
                          Decimal("0.1"), "SCENARIO", "0" * 64)
    assert round_trip_cost_per_lot(rule100, Decimal("96"), Decimal("100")) == Decimal("22")
    # Coverage flips when the points leg is properly converted: gross from a
    # worst entry 96.2 to 100 at multiplier 100 is 380 >= 2*22 -> covered;
    # shrinking the spread to 1.5 points gives 150 < 44+... -> blocked.
    from ak_trading.v2.strategies.registry import cost_coverage_ok
    ok, _ = cost_coverage_ok(rule100, 1, Decimal("96"), Decimal("100"), Decimal("1"),
                             Decimal("1"))
    assert ok
    # Worst entry 99.9 leaves only 0.1 points -> gross 10 < 2*22: blocked.
    blocked, reason = cost_coverage_ok(rule100, 1, Decimal("99.7"), Decimal("100"),
                                       Decimal("1"), Decimal("2"))
    assert not blocked and reason == "COST_COVERAGE_INSUFFICIENT"


def test_grid_no_fourth_layer_and_cap_respected():
    spec, strategy = grid_strategy()
    snap = features(Decimal("91"), Decimal("1"))
    forecasts = range_forecasts(snap)
    holding = holding_state(spec, forecasts[0].window_end_nano)
    assert holding.budget is not None
    full = _with_lots(holding, tuple(layer.lots for layer in holding.budget.layers))
    context = make_context(
        spec, bars=minute_bars([92, 91, 90.5]), forecasts=forecasts,
        regime_obj=regime("ORDERED_RANGE", (), frozen_range()), features_snap=snap,
        account_snap=holding_account(spec, holding),
        position=PositionSnapshot("SHFE.RB2605", spec.strategy_id,
                                  holding.episode_id or "",
                                  sum(layer.lots for layer in holding.budget.layers),
                                  Decimal("93"), False))
    proposal = strategy.evaluate(context, full)
    assert proposal.action in ("HOLD", "EXIT")
    assert proposal.action != "ADD"
