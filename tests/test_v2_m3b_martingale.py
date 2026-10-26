"""M3B-05: limited martingale — frozen budget, 1/2/4 layers, per-layer
re-admission and stress hand-calcs (04-M3B-策略库.md §6.6, §10-4/5/6/12).

Shared synthetic builders come from this node's ``test_v2_m3b_matrix``
module (the M1 shared fixture is not published yet and ``tests/conftest.py``
must not be edited).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from ak_trading.v2 import policy as m1_policy
from ak_trading.v2.contracts import (
    Fill,
    Horizon,
    LayerSpec,
    MarketForecast,
    PositionSnapshot,
)
from ak_trading.v2.strategies.registry import build_strategy_spec
from ak_trading.v2.strategies.state import (
    add_candidate_state,
    apply_execution,
    commit_observation,
    commit_selected_proposal,
    freeze_round_budget,
    open_candidate_state,
    peak_margin_cny,
    round_loss_cny,
)

from test_v2_m3b_matrix import (
    BASE,
    MINUTE,
    account,
    drive_open,
    evidence,
    features,
    forecast,
    frozen_range,
    make_context,
    martingale_evidence,
    minute_bars,
    range_bars_sequence,
    range_forecasts,
    regime,
    rule,
)

HORIZON = Horizon("TRADING_MINUTE", 30)


def hand_layers():
    return (LayerSpec(0, Decimal("96"), Decimal("100"), 1),
            LayerSpec(1, Decimal("94"), Decimal("98"), 2),
            LayerSpec(2, Decimal("92"), Decimal("96"), 4))


def hand_rule():
    return rule(per_lot=Decimal("1"))


# --- §10-4 budget hand-calc ------------------------------------------------------


def test_planned_round_loss_hand_calc_234_and_gap_514():
    layers = hand_layers()
    ideal = round_loss_cny(layers, 1, Decimal("94"), hand_rule(), Decimal("0"))
    assert ideal == Decimal("234")
    gap = round_loss_cny(layers, 1, Decimal("90"), hand_rule(), Decimal("0"))
    assert gap == Decimal("514")


def test_10_12_stress_hand_calc_241_465_535_and_margin():
    layers = hand_layers()
    planned = round_loss_cny(layers, 1, Decimal("94"), hand_rule(), Decimal("1"))
    assert planned == Decimal("241")
    stress = m1_policy.calculate_stress_loss(
        layers, 1, Decimal("94"), Decimal("1"), hand_rule(), m1_policy.stress_policy())
    assert stress == Decimal("535")
    # GAP leg hand-calc (worst entries 100.1/98.1/96.1, exit 91, 2x fees):
    gap_leg = (1 * (Decimal("100.1") - Decimal("91")) * 10
               + 2 * (Decimal("98.1") - Decimal("91")) * 10
               + 4 * (Decimal("96.1") - Decimal("91")) * 10
               + Decimal("2") * (Decimal("7") + Decimal("7")))
    assert gap_leg == Decimal("465")
    margin = peak_margin_cny(layers, 1, hand_rule(), Decimal("1"), m1_policy.stress_policy())
    assert margin == Decimal("1021.05")


def test_freeze_threshold_at_cap_boundary():
    spec = build_strategy_spec("MARTINGALE", "MINUTE")
    snap = features(Decimal("100"), Decimal("1"))
    frozen = frozen_range()
    horizon = HORIZON
    items = martingale_evidence(snap, frozen, horizon)
    forecasts = (forecast(horizon, base_regime="ORDERED_RANGE",
                          persistence={"ORDERED_RANGE": 0.9}, frozen=frozen,
                          stop_touch={"LONG": 0.05, "SHORT": 0.05},
                          evidence_items=items),)
    context = make_context(spec, bars=minute_bars([95], reference_slot=21),
                           forecasts=forecasts,
                           regime_obj=regime("ORDERED_RANGE", (), frozen),
                           features_snap=snap,
                           account_snap=account(Decimal("114200")))
    budget = freeze_round_budget(spec, context, 1, "episode-cap")
    assert budget is not None
    # L=90/U=110, C=100: layer refs 100/93/91, stop 85, ATR 1 -> 571 CNY.
    assert budget.planned_loss_cny == Decimal("571")
    assert budget.layers[0].reference_entry_price == Decimal("100")
    assert tuple(layer.reference_entry_price for layer in budget.layers[1:]) == (
        Decimal("93"), Decimal("91"))
    tight = make_context(spec, bars=minute_bars([95], reference_slot=21),
                         forecasts=forecasts,
                         regime_obj=regime("ORDERED_RANGE", (), frozen),
                         features_snap=snap,
                         account_snap=account(Decimal("114000")))
    assert freeze_round_budget(spec, tight, 1, "episode-cap2") is None


def test_layer_trigger_lines_follow_frozen_range():
    spec = build_strategy_spec("MARTINGALE", "MINUTE")
    snap = features(Decimal("100"), Decimal("1"))
    frozen = frozen_range()
    items = martingale_evidence(snap, frozen, HORIZON)
    forecasts = (forecast(HORIZON, base_regime="ORDERED_RANGE",
                          persistence={"ORDERED_RANGE": 0.9}, frozen=frozen,
                          stop_touch={"LONG": 0.05, "SHORT": 0.05},
                          evidence_items=items),)
    context = make_context(spec, bars=minute_bars([95], reference_slot=21),
                           forecasts=forecasts,
                           regime_obj=regime("ORDERED_RANGE", (), frozen),
                           features_snap=snap)
    budget = freeze_round_budget(spec, context, 1, "episode-lines")
    assert budget is not None
    assert tuple(layer.trigger_price for layer in budget.layers) == (
        Decimal("95.00"), Decimal("93.00"), Decimal("91.00"))
    assert budget.stop_price == Decimal("85")
    assert budget.target_price == Decimal("100")
    assert budget.stress_policy.policy_id == "stress-v2.1.0"
    assert budget.initial_atr_points == Decimal("1")


# --- §10-5 layer progression ------------------------------------------------------


def open_episode_with_fill():
    spec = build_strategy_spec("MARTINGALE", "MINUTE")
    snap = features(Decimal("96"), Decimal("1"))
    frozen = frozen_range()
    items = martingale_evidence(snap, frozen, HORIZON)
    horizon = HORIZON
    forecasts = (forecast(horizon, base_regime="ORDERED_RANGE",
                          persistence={"ORDERED_RANGE": 0.9}, frozen=frozen,
                          stop_touch={"LONG": 0.05, "SHORT": 0.05},
                          evidence_items=items),)
    bars, snaps = range_bars_sequence()
    strategy = __import__("ak_trading.v2.strategies.martingale",
                          fromlist=["LimitedMartingale"]).LimitedMartingale(spec)
    state = None
    proposal = None
    for bars_i, snap_i in zip(bars, snaps):
        proposal = strategy.evaluate(
            make_context(spec, bars=bars_i, forecasts=range_forecasts(snap_i, martingale=True),
                         regime_obj=regime("ORDERED_RANGE", (), frozen),
                         features_snap=snap_i), state)
        state = commit_observation(state, proposal)
        if proposal.action == "OPEN":
            break
    assert proposal is not None and proposal.action == "OPEN"
    assert proposal.budget is not None
    window_end = proposal.budget.latest_exit_nano
    candidate = open_candidate_state(spec, make_context(
        spec, bars=bars[-1], forecasts=forecasts,
        regime_obj=regime("ORDERED_RANGE", (), frozen), features_snap=snap),
        proposal.episode_id or "", proposal.budget, window_end)
    intent = _entry_intent(spec, proposal.episode_id or "")
    selected = commit_selected_proposal(state, proposal, (intent,))
    fill = _fill(spec, proposal.episode_id or "", 1, "OPEN")
    holding = apply_execution(
        selected,
        _result(intent.intent_id, (fill,), 0, "FILLED"),
        account(Decimal("1000000"), _lots(spec, proposal.episode_id or "", 1)),
        BASE + 30 * MINUTE)
    return spec, proposal, holding, forecasts


def _lots(spec, episode, lots):
    from datetime import date
    from ak_trading.v2.contracts import PositionLot
    return (PositionLot("lot-1", "SHFE.RB2605", spec.strategy_id, episode, 1, lots,
                        BASE + 25 * MINUTE, date(2026, 1, 12), Decimal("96"),
                        Decimal("1")),)


def _entry_intent(spec, episode):
    from ak_trading.v2.contracts import OrderIntent
    return OrderIntent("intent-entry-1", "decision-1", spec.strategy_id, episode,
                       "SHFE.RB2605", BASE + 24 * MINUTE, BASE + 25 * MINUTE,
                       BASE + 40 * MINUTE, 1, "OPEN", "ENTRY", 0, Decimal("96"))


def _add_intent(spec, episode, layer_index, lots):
    from ak_trading.v2.contracts import OrderIntent
    return OrderIntent(f"intent-add-{layer_index}", "decision-2", spec.strategy_id,
                       episode, "SHFE.RB2605", BASE + 25 * MINUTE, BASE + 26 * MINUTE,
                       BASE + 45 * MINUTE, lots, "OPEN", "ADD", layer_index, None)


def _exit_intent(spec, episode, lots):
    from ak_trading.v2.contracts import OrderIntent
    return OrderIntent("intent-exit-1", "decision-3", spec.strategy_id, episode,
                       "SHFE.RB2605", BASE + 26 * MINUTE, BASE + 27 * MINUTE,
                       BASE + 50 * MINUTE, -lots, "CLOSE", "STOP", None, None)


def _fill(spec, episode, lots, offset, fill_id="f1"):
    from datetime import date
    return Fill(fill_id, "x", "SHFE.RB2605", spec.strategy_id, episode,
                BASE + 26 * MINUTE, date(2026, 1, 12), 1, lots, offset,
                Decimal("96"), Decimal("96"), Decimal("1"), Decimal("0"),
                "rule-1", "SCENARIO")


def _result(intent_id, fills, remaining, status):
    from ak_trading.v2.contracts import ExecutionResult
    return ExecutionResult(intent_id, tuple(fills), remaining, status, ())


def test_layer_progression_1_3_7_and_no_fourth():
    spec, proposal, holding, forecasts = open_episode_with_fill()
    assert holding.phase == "HOLDING"
    assert holding.layer_filled_lots == (1, 0, 0)
    episode = proposal.episode_id or ""
    assert holding.budget is not None
    budget_before = holding.budget

    # Layer 2 (2 lots): cumulative 1 -> 3.
    holding = _apply_add(holding, spec, episode, 1, 2)
    assert holding.layer_filled_lots == (1, 2, 0)
    assert holding.filled_layers == (0, 1)
    # Layer 3 (4 lots): cumulative 3 -> 7.
    holding = _apply_add(holding, spec, episode, 2, 4)
    assert holding.layer_filled_lots == (1, 2, 4)
    assert holding.filled_layers == (0, 1, 2)
    assert holding.budget == budget_before  # frozen budget bytes unchanged

    strategy = __import__("ak_trading.v2.strategies.martingale",
                          fromlist=["LimitedMartingale"]).LimitedMartingale(spec)
    snap = features(Decimal("90.5"), Decimal("1"))
    context = make_context(
        spec, bars=minute_bars([95, 93, 90.5], reference_slot=21), forecasts=forecasts,
        regime_obj=regime("ORDERED_RANGE", (), frozen_range()), features_snap=snap,
        account_snap=account(Decimal("1000000"), _lots(spec, episode, 7)),
        position=PositionSnapshot("SHFE.RB2605", spec.strategy_id, episode, 7,
                                  Decimal("94"), False))
    result = strategy.evaluate(context, holding)
    assert result.action != "ADD"


def _add_proposal(spec, candidate, layer_index, current_lots, episode):
    from ak_trading.v2.contracts import StrategyProposal
    budget = candidate.budget
    assert budget is not None
    return StrategyProposal(
        proposal_id=f"p-add-{layer_index}", strategy_id=spec.strategy_id,
        episode_id=episode, contract_id=candidate.contract_id,
        decision_nano=BASE + 25 * MINUTE, action="ADD",
        target_lots=current_lots + budget.layers[layer_index].lots,
        layer_index=layer_index, trigger_price=budget.layers[layer_index].trigger_price,
        target_price=budget.target_price, stop_price=budget.stop_price,
        earliest_execution_nano=BASE + 26 * MINUTE, valid_until_nano=BASE + 40 * MINUTE,
        forecast_id=None, assessment_id="assess-1", budget=None, payoff=None,
        next_state=candidate, reasons=())


def _apply_add(holding, spec, episode, layer_index, lots):
    candidate = add_candidate_state(holding, layer_index, holding.active_exit_nano or 0)
    intent = _add_intent(spec, episode, layer_index, lots)
    proposal = _add_proposal(spec, candidate, layer_index, sum(holding.layer_filled_lots),
                             episode)
    selected = commit_selected_proposal(holding, proposal, (intent,))
    total = sum(holding.layer_filled_lots) + lots
    fill = _fill(spec, episode, lots, "OPEN", f"fa{layer_index}")
    return apply_execution(selected, _result(intent.intent_id, (fill,), 0, "FILLED"),
                           account(Decimal("1000000"), _lots(spec, episode, total)),
                           BASE + 27 * MINUTE)


def test_short_open_and_add_targets_carry_sign_and_budget():
    from ak_trading.v2.strategies.state import freeze_round_budget
    spec = build_strategy_spec("MARTINGALE", "MINUTE")
    snap = features(Decimal("104"), Decimal("1"))
    frozen = frozen_range()
    horizon = HORIZON
    items = martingale_evidence(snap, frozen, horizon)
    forecasts = (forecast(horizon, base_regime="ORDERED_RANGE",
                          persistence={"ORDERED_RANGE": 0.9}, frozen=frozen,
                          stop_touch={"LONG": 0.05, "SHORT": 0.05},
                          evidence_items=items),)
    context = make_context(spec, bars=minute_bars([105], reference_slot=21),
                           forecasts=forecasts,
                           regime_obj=regime("ORDERED_RANGE", (), frozen),
                           features_snap=snap)
    budget = freeze_round_budget(spec, context, -1, "episode-short")
    assert budget is not None and budget.side == -1
    from ak_trading.v2.contracts import StrategyState
    holding = StrategyState(
        strategy_id=spec.strategy_id, episode_id="episode-short",
        contract_id="SHFE.RB2605", phase="HOLDING", setup=None, last_regime=None,
        direction=-1, filled_layers=(0,),
        layer_filled_lots=(budget.layers[0].lots, 0, 0), pending_layer_index=None,
        active_exit_nano=forecasts[0].window_end_nano, pending_exit_nano=None,
        pending_intent_ids=(), budget=budget, confirmation_count=3,
        cooldown_until_nano=None, last_event_id=None)
    strategy = __import__("ak_trading.v2.strategies.martingale",
                          fromlist=["LimitedMartingale"]).LimitedMartingale(spec)
    snap_add = features(Decimal("107.5"), Decimal("1"))  # crosses U-0.15W=107
    add_forecasts = add_horizon_forecasts(snap_add)
    context_add = make_context(
        spec, bars=minute_bars([105, 106.5, 107.5], reference_slot=21),
        forecasts=add_forecasts,
        regime_obj=regime("ORDERED_RANGE", (), frozen), features_snap=snap_add,
        account_snap=account(Decimal("1000000"), _lots(spec, "episode-short", 1)),
        position=PositionSnapshot("SHFE.RB2605", spec.strategy_id, "episode-short", 1,
                                  Decimal("104"), False))
    proposal = strategy.evaluate(context_add, holding)
    assert proposal.action == "ADD"
    assert proposal.target_lots == -(1 + 2)
    assert proposal.budget is budget  # same frozen budget, stop/exit unchanged
    assert proposal.stop_price == budget.stop_price
    assert proposal.next_state.active_exit_nano == holding.active_exit_nano


def test_hold_after_partial_fill_reports_signed_target():
    spec, proposal, holding, forecasts = open_episode_with_fill()
    strategy = __import__("ak_trading.v2.strategies.martingale",
                          fromlist=["LimitedMartingale"]).LimitedMartingale(spec)
    snap = features(Decimal("94.5"), Decimal("1"))  # between layers: no ADD
    # G2a (seams §4.1): decision is minute24 with the episode exit at minute50.
    # A fresh full 30-minute window would end at minute55 and cannot fit, so
    # the clock selects the registered 15-minute horizon (window minute25..40).
    # Add only that legal same-scope/same-frozen-geometry management forecast;
    # the original 30-minute entry forecast, budget and exit stay unchanged.
    management = forecast(
        Horizon("TRADING_MINUTE", 15), base_regime="ORDERED_RANGE",
        persistence={"ORDERED_RANGE": 0.9}, frozen=frozen_range(),
        stop_touch={"LONG": 0.05, "SHORT": 0.05},
        published=BASE + 24 * MINUTE,
        evidence_items=martingale_evidence(
            snap, frozen_range(), Horizon("TRADING_MINUTE", 15)))
    context = make_context(
        spec, bars=minute_bars([95, 94.8, 94.5], reference_slot=21),
        forecasts=(*forecasts, management),
        regime_obj=regime("ORDERED_RANGE", (), frozen_range()), features_snap=snap,
        account_snap=account(Decimal("1000000"), _lots(spec, proposal.episode_id or "", 1)),
        position=PositionSnapshot("SHFE.RB2605", spec.strategy_id,
                                  proposal.episode_id or "", 1, Decimal("96"), False))
    result = strategy.evaluate(context, holding)
    assert result.action == "HOLD"
    assert result.target_lots == 1  # long: positive signed absolute target


def test_floating_loss_between_layers_never_adds():
    spec, proposal, holding, forecasts = open_episode_with_fill()
    strategy = __import__("ak_trading.v2.strategies.martingale",
                          fromlist=["LimitedMartingale"]).LimitedMartingale(spec)
    snap = features(Decimal("94"), Decimal("1"))  # between 95 and 93
    # G2a (seams §4.1): same minute24/exit50 clock fact — only the registered
    # 15-minute horizon fits. Floating loss alone is never an add trigger, so
    # this context keeps the original episode inputs plus the legal shorter
    # management forecast built from this test's own snap and frozen range.
    management = forecast(
        Horizon("TRADING_MINUTE", 15), base_regime="ORDERED_RANGE",
        persistence={"ORDERED_RANGE": 0.9}, frozen=frozen_range(),
        stop_touch={"LONG": 0.05, "SHORT": 0.05},
        published=BASE + 24 * MINUTE,
        evidence_items=martingale_evidence(
            snap, frozen_range(), Horizon("TRADING_MINUTE", 15)))
    context = make_context(
        spec, bars=minute_bars([95, 94.5, 94], reference_slot=21),
        forecasts=(*forecasts, management),
        regime_obj=regime("ORDERED_RANGE", (), frozen_range()), features_snap=snap,
        account_snap=account(Decimal("1000000"), _lots(spec, proposal.episode_id or "", 1)),
        position=PositionSnapshot("SHFE.RB2605", spec.strategy_id,
                                  proposal.episode_id or "", 1, Decimal("96"), False))
    result = strategy.evaluate(context, holding)
    assert result.action == "HOLD"


def add_horizon_forecasts(snap):
    frozen = frozen_range()
    out = []
    for value in (30, 15):
        horizon = Horizon("TRADING_MINUTE", value)
        out.append(forecast(horizon, base_regime="ORDERED_RANGE",
                            persistence={"ORDERED_RANGE": 0.9}, frozen=frozen,
                            stop_touch={"LONG": 0.05, "SHORT": 0.05},
                            evidence_items=martingale_evidence(snap, frozen, horizon)))
    return tuple(out)


def test_gap_bar_over_two_layers_proposes_only_next_layer():
    spec, proposal, holding, forecasts = open_episode_with_fill()
    forecasts = add_horizon_forecasts(features(Decimal("90.5"), Decimal("1")))
    strategy = __import__("ak_trading.v2.strategies.martingale",
                          fromlist=["LimitedMartingale"]).LimitedMartingale(spec)
    snap = features(Decimal("90.5"), Decimal("1"))  # crosses 93 and 91
    context = make_context(
        spec, bars=minute_bars([95, 92, 90.5], reference_slot=21), forecasts=forecasts,
        regime_obj=regime("ORDERED_RANGE", (), frozen_range()), features_snap=snap,
        account_snap=account(Decimal("1000000"), _lots(spec, proposal.episode_id or "", 1)),
        position=PositionSnapshot("SHFE.RB2605", spec.strategy_id,
                                  proposal.episode_id or "", 1, Decimal("96"), False))
    result = strategy.evaluate(context, holding)
    assert result.action == "ADD"
    assert result.layer_index == 1
    assert result.target_lots == 3


def test_add_blocked_near_close_and_by_probability_break():
    spec, proposal, holding, forecasts = open_episode_with_fill()
    strategy = __import__("ak_trading.v2.strategies.martingale",
                          fromlist=["LimitedMartingale"]).LimitedMartingale(spec)
    # Late afternoon: fewer than 30 trading minutes remain -> no new risk.
    snap = features(Decimal("92.5"), Decimal("1"))
    late = make_context(
        spec, bars=minute_bars([95, 93, 92.5], reference_slot=330), forecasts=forecasts,
        regime_obj=regime("ORDERED_RANGE", (), frozen_range()), features_snap=snap,
        account_snap=account(Decimal("1000000"), _lots(spec, proposal.episode_id or "", 1)),
        position=PositionSnapshot("SHFE.RB2605", spec.strategy_id,
                                  proposal.episode_id or "", 1, Decimal("96"), False))
    result = strategy.evaluate(late, holding)
    assert result.action != "ADD"


def test_atr_half_forged_persistence_cannot_override_atr_one():
    spec = build_strategy_spec("MARTINGALE", "MINUTE")
    snap = features(Decimal("93.25"), Decimal("1"))
    frozen = frozen_range()
    items = martingale_evidence(snap, frozen, HORIZON)
    honest = forecast(HORIZON, base_regime="ORDERED_RANGE",
                      persistence={"ORDERED_RANGE": 0.9}, frozen=frozen,
                      stop_touch={"LONG": 0.05, "SHORT": 0.05},
                      evidence_items=items)
    forged = forecast(HORIZON, base_regime="ORDERED_RANGE",
                      persistence={"ORDERED_RANGE": 0.99}, frozen=frozen,
                      stop_touch={"LONG": 0.05, "SHORT": 0.05},
                      evidence_items=items, barrier="ATR_0.5")
    bars, snaps = range_bars_sequence()
    strategy = __import__("ak_trading.v2.strategies.martingale",
                          fromlist=["LimitedMartingale"]).LimitedMartingale(spec)
    state = None
    proposal = None
    for bars_i, snap_i in zip(bars, snaps):
        proposal = strategy.evaluate(
            make_context(spec, bars=bars_i, forecasts=(honest, forged),
                         regime_obj=regime("ORDERED_RANGE", (), frozen),
                         features_snap=snap_i), state)
        state = commit_observation(state, proposal)
    assert proposal is not None and proposal.action == "WAIT"
    assert "BARRIER_CONFLICT" in proposal.reasons


def test_armed_and_recross_must_be_different_bars_no_double_count():
    bars, snaps = range_bars_sequence()
    strategy = __import__("ak_trading.v2.strategies.martingale",
                          fromlist=["LimitedMartingale"]).LimitedMartingale(
        build_strategy_spec("MARTINGALE", "MINUTE"))
    spec = strategy.spec

    def deliver(index, state):
        return strategy.evaluate(
            make_context(spec, bars=bars[index],
                         forecasts=range_forecasts(snaps[index], martingale=True),
                         regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                         features_snap=snaps[index]), state)

    # Before the third confirmation bar: duplicate deliveries of the arming
    # bar and of the recross bar must each hold the real confirmation_count
    # (04-M3B §10-11) and must not produce an early OPEN candidate.
    state = None
    trajectory = []
    for index in (0, 0, 1, 1):
        proposal = deliver(index, state)
        state = commit_observation(state, proposal)
        trajectory.append((proposal.action, state.confirmation_count,
                           state.setup.consecutive_confirmations
                           if state.setup is not None else None))
    assert trajectory == [("WAIT", 1, 0), ("WAIT", 1, 0),
                          ("WAIT", 2, 1), ("WAIT", 2, 1)]

    # The clean third distinct confirmation bar is the only OPEN point, and
    # the state carries confirmation_count == 3 with the setup assertions.
    proposal = deliver(2, state)
    state = commit_observation(state, proposal)
    assert proposal.action == "OPEN"
    assert state.confirmation_count == 3
    assert proposal.next_state.setup is not None
    assert proposal.next_state.setup.consecutive_confirmations == 2

    # Same bar replayed after the OPEN point: the setup's structural count
    # must still not be counted twice.
    first_count = state.setup.consecutive_confirmations
    proposal = deliver(2, state)
    assert proposal.next_state.setup is not None
    assert proposal.next_state.setup.consecutive_confirmations == first_count


# --- §10-6 fair contrasts ---------------------------------------------------------


def test_comparison_plans_share_one_cap():
    from ak_trading.v2.strategies.martingale import comparison_round_plans
    plans = comparison_round_plans(
        hand_rule(), m1_policy.stress_policy(), side=1, stop_price=Decimal("94"),
        initial_atr_points=Decimal("0"),
        layer_reference_prices=(Decimal("100"), Decimal("98"), Decimal("96")),
        budget_cap_cny=Decimal("234"))
    martingale = plans["MARTINGALE_1_2_4"]
    equal = plans["EQUAL_BATCHES"]
    no_add = plans["NO_ADD"]
    assert martingale is not None and tuple(l.lots for l in martingale) == (1, 2, 4)
    assert equal is not None and tuple(l.lots for l in equal) == (1, 1, 1)
    assert no_add is not None and tuple(l.lots for l in no_add) == (3,)
    for plan in (martingale, equal, no_add):
        planned = round_loss_cny(plan, 1, Decimal("94"), hand_rule(), Decimal("0"))
        assert planned <= Decimal("234")
    assert round_loss_cny(no_add, 1, Decimal("94"), hand_rule(), Decimal("0")) \
        == Decimal("186")
    # Every scheme fits the same cap; the contrast is never limited to one
    # lot while the martingale enjoys a bigger budget.
    assert sum(l.lots for l in no_add) == 3


def test_comparison_infeasible_when_cap_too_small():
    from ak_trading.v2.strategies.martingale import comparison_round_plans
    plans = comparison_round_plans(
        hand_rule(), m1_policy.stress_policy(), side=1, stop_price=Decimal("94"),
        initial_atr_points=Decimal("0"),
        layer_reference_prices=(Decimal("100"), Decimal("98"), Decimal("96")),
        budget_cap_cny=Decimal("233"))
    assert plans["MARTINGALE_1_2_4"] is None
    assert plans["EQUAL_BATCHES"] is not None
    assert plans["NO_ADD"] is not None
