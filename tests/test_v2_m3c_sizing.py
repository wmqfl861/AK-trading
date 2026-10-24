"""M3C P2 sizing tests (D3 quantity algebra)."""
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
"""M3C P2 sizing tests: D3 quantity algebra, Qbase protocol, conservation.

SYNTHETIC counterexamples per the P2 design §7.2, D3 §3 vectors and
08 §5.3/§10-5.
"""
import dataclasses
from decimal import Decimal

import pytest

from ak_trading.v2.exits.arbitration import arbitrate_exit
from ak_trading.v2.exits.registry import evaluate_exit
from ak_trading.v2.exits.sizing import (
    SizingError, closing_lot_step, cumulative_target_lots, issue_deficit,
    reduce_target_lots, remaining_target_lots, scale_out_enums,
)
from ak_trading.v2.exits.state import (
    ExitStateError, apply_exit_execution, commit_selected_exit, freeze_qbase,
)
from ak_trading.v2.contracts import ExecutionResult


def test_p2_sizing_hand_calc_and_no_summing() -> None:
    # 08 §10-5: Qbase=7, l=1 -> cumulative 2/4/7; with C=2, P=1 the one-shot
    # deficit at stage two is exactly 1. Two 50% rules never sum to 75%.
    assert [cumulative_target_lots(7, Decimal(f), 1, final)
            for f, final in (("0.33", False), ("0.67", False), ("1", True))] \
        == [2, 4, 7]
    assert issue_deficit(4, 2, 1) == 1
    # Qbase=8, two 50% rules: each D=4; the merged remaining target is 4 —
    # never 8 lots ordered and never a sequential 2.
    assert cumulative_target_lots(8, Decimal("0.5"), 1, False) == 4
    assert remaining_target_lots(8, 4) == 4
    # Two REDUCE candidates with targets 4 and 4: arbitrate once -> one
    # winner at |4|, not the sum.
    binding = _p2_binding((LADDER_SPEC, TIME_DECAY_V2_SPEC),
                          priority=(LADDER_SPEC.exit_policy_id,
                                    TIME_DECAY_V2_SPEC.exit_policy_id))
    base = _p2_state(binding, qbase=8, fees_paid_cny=Decimal("8"))
    ladder_candidate = dataclasses.replace(
        _keep_like(binding), target_lots=4, action="REDUCE",
        triggering_policy_id=LADDER_SPEC.exit_policy_id)
    decay_candidate = dataclasses.replace(
        _keep_like(binding), target_lots=4, action="REDUCE",
        triggering_policy_id=TIME_DECAY_V2_SPEC.exit_policy_id)
    context = _p2_context(10 * MINUTE, _p2_mark("102", NANO + 10 * MINUTE),
                          specs=(LADDER_SPEC, TIME_DECAY_V2_SPEC),
                          previous=base, binding=binding, layer_lots=(8,))
    winner = arbitrate_exit((ladder_candidate, decay_candidate), context)
    assert winner.target_lots == 4
    assert winner.triggering_policy_id == LADDER_SPEC.exit_policy_id


def _keep_like(binding):
    from ak_trading.v2.exits.time_decay import TimeDecayPolicy
    context = _p2_context(10 * MINUTE, _p2_mark("102", NANO + 10 * MINUTE),
                          specs=(TIME_DECAY_SPEC,), binding=binding,
                          previous=_p2_state(binding))
    return TimeDecayPolicy(TIME_DECAY_SPEC).evaluate(
        context, context.previous_exit_state)


def test_p2_sizing_lot_step_and_masquerade() -> None:
    # l=2, Qbase=7, f=0.67: floor(4.69/2)*2 = 4; the final stage is the
    # full Qbase (ALL_REMAINDER, no zombie tail). The closing step is
    # TradingRule.lot_step — min_open_lots (5 here) can never masquerade.
    rule = _p2_rule(lot_step=2, min_open=5)
    assert closing_lot_step(rule) == 2
    assert cumulative_target_lots(7, Decimal("0.67"), 2, False) == 4
    assert remaining_target_lots(7, 4) == 3
    assert cumulative_target_lots(7, Decimal("1"), 2, True) == 7
    with pytest.raises(SizingError):
        closing_lot_step(None)                      # unknown rule -> no guess
    with pytest.raises(SizingError):
        cumulative_target_lots(7, Decimal("0.67"), 0, False)
    with pytest.raises(SizingError):
        cumulative_target_lots(0, Decimal("0.67"), 1, False)
    with pytest.raises(SizingError):
        reduce_target_lots(0, 7, 4)                 # side must be -1/+1
    assert scale_out_enums(None) == {
        "sizing_mode": "CUMULATIVE_ORIGINAL_LOTS",
        "lot_rounding": "FLOOR_CLOSE", "final_step": "ALL_REMAINDER"}
    assert scale_out_enums(dict(SCALE_OUT_SPEC.parameters))["final_step"] \
        == "ALL_REMAINDER"


def test_p2_sizing_cross_rule_take_min_d3_vector() -> None:
    # D3 vector: Qbase=7; ladder 67% (needs Nt>=1.0) gives remaining 3 and
    # time-decay params-2 stage one 50% (needs Nt<0.25, elapsed in [0.5,0.75))
    # gives remaining 4. The two trigger conditions cannot hold on the same
    # Nt, so the candidates come from the real policies in their own
    # contexts sharing one binding, then folded by the single arbitrator:
    # the unique merged remaining target is 3. With C=2, P=1 (q=5) M4
    # issues exactly one new lot (5-3-1).
    specs = (LADDER_SPEC, TIME_DECAY_V2_SPEC)
    binding = _p2_binding(specs)
    base = _p2_state(binding, qbase=7, fees_paid_cny=Decimal("7"))
    ladder_ctx = _p2_context(
        10 * MINUTE, _p2_mark("101.9", NANO + 10 * MINUTE),
        specs=specs, previous=base, binding=binding, layer_lots=(7,))
    decay_ctx = _p2_context(
        18 * MINUTE, _p2_mark("100.3", NANO + 18 * MINUTE),
        specs=specs, previous=base, binding=binding, layer_lots=(7,))
    ladder = {p.triggering_policy_id: p
              for p in evaluate_exit(ladder_ctx, base)}[LADDER_SPEC.exit_policy_id]
    assert ladder.action == "REDUCE" and ladder.target_lots == 3
    decay = {p.triggering_policy_id: p
             for p in evaluate_exit(decay_ctx, base)}[TIME_DECAY_V2_SPEC.exit_policy_id]
    assert decay.action == "REDUCE" and decay.target_lots == 4
    winner = arbitrate_exit((ladder, decay), ladder_ctx)
    assert winner.target_lots == 3               # take-min on |remaining|
    assert winner.triggering_policy_id == LADDER_SPEC.exit_policy_id
    # M4 one-shot deficit for the unique target: D*=7-3=4, C=2, P=1 -> 1.
    assert issue_deficit(4, 2, 1) == 1


def test_p2_sizing_qbase_freeze_protocol() -> None:
    binding = _p2_binding((LADDER_SPEC,))
    unfrozen = _p2_state(binding, qbase=None, closed_lots=0,
                         ladder_progress=(), fees_paid_cny=Decimal("3"))
    frozen = freeze_qbase(unfrozen, 7, "qbase:SYN-EP-1")
    assert frozen.qbase == 7 and frozen.closed_lots == 0
    with pytest.raises(ExitStateError):
        freeze_qbase(frozen, 9, "qbase-again")       # never re-frozen
    with pytest.raises(ExitStateError):
        freeze_qbase(unfrozen, 0, "qbase-zero")      # positive lots required
    # Freezing must cover already-closed lots (D3 hard-exit path). The
    # M1 constructor today forbids qbase=None with closed_lots>0 — the D3
    # nullable relaxation is an M1 dependency; until it lands the freeze
    # refuses totals below zero/one and never guesses.
    with pytest.raises(ExitStateError):
        freeze_qbase(_p2_state(binding, qbase=None, closed_lots=0,
                               ladder_progress=()), 0, "qbase-zero-again")
    # Layer books are M3B-side and never shrink here: the ExitState carries
    # no layer fields and freeze touches only qbase + the prefix chain.
    assert frozen.ladder_progress == () and frozen.phase == "MANAGING"
    assert frozen.state_prefix_hash != unfrozen.state_prefix_hash


def test_p2_sizing_upgrade_conservation() -> None:
    # REDUCING in flight (selected remaining 3, q=5, P=2) when a drawdown
    # EXIT is selected: LIQUIDATING wins, the upgrade is irreversible and a
    # later REDUCE can never pull it back. Hard-exit issuance ignores Qbase:
    # max(0, q - P) = 3 new lots at most.
    binding = _p2_binding((LADDER_SPEC, DRAWDOWN_SPEC))
    reducing = _p2_state(binding, qbase=7, closed_lots=2, phase="REDUCING",
                         no_more_add=True, fees_paid_cny=Decimal("7"))
    exit_candidate = _keep_like(binding)
    exit_candidate = dataclasses.replace(
        exit_candidate, action="EXIT", target_lots=0,
        triggering_policy_id=DRAWDOWN_SPEC.exit_policy_id,
        next_state=reducing)
    upgraded = commit_selected_exit(
        reducing, exit_candidate,
        (_link(binding.binding_sha256, exit_candidate.proposal_id,
               "EXIT", 0, 3),))
    assert upgraded.phase == "LIQUIDATING"
    reduce_candidate = dataclasses.replace(
        exit_candidate, action="REDUCE", target_lots=4,
        next_state=upgraded)
    with pytest.raises(ExitStateError):
        commit_selected_exit(upgraded, reduce_candidate,
                             (_link(binding.binding_sha256, "SYN-X",
                                    "REDUCE", 4, 3),))
    # Conservation: with q=5 and P=2 the hard path can issue at most 3.
    q = 7 - 2
    assert max(0, q - 2) == 3


def test_p2_sizing_receipt_books_and_recovery_consistency() -> None:
    # The ladder three books across real fills: requested latched at 4 by
    # selection; completed = min(closed, requested) per stage; cancels and
    # replays advance nothing.
    binding = _p2_binding((LADDER_SPEC,))
    selected = _p2_state(
        binding, qbase=7, closed_lots=0, phase="REDUCING", no_more_add=True,
        fees_paid_cny=Decimal("7"),
        ladder_progress=_ladder_steps(7, triggered=(0, 1), requested=(0, 4)))
    now = NANO + 25 * MINUTE
    link = _link(binding.binding_sha256, action="REDUCE", target=3,
                 requested=1, intent="SYN-INT-CLOSE-1")
    fill = _p2_fill(fill_id="SYN-FILL-CLOSE-1", intent_id="SYN-INT-CLOSE-1",
                    offset="CLOSE", lots=1, price="104", fee="1",
                    nano=23 * MINUTE)
    receipt = ExecutionResult("SYN-INT-CLOSE-1", (fill,), 2, "PARTIAL", ())
    partial = apply_exit_execution(selected, receipt, link,
                                   _p2_account(now, (_p2_lot(lots=5),)),
                                   now, multiplier=Decimal("10"))
    assert partial.closed_lots == 1
    assert partial.ladder_progress[1].completed_lots == 1
    assert partial.ladder_progress[0].completed_lots == 0   # separate books
    assert partial.phase == "REDUCING"                     # target 3 not yet met
    # Cancelled / empty receipts advance nothing (idempotent no-op).
    blocked = ExecutionResult("SYN-INT-CLOSE-1", (), 3, "BLOCKED",
                              ("EXIT_PENDING",))
    unchanged = apply_exit_execution(partial, blocked, link,
                                     _p2_account(now, (_p2_lot(lots=5),)),
                                     now, multiplier=Decimal("10"))
    assert unchanged is partial
    # Reaching the selected remaining target returns MANAGING (no_more_add
    # stays); closing out the round gives CLOSED with full books.
    fill2 = _p2_fill(fill_id="SYN-FILL-CLOSE-2", intent_id="SYN-INT-CLOSE-2",
                     offset="CLOSE", lots=3, price="103", fee="3",
                     nano=24 * MINUTE)
    link2 = _link(binding.binding_sha256, action="REDUCE", target=3,
                  requested=3, intent="SYN-INT-CLOSE-2", link_id="SYN-LINK-2")
    receipt2 = ExecutionResult("SYN-INT-CLOSE-2", (fill2,), 0, "FILLED", ())
    reached = apply_exit_execution(
        partial, receipt2, link2, _p2_account(now, (_p2_lot(lots=4),)), now,
        multiplier=Decimal("10"))
    assert reached.closed_lots == 4
    assert reached.phase == "MANAGING"      # remaining 3 == selected target
    assert reached.no_more_add is True
    fill3 = _p2_fill(fill_id="SYN-FILL-CLOSE-3", intent_id="SYN-INT-CLOSE-3",
                     offset="CLOSE", lots=3, price="103", fee="3",
                     nano=26 * MINUTE)
    link3 = _link(binding.binding_sha256, action="EXIT", target=0,
                  requested=3, intent="SYN-INT-CLOSE-3", link_id="SYN-LINK-3")
    receipt3 = ExecutionResult("SYN-INT-CLOSE-3", (fill3,), 0, "FILLED", ())
    closed = apply_exit_execution(
        _p2_state(binding=binding, qbase=7, closed_lots=4, phase="LIQUIDATING",
                  no_more_add=True, fees_paid_cny=Decimal("7"),
                  first_fill_nano=NANO,
                  ladder_progress=_ladder_steps(7, triggered=(0, 1, 2),
                                                requested=(0, 4, 7),
                                                completed=(0, 4, 4)),
                  exit_binding_sha256=binding.binding_sha256),
        receipt3, link3, _p2_account(now, (_p2_lot(lots=3),)), now,
        multiplier=Decimal("10"))
    assert closed.phase == "CLOSED"
    assert [step.completed_lots for step in closed.ladder_progress] == [0, 4, 7]
