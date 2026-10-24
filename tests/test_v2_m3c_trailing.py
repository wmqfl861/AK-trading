"""M3C P0 contract tests: trailing spec, protection line formula, ATR basis.

OQ-2 Plan A P0 entry ``test_p0_contract_trailing_spec_and_protection_record``
(open-questions-resolution §2). stage=P0 / contract-only: the TRAILING_STOP
whitelist and FROZEN_ATR/COMPLETED_CLOSE literals, the ExitProtectionRecord
type, and the 08 §5.3/§10.2 effective-line formula ``b_t = max(b0,
b_(t-1), h_t - k*A0)`` hand-computed here with pure Decimal arithmetic on
SYNTHETIC closes and stored into M1 records. No ``ak_trading.v2.exits``
implementation exists at P0 and none is imported; the module itself is NOT
claimed verified. M1 types are consumed read-only.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import pytest

from ak_trading.v2.contracts import (
    EXIT_PARAMETER_WHITELISTS,
    DataQuality,
    ExitFeatureValue,
    ExitPolicyBinding,
    ExitProtectionRecord,
    ExitSpec,
    ExitState,
    LayerSpec,
    RoundBudget,
    StressPolicy,
    exit_binding_content_hash,
)
from ak_trading.v2.schemas import decode_record, encode_record

H64 = "a" * 64
H64_B = "b" * 64
H64_C = "c" * 64
NANO = 1_767_920_700_000_000_000
DQ_READY = DataQuality(status="READY", reasons=(), temporal_basis="observed",
                       input_hash=H64)
DQ_INVALID = DataQuality(status="INVALID", reasons=("INITIAL_ATR_NON_POSITIVE",),
                         temporal_basis="unknown", input_hash=H64)

TRAILING_PARAMETERS = {
    "arm_profit_r": "0.5", "trail_atr_multiple": "1",
    "atr_basis": "FROZEN_ATR", "peak_source": "COMPLETED_CLOSE",
}


def _trailing_spec(mode: str = "MINUTE", **overrides) -> ExitSpec:
    suffix, duration = ("m1", 60) if mode == "MINUTE" else ("d1", 86400)
    values = dict(
        exit_policy_id=f"SYNTHETIC:exit_trailing_atr_{suffix}_v1",
        family="TRAILING_STOP", mode=mode, version="1.0.0",
        parameter_version=f"SYNTHETIC:exit_trailing_atr_{suffix}_v1:params-1",
        code_sha256=H64, parameters=dict(TRAILING_PARAMETERS),
        required_market_keys=(f"BASE:close:v2.1.0:d{duration}",),
        required_state_keys=(
            f"EXITSTATE:effective_stop_distance_atr:v1.0.0:d{duration}",
            f"EXITSTATE:episode_net_liquidation_r:v1.0.0:d{duration}",
            f"EXITSTATE:peak_favorable_price:v1.0.0:d{duration}"),
        applicable_entry_families=("BREAKOUT", "PULLBACK", "TREND"),
        observation_duration_seconds=duration,
        execution_timing="NEXT_LEGAL_WINDOW", validation_stage="RESEARCH")
    values.update(overrides)
    return ExitSpec(**values)


def _binding() -> ExitPolicyBinding:
    values = dict(
        strategy_id="SYNTHETIC:entry-1", entry_spec_sha256=H64,
        exit_spec_sha256s=(H64_B, H64_C), sizing_spec_sha256=None,
        priority=("SYNTHETIC:exit_trailing_atr_m1_v1",),
        legacy_rules_version="m3b-legacy-rules-v2.1.2")
    sha = exit_binding_content_hash(**values)
    return ExitPolicyBinding(binding_sha256=sha, **values)


def _budget(initial_atr: Decimal = Decimal("2"), stop: Decimal = Decimal("98"),
            target: Decimal = Decimal("106")) -> RoundBudget:
    return RoundBudget(
        budget_id="SYN-BUDGET-1", episode_id="SYN-EP-1", contract_id="SYN.rb2609",
        frozen_at_nano=NANO, range_id=None, frozen_range=None,
        stress_policy=StressPolicy(
            policy_id="SYNTHETIC:stress-1", gap_atr_multiple=Decimal("1"),
            locked_trading_days=3, adverse_atr_per_day=Decimal("1"),
            release_extra_atr=Decimal("1"), fee_multiplier=Decimal("1"),
            slippage_multiplier=Decimal("1"), margin_multiplier=Decimal("1")),
        initial_atr_points=initial_atr, side=1,
        layers=(LayerSpec(index=0, trigger_price=Decimal("100"),
                          reference_entry_price=Decimal("100"), lots=3),),
        stop_price=stop, target_price=target,
        latest_exit_nano=NANO + 86_400_000_000_000, maximum_lots=3,
        planned_loss_cny=Decimal("100"), stress_loss_cny=Decimal("120"),
        reserved_margin_cny=Decimal("1000"), day_loss_limit_cny=Decimal("200"),
        equity_at_start_cny=Decimal("100000"))


def _state(**overrides) -> ExitState:
    values = dict(
        account_id="SYN-ACCOUNT-1", contract_id="SYN.rb2609",
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        first_fill_nano=NANO, exit_binding_sha256=_binding().binding_sha256,
        realized_gross_cny=Decimal("0"), fees_paid_cny=Decimal("0"),
        price_peak=None, price_peak_source_nano=None, price_peak_source_hash=None,
        net_peak_r=None, net_peak_source_nano=None, net_peak_source_hash=None,
        armed=False, protection_stop_price=None, protection_line_version=None,
        protection_effective_from_nano=None, confirmations=(),
        no_more_add=False, qbase=None, closed_lots=0, ladder_progress=(),
        phase="MANAGING", last_committed_event_id=None, state_prefix_hash=H64)
    values.update(overrides)
    return ExitState(**values)


# ─── 08 §5.3 effective-line chain, computed locally on SYNTHETIC closes ───

def _x(side: int, price: Decimal) -> Decimal:
    """Direction transform x = s*C (long raises x, short lowers it)."""
    return side * price


def _effective_line(b0: Decimal, previous: Decimal, candidate: Decimal) -> Decimal:
    """b_t = max(b0, b_(t-1), candidate_t) — monotone tightening by construction."""
    return max(b0, previous, candidate)


def _peak(side: int, closes: tuple[Decimal, ...]) -> Decimal:
    """h = s*max_u(s*C_u): the max close long-side, negated min close short-side."""
    if side == 1:
        return max(closes)
    return -min(closes)


def _tick_round(side: int, price: Decimal, tick: Decimal) -> Decimal:
    """Long rounds the line UP to tick, short DOWN (tightening direction)."""
    steps = price / tick
    if side == 1:
        return tick * steps.to_integral_value(rounding=ROUND_CEILING)
    return tick * steps.to_integral_value(rounding=ROUND_FLOOR)


def test_p0_contract_trailing_spec_and_protection_record() -> None:
    spec = _trailing_spec()
    assert set(spec.parameters) == EXIT_PARAMETER_WHITELISTS["TRAILING_STOP"]
    assert spec.parameters["atr_basis"] == "FROZEN_ATR"
    assert spec.parameters["peak_source"] == "COMPLETED_CLOSE"
    assert spec.execution_timing == "NEXT_LEGAL_WINDOW"
    assert spec.parameter_version.endswith(":params-1")
    # Counterexamples: whitelist drift, enum literals, value domains.
    with pytest.raises(ValueError, match="exit_parameters_keys"):
        _trailing_spec(parameters={"arm_profit_r": "0.5"})                  # missing
    with pytest.raises(ValueError, match="exit_parameters_keys"):
        _trailing_spec(parameters={**TRAILING_PARAMETERS, "confirmation_bars": 1})
    with pytest.raises(ValueError, match="exit_parameter_atr_basis_enum"):
        _trailing_spec(parameters={**TRAILING_PARAMETERS,
                                   "atr_basis": "BASE_ATR14"})  # P2 basis, not here
    with pytest.raises(ValueError, match="exit_parameter_peak_source_enum"):
        _trailing_spec(parameters={**TRAILING_PARAMETERS,
                                   "peak_source": "INTRADAY_HIGH"})
    with pytest.raises(ValueError, match="arm_profit_r_must_be_positive"):
        _trailing_spec(parameters={**TRAILING_PARAMETERS, "arm_profit_r": "0"})
    with pytest.raises(ValueError, match="trail_atr_multiple_must_be_positive"):
        _trailing_spec(parameters={**TRAILING_PARAMETERS,
                                   "trail_atr_multiple": "-1"})
    with pytest.raises(ValueError, match="invalid_json_value_type"):
        _trailing_spec(parameters={**TRAILING_PARAMETERS, "arm_profit_r": 0.5})
    # A0 lives in the frozen RoundBudget and must be positive; a non-positive
    # A0 cannot even be frozen, so the derived-line branch can only be None
    # plus an INVALID quality record (no epsilon patch-up).
    assert _budget().initial_atr_points == Decimal("2")
    with pytest.raises(ValueError, match="initial_atr_points_must_be_positive"):
        _budget(initial_atr=Decimal("0"))
    invalid_feature = ExitFeatureValue(
        feature_key="EXITSTATE:trailing_atr_distance:v1.0.0:d60", value=None,
        observed_at_nano=NANO, available_at_nano=NANO, quality=DQ_INVALID,
        source_hashes={"SYN.rb2609": H64})
    assert invalid_feature.value is None
    assert invalid_feature.quality.status == "INVALID"
    # Protection record: restored actual price, version identity, timing.
    record = ExitProtectionRecord(
        policy_id=spec.exit_policy_id, stop_price=Decimal("103.2"),
        line_version="trailing-line-v1", effective_from_nano=NANO + 60_000_000_000)
    assert record.line_version == "trailing-line-v1"
    with pytest.raises(ValueError, match="stop_price_must_be_positive"):
        ExitProtectionRecord(policy_id=spec.exit_policy_id,
                             stop_price=Decimal("-103"),
                             line_version="line", effective_from_nano=NANO)
    # armed/protection nullable coupling on the state side.
    with pytest.raises(ValueError, match="armed_requires"):
        _state(armed=True)
    with pytest.raises(ValueError, match="unarmed_requires"):
        _state(protection_stop_price=Decimal("103"))


def test_p0_trailing_effective_line_hand_computed() -> None:
    """08 §10.2 hand arithmetic: long 98/105/k=1/A0=2 → 103, short 102/95 → 97."""
    budget = _budget()  # S0=98, A0=2, side=+1
    k = Decimal(TRAILING_PARAMETERS["trail_atr_multiple"])
    # Long leg: peak 105 gives candidate 103; a later candidate 102 never
    # lowers the effective line; the b0 floor keeps early candidates at 98.
    b0 = _x(1, budget.stop_price)
    h1 = _peak(1, (Decimal("105"),))
    line1 = _effective_line(b0, b0, h1 - k * budget.initial_atr_points)
    assert line1 == Decimal("103")
    line2 = _effective_line(b0, line1, Decimal("102"))
    assert line2 == Decimal("103")                        # one-way tightening
    line_early = _effective_line(b0, b0, Decimal("97"))   # below b0
    assert line_early == b0 == Decimal("98")              # S0 floor (envelope)
    # Short leg (s=-1): S0=102, completed-close peak 95 → effective 97 and
    # never rises back to 98.
    short_budget = _budget(stop=Decimal("102"))
    sb0 = _x(-1, short_budget.stop_price)
    sh = _peak(-1, (Decimal("95"),))
    s_line = _effective_line(sb0, sb0, sh - k * short_budget.initial_atr_points)
    assert -1 * s_line == Decimal("97")
    s_line_next = _effective_line(sb0, s_line, Decimal("-98"))
    assert -1 * s_line_next == Decimal("97")              # no relaxation to 98
    # ATR basis: only the frozen A0 enters the formula. A runtime BASE:atr14
    # expanding from 1 to 4 must not move the line; even a wrongly computed
    # looser candidate cannot pass the max() guard.
    runtime_atr = Decimal("4")
    wrong_candidate = h1 - k * runtime_atr                # 101: would relax
    assert wrong_candidate == Decimal("101")
    assert _effective_line(b0, line1, wrong_candidate) == Decimal("103")
    # Tick placement: long 103.01 → 103.2 (up), short 96.99 → 96.8 (down).
    assert _tick_round(1, Decimal("103.01"), Decimal("0.2")) == Decimal("103.2")
    assert _tick_round(-1, Decimal("96.99"), Decimal("0.2")) == Decimal("96.8")
    # The hand-computed line is representable in the committed M1 records and
    # survives a lossless roundtrip (actual price S_t = s*b_t).
    state = _state(armed=True, protection_stop_price=Decimal("103"),
                   protection_line_version="trailing-line-v1",
                   protection_effective_from_nano=NANO + 60_000_000_000,
                   price_peak=Decimal("105"),
                   price_peak_source_nano=NANO + 60_000_000_000,
                   price_peak_source_hash=H64_B)
    assert state.protection_stop_price == Decimal("103")
    assert decode_record(ExitState, encode_record(state)) == state
    record = ExitProtectionRecord(
        policy_id="SYNTHETIC:exit_trailing_atr_m1_v1", stop_price=Decimal("103.2"),
        line_version="trailing-line-v1",
        effective_from_nano=NANO + 120_000_000_000)
    assert decode_record(ExitProtectionRecord, encode_record(record)) == record
    # Arm latching at state level: once armed the protection triple persists
    # even when the profit arm condition later falls away (08 §5.3).
    relapsed = dataclasses.replace(state, net_peak_r=Decimal("0.3"),
                                   net_peak_source_nano=NANO + 180_000_000_000,
                                   net_peak_source_hash=H64_C)
    assert relapsed.armed is True
    assert relapsed.protection_stop_price == Decimal("103")


def test_p0_trailing_next_window_causality_shape() -> None:
    """First-version execution: completed-close trigger → next legal window only."""
    from ak_trading.v2.contracts import ExitProposal

    observed = NANO + 120_000_000_000          # completion time of the t bar
    earliest = NANO + 180_000_000_000          # next legal window after t
    proposal = ExitProposal(
        proposal_id="SYN-EXIT-P1", strategy_id="SYNTHETIC:entry-1",
        episode_id="SYN-EP-1", contract_id="SYN.rb2609",
        exit_binding_sha256=_binding().binding_sha256, action="EXIT",
        target_lots=0,
        triggering_policy_id="SYNTHETIC:exit_trailing_atr_m1_v1",
        input_sha256=H64, observed_at_nano=observed,
        earliest_execution_nano=earliest,
        protection=ExitProtectionRecord(
            policy_id="SYNTHETIC:exit_trailing_atr_m1_v1",
            stop_price=Decimal("103"), line_version="trailing-line-v1",
            effective_from_nano=observed),
        next_state=_state(armed=True, protection_stop_price=Decimal("103"),
                          protection_line_version="trailing-line-v1",
                          protection_effective_from_nano=observed),
        reasons=("TRAILING_LINE_BREACHED",))
    assert proposal.earliest_execution_nano > proposal.observed_at_nano
    assert all("STOP" not in reason and "TARGET" not in reason
               and "ROLL" not in reason for reason in proposal.reasons)
    # Counterexamples: the contract forbids same-bar execution claims and a
    # wrong-shaped candidate state.
    with pytest.raises(ValueError, match="precede_observation"):
        dataclasses.replace(proposal, earliest_execution_nano=observed - 1)
    with pytest.raises(ValueError, match="scope_must_match"):
        dataclasses.replace(proposal, next_state=_state(episode_id="SYN-EP-2"))
    with pytest.raises(ValueError, match="zero_target_lots"):
        dataclasses.replace(proposal, target_lots=2)
    with pytest.raises(TypeError, match="next_state_must_be"):
        dataclasses.replace(proposal, next_state=None)
    assert decode_record(ExitProposal, encode_record(proposal)) == proposal
    # OQ-4 byte roundtrip for the binding carried by the proposal.
    binding = _binding()
    payload = {
        "strategy_id": binding.strategy_id,
        "entry_spec_sha256": binding.entry_spec_sha256,
        "exit_spec_sha256s": list(binding.exit_spec_sha256s),
        "sizing_spec_sha256": binding.sizing_spec_sha256,
        "priority": list(binding.priority),
        "legacy_rules_version": binding.legacy_rules_version,
    }
    canonical = (json.dumps(payload, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == binding.binding_sha256
    assert proposal.exit_binding_sha256 == binding.binding_sha256


# ═══ P1 behavior: the frozen-ATR trailing policy over real ExitContexts ═══
# Fixtures below are M3C-owned SYNTHETIC builders (P0 fixtures above stay
# untouched). Entry spec is the real frozen M3B TREND spec; the budget is the
# 08 §10.2 hand-calc geometry (S0=98, A0=2, tick=0.2, R0=100).

import dataclasses  # noqa: E402
import datetime  # noqa: E402

from ak_trading.v2.contracts import (  # noqa: E402
    AccountSnapshot, ExitContext, FeeLeg, Fill, PositionLot, SessionWindow,
    StrategyState, TradingRule, ValuationMark,
)
from ak_trading.v2.strategies.registry import strategy_registry_v2  # noqa: E402
from ak_trading.v2.exits.features import collect_exit_facts  # noqa: E402
from ak_trading.v2.exits.registry import exit_registry_specs, exit_spec_sha256  # noqa: E402
from ak_trading.v2.exits.state import (  # noqa: E402
    advance_state, commit_exit_observation, initialize_exit_state,
)
from ak_trading.v2.exits.trailing import TrailingStopPolicy  # noqa: E402

MINUTE = 60_000_000_000
DAY1 = datetime.date(2026, 1, 8)
P1_SPECS = exit_registry_specs("MINUTE")
TRAILING_SPEC = next(s for s in P1_SPECS if s.family == "TRAILING_STOP")
ENTRY_SPEC = strategy_registry_v2("MINUTE")["trend_m1_v2"].spec


def _p1_rule(tick: str = "0.2") -> TradingRule:
    return TradingRule(
        rule_id="SYN-RULE-1", contract_id="SYN.rb2609",
        effective_from_nano=NANO - 10 ** 12,
        effective_until_nano=NANO + 10 ** 15,
        available_at_nano=NANO - 10 ** 12, multiplier=Decimal("10"),
        price_tick=Decimal(tick), lot_step=1, min_open_lots=1,
        open_fee=FeeLeg(Decimal("1"), Decimal("0"), True),
        close_fee=FeeLeg(Decimal("1"), Decimal("0"), True),
        close_today_fee=FeeLeg(Decimal("1"), Decimal("0"), True),
        long_margin_rate=Decimal("0.1"), short_margin_rate=Decimal("0.1"),
        limit_up=None, limit_down=None, slippage_ticks=Decimal("0"),
        impact_ticks=Decimal("0"), participation_rate=Decimal("1"),
        evidence="VERIFIED", source_sha256=H64)


def _p1_mark(price, as_of_offset_nano, status="CURRENT"):
    if status == "UNVALUED":
        return ValuationMark(contract_id="SYN.rb2609", price=None,
                             as_of_nano=None, valued_at_nano=None,
                             source_bar_id=None, source_bar_sha256=None,
                             rule_id=None, status="UNVALUED",
                             reasons=("PRICE_MISSING",))
    return ValuationMark(
        contract_id="SYN.rb2609", price=Decimal(price),
        as_of_nano=NANO + as_of_offset_nano,
        valued_at_nano=NANO + as_of_offset_nano,
        source_bar_id="SYN-BAR-1", source_bar_sha256=H64,
        rule_id="SYN-RULE-1", status=status,
        reasons=("PRICE_STALE",) if status == "STALE" else ())


def _p1_session(start, end, sid="SYN-SESS-1"):
    return SessionWindow(session_id=sid, product="SYN", trading_day=DAY1,
                         start_nano=start, end_nano=end, kind="DAY",
                         calendar_version="cal-v1", evidence_status="VERIFIED")


def _p1_open_fill(offset_nano=0, lots=3, price="100", side=1):
    return Fill(fill_id="SYN-FILL-OPEN-1", intent_id="SYN-INT-OPEN-1",
                contract_id="SYN.rb2609", strategy_id="SYNTHETIC:entry-1",
                episode_id="SYN-EP-1", execution_nano=NANO + offset_nano,
                trading_day=DAY1, side=side, lots=lots, offset="OPEN",
                reference_price=Decimal(price), execution_price=Decimal(price),
                fee_cny=Decimal(lots), slippage_cny=Decimal("0"),
                rule_id="SYN-RULE-1", evidence="VERIFIED")


def _p1_lot(lots=3, price="100", side=1):
    return PositionLot(lot_id="SYN-LOT-1", contract_id="SYN.rb2609",
                       strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
                       side=side, lots=lots, opened_at_nano=NANO,
                       opened_trading_day=DAY1, entry_price=Decimal(price),
                       opening_fee_cny=Decimal(lots))


def _p1_account(now, lots=()):
    return AccountSnapshot(
        account_id="SYN-ACCOUNT-1", as_of_nano=now, trading_day=DAY1,
        initial_cash_cny=Decimal("100000"), cash_cny=Decimal("100000"),
        realized_gross_cny=Decimal("0"), fees_cny=Decimal("0"),
        slippage_cny=Decimal("0"), unrealized_cny=Decimal("0"),
        equity_cny=Decimal("100000"), margin_cny=Decimal("0"),
        reserved_margin_cny=Decimal("0"),
        day_start_equity_cny=Decimal("100000"), day_net_cny=Decimal("0"),
        peak_equity_cny=Decimal("100000"), lots=tuple(lots), killed=False,
        quality=DQ_READY)


def _p1_budget(side=1, initial_atr="2", stop="98", target="106",
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
        latest_exit_nano=NANO + latest_offset,
        maximum_lots=3, planned_loss_cny=Decimal(planned_loss),
        stress_loss_cny=Decimal("120"), reserved_margin_cny=Decimal("1000"),
        day_loss_limit_cny=Decimal("200"), equity_at_start_cny=Decimal("100000"))


def _p1_binding(specs, priority=None):
    values = dict(strategy_id="SYNTHETIC:entry-1", entry_spec_sha256=H64,
                  exit_spec_sha256s=tuple(sorted({exit_spec_sha256(s) for s in specs})),
                  sizing_spec_sha256=None,
                  priority=priority or tuple(s.exit_policy_id for s in specs),
                  legacy_rules_version="m3b-legacy-rules-v2.1.2")
    return ExitPolicyBinding(binding_sha256=exit_binding_content_hash(**values),
                             **values)


def _p1_state(binding, **overrides):
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
        phase="MANAGING", last_committed_event_id=None, state_prefix_hash=H64)
    values.update(overrides)
    return ExitState(**values)


def _p1_entry_state(budget, active_exit_offset=30 * MINUTE):
    return StrategyState(
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        contract_id="SYN.rb2609", phase="HOLDING", setup=None, last_regime=None,
        direction=1, filled_layers=(0,), layer_filled_lots=(3,),
        pending_layer_index=None, active_exit_nano=NANO + active_exit_offset,
        pending_exit_nano=None, pending_intent_ids=(), budget=budget,
        confirmation_count=0, cooldown_until_nano=None,
        last_event_id="SYN-FILL-OPEN-1")


def _p1_context(now_offset, mark, previous=None, specs=None, budget=None,
                lots=None, fills=None, sessions=None, side=1):
    specs = specs or (TRAILING_SPEC,)
    budget = budget or _p1_budget(side=side)
    lots = [_p1_lot(side=side)] if lots is None else lots
    fills = [_p1_open_fill(side=side)] if fills is None else fills
    sessions = sessions if sessions is not None else [
        _p1_session(NANO - MINUTE, NANO + 60 * MINUTE)]
    now = NANO + now_offset
    context = ExitContext(
        now_nano=now, account_id="SYN-ACCOUNT-1", contract_id="SYN.rb2609",
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        exit_binding=_p1_binding(specs), exit_specs=tuple(specs),
        market_features=(), forecasts=(), entry_spec=ENTRY_SPEC,
        entry_state=_p1_entry_state(budget), budget=budget,
        account=_p1_account(now, lots), lots=tuple(lots), marks=(mark,),
        episode_fills=tuple(fills), rules=(_p1_rule(),),
        sessions=tuple(sessions),
        previous_exit_state=previous, continuation=None)
    return context, budget


def _policy():
    return TrailingStopPolicy(TRAILING_SPEC)


def test_p1_trailing_arms_then_latches_line() -> None:
    # Counterexample 3: arm at Nt/R0>=0.5 then profit falls back — the line
    # must persist and still breach later (never disarmed).
    binding = _p1_binding((TRAILING_SPEC,))
    policy = _policy()
    context, _ = _p1_context(22 * MINUTE, _p1_mark("102", 22 * MINUTE),
                             previous=_p1_state(binding))
    first = policy.evaluate(context, context.previous_exit_state)
    assert first.action == "KEEP"
    assert "TRAILING_ARMED" in first.reasons
    assert first.next_state.armed is True
    assert first.next_state.protection_stop_price == Decimal("100")  # 102-1*2
    committed = commit_exit_observation(context.previous_exit_state, first)
    assert committed.armed is True
    # Profit falls back towards 0.09R: latch holds, line unchanged at 100.
    context2, _ = _p1_context(23 * MINUTE, _p1_mark("100.5", 23 * MINUTE),
                              previous=committed)
    second = policy.evaluate(context2, committed)
    assert second.action == "KEEP"
    assert second.next_state.armed is True
    assert second.next_state.protection_stop_price == Decimal("100")
    # Drop through the line: EXIT at the next legal window.
    context3, _ = _p1_context(24 * MINUTE, _p1_mark("99.9", 24 * MINUTE),
                              previous=committed)
    third = policy.evaluate(context3, committed)
    assert third.action == "EXIT" and third.target_lots == 0
    assert "TRAILING_LINE_BREACHED" in third.reasons
    assert third.earliest_execution_nano > third.observed_at_nano
    assert third.protection is not None
    assert third.protection.stop_price == Decimal("100")


def test_p1_trailing_line_never_loosens() -> None:
    # Counterexample 1 (08 §10.2): armed line 103; a lower close yields a
    # lower fresh candidate and the effective line must stay 103.
    binding = _p1_binding((TRAILING_SPEC,))
    armed = _p1_state(
        binding, armed=True, price_peak=Decimal("105"),
        price_peak_source_nano=NANO + 5 * MINUTE, price_peak_source_hash=H64,
        net_peak_r=Decimal("1.2"), net_peak_source_nano=NANO + 5 * MINUTE,
        net_peak_source_hash=H64,
        protection_stop_price=Decimal("103"),
        protection_line_version="exit_trailing_atr_m1_v1:line:103",
        protection_effective_from_nano=NANO + 5 * MINUTE)
    policy = _policy()
    context, _ = _p1_context(10 * MINUTE, _p1_mark("104", 10 * MINUTE),
                             previous=armed)
    proposal = policy.evaluate(context, armed)
    assert proposal.next_state.protection_stop_price == Decimal("103")
    # Peak 104 would give candidate 102: rejected by max(); line stays 103.
    context_low, _ = _p1_context(11 * MINUTE, _p1_mark("103.5", 11 * MINUTE),
                                 previous=armed)
    kept = policy.evaluate(context_low, armed)
    assert kept.next_state.protection_stop_price == Decimal("103")


def test_p1_trailing_tick_landing_and_next_window_only() -> None:
    # Counterexample 2 (08 §10.2): long 103.01 -> 103.2 (ceil), short
    # 96.99 -> 96.8 (floor); a tightened line pierced by the current close
    # only forms a NEXT-window exit request.
    binding = _p1_binding((TRAILING_SPEC,))
    policy = _policy()
    context, _ = _p1_context(10 * MINUTE, _p1_mark("105.01", 10 * MINUTE),
                             previous=_p1_state(binding))
    long_proposal = policy.evaluate(context, context.previous_exit_state)
    assert long_proposal.next_state.armed is True
    assert long_proposal.next_state.protection_stop_price == Decimal("103.2")
    # Short mirror: S0=102, peak 94.99 -> candidate 96.99 -> floor tick 96.8.
    short_binding = _p1_binding((TRAILING_SPEC,))
    short_context, _ = _p1_context(
        10 * MINUTE, _p1_mark("94.99", 10 * MINUTE),
        previous=_p1_state(short_binding), side=-1,
        budget=_p1_budget(side=-1, stop="102", target="94"))
    short_proposal = policy.evaluate(short_context,
                                     short_context.previous_exit_state)
    assert short_proposal.next_state.armed is True
    assert short_proposal.next_state.protection_stop_price == Decimal("96.8")
    # Pierce: close below the tightened line -> EXIT, earliest strictly after
    # the observation; nothing claims an intra-bar fill.
    committed = commit_exit_observation(_p1_state(binding), long_proposal)
    pierced_context, _ = _p1_context(11 * MINUTE, _p1_mark("103.1", 11 * MINUTE),
                                     previous=committed)
    pierced = policy.evaluate(pierced_context, committed)
    assert pierced.action == "EXIT"
    assert pierced.earliest_execution_nano > pierced.observed_at_nano
    assert pierced.next_state.phase == "MANAGING"  # candidate, no fill facts


def test_p1_trailing_uses_frozen_a0_only() -> None:
    # Counterexample 4: the P1 trailing scale is the budget-frozen A0; no
    # runtime ATR market key is even declared, and A0=4 moves the candidate
    # exactly as the frozen scale dictates.
    assert TRAILING_SPEC.required_market_keys == ("BASE:close:v2.1.0:d60",)
    assert not any("atr" in key for key in TRAILING_SPEC.required_market_keys)
    binding = _p1_binding((TRAILING_SPEC,))
    policy = _policy()
    context, _ = _p1_context(10 * MINUTE, _p1_mark("102", 10 * MINUTE),
                             previous=_p1_state(binding),
                             budget=_p1_budget(initial_atr="4"))
    proposal = policy.evaluate(context, context.previous_exit_state)
    assert proposal.next_state.armed is True
    # peak 102 - 1*4 = 98 == b0: the frozen-A0 candidate, never a market ATR.
    assert proposal.next_state.protection_stop_price == Decimal("98")


def test_p1_trailing_s0_envelope_and_budget_bytes_untouched() -> None:
    # Counterexample 5: a candidate below b0 is clamped to the original stop
    # and the frozen RoundBudget encoding never changes.
    binding = _p1_binding((TRAILING_SPEC,))
    armed = _p1_state(
        binding, armed=True, price_peak=Decimal("99.5"),
        price_peak_source_nano=NANO + MINUTE, price_peak_source_hash=H64,
        protection_stop_price=Decimal("98"),
        protection_line_version="exit_trailing_atr_m1_v1:line:98",
        protection_effective_from_nano=NANO + MINUTE)
    budget = _p1_budget()
    before = encode_record(budget)
    context, _ = _p1_context(9 * MINUTE, _p1_mark("99.9", 9 * MINUTE),
                             previous=armed, budget=budget)
    policy = _policy()
    proposal = policy.evaluate(context, armed)
    # candidate 99.9-2 = 97.9 < b0 98 -> clamped to the S0 envelope.
    assert proposal.next_state.protection_stop_price == Decimal("98")
    assert encode_record(budget) == before
    assert budget.target_price == Decimal("106")  # T0 untouched


def test_p1_trailing_same_bar_new_peak_cannot_trigger() -> None:
    # Counterexample 6: a close that raises the peak cannot breach inside
    # its own bar (candidate = x - k*A0 < x structurally).
    binding = _p1_binding((TRAILING_SPEC,))
    armed = _p1_state(
        binding, armed=True, price_peak=Decimal("102"),
        price_peak_source_nano=NANO + MINUTE, price_peak_source_hash=H64,
        protection_stop_price=Decimal("100"),
        protection_line_version="exit_trailing_atr_m1_v1:line:100",
        protection_effective_from_nano=NANO + MINUTE)
    policy = _policy()
    context, _ = _p1_context(12 * MINUTE, _p1_mark("110", 12 * MINUTE),
                             previous=armed)
    proposal = policy.evaluate(context, armed)
    assert proposal.action == "KEEP"
    assert proposal.next_state.price_peak == Decimal("110")
    assert proposal.next_state.protection_stop_price == Decimal("108")
    # The next completed close below the COMMITTED line 108 breaches on
    # the following window (the line from t's close is effective for t+1).
    committed = commit_exit_observation(armed, proposal)
    assert committed.protection_stop_price == Decimal("108")
    later_context, _ = _p1_context(13 * MINUTE, _p1_mark("107.9", 13 * MINUTE),
                                   previous=committed)
    later = policy.evaluate(later_context, committed)
    assert later.action == "EXIT"


def test_p1_trailing_unvalued_mark_never_arms_or_guesses() -> None:
    # Counterexample 7: UNVALUED mark -> no Nt, no arm, no guessed values;
    # an already-armed line is carried unchanged.
    binding = _p1_binding((TRAILING_SPEC,))
    policy = _policy()
    context, _ = _p1_context(10 * MINUTE, _p1_mark(None, 10 * MINUTE, "UNVALUED"),
                             previous=_p1_state(binding))
    proposal = policy.evaluate(context, context.previous_exit_state)
    assert proposal.action == "KEEP"
    assert proposal.next_state.armed is False
    assert proposal.next_state.price_peak is None
    armed = _p1_state(
        binding, armed=True, price_peak=Decimal("105"),
        price_peak_source_nano=NANO + MINUTE, price_peak_source_hash=H64,
        protection_stop_price=Decimal("103"),
        protection_line_version="exit_trailing_atr_m1_v1:line:103",
        protection_effective_from_nano=NANO + MINUTE)
    context2, _ = _p1_context(11 * MINUTE, _p1_mark(None, 11 * MINUTE, "UNVALUED"),
                              previous=armed)
    carried = policy.evaluate(context2, armed)
    assert carried.action == "KEEP"
    assert carried.next_state.armed is True
    assert carried.next_state.protection_stop_price == Decimal("103")
    facts = collect_exit_facts(context2, armed)
    assert facts.net_liquidation is None
    assert facts.nli_reasons


def test_p1_trailing_commit_rejects_tampered_candidate() -> None:
    binding = _p1_binding((TRAILING_SPEC,))
    base = _p1_state(binding)
    policy = _policy()
    context, _ = _p1_context(22 * MINUTE, _p1_mark("102", 22 * MINUTE),
                             previous=base)
    proposal = policy.evaluate(context, base)
    committed = commit_exit_observation(base, proposal)
    assert committed.armed is True
    # Tampering with the candidate after hashing breaks the chain advance.
    tampered = dataclasses.replace(proposal.next_state, price_peak=Decimal("999"))
    with pytest.raises(Exception):
        commit_exit_observation(base, dataclasses.replace(
            proposal, next_state=tampered))
    # A candidate built under a different event identity cannot commit either.
    forged = advance_state(base, "SYN-FORGED-EVENT", armed=True,
                           protection_stop_price=Decimal("100"),
                           protection_line_version="v",
                           protection_effective_from_nano=NANO + 22 * MINUTE,
                           price_peak=Decimal("102"),
                           price_peak_source_nano=NANO + 22 * MINUTE,
                           price_peak_source_hash=H64)
    with pytest.raises(Exception):
        commit_exit_observation(base, dataclasses.replace(
            proposal, next_state=forged))
    # A closing fill can never initialize the state (08 §4.2: only the
    # first real OPEN fill starts the age clock).
    close_fill = dataclasses.replace(
        _p1_open_fill(), fill_id="SYN-FILL-CLOSE-1", offset="CLOSE", lots=1)
    with pytest.raises(Exception):
        initialize_exit_state(context, close_fill)
