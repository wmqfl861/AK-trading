"""M3B-06: state machine, partial fills, cooldown and restore (04-M3B §7,
§10-9; plus the partial-fill / BLOCKED-exit interface cases).

Shared synthetic builders come from this node's ``test_v2_m3b_matrix``
module (the M1 shared fixture is not published yet and ``tests/conftest.py``
must not be edited).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ak_trading.v2 import policy as m1_policy
from ak_trading.v2.contracts import (
    AccountSnapshot,
    ExecutionResult,
    Fill,
    Horizon,
    OrderIntent,
    PositionLot,
    PositionSnapshot,
    StrategyState,
)
from ak_trading.v2.strategies.registry import build_strategy_spec, fresh_state
from ak_trading.v2.strategies.state import (
    StateTransitionError,
    add_candidate_state,
    apply_execution,
    commit_observation,
    commit_selected_proposal,
    freeze_round_budget,
    open_candidate_state,
    validate_restored_state,
)

from test_v2_m3b_matrix import (
    BASE,
    MINUTE,
    account,
    features,
    forecast,
    frozen_range,
    make_context,
    martingale_evidence,
    minute_bars,
    range_forecasts,
    regime,
)

HORIZON = Horizon("TRADING_MINUTE", 30)
NANO = 1_000_000_000


def spec_of():
    return build_strategy_spec("MARTINGALE", "MINUTE")


def budget_for(spec, equity=Decimal("1000000")):
    snap = features(Decimal("96"), Decimal("1"))
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
                           features_snap=snap, account_snap=account(equity))
    budget = freeze_round_budget(spec, context, 1, "episode-state")
    assert budget is not None
    return budget, forecasts[0].window_end_nano


def entry_state(spec, budget, window_end):
    snap = features(Decimal("96"), Decimal("1"))
    frozen = frozen_range()
    horizon = HORIZON
    items = martingale_evidence(snap, frozen, horizon)
    forecasts = (forecast(horizon, base_regime="ORDERED_RANGE",
                          persistence={"ORDERED_RANGE": 0.9}, frozen=frozen,
                          stop_touch={"LONG": 0.05, "SHORT": 0.05},
                          evidence_items=items),)
    return open_candidate_state(
        spec, make_context(spec, bars=minute_bars([95], reference_slot=21),
                           forecasts=forecasts,
                           regime_obj=regime("ORDERED_RANGE", (), frozen),
                           features_snap=snap),
        "episode-state", budget, window_end)


def intent_entry(spec, episode, lots=1):
    return OrderIntent("intent-entry", "d1", spec.strategy_id, episode,
                       "SHFE.RB2605", BASE + 24 * MINUTE, BASE + 25 * MINUTE,
                       BASE + 45 * MINUTE, lots, "OPEN", "ENTRY", 0, Decimal("96"))


def intent_add(spec, episode, layer, lots):
    return OrderIntent(f"intent-add-{layer}", "d2", spec.strategy_id, episode,
                       "SHFE.RB2605", BASE + 25 * MINUTE, BASE + 26 * MINUTE,
                       BASE + 45 * MINUTE, lots, "OPEN", "ADD", layer, None)


def intent_exit(spec, episode, lots):
    return OrderIntent("intent-exit", "d3", spec.strategy_id, episode,
                       "SHFE.RB2605", BASE + 26 * MINUTE, BASE + 27 * MINUTE,
                       BASE + 50 * MINUTE, -lots, "CLOSE", "STOP", None, None)


def fill_entry(spec, episode, lots, fill_id="f-entry", offset="OPEN"):
    return Fill(fill_id, "x", "SHFE.RB2605", spec.strategy_id, episode,
                BASE + 26 * MINUTE, date(2026, 1, 12), 1, lots, offset,
                Decimal("96"), Decimal("96"), Decimal("1"), Decimal("0"),
                "rule-1", "SCENARIO")


def fill_exit(spec, episode, lots, fill_id="f-exit"):
    return fill_entry(spec, episode, lots, fill_id, offset="CLOSE")


def result(intent_id, fills=(), remaining=0, status="FILLED"):
    return ExecutionResult(intent_id, tuple(fills), remaining, status, ())


def lots_account(spec, episode, lots):
    lot = PositionLot("lot-1", "SHFE.RB2605", spec.strategy_id, episode, 1, lots,
                      BASE + 26 * MINUTE, date(2026, 1, 12), Decimal("96"), Decimal("1"))
    return account(Decimal("1000000"), (lot,))


# --- OPEN fill / partial fill progression ---------------------------------------


def selected_open(spec=None):
    spec = spec or spec_of()
    budget, window_end = budget_for(spec)
    candidate = entry_state(spec, budget, window_end)
    selected = commit_selected_proposal(
        fresh_state(spec.strategy_id, "SHFE.RB2605"), _open_proposal(spec, candidate),
        (intent_entry(spec, "episode-state"),))
    return spec, selected, budget, window_end


def _open_proposal(spec, candidate):
    from ak_trading.v2.contracts import StrategyProposal
    budget = candidate.budget
    assert budget is not None
    return StrategyProposal(
        proposal_id="p-open", strategy_id=spec.strategy_id, episode_id="episode-state",
        contract_id="SHFE.RB2605", decision_nano=BASE + 24 * MINUTE, action="OPEN",
        target_lots=budget.layers[0].lots, layer_index=0,
        trigger_price=budget.layers[0].trigger_price, target_price=budget.target_price,
        stop_price=budget.stop_price, earliest_execution_nano=BASE + 25 * MINUTE,
        valid_until_nano=window_end_of(candidate), forecast_id=None,
        assessment_id="assess-1", budget=budget, payoff=None, next_state=candidate,
        reasons=())


def window_end_of(candidate):
    return candidate.pending_exit_nano or BASE + 50 * MINUTE


def test_open_fill_commits_active_exit_and_holding():
    spec, selected, budget, window_end = selected_open()
    holding = apply_execution(selected, result("intent-entry", (fill_entry(
        spec, "episode-state", budget.layers[0].lots),)), lots_account(
        spec, "episode-state", budget.layers[0].lots), BASE + 27 * MINUTE)
    assert holding.phase == "HOLDING"
    assert holding.active_exit_nano == min(budget.latest_exit_nano, window_end)
    assert holding.pending_exit_nano is None
    assert holding.filled_layers == (0,)
    assert "intent-entry" not in holding.pending_intent_ids


def test_partial_fill_then_remaining_fill_completes_layer():
    spec, selected, budget, window_end = selected_open()
    episode = "episode-state"
    # 1 of 1 planned layer-0 lots... plan is (1, 2, 4); use an ADD layer for
    # genuine partiality: fill OPEN fully first.
    holding = apply_execution(selected, result("intent-entry", (fill_entry(spec, episode, 1),)),
                              lots_account(spec, episode, 1), BASE + 26 * MINUTE)
    # ADD layer 1 (2 lots): partial 1 lot now, remaining 1 lot later.
    candidate = add_candidate_state(holding, 1, holding.active_exit_nano or 0)
    add_proposal = _add_proposal(spec, candidate, holding)
    selected_add = commit_selected_proposal(holding, add_proposal,
                                            (intent_add(spec, episode, 1, 2),))
    partial = apply_execution(
        selected_add,
        result("intent-add-1", (fill_entry(spec, episode, 1, "f-add-a"),), 1, "PARTIAL"),
        lots_account(spec, episode, 2), BASE + 27 * MINUTE)
    assert partial.phase == "HOLDING"
    assert partial.layer_filled_lots == (1, 1, 0)
    assert partial.pending_layer_index == 1  # layer still owed
    assert "intent-add-1" in partial.pending_intent_ids
    assert partial.active_exit_nano == holding.active_exit_nano  # not extended
    # The remaining fill arrives in a later receipt for the same intent.
    completed = apply_execution(
        partial,
        result("intent-add-1", (fill_entry(spec, episode, 1, "f-add-b"),), 0, "FILLED"),
        lots_account(spec, episode, 3), BASE + 28 * MINUTE)
    assert completed.layer_filled_lots == (1, 2, 0)
    assert completed.filled_layers == (0, 1)
    assert completed.pending_layer_index is None
    assert "intent-add-1" not in completed.pending_intent_ids
    assert completed.active_exit_nano == partial.active_exit_nano


def _add_proposal(spec, candidate, previous):
    from ak_trading.v2.contracts import StrategyProposal
    budget = candidate.budget
    assert budget is not None
    return StrategyProposal(
        proposal_id="p-add", strategy_id=spec.strategy_id,
        episode_id=candidate.episode_id, contract_id="SHFE.RB2605",
        decision_nano=BASE + 25 * MINUTE, action="ADD", target_lots=3, layer_index=1,
        trigger_price=budget.layers[1].trigger_price, target_price=budget.target_price,
        stop_price=budget.stop_price, earliest_execution_nano=BASE + 26 * MINUTE,
        valid_until_nano=candidate.pending_exit_nano or BASE + 45 * MINUTE,
        forecast_id=None, assessment_id="assess-1", budget=None, payoff=None,
        next_state=candidate, reasons=())


def test_duplicate_empty_terminal_replay_is_idempotent():
    spec, selected, budget, _ = selected_open()
    cancelled = apply_execution(selected, result("intent-entry", (), 0, "CANCELLED"),
                                account(), BASE + 26 * MINUTE)
    assert cancelled.phase == "OBSERVE" and cancelled.episode_id is None
    again = apply_execution(cancelled, result("intent-entry", (), 0, "CANCELLED"),
                            account(), BASE + 27 * MINUTE)
    assert again == cancelled


def test_late_new_fills_still_advance_after_partial():
    spec, selected, budget, _ = selected_open()
    episode = "episode-state"
    holding = apply_execution(selected, result("intent-entry", (fill_entry(spec, episode, 1),)),
                              lots_account(spec, episode, 1), BASE + 26 * MINUTE)
    candidate = add_candidate_state(holding, 1, holding.active_exit_nano or 0)
    selected_add = commit_selected_proposal(holding, _add_proposal(spec, candidate, holding),
                                            (intent_add(spec, episode, 1, 2),))
    partial = apply_execution(
        selected_add, result("intent-add-1", (fill_entry(spec, episode, 1, "fa"),), 1, "PARTIAL"),
        lots_account(spec, episode, 2), BASE + 27 * MINUTE)
    late = apply_execution(
        partial, result("intent-add-1", (fill_entry(spec, episode, 1, "fb"),), 0, "FILLED"),
        lots_account(spec, episode, 3), BASE + 30 * MINUTE)
    assert late.layer_filled_lots == (1, 2, 0)


def test_blocked_exit_keeps_pending_and_later_fill_exits():
    spec, selected, budget, _ = selected_open()
    episode = "episode-state"
    holding = apply_execution(selected, result("intent-entry", (fill_entry(spec, episode, 1),)),
                              lots_account(spec, episode, 1), BASE + 26 * MINUTE)
    exit_proposal = _exit_proposal(spec, holding)
    selected_exit = commit_selected_proposal(holding, exit_proposal,
                                             (intent_exit(spec, episode, 1),))
    blocked = apply_execution(selected_exit, result("intent-exit", (), 1, "BLOCKED"),
                              lots_account(spec, episode, 1), BASE + 27 * MINUTE)
    assert blocked.phase == "EXITING"
    assert "intent-exit" in blocked.pending_intent_ids  # exit still owed
    # A later fill for the same exit intent completes the exit.
    paused = apply_execution(blocked, result("intent-exit", (fill_exit(spec, episode, 1),),
                                             0, "FILLED"),
                             account(), BASE + 28 * MINUTE)
    assert paused.phase == "PAUSED"
    assert paused.episode_id is None and paused.budget is None


def _exit_proposal(spec, state):
    from ak_trading.v2.contracts import StrategyProposal
    budget = state.budget
    assert budget is not None
    exiting = StrategyState(
        strategy_id=state.strategy_id, episode_id=state.episode_id,
        contract_id=state.contract_id, phase="EXITING", setup=state.setup,
        last_regime=state.last_regime, direction=state.direction,
        filled_layers=state.filled_layers, layer_filled_lots=state.layer_filled_lots,
        pending_layer_index=None, active_exit_nano=state.active_exit_nano,
        pending_exit_nano=None, pending_intent_ids=(), budget=budget,
        confirmation_count=state.confirmation_count,
        cooldown_until_nano=state.cooldown_until_nano,
        last_event_id=state.last_event_id)
    return StrategyProposal(
        proposal_id="p-exit", strategy_id=spec.strategy_id,
        episode_id=state.episode_id, contract_id="SHFE.RB2605",
        decision_nano=BASE + 26 * MINUTE, action="EXIT", target_lots=0, layer_index=None,
        trigger_price=None, target_price=budget.target_price, stop_price=budget.stop_price,
        earliest_execution_nano=BASE + 27 * MINUTE,
        valid_until_nano=state.active_exit_nano or BASE + 50 * MINUTE,
        forecast_id=None, assessment_id="assess-1", budget=None, payoff=None,
        next_state=exiting, reasons=())


def test_full_open_cancel_clears_candidate_state():
    spec, selected, budget, _ = selected_open()
    cleared = apply_execution(selected, result("intent-entry", (), 0, "CANCELLED"),
                              account(), BASE + 26 * MINUTE)
    assert cleared.phase == "OBSERVE"
    assert cleared.episode_id is None and cleared.budget is None
    assert cleared.setup is None
    assert cleared.layer_filled_lots == () and cleared.pending_intent_ids == ()
    assert cleared.filled_layers == ()
    assert cleared.active_exit_nano is None and cleared.pending_exit_nano is None


def test_add_cancel_keeps_episode_and_layers():
    spec, selected, budget, _ = selected_open()
    episode = "episode-state"
    holding = apply_execution(selected, result("intent-entry", (fill_entry(spec, episode, 1),)),
                              lots_account(spec, episode, 1), BASE + 26 * MINUTE)
    candidate = add_candidate_state(holding, 1, holding.active_exit_nano or 0)
    selected_add = commit_selected_proposal(holding, _add_proposal(spec, candidate, holding),
                                            (intent_add(spec, episode, 1, 2),))
    cancelled = apply_execution(selected_add, result("intent-add-1", (), 2, "CANCELLED"),
                                lots_account(spec, episode, 1), BASE + 27 * MINUTE)
    assert cancelled.phase == "HOLDING"
    assert cancelled.episode_id == episode
    assert cancelled.budget is not None
    assert cancelled.layer_filled_lots == (1, 0, 0)
    assert cancelled.pending_layer_index is None
    assert cancelled.pending_exit_nano is None


def test_partial_add_cancel_keeps_real_layer_and_tightened_exit():
    spec, selected, budget, window_end = selected_open()
    episode = "episode-state"
    holding = apply_execution(selected, result("intent-entry", (fill_entry(spec, episode, 1),)),
                              lots_account(spec, episode, 1), BASE + 26 * MINUTE)
    tightened_exit = holding.active_exit_nano - 15 * MINUTE if holding.active_exit_nano \
        else window_end
    candidate = add_candidate_state(holding, 1, tightened_exit)
    selected_add = commit_selected_proposal(holding, _add_proposal(spec, candidate, holding),
                                            (intent_add(spec, episode, 1, 2),))
    partial = apply_execution(
        selected_add, result("intent-add-1", (fill_entry(spec, episode, 1, "fa"),), 1, "PARTIAL"),
        lots_account(spec, episode, 2), BASE + 27 * MINUTE)
    assert partial.active_exit_nano == tightened_exit
    cancelled = apply_execution(partial, result("intent-add-1", (), 1, "CANCELLED"),
                                lots_account(spec, episode, 2), BASE + 28 * MINUTE)
    assert cancelled.layer_filled_lots == (1, 1, 0)  # real fill never rolled back
    assert cancelled.active_exit_nano == tightened_exit  # tightening not reverted
    assert cancelled.pending_layer_index is None


def test_unknown_intent_and_mismatched_fill_rejected():
    spec, selected, budget, _ = selected_open()
    with pytest.raises(StateTransitionError):
        apply_execution(selected, result("intent-unknown"), account(), BASE + 26 * MINUTE)
    bad = fill_entry(spec, "episode-other", 1)
    with pytest.raises(StateTransitionError):
        apply_execution(selected, result("intent-entry", (bad,)), account(),
                        BASE + 26 * MINUTE)


def test_exit_fill_flat_transitions_to_paused_with_cooldown_anchor():
    spec, selected, budget, window_end = selected_open()
    episode = "episode-state"
    holding = apply_execution(selected, result("intent-entry", (fill_entry(spec, episode, 1),)),
                              lots_account(spec, episode, 1), BASE + 26 * MINUTE)
    from ak_trading.v2.strategies.registry import cooldown_end_nano, ensure_cooldown
    from ak_trading.v2.strategies.martingale import LimitedMartingale
    snap = features(Decimal("96"), Decimal("1"))
    selected_exit = commit_selected_proposal(holding, _exit_proposal(spec, holding),
                                              (intent_exit(spec, episode, 1),))
    exiting = apply_execution(
        selected_exit, result("intent-exit", (fill_exit(spec, episode, 1),), 0, "FILLED"),
        account(), BASE + 27 * MINUTE)
    assert exiting.phase == "PAUSED"
    context = make_context(spec, bars=minute_bars([96], reference_slot=21),
                           forecasts=range_forecasts(snap, martingale=True),
                           regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                           features_snap=snap,
                           account_snap=account(Decimal("1000000"),
                                                tuple(lot for lot in ())),
                           position=None)
    anchored = ensure_cooldown(context, exiting)
    assert anchored.cooldown_until_nano is not None
    assert anchored.cooldown_until_nano > context.now_nano
    proposal = LimitedMartingale(spec).evaluate(context, anchored)
    assert proposal.action == "WAIT"
    assert "COOLDOWN_ACTIVE" in proposal.reasons


def test_killed_account_pushes_to_exiting():
    spec, selected, budget, _ = selected_open()
    episode = "episode-state"
    holding = apply_execution(selected, result("intent-entry", (fill_entry(spec, episode, 1),)),
                              lots_account(spec, episode, 1), BASE + 26 * MINUTE)
    killed_account = AccountSnapshot(
        "acct-1", BASE + 27 * MINUTE, date(2026, 1, 12), Decimal("1000000"),
        Decimal("990000"), Decimal("-10000"), Decimal("0"), Decimal("0"),
        Decimal("-10000"), Decimal("980000"), Decimal("0"), Decimal("0"),
        Decimal("1000000"), Decimal("-20000"), Decimal("1000000"),
        (PositionLot("lot-1", "SHFE.RB2605", spec.strategy_id, episode, 1, 1,
                     BASE + 26 * MINUTE, date(2026, 1, 12), Decimal("96"), Decimal("1")),),
        True, make_quality())
    validate = StrategyState(
        strategy_id=spec.strategy_id, episode_id=episode, contract_id="SHFE.RB2605",
        phase="HOLDING", setup=None, last_regime=None, direction=1,
        filled_layers=(0,), layer_filled_lots=(1, 0, 0), pending_layer_index=None,
        active_exit_nano=holding.active_exit_nano, pending_exit_nano=None,
        pending_intent_ids=(), budget=holding.budget, confirmation_count=3,
        cooldown_until_nano=None, last_event_id=None)
    with pytest.raises(StateTransitionError):
        validate_restored_state(validate, killed_account)
    validate_restored_state(validate, lots_account(spec, episode, 1))


def make_quality():
    from ak_trading.v2.contracts import DataQuality
    return DataQuality("READY", (), "observed", "0" * 64)


# --- restore validation -----------------------------------------------------------


def test_validate_restored_state_rejects_corruption():
    spec, selected, budget, _ = selected_open()
    episode = "episode-state"
    good = StrategyState(
        strategy_id=spec.strategy_id, episode_id=episode, contract_id="SHFE.RB2605",
        phase="HOLDING", setup=None, last_regime=None, direction=1,
        filled_layers=(0,), layer_filled_lots=(1, 0, 0), pending_layer_index=None,
        active_exit_nano=selected.pending_exit_nano, pending_exit_nano=None,
        pending_intent_ids=(), budget=budget, confirmation_count=3,
        cooldown_until_nano=None, last_event_id=None)
    validate_restored_state(good, lots_account(spec, episode, 1))
    flat_state = StrategyState(
        strategy_id=spec.strategy_id, episode_id=episode, contract_id="SHFE.RB2605",
        phase="HOLDING", setup=None, last_regime=None, direction=1,
        filled_layers=(0,), layer_filled_lots=(1, 0, 0), pending_layer_index=None,
        active_exit_nano=selected.pending_exit_nano, pending_exit_nano=None,
        pending_intent_ids=(), budget=budget, confirmation_count=3,
        cooldown_until_nano=None, last_event_id=None)
    with pytest.raises(StateTransitionError):
        validate_restored_state(flat_state, account())  # holding without lots
    foreign = account(Decimal("1000000"), (PositionLot(
        "lot-x", "SHFE.RB2605", spec.strategy_id, "episode-other", 1, 1,
        BASE + 26 * MINUTE, date(2026, 1, 12), Decimal("96"), Decimal("1")),))
    with pytest.raises(StateTransitionError):
        validate_restored_state(good, foreign)


# --- observation / selected commits -------------------------------------------------


def test_commit_observation_never_touches_execution_fields():
    spec, selected, budget, window_end = selected_open()
    episode = "episode-state"
    holding = apply_execution(selected, result("intent-entry", (fill_entry(spec, episode, 1),)),
                              lots_account(spec, episode, 1), BASE + 26 * MINUTE)
    from ak_trading.v2.contracts import StrategyProposal
    tampered = StrategyProposal(
        proposal_id="p-wait", strategy_id=spec.strategy_id, episode_id=None,
        contract_id="SHFE.RB2605", decision_nano=BASE + 27 * MINUTE, action="WAIT",
        target_lots=0, layer_index=None, trigger_price=None, target_price=None,
        stop_price=None, earliest_execution_nano=BASE + 28 * MINUTE,
        valid_until_nano=BASE + 29 * MINUTE, forecast_id=None, assessment_id="assess-1",
        budget=None, payoff=None,
        next_state=StrategyState(
            strategy_id=spec.strategy_id, episode_id=None, contract_id="SHFE.RB2605",
            phase="OBSERVE", setup=None, last_regime=None, direction=0,
            filled_layers=(), layer_filled_lots=(), pending_layer_index=None,
            active_exit_nano=None, pending_exit_nano=None, pending_intent_ids=(),
            budget=None, confirmation_count=0, cooldown_until_nano=None,
            last_event_id=None),
        reasons=())
    observed = commit_observation(holding, tampered)
    assert observed.episode_id == holding.episode_id
    assert observed.budget == holding.budget
    assert observed.layer_filled_lots == holding.layer_filled_lots
    assert observed.phase == holding.phase
    assert observed.pending_intent_ids == holding.pending_intent_ids


def test_commit_selected_proposal_binds_intents_and_rejects_mismatch():
    spec, selected, budget, window_end = selected_open()
    bad_intent = OrderIntent("intent-bad", "d1", "other_strategy", "episode-state",
                             "SHFE.RB2605", BASE, BASE, BASE + 45 * MINUTE, 1,
                             "OPEN", "ENTRY", 0, None)
    with pytest.raises(StateTransitionError):
        commit_selected_proposal(fresh_state(spec.strategy_id, "SHFE.RB2605"),
                                 _open_proposal(spec, selected), (bad_intent,))
    bound = commit_selected_proposal(
        fresh_state(spec.strategy_id, "SHFE.RB2605"), _open_proposal(spec, selected),
        (intent_entry(spec, "episode-state"),))
    assert bound.pending_intent_ids == ("intent-entry",)
    assert bound.phase == "WAIT_TRIGGER"


def test_cannot_rebind_episode():
    spec, selected, budget, window_end = selected_open()
    episode = "episode-state"
    holding = apply_execution(selected, result("intent-entry", (fill_entry(spec, episode, 1),)),
                              lots_account(spec, episode, 1), BASE + 26 * MINUTE)
    other = _open_proposal(spec, open_candidate_state(
        spec, make_context(spec, bars=minute_bars([95]),
                           forecasts=range_forecasts(features(Decimal("96"), Decimal("1")),
                                                     martingale=True),
                           regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                           features_snap=features(Decimal("96"), Decimal("1"))),
        "episode-second", budget, window_end))
    with pytest.raises(StateTransitionError):
        commit_selected_proposal(holding, other, ())


# --- remaining horizons ---------------------------------------------------------------


def test_discrete_add_horizon_selection_and_limits():
    from ak_trading.v2.strategies.registry import select_add_horizon
    spec = spec_of()
    snap = features(Decimal("96"), Decimal("1"))
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
                           features_snap=snap)

    def context_at(now):
        return make_context(spec, bars=minute_bars([95], reference_slot=21),
                            forecasts=forecasts,
                            regime_obj=regime("ORDERED_RANGE", (), frozen),
                            features_snap=snap, now=now)

    active = forecasts[0].window_end_nano
    # One minute before the window start the full 30-minute horizon still
    # fits exactly (slots are strictly after the decision time).
    assert select_add_horizon(context_at(BASE + 19 * MINUTE), active) ==         Horizon("TRADING_MINUTE", 30)
    # With 16 trading minutes left before the active exit: 30/60 no longer
    # fit, 15 still fits exactly (slots run strictly after the decision).
    assert select_add_horizon(context_at(active - 16 * MINUTE), active) ==         Horizon("TRADING_MINUTE", 15)
    # Two minutes before the active exit only the 1-minute horizon fits.
    assert select_add_horizon(context_at(active - 2 * MINUTE), active) ==         Horizon("TRADING_MINUTE", 1)
    # Past the active exit nothing fits.
    assert select_add_horizon(context_at(active + MINUTE), active) is None


def test_add_candidate_never_extends_active_exit():
    spec, selected, budget, window_end = selected_open()
    episode = "episode-state"
    holding = apply_execution(selected, result("intent-entry", (fill_entry(spec, episode, 1),)),
                              lots_account(spec, episode, 1), BASE + 26 * MINUTE)
    assert holding.active_exit_nano is not None
    candidate = add_candidate_state(holding, 1, holding.active_exit_nano + 30 * MINUTE)
    assert candidate.pending_exit_nano == holding.active_exit_nano
