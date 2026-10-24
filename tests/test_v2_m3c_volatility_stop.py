"""M3C P2 volatility-stop tests (D1/D2)."""
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
"""M3C P2 volatility-stop tests: At channel, monotone line, D2 discipline.

SYNTHETIC counterexamples per the P2 design §7.3, D1/D2 acceptance anchors
and 08 §5.3/§10-3.
"""
import dataclasses
from decimal import Decimal

import pytest

from ak_trading.v2.exits.features import (
    MarketChannelUnavailable, resolve_market_snapshot,
)
from ak_trading.v2.exits.state import commit_exit_observation
from ak_trading.v2.exits.volatility_stop import VolatilityStopPolicy
from ak_trading.v2.contracts import DataQuality, FactorValue

DQ_MISSING = DataQuality(status="MISSING", reasons=("WARMUP_BARS",),
                         temporal_basis="unknown", input_hash=H64A)


def _vol_context(previous, *, now=10 * MINUTE, side=1, stop="98",
                 lots=None, mark_price="102"):
    lots = lots if lots is not None else [_p2_lot(side=side)]
    budget = _p2_budget(side=side, stop=stop,
                        target="94" if side == -1 else "106")
    return _p2_context(now, _p2_mark(mark_price, NANO + now),
                       specs=(VOL_SPEC,), previous=previous, lots=lots,
                       budget=budget, fills=[_p2_fill(side=side)])


def test_p2_vol_hand_calc_monotone_under_atr_expansion() -> None:
    # 08 §10-3: with the committed line at 104 (formed at C=105, At=1), At
    # expanding to 4 drops the fresh candidate to 101 — the line STAYS 104.
    # A later At collapse only tightens further; the line never recedes.
    binding = _p2_binding((VOL_SPEC,))
    base = _p2_state(binding)
    policy = VolatilityStopPolicy(VOL_SPEC)
    context = _vol_context(base)
    first = policy.evaluate_with_facts(
        context, base, _market_facts("1", "105", NANO + 10 * MINUTE))
    assert first.action == "KEEP"
    assert first.next_state.armed is True            # no profit gate (D1)
    assert first.next_state.protection_stop_price == Decimal("104")
    committed = commit_exit_observation(base, first)
    # At expands 1 -> 4: candidate 101 < 104, line unchanged.
    expanded = policy.evaluate_with_facts(
        _vol_context(committed), committed,
        _market_facts("4", "105", NANO + 11 * MINUTE))
    assert expanded.next_state.protection_stop_price == Decimal("104")
    assert "VOL_LINE_KEPT" in expanded.reasons
    # Version discipline (D1): an unchanged line keeps its original version
    # and effective boundary — no age refresh.
    assert expanded.next_state.protection_line_version \
        == committed.protection_line_version
    assert expanded.next_state.protection_effective_from_nano \
        == committed.protection_effective_from_nano
    # At collapses to 0.5: candidate 104.5 tightens the line upward only.
    tightened = policy.evaluate_with_facts(
        _vol_context(committed, now=12 * MINUTE), committed,
        _market_facts("0.8", "105", NANO + 12 * MINUTE))
    assert tightened.next_state.protection_stop_price == Decimal("104.2")
    assert "VOL_LINE_TIGHTENED" in tightened.reasons


def test_p2_vol_d2_vector_wilder_ignored_and_tick() -> None:
    # D2 acceptance: BASE ATR=2, Wilder atr_points=5 (ignored), C=105, k=1
    # -> long candidate 103. Wrong-duration keys never satisfy the spec.
    binding = _p2_binding((VOL_SPEC,))
    base = _p2_state(binding)
    context = _vol_context(base)
    policy = VolatilityStopPolicy(VOL_SPEC)
    facts = _market_facts("2", "105", NANO + 10 * MINUTE)
    assert facts.snapshot.atr_points == Decimal("5")   # present and ignored
    proposal = policy.evaluate_with_facts(context, base, facts)
    assert proposal.next_state.protection_stop_price == Decimal("103")
    # A d86400 key cannot feed the d60 spec.
    with pytest.raises(MarketChannelUnavailable):
        resolve_market_snapshot(
            (_market_snapshot("2", "105", NANO + 10 * MINUTE, duration=86400),),
            "SYN.rb2609", 60,
            ("BASE:atr14:v2.1.0:d60", "BASE:close:v2.1.0:d60"),
            NANO + 10 * MINUTE)
    # Tick landing: long 103.01 -> 103.2; short 96.99 -> 96.8 (08 §10-2).
    long_line = policy.evaluate_with_facts(
        context, base, _market_facts("2", "105.01", NANO + 10 * MINUTE))
    assert long_line.next_state.protection_stop_price == Decimal("103.2")
    short_binding = _p2_binding((VOL_SPEC,))
    short_base = _p2_state(short_binding)
    short_context = _vol_context(short_base, side=-1, stop="102",
                                 mark_price="94.99")
    short_line = policy.evaluate_with_facts(
        short_context, short_base, _market_facts("2", "94.99", NANO + 10 * MINUTE))
    assert short_line.next_state.protection_stop_price == Decimal("96.8")


def test_p2_vol_breach_next_window_and_at_missing() -> None:
    # The committed line at 103 with a later close of exactly 103 breaches
    # on equality and only produces a NEXT-window request. A missing At
    # never blocks that breach comparison (D1); a missing close resets the
    # soft confirmation and keeps the line.
    binding = _p2_binding((VOL_SPEC,))
    policy = VolatilityStopPolicy(VOL_SPEC)
    base = _p2_state(binding)
    context = _vol_context(base)
    first = policy.evaluate_with_facts(
        context, base, _market_facts("2", "105", NANO + 10 * MINUTE))
    committed = commit_exit_observation(base, first)
    assert committed.protection_stop_price == Decimal("103")
    breach_context = _vol_context(committed, now=11 * MINUTE)
    # At unavailable (unavailable_keys), close valid at 103: breach fires.
    snapshot = _market_snapshot("2", "103", NANO + 11 * MINUTE)
    from ak_trading.v2.exits.features import MarketKeyFacts
    close_only = MarketKeyFacts(
        snapshot=snapshot, slot_end_nano=snapshot.decision_nano,
        values={f"BASE:close:v2.1.0:d60": next(
            v for v in snapshot.values if v.factor_key.startswith("BASE:close"))},
        unavailable_keys=("BASE:atr14:v2.1.0:d60",),
        reasons=("MARKET_KEY_MISSING:atr14",))
    breached = policy.evaluate_with_facts(breach_context, committed, close_only)
    assert breached.action == "EXIT" and breached.target_lots == 0
    assert breached.earliest_execution_nano > breached.observed_at_nano
    assert "VOL_LINE_BREACHED" in breached.reasons
    assert "VOL_ATR_UNAVAILABLE" in breached.reasons
    # No close at all: evidence insufficient — the confirmation resets to 0
    # and the committed line survives untouched.
    frozen = policy.evaluate_with_facts(breach_context, committed, None,
                                        ("MARKET_SNAPSHOT_MISSING",))
    assert frozen.action == "KEEP"
    assert frozen.next_state.protection_stop_price == Decimal("103")
    assert frozen.next_state.armed is True
    trigger = [c for c in frozen.next_state.confirmations
               if c.rule_id == "trigger"]
    assert trigger and trigger[0].count == 0


def test_p2_vol_short_monotone_no_rewind() -> None:
    # Short line: S0=102, At=1, C=95 -> x=-95, candidate -96 -> line 96.
    # C=93 with At=5 gives a LOOSER candidate (88) — rejected; a valid
    # tighter candidate only moves the line DOWN (short tightening).
    binding = _p2_binding((VOL_SPEC,))
    base = _p2_state(binding)
    policy = VolatilityStopPolicy(VOL_SPEC)
    context = _vol_context(base, side=-1, stop="102", mark_price="95")
    first = policy.evaluate_with_facts(
        context, base, _market_facts("1", "95", NANO + 10 * MINUTE))
    assert first.next_state.protection_stop_price == Decimal("96")
    committed = commit_exit_observation(base, first)
    looser = policy.evaluate_with_facts(
        _vol_context(committed, side=-1, stop="102", mark_price="93",
                     now=11 * MINUTE), committed,
        _market_facts("5", "93", NANO + 11 * MINUTE))
    assert looser.next_state.protection_stop_price == Decimal("96")
    tighter = policy.evaluate_with_facts(
        _vol_context(committed, side=-1, stop="102", mark_price="93",
                     now=12 * MINUTE), committed,
        _market_facts("1", "93", NANO + 12 * MINUTE))
    assert tighter.next_state.protection_stop_price == Decimal("94")
    # A short breach: close 93.9 against the committed line 94 — but the
    # line is only committed after the observation; the breach compares the
    # current completed close against the EFFECTIVE line (94 >= 93.9 short).
    breach = policy.evaluate_with_facts(
        _vol_context(committed, side=-1, stop="102", mark_price="93.9",
                     now=13 * MINUTE), committed,
        _market_facts("1", "93.9", NANO + 13 * MINUTE))
    assert breach.action in ("KEEP", "EXIT")  # line 94 vs close 93.9: x-space
    # s*C = -93.9 <= s*line = -94 is FALSE (line not yet breached) -> KEEP.
    assert breach.action == "KEEP"


def test_p2_vol_no_profit_gate_and_shared_line_merge() -> None:
    # D1 anchor: Nt/R0 = 0.1 (trailing NOT profit-gated) with a live
    # position and valid At/close -> the vol line forms and armed=True;
    # trailing's own candidate stays line-free. The arbitration merge keeps
    # exactly ONE line — the tightest — with no second divergent field.
    specs = (VOL_SPEC, TRAILING_SPEC)
    binding = _p2_binding(specs)
    base = _p2_state(binding)
    vol = VolatilityStopPolicy(VOL_SPEC)
    from ak_trading.v2.exits.trailing import TrailingStopPolicy
    trailing = TrailingStopPolicy(TRAILING_SPEC)
    context = _p2_context(10 * MINUTE, _p2_mark("100.3", NANO + 10 * MINUTE),
                          specs=specs, previous=base, binding=binding)
    vol_candidate = vol.evaluate_with_facts(
        context, base, _market_facts("2", "100.3", NANO + 10 * MINUTE))
    trail_candidate = trailing.evaluate(context, base)
    assert vol_candidate.next_state.armed is True
    assert vol_candidate.next_state.protection_stop_price == Decimal("98.4")
    # Trailing is NOT profit-gated at 0.1R: it neither forms a line nor
    # erases the vol line; its candidate carries no protection of its own.
    assert trail_candidate.next_state.armed is False
    assert trail_candidate.next_state.protection_stop_price is None
    from ak_trading.v2.exits.arbitration import arbitrate_exit
    winner = arbitrate_exit((vol_candidate, trail_candidate), context)
    merged = winner.next_state
    assert merged.armed is True
    assert merged.protection_stop_price == Decimal("98.4")
    assert merged.net_peak_r == Decimal("0.03")  # observation facts preserved


def test_p2_vol_channel_selection_dedup_and_field_quality() -> None:
    # Whole-snapshot selection: the latest decision anchor wins; exact
    # duplicates and two different originals at one anchor are rejected;
    # future anchors are invisible; per-field quality gates the keys and an
    # unrelated invalid factor never vetoes legal ones.
    keys = ("BASE:atr14:v2.1.0:d60", "BASE:close:v2.1.0:d60")
    now = NANO + 10 * MINUTE
    older = _market_snapshot("1", "104", now - MINUTE)
    newer = _market_snapshot("2", "105", now)
    selected = resolve_market_snapshot((older, newer), "SYN.rb2609", 60,
                                       keys, now)
    assert selected.slot_end_nano == now
    with pytest.raises(MarketChannelUnavailable):
        resolve_market_snapshot((newer, newer), "SYN.rb2609", 60, keys, now)
    other = _market_snapshot("3", "106", now)
    other = dataclasses.replace(other, snapshot_id="SYN-MKT-OTHER")
    with pytest.raises(MarketChannelUnavailable):
        resolve_market_snapshot((newer, other), "SYN.rb2609", 60, keys, now)
    future = _market_snapshot("2", "105", now + 5 * MINUTE)
    with pytest.raises(MarketChannelUnavailable):
        resolve_market_snapshot((future,), "SYN.rb2609", 60, keys, now)
    # Unrelated invalid factor + valid close/atr: both keys still usable.
    noisy = _market_snapshot(
        "2", "105", now,
        extra_values=(FactorValue(factor_key="BASE:zscore20:v2.1.0:d60",
                                  value=None, available_at_nano=now,
                                  quality=DQ_MISSING),))
    facts = resolve_market_snapshot((noisy,), "SYN.rb2609", 60, keys, now)
    assert not facts.unavailable_keys
    # A non-READY ATR field makes only that key unusable (no epsilon).
    bad_atr = _market_snapshot("2", "105", now)
    bad_atr = dataclasses.replace(
        bad_atr,
        values=tuple(
            dataclasses.replace(v, quality=DQ_MISSING)
            if v.factor_key.startswith("BASE:atr14") else v
            for v in bad_atr.values))
    degraded = resolve_market_snapshot((bad_atr,), "SYN.rb2609", 60, keys, now)
    assert degraded.unavailable_keys == ("BASE:atr14:v2.1.0:d60",)
    # evaluate() degrades to KEEP while no legal M2 original exists; the
    # dedicated channel-pending reason fires at the resolver level the
    # moment a non-FeatureSnapshot rides the tuple (pre-M1-D2 world).
    policy = VolatilityStopPolicy(VOL_SPEC)
    base = _p2_state(_p2_binding((VOL_SPEC,)))
    empty = policy.evaluate(_vol_context(base), base)
    assert empty.action == "KEEP"
    assert "VOL_ATR_UNAVAILABLE" in empty.reasons
    with pytest.raises(MarketChannelUnavailable) as pending:
        resolve_market_snapshot(("not-a-feature-snapshot",), "SYN.rb2609",
                                60, keys, now)
    assert pending.value.reasons == ("MARKET_CHANNEL_PENDING_M1_D2_TYPE",)
