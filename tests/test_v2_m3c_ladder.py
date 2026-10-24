"""M3C P2 ladder tests (08 SS5.2/5.3)."""
import pytest
from decimal import Decimal

"""Shared P2 SYNTHETIC fixtures (M3C-owned; mirrors the P1 test builders).

Geometry follows 08 §10: S0=98/T0=106, A0=2, R0=100, multiplier 10,
tick 0.2, 1 CNY/lot fees, zero slippage. The market-snapshot builder
constructs REAL M2 FeatureSnapshot originals so the D2 resolver runs
end-to-end in tests.
"""
import dataclasses
import datetime

from decimal import Decimal

from ak_trading.v2.contracts import (
    AccountSnapshot, DataQuality, ExitContext, ExitExecutionLink,
    ExitLadderStep, ExitPolicyBinding, ExitState, FactorValue, FeeLeg,
    FeatureSnapshot, Fill, LayerSpec, PositionLot, RoundBudget,
    SessionWindow, StrategyState, StressPolicy, TradingRule, ValuationMark,
    exit_binding_content_hash,
)
from ak_trading.v2.exits.features import (
    MarketKeyFacts, collect_exit_facts, resolve_market_snapshot,
)
from ak_trading.v2.exits.registry import exit_registry_specs, exit_spec_sha256

NANO = 1_767_920_700_000_000_000
MINUTE = 60_000_000_000
HOUR = 60 * MINUTE
DAY1 = datetime.date(2026, 1, 8)
DAY2 = datetime.date(2026, 1, 9)
H64A = "a" * 64
DQ_READY = DataQuality(status="READY", reasons=(), temporal_basis="observed",
                       input_hash=H64A)

SPECS_M1 = exit_registry_specs("MINUTE")
SPECS_D1 = exit_registry_specs("DAILY")


def _spec_of(specs, family, which=0):
    matches = [s for s in specs if s.family == family]
    return matches[which]


LEGACY_SPEC = _spec_of(SPECS_M1, "LEGACY")
TRAILING_SPEC = _spec_of(SPECS_M1, "TRAILING_STOP")
TIME_DECAY_SPEC = _spec_of(SPECS_M1, "TIME_DECAY", 0)
TIME_DECAY_V2_SPEC = _spec_of(SPECS_M1, "TIME_DECAY", 1)
LADDER_SPEC = _spec_of(SPECS_M1, "PROFIT_LADDER")
VOL_SPEC = _spec_of(SPECS_M1, "VOLATILITY_STOP")
DRAWDOWN_SPEC = _spec_of(SPECS_M1, "DRAWDOWN_EXIT")
SCALE_OUT_SPEC = _spec_of(SPECS_M1, "PARTIAL_LIQUIDATION")

from ak_trading.v2.strategies.registry import strategy_registry_v2  # noqa: E402
ENTRY_SPEC = strategy_registry_v2("MINUTE")["trend_m1_v2"].spec


def _p2_rule(tick="0.2", close_fee_per_lot="1", today_fee_per_lot="1",
             lot_step=1, min_open=1):
    return TradingRule(
        rule_id="SYN-RULE-1", contract_id="SYN.rb2609",
        effective_from_nano=NANO - 10 ** 12,
        effective_until_nano=NANO + 10 ** 15,
        available_at_nano=NANO - 10 ** 12, multiplier=Decimal("10"),
        price_tick=Decimal(tick), lot_step=lot_step, min_open_lots=min_open,
        open_fee=FeeLeg(Decimal("1"), Decimal("0"), True),
        close_fee=FeeLeg(Decimal(close_fee_per_lot), Decimal("0"), True),
        close_today_fee=FeeLeg(Decimal(today_fee_per_lot), Decimal("0"), True),
        long_margin_rate=Decimal("0.1"), short_margin_rate=Decimal("0.1"),
        limit_up=None, limit_down=None, slippage_ticks=Decimal("0"),
        impact_ticks=Decimal("0"), participation_rate=Decimal("1"),
        evidence="VERIFIED", source_sha256=H64A)


def _p2_mark(price, as_of_nano, status="CURRENT"):
    if status == "UNVALUED":
        return ValuationMark(contract_id="SYN.rb2609", price=None,
                             as_of_nano=None, valued_at_nano=None,
                             source_bar_id=None, source_bar_sha256=None,
                             rule_id=None, status="UNVALUED",
                             reasons=("PRICE_MISSING",))
    return ValuationMark(
        contract_id="SYN.rb2609", price=Decimal(price), as_of_nano=as_of_nano,
        valued_at_nano=as_of_nano, source_bar_id="SYN-BAR-1",
        source_bar_sha256=H64A, rule_id="SYN-RULE-1", status=status,
        reasons=("PRICE_STALE",) if status == "STALE" else ())


def _p2_session(start, end, day=DAY1, sid="SYN-SESS-1"):
    return SessionWindow(session_id=sid, product="SYN", trading_day=day,
                         start_nano=start, end_nano=end, kind="DAY",
                         calendar_version="cal-v1", evidence_status="VERIFIED")


def _p2_fill(fill_id="SYN-FILL-OPEN-1", intent_id="SYN-INT-OPEN-1",
             offset="OPEN", lots=3, price="100", fee=None, nano=0, side=1):
    return Fill(fill_id=fill_id, intent_id=intent_id, contract_id="SYN.rb2609",
                strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
                execution_nano=NANO + nano, trading_day=DAY1, side=side,
                lots=lots, offset=offset, reference_price=Decimal(price),
                execution_price=Decimal(price),
                fee_cny=Decimal(lots) if fee is None else Decimal(fee),
                slippage_cny=Decimal("0"), rule_id="SYN-RULE-1",
                evidence="VERIFIED")


def _p2_lot(lots=3, price="100", side=1, lot_id="SYN-LOT-1"):
    return PositionLot(lot_id=lot_id, contract_id="SYN.rb2609",
                       strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
                       side=side, lots=lots, opened_at_nano=NANO,
                       opened_trading_day=DAY1, entry_price=Decimal(price),
                       opening_fee_cny=Decimal(lots))


def _p2_account(now, lots=(), quality=None):
    return AccountSnapshot(
        account_id="SYN-ACCOUNT-1", as_of_nano=now, trading_day=DAY1,
        initial_cash_cny=Decimal("100000"), cash_cny=Decimal("100000"),
        realized_gross_cny=Decimal("0"), fees_cny=Decimal("0"),
        slippage_cny=Decimal("0"), unrealized_cny=Decimal("0"),
        equity_cny=Decimal("100000"), margin_cny=Decimal("0"),
        reserved_margin_cny=Decimal("0"),
        day_start_equity_cny=Decimal("100000"), day_net_cny=Decimal("0"),
        peak_equity_cny=Decimal("100000"), lots=tuple(lots), killed=False,
        quality=quality or DQ_READY)


def _p2_budget(side=1, initial_atr="2", stop="98", target="106",
               latest_offset=30 * MINUTE, planned_loss="100"):
    return RoundBudget(
        budget_id="SYN-BUDGET-1", episode_id="SYN-EP-1", contract_id="SYN.rb2609",
        frozen_at_nano=NANO, range_id=None, frozen_range=None,
        stress_policy=StressPolicy(
            policy_id="SYNTHETIC:stress-1", gap_atr_multiple=Decimal("1"),
            locked_trading_days=3, adverse_atr_per_day=Decimal("1"),
            release_extra_atr=Decimal("1"), fee_multiplier=Decimal("1"),
            slippage_multiplier=Decimal("1"), margin_multiplier=Decimal("1")),
        initial_atr_points=Decimal(initial_atr), side=side,
        layers=(LayerSpec(index=0, trigger_price=Decimal("100"),
                          reference_entry_price=Decimal("100"), lots=3),),
        stop_price=Decimal(stop), target_price=Decimal(target),
        latest_exit_nano=NANO + latest_offset, maximum_lots=3,
        planned_loss_cny=Decimal(planned_loss), stress_loss_cny=Decimal("120"),
        reserved_margin_cny=Decimal("1000"), day_loss_limit_cny=Decimal("200"),
        equity_at_start_cny=Decimal("100000"))


def _p2_binding(specs, priority=None):
    values = dict(strategy_id="SYNTHETIC:entry-1", entry_spec_sha256=H64A,
                  exit_spec_sha256s=tuple(sorted({exit_spec_sha256(s) for s in specs})),
                  sizing_spec_sha256=None,
                  priority=priority or tuple(s.exit_policy_id for s in specs),
                  legacy_rules_version="m3b-legacy-rules-v2.1.2")
    return ExitPolicyBinding(binding_sha256=exit_binding_content_hash(**values),
                             **values)


def _p2_state(binding, **overrides):
    values = dict(
        account_id="SYN-ACCOUNT-1", contract_id="SYN.rb2609",
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        first_fill_nano=NANO, exit_binding_sha256=binding.binding_sha256,
        realized_gross_cny=Decimal("0"), fees_paid_cny=Decimal("3"),
        price_peak=None, price_peak_source_nano=None, price_peak_source_hash=None,
        net_peak_r=None, net_peak_source_nano=None, net_peak_source_hash=None,
        armed=False, protection_stop_price=None, protection_line_version=None,
        protection_effective_from_nano=None, confirmations=(),
        no_more_add=False, qbase=3, closed_lots=0, ladder_progress=(),
        phase="MANAGING", last_committed_event_id=None, state_prefix_hash=H64A)
    values.update(overrides)
    return ExitState(**values)


def _ladder_steps(qbase, triggered=(), requested=(), completed=()):
    steps = []
    for index in range(3):
        steps.append(ExitLadderStep(
            level_index=index, triggered=index in triggered,
            requested_lots=requested[index] if index < len(requested) else 0,
            completed_lots=completed[index] if index < len(completed) else 0))
    return tuple(steps)


def _p2_entry_state(budget, active_exit_offset=30 * MINUTE,
                    pending_layer=None, layer_lots=(3,)):
    return StrategyState(
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        contract_id="SYN.rb2609", phase="HOLDING", setup=None, last_regime=None,
        direction=1, filled_layers=(0,) if pending_layer is None else (0,),
        layer_filled_lots=layer_lots, pending_layer_index=pending_layer,
        active_exit_nano=NANO + active_exit_offset,
        pending_exit_nano=None, pending_intent_ids=(), budget=budget,
        confirmation_count=0, cooldown_until_nano=None,
        last_event_id="SYN-FILL-OPEN-1")


def _p2_context(now_offset, mark, *, specs=None, budget=None, lots=None,
                fills=None, sessions=None, previous=None, account=None,
                active_exit_offset=30 * MINUTE, rule=None,
                pending_layer=None, binding=None, layer_lots=(3,)):
    specs = specs or (LADDER_SPEC,)
    budget = budget or _p2_budget()
    lots = [_p2_lot(lots=layer_lots[0])] if lots is None else lots
    lots_total = layer_lots[0] if layer_lots else 3
    fills = fills if fills is not None else [_p2_fill(lots=lots_total)]
    sessions = sessions if sessions is not None else [
        _p2_session(NANO - MINUTE, NANO + 60 * MINUTE)]
    now = NANO + int(now_offset)
    return ExitContext(
        now_nano=now, account_id="SYN-ACCOUNT-1", contract_id="SYN.rb2609",
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        exit_binding=binding or _p2_binding(specs), exit_specs=tuple(specs),
        market_features=(), forecasts=(), entry_spec=ENTRY_SPEC,
        entry_state=_p2_entry_state(budget, active_exit_offset,
                                     pending_layer=pending_layer,
                                     layer_lots=layer_lots),
        budget=budget, account=account or _p2_account(now, lots),
        lots=tuple(lots), marks=(mark,) if mark is not None else (),
        episode_fills=tuple(fills), rules=(rule or _p2_rule(),),
        sessions=tuple(sessions),
        previous_exit_state=previous, continuation=None)


def _market_snapshot(atr, close, decision_nano, duration=60, contract=None,
                     close_key=None, atr_key=None, extra_values=(),
                     quality=None):
    duration_key = f":d{duration}"
    close_k = close_key or f"BASE:close:v2.1.0{duration_key}"
    atr_k = atr_key or f"BASE:atr14:v2.1.0{duration_key}"
    values = [
        FactorValue(factor_key=close_k, value=Decimal(close),
                    available_at_nano=decision_nano, quality=DQ_READY),
        FactorValue(factor_key=atr_k, value=Decimal(atr),
                    available_at_nano=decision_nano, quality=DQ_READY),
    ]
    values.extend(extra_values)
    return FeatureSnapshot(
        snapshot_id=f"SYN-MKT-{decision_nano}", contract_id=contract or "SYN.rb2609",
        raw_symbol="rb2609", product="SYN", trading_day=DAY1,
        session_id="SYN-SESS-1", session_kind="DAY", decision_nano=decision_nano,
        available_at_nano=decision_nano, duration_seconds=duration,
        source_bar_ids=("SYN-BAR-1",), reference_price=None,
        atr_points=Decimal("5"),  # Wilder scale: must be IGNORED by vol-stop
        values=tuple(values), daily_background_id=None,
        quality=quality or DQ_READY)


def _market_facts(atr, close, decision_nano, duration=60, contract=None,
                  keys=None):
    duration_key = f":d{duration}"
    default_keys = (f"BASE:atr14:v2.1.0{duration_key}",
                    f"BASE:close:v2.1.0{duration_key}")
    snapshot = _market_snapshot(atr, close, decision_nano, duration, contract)
    return resolve_market_snapshot(
        (snapshot,), contract or "SYN.rb2609", duration,
        keys or default_keys, decision_nano)


def _link(binding_sha256, proposal_id="SYN-EXIT-1", action="EXIT",
          target=0, requested=3, intent="SYN-INT-1", link_id="SYN-LINK-1"):
    return ExitExecutionLink(
        link_id=link_id, exit_proposal_id=proposal_id,
        strategy_proposal_id="SYN-PROP-1", decision_id="SYN-DEC-1",
        intent_id=intent, exit_binding_sha256=binding_sha256,
        action=action, target_lots=target, requested_lots=requested,
        execution_kind="NEXT_WINDOW_CLOSE")
"""M3C P2 ladder tests: R-threshold staging, three books, one-jump deficit.

SYNTHETIC counterexamples per the P2 design §7.1 and 08 §5.2/§5.3/§10-5.
"""
from decimal import Decimal

import pytest

from ak_trading.v2.exits.profit_ladder import ProfitLadderPolicy
from ak_trading.v2.exits.sizing import (
    cumulative_target_lots, issue_deficit,
)
from ak_trading.v2.exits.state import commit_selected_exit


def _ladder_context(price, *, previous, specs=(LADDER_SPEC,), lots=None,
                    fills=None, rule=None, pending_layer=None, budget=None,
                    binding=None, layer_lots=(3,), now=10 * MINUTE):
    total = layer_lots[0]
    lots = lots if lots is not None else [_p2_lot(lots=total)]
    fills = fills if fills is not None else [_p2_fill(lots=total,
                                                      fee=str(total))]
    return _p2_context(now, _p2_mark(price, NANO + now), specs=specs,
                       previous=previous, lots=lots, fills=fills, rule=rule,
                       pending_layer=pending_layer, budget=budget,
                       binding=binding, layer_lots=layer_lots)


def test_p2_ladder_multi_threshold_jump_one_deficit() -> None:
    # Qbase=7, l=1, 33/67/100% -> D = 2/4/7 (08 §10-5). Nt/R0 jumping from
    # below 0.5 straight past 1.0 latches BOTH stages in one observation and
    # issues exactly ONE cumulative deficit at the highest stage (D=4).
    binding = _p2_binding((LADDER_SPEC,))
    base = _p2_state(binding, qbase=7, closed_lots=0, fees_paid_cny=Decimal("7"))
    # 7 lots @100, fee 1/lot: N = 70*(P-100) - 7 - 7; P=101.9 -> 119 -> 1.19R.
    context = _ladder_context("101.9", previous=base, layer_lots=(7,))
    policy = ProfitLadderPolicy(LADDER_SPEC)
    proposal = policy.evaluate(context, base)
    assert proposal.action == "REDUCE"
    assert proposal.target_lots == 3            # s*(7-4): one target, not a sum
    steps = proposal.next_state.ladder_progress
    assert [step.triggered for step in steps] == [True, True, False]
    # D3: the candidate never pre-writes requests; selection latches them.
    assert all(step.requested_lots == 0 for step in steps)
    selected = commit_selected_exit(base, proposal, (_link(
        binding.binding_sha256, proposal.proposal_id, "REDUCE", 3, 4),))
    assert selected.ladder_progress[1].requested_lots == 4
    assert selected.ladder_progress[1].triggered is True
    assert selected.ladder_progress[0].requested_lots == 0  # subsumed target
    assert selected.no_more_add is True and selected.phase == "REDUCING"


def test_p2_ladder_dedup_no_remultiplication() -> None:
    # Closed=2, pending=1, stage-2 cumulative D=4 -> the M4 deficit is ONE
    # lot; repeated polls keep the same target and never re-multiply the
    # remaining position ("two 50%s never become 75%", 08 §10-5).
    assert issue_deficit(4, 2, 1) == 1
    binding = _p2_binding((LADDER_SPEC,))
    held = _p2_state(binding, qbase=7, closed_lots=2,
                     fees_paid_cny=Decimal("7"),
                     ladder_progress=_ladder_steps(7, triggered=(0, 1)))
    context = _ladder_context("101.9", previous=held, layer_lots=(7,),
                              lots=[_p2_lot(lots=5)])
    policy = ProfitLadderPolicy(LADDER_SPEC)
    first = policy.evaluate(context, held)
    second = policy.evaluate(context, held)
    assert first.action == second.action == "REDUCE"
    assert first.target_lots == second.target_lots == 3   # 7-4, stable
    # A "re-multiply the remainder" input (5 lots * 67%) is rejected by
    # construction: the target stays bound to the frozen Qbase.
    assert abs(first.target_lots) != 5 - 3


def test_p2_ladder_zero_lot_stage_and_qbase_one() -> None:
    # f=0.33 with Qbase=1 -> D=0: the stage latches but issues nothing;
    # only the final stage closes everything.
    binding = _p2_binding((LADDER_SPEC,))
    base = _p2_state(binding, qbase=1, fees_paid_cny=Decimal("1"))
    context = _ladder_context("105.2", previous=base, layer_lots=(1,),
                              lots=[_p2_lot(lots=1)],
                              fills=[_p2_fill(lots=1, fee="1")])
    policy = ProfitLadderPolicy(LADDER_SPEC)
    early = policy.evaluate(context, base)
    assert early.action == "KEEP"
    assert "LADDER_NO_EXECUTABLE_LOTS" in early.reasons
    assert early.next_state.ladder_progress[0].triggered is True
    # Nt/R0 >= 1.5: the final stage is a full EXIT with target 0.
    final_context = _ladder_context("115.2", previous=base, layer_lots=(1,),
                                    lots=[_p2_lot(lots=1)],
                                    fills=[_p2_fill(lots=1, fee="1")])
    final = policy.evaluate(final_context, base)
    assert final.action == "EXIT" and final.target_lots == 0
    assert "LADDER_FINAL_STAGE_EXIT" in final.reasons


def test_p2_ladder_trigger_latch_survives_fallback() -> None:
    # Stage 0 latched at 0.6R; Nt falls to 0.4R: the latch stays, stage 1
    # does not trigger, and the stage-0 target (D=2) still stands.
    binding = _p2_binding((LADDER_SPEC,))
    base = _p2_state(binding, qbase=7, fees_paid_cny=Decimal("7"),
                     ladder_progress=_ladder_steps(7, triggered=(0,)))
    context = _ladder_context("100.6", previous=base, layer_lots=(7,))
    policy = ProfitLadderPolicy(LADDER_SPEC)
    held = policy.evaluate(context, base)
    assert held.action == "REDUCE" and held.target_lots == 5   # 7-2
    fallback_context = _ladder_context("100.4", previous=base, layer_lots=(7,))
    fallback = policy.evaluate(fallback_context, base)
    steps = fallback.next_state.ladder_progress
    assert steps[0].triggered is True and steps[1].triggered is False
    assert fallback.action == "REDUCE" and fallback.target_lots == 5
    # A committed latch cannot be reset by a later candidate.
    import dataclasses as dc
    unlatched = dc.replace(
        fallback.next_state,
        ladder_progress=(dc.replace(steps[0], triggered=False),) + steps[1:])
    from ak_trading.v2.exits.state import ExitStateError, commit_exit_observation
    with pytest.raises(ExitStateError):
        commit_exit_observation(base, dc.replace(fallback,
                                                 next_state=unlatched))


def test_p2_ladder_nli_dimension_not_floating_pnl() -> None:
    # unrealized_pnl_r = 0.6 while episode_net_liquidation_r = 0.315 (heavy
    # close costs): the ladder must NOT trigger off the floating number.
    binding = _p2_binding((LADDER_SPEC,))
    base = _p2_state(binding, qbase=3)
    context = _ladder_context("102", previous=base,
                              rule=_p2_rule(close_fee_per_lot="8.5",
                                            today_fee_per_lot="8.5"))
    facts = collect_exit_facts(context, base)
    assert facts.unrealized == Decimal("60")
    assert facts.net_liquidation == Decimal("31.5")
    policy = ProfitLadderPolicy(LADDER_SPEC)
    proposal = policy.evaluate(context, base)
    assert proposal.action == "KEEP"
    assert "LADDER_LEVEL_TRIGGERED" not in proposal.reasons
    # Nt undecidable: KEEP + NLI_UNVALUED, never a guessed zero.
    unvalued = _p2_context(10 * MINUTE,
                           _p2_mark(None, NANO + 10 * MINUTE, "UNVALUED"),
                           specs=(LADDER_SPEC,), previous=base)
    kept = policy.evaluate(unvalued, base)
    assert kept.action == "KEEP" and "NLI_UNVALUED" in kept.reasons


def test_p2_ladder_qbase_preconditions_and_no_more_add() -> None:
    binding = _p2_binding((LADDER_SPEC,))
    policy = ProfitLadderPolicy(LADDER_SPEC)
    # Pending ADD not terminal: the voluntary precondition fails; hard exits
    # are never blocked by this (they run in the M4 channel).
    pending = _p2_state(binding, qbase=None, closed_lots=0,
                        ladder_progress=())
    context = _ladder_context("101.9", previous=pending, layer_lots=(7,),
                              pending_layer=0)
    proposal = policy.evaluate(context, pending)
    assert proposal.action == "KEEP"
    assert "SIZING_PENDING_ADD_RESOLUTION" in proposal.reasons
    # No pending ADD but no frozen Qbase either.
    unfrozen = _ladder_context("101.9", previous=pending, layer_lots=(7,))
    unfrozen_proposal = policy.evaluate(unfrozen, pending)
    assert "LADDER_QBASE_NOT_FROZEN" in unfrozen_proposal.reasons
    # First voluntary REDUCE selected -> no_more_add latches permanently,
    # surviving later calm observations (no re-opened layers).
    base = _p2_state(binding, qbase=7, fees_paid_cny=Decimal("7"))
    calm = _ladder_context("101.9", previous=base, layer_lots=(7,))
    calm_proposal = policy.evaluate(calm, base)
    selected = commit_selected_exit(
        base, calm_proposal,
        (_link(binding.binding_sha256, calm_proposal.proposal_id,
               "REDUCE", 3, 4),))
    assert selected.no_more_add is True
    after = _ladder_context("101.0", previous=selected, layer_lots=(7,),
                            lots=[_p2_lot(lots=7)])
    later = policy.evaluate(after, selected)
    assert later.next_state.no_more_add is True
