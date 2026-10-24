"""M3C P2 drawdown tests (D1)."""
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
"""M3C P2 drawdown tests: derived arm, giveback equality, peak integrity.

SYNTHETIC counterexamples per the P2 design §7.4, D1 acceptance anchors
and 08 §5.3/§10-4.
"""
from decimal import Decimal

import pytest

from ak_trading.v2.exits.drawdown import DrawdownExitPolicy
from ak_trading.v2.exits.state import (
    commit_exit_observation, commit_selected_exit,
)
from ak_trading.v2.exits.time_decay import TimeDecayPolicy


def _dd_context(price, previous, *, now=10 * MINUTE, lots=None, fills=None):
    # Entry geometry of 08 §10-4: 3 lots @100, one closed @104, 2 remaining.
    close_fill = _p2_fill(fill_id="SYN-FILL-CLOSE-1",
                          intent_id="SYN-INT-CLOSE-1", offset="CLOSE",
                          lots=1, price="104", fee="1", nano=5 * MINUTE)
    lots = lots if lots is not None else [_p2_lot(lots=2)]
    fills = fills if fills is not None else [_p2_fill(), close_fill]
    return _p2_context(now, _p2_mark(price, NANO + now),
                       specs=(DRAWDOWN_SPEC,), previous=previous,
                       lots=lots, fills=fills)


def test_p2_drawdown_hand_calc_no_trigger_then_trigger() -> None:
    # 08 §10-4: RG=40, F=4, K=2, U=60 -> N=94 = 0.94R with a committed peak
    # of 1.2R: giveback 0.26 < 0.5 -> KEEP. Nt falling to 65 (0.65R) gives
    # giveback 0.55 >= 0.5 -> EXIT / target 0.
    binding = _p2_binding((DRAWDOWN_SPEC,))
    peaked = _p2_state(binding, qbase=3, closed_lots=1,
                       fees_paid_cny=Decimal("4"),
                       net_peak_r=Decimal("1.2"),
                       net_peak_source_nano=NANO + 5 * MINUTE,
                       net_peak_source_hash=H64A)
    policy = DrawdownExitPolicy(DRAWDOWN_SPEC)
    calm = policy.evaluate(_dd_context("103", peaked), peaked)
    assert calm.action == "KEEP"
    assert "DRAWDOWN_GIVEBACK_BREACHED" not in calm.reasons
    # P=101.55: U = 2*(101.55-100)*10 = 31 -> N = 40-4+31-2 = 65.
    breached = policy.evaluate(_dd_context("101.55", peaked), peaked)
    assert breached.action == "EXIT" and breached.target_lots == 0
    assert "DRAWDOWN_GIVEBACK_BREACHED" in breached.reasons
    assert breached.earliest_execution_nano > breached.observed_at_nano


def test_p2_drawdown_equality_and_time_decay_contrast() -> None:
    # Peak EXACTLY 1.0R arms (equality counts); giveback EXACTLY 0.5
    # triggers. Contrast: TIME_DECAY's progress threshold at exactly the
    # minimum does NOT trigger ("strictly below", 08 §5.3).
    binding = _p2_binding((DRAWDOWN_SPEC,))
    base = _p2_state(binding, qbase=3, fees_paid_cny=Decimal("3"))
    policy = DrawdownExitPolicy(DRAWDOWN_SPEC)
    # Nt = 1.0R exactly: no prior peak -> the candidate peak is 1.0 -> armed.
    armed = policy.evaluate(_dd_context("103.3", base), base)
    assert armed.action == "KEEP"                    # giveback 0 at the peak
    assert "DRAWDOWN_ARMED" in armed.reasons
    committed = commit_exit_observation(base, armed)
    assert committed.net_peak_r == Decimal("1")
    # Nt falls to 0.5R: giveback exactly 0.5 -> equality triggers.
    fallen = policy.evaluate(_dd_context("100.8", committed), committed)
    assert fallen.action == "EXIT"
    # Contrast with time decay: elapsed 0.75 (equality fires the time side)
    # but Nt/R0 exactly 0.50 does NOT fire "insufficient progress".
    decay_binding = _p2_binding((TIME_DECAY_SPEC,))
    decay_base = _p2_state(decay_binding, qbase=3, fees_paid_cny=Decimal("3"))
    context = _p2_context(
        Decimal("22.5") * MINUTE,
        _p2_mark("102", NANO + int(Decimal("22.5") * MINUTE)),
        specs=(TIME_DECAY_SPEC,), previous=decay_base,
        binding=decay_binding,
        budget=_p2_budget(planned_loss="108"))   # Nt=54 -> exactly 0.50R
    decay = TimeDecayPolicy(TIME_DECAY_SPEC).evaluate(context, decay_base)
    assert decay.action == "KEEP"
    assert "PROGRESS_AT_OR_ABOVE_THRESHOLD" in decay.reasons


def test_p2_drawdown_partial_close_no_fake_collapse() -> None:
    # A ladder leg closing 1/3 keeps RGt inside Nt: the committed peak of
    # 1.2R neither resets nor collapses and closed_fraction never enters
    # the formula — the giveback stays a continuous whole-round quantity.
    binding = _p2_binding((DRAWDOWN_SPEC,))
    peaked = _p2_state(binding, qbase=3, closed_lots=1,
                       fees_paid_cny=Decimal("4"),
                       net_peak_r=Decimal("1.2"),
                       net_peak_source_nano=NANO + 5 * MINUTE,
                       net_peak_source_hash=H64A)
    policy = DrawdownExitPolicy(DRAWDOWN_SPEC)
    from ak_trading.v2.exits.features import collect_exit_facts
    context = _dd_context("103", peaked)
    facts = collect_exit_facts(context, peaked)
    assert facts.closed_fraction == Decimal(1) / Decimal(3)
    assert facts.realized_gross == Decimal("40")   # closed leg retained
    assert facts.net_peak_r == Decimal("1.2")      # peak untouched
    assert facts.giveback_r == Decimal("0.26")
    proposal = policy.evaluate(context, peaked)
    assert proposal.action == "KEEP"
    assert proposal.next_state.net_peak_r == Decimal("1.2")
    assert "closed_fraction" not in " ".join(proposal.reasons)


def test_p2_drawdown_peak_warmup_stale_and_backfill() -> None:
    binding = _p2_binding((DRAWDOWN_SPEC,))
    base = _p2_state(binding, qbase=3, fees_paid_cny=Decimal("3"))
    policy = DrawdownExitPolicy(DRAWDOWN_SPEC)
    # No qualifying net observation yet: peak None -> WARMUP, never a
    # fabricated 0.
    warm = policy.evaluate(
        _p2_context(10 * MINUTE, _p2_mark(None, NANO + 10 * MINUTE, "UNVALUED"),
                    specs=(DRAWDOWN_SPEC,), previous=base), base)
    assert warm.action == "KEEP"
    assert warm.reasons and set(warm.reasons) <= {
        "DRAWDOWN_PEAK_WARMUP", "NLI_UNVALUED", "MARK_UNVALUED",
        "MARK_MISSING", "VALUATION_INCOMPLETE"}
    # STALE mark: the peak is NOT refreshed; a committed peak survives with
    # its original source and a later NLI_UNVALUED window triggers nothing.
    peaked = _p2_state(binding, qbase=3, closed_lots=1,
                       fees_paid_cny=Decimal("4"),
                       net_peak_r=Decimal("1.2"),
                       net_peak_source_nano=NANO + 5 * MINUTE,
                       net_peak_source_hash=H64A)
    stale = policy.evaluate(
        _p2_context(11 * MINUTE, _p2_mark("90", NANO + 11 * MINUTE, "STALE"),
                    specs=(DRAWDOWN_SPEC,), previous=peaked,
                    lots=[_p2_lot(lots=2)],
                    fills=(_p2_fill(),
                           _p2_fill(fill_id="SYN-FILL-CLOSE-1",
                                    intent_id="SYN-INT-CLOSE-1",
                                    offset="CLOSE", lots=1, price="104",
                                    fee="1", nano=5 * MINUTE))), peaked)
    assert stale.action == "KEEP"
    assert stale.next_state.net_peak_r == Decimal("1.2")
    assert stale.next_state.net_peak_source_nano == NANO + 5 * MINUTE
    # Backfill never rewrites the committed decision: the committed state's
    # source hash stays byte-identical after any later observation.
    committed = commit_exit_observation(peaked, stale)
    assert committed.net_peak_source_hash == H64A


def test_p2_drawdown_account_separation_and_derived_only_arm() -> None:
    binding = _p2_binding((DRAWDOWN_SPEC,))
    base = _p2_state(binding, qbase=3, fees_paid_cny=Decimal("3"))
    policy = DrawdownExitPolicy(DRAWDOWN_SPEC)
    # A 12% account drawdown never arms or triggers this family: the key is
    # simply not consumed (the 10% kill switch is the M4 hard channel).
    degraded = _p2_account(
        NANO + 10 * MINUTE, quality=DataQuality(
            status="STALE", reasons=("MARK_STALE",),
            temporal_basis="observed", input_hash=H64A))
    context = _p2_context(
        10 * MINUTE, _p2_mark("103", NANO + 10 * MINUTE),
        specs=(DRAWDOWN_SPEC,), previous=base, account=degraded)
    from ak_trading.v2.exits.features import collect_exit_facts
    facts = collect_exit_facts(context, base)
    assert facts.account_drawdown is None and facts.account_reasons
    proposal = policy.evaluate(context, base)
    assert "ACCOUNT" not in " ".join(proposal.reasons)
    # D1 anchor: drawdown-only binding at 1R -> derived arm WITHOUT the
    # shared armed latch or any protection triple.
    peak_context = _dd_context("103.3", base)
    exited = policy.evaluate(peak_context, base)
    committed = commit_exit_observation(base, exited)
    trigger = _dd_context("100.8", committed)
    final = policy.evaluate(trigger, committed)
    assert final.action == "EXIT"
    assert final.next_state.armed is False
    assert final.next_state.protection_stop_price is None
    selected = commit_selected_exit(
        committed, final,
        (_link(binding.binding_sha256, final.proposal_id, "EXIT", 0, 3),))
    assert selected.armed is False            # EXIT keeps the audit clean
    assert selected.protection_stop_price is None
    assert selected.phase == "LIQUIDATING" and selected.no_more_add is True
