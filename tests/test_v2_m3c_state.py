"""M3C P0 contract tests: exit state machine, nesting and nullable matrix.

OQ-2 Plan A P0 entry ``test_p0_contract_state_confirmation_roundtrip``
(open-questions-resolution §2, OQ-6 nested ExitConfirmation). stage=P0 /
contract-only: the ExitState state-machine surface frozen by M1 — nullable
combinations (armed/protection, peak triples, qbase/closed-lots, CLOSED
phase), ExitLadderStep and ExitProtectionRecord legality, confirmation
nesting/immutability and lossless encoding. Observation/selection/execution
commit boundaries are P1 behaviour and are NOT claimed here. All fixtures
are SYNTHETIC; M1 types are consumed read-only.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from ak_trading.v2.contracts import (
    DataQuality,
    ExitConfirmation,
    ExitLadderStep,
    ExitPolicyBinding,
    ExitProtectionRecord,
    ExitState,
    exit_binding_content_hash,
    exit_observation_slot_id,
)
from ak_trading.v2.schemas import (
    EXIT_STATES_SCHEMA_ID,
    decode_record,
    decode_record_file,
    encode_record,
    encode_record_file,
)

H64 = "a" * 64
H64_B = "b" * 64
H64_C = "c" * 64
NANO = 1_767_920_700_000_000_000
CONTRACT = "SYN.rb2609"
SLOT = exit_observation_slot_id(CONTRACT, 60, NANO + 60_000_000_000)


def _binding() -> ExitPolicyBinding:
    sha = exit_binding_content_hash(
        "SYNTHETIC:entry-1", H64, (H64_B, H64_C), None,
        ("SYNTHETIC:exit_trailing_atr_m1_v1", "SYNTHETIC:exit_time_decay_m1_v1"),
        "m3b-legacy-rules-v2.1.2")
    return ExitPolicyBinding(
        binding_sha256=sha, strategy_id="SYNTHETIC:entry-1", entry_spec_sha256=H64,
        exit_spec_sha256s=(H64_B, H64_C), sizing_spec_sha256=None,
        priority=("SYNTHETIC:exit_trailing_atr_m1_v1",
                  "SYNTHETIC:exit_time_decay_m1_v1"),
        legacy_rules_version="m3b-legacy-rules-v2.1.2")


def _state(**overrides) -> ExitState:
    values = dict(
        account_id="SYN-ACCOUNT-1", contract_id=CONTRACT,
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        first_fill_nano=NANO, exit_binding_sha256=_binding().binding_sha256,
        realized_gross_cny=Decimal("0"), fees_paid_cny=Decimal("0"),
        price_peak=None, price_peak_source_nano=None, price_peak_source_hash=None,
        net_peak_r=None, net_peak_source_nano=None, net_peak_source_hash=None,
        armed=False, protection_stop_price=None, protection_line_version=None,
        protection_effective_from_nano=None, confirmations=(),
        no_more_add=False, qbase=None, closed_lots=0, ladder_progress=(),
        phase="MANAGING", last_committed_event_id=None, state_prefix_hash=H64,
    )
    values.update(overrides)
    return ExitState(**values)


def test_p0_contract_state_confirmation_roundtrip() -> None:
    """OQ-6 nested confirmations: structure, dedup, slot IDs, lossless codec."""
    confirmation = ExitConfirmation(
        exit_policy_id="SYNTHETIC:exit_signal_reversal_m1_v1",
        rule_id="trigger", count=2, last_observation_id=SLOT)
    state = _state(confirmations=(confirmation,))
    assert state.confirmations[0].count == 2
    assert state.confirmations[0].last_observation_id == \
        json.dumps([CONTRACT, 60, NANO + 60_000_000_000],
                   ensure_ascii=False, separators=(",", ":"))
    # Deep immutability: records are frozen and tuples carry no mutation path.
    with pytest.raises(FrozenInstanceError):
        state.confirmations[0].count = 3
    with pytest.raises(FrozenInstanceError):
        state.armed = True
    assert isinstance(state.confirmations, tuple)
    # Counterexamples: rule-key duplication, ordering and count legality.
    with pytest.raises(ValueError, match="sorted_unique"):
        _state(confirmations=(
            ExitConfirmation(exit_policy_id="SYNTHETIC:p", rule_id="level:0",
                             count=0, last_observation_id=None),
            ExitConfirmation(exit_policy_id="SYNTHETIC:p", rule_id="level:0",
                             count=1, last_observation_id=SLOT)))
    with pytest.raises(ValueError, match="sorted_unique"):
        _state(confirmations=(
            ExitConfirmation(exit_policy_id="SYNTHETIC:p-b", rule_id="trigger",
                             count=1, last_observation_id=SLOT),
            ExitConfirmation(exit_policy_id="SYNTHETIC:p-a", rule_id="trigger",
                             count=1, last_observation_id=SLOT)))
    with pytest.raises(ValueError, match="requires_last_observation_id"):
        ExitConfirmation(exit_policy_id="SYNTHETIC:p", rule_id="trigger",
                         count=1, last_observation_id=None)
    with pytest.raises(ValueError, match="non_negative"):
        ExitConfirmation(exit_policy_id="SYNTHETIC:p", rule_id="trigger",
                         count=-1, last_observation_id=None)
    with pytest.raises(TypeError, match="count_must_be_int"):
        ExitConfirmation(exit_policy_id="SYNTHETIC:p", rule_id="trigger",
                         count=True, last_observation_id=SLOT)
    # Observation slot identity: canonical compact JSON only, scoped to the
    # parent contract, duration restricted to the two registered modes.
    with pytest.raises(ValueError, match="must_be_canonical"):
        _state(confirmations=(
            ExitConfirmation(exit_policy_id="SYNTHETIC:p", rule_id="trigger",
                             count=1, last_observation_id=f" {SLOT}"),))
    with pytest.raises(ValueError, match="match_parent_state"):
        _state(confirmations=(
            ExitConfirmation(exit_policy_id="SYNTHETIC:p", rule_id="trigger",
                             count=1, last_observation_id=exit_observation_slot_id(
                                 "SYN.other2609", 60, NANO)),))
    with pytest.raises(ValueError, match="duration_must_be"):
        _state(confirmations=(
            ExitConfirmation(exit_policy_id="SYNTHETIC:p", rule_id="trigger",
                             count=1, last_observation_id=exit_observation_slot_id(
                                 CONTRACT, 300, NANO)),))
    with pytest.raises(ValueError, match="three_element_array"):
        _state(confirmations=(
            ExitConfirmation(exit_policy_id="SYNTHETIC:p", rule_id="trigger",
                             count=1, last_observation_id='["SYN.rb2609"]'),))
    with pytest.raises(ValueError, match="must_be_canonical_json_array"):
        _state(confirmations=(
            ExitConfirmation(exit_policy_id="SYNTHETIC:p", rule_id="trigger",
                             count=1, last_observation_id="not-json"),))
    # Lossless roundtrip: decode revalidates and preserves the nested record.
    assert EXIT_STATES_SCHEMA_ID == "ak_probability_exit_states_v1.0.0"
    decoded = decode_record(ExitState, encode_record(state))
    assert decoded == state
    assert decoded.confirmations[0].last_observation_id == SLOT
    envelope = encode_record_file("ExitState", EXIT_STATES_SCHEMA_ID, (state,))
    assert decode_record_file(envelope, ExitState, EXIT_STATES_SCHEMA_ID) == (state,)


def test_p0_state_nullable_matrix_ladder_and_protection() -> None:
    """Frozen nullable combinations and ladder/protection legality."""
    armed = _state(armed=True, protection_stop_price=Decimal("103"),
                   protection_line_version="trailing-line-1",
                   protection_effective_from_nano=NANO + 60_000_000_000)
    assert armed.armed is True
    peaked = _state(price_peak=Decimal("105"), price_peak_source_nano=NANO + 1,
                    price_peak_source_hash=H64_B,
                    net_peak_r=Decimal("0.6"), net_peak_source_nano=NANO + 1,
                    net_peak_source_hash=H64_B)
    assert peaked.net_peak_r == Decimal("0.6")
    closed = _state(qbase=7, closed_lots=7, phase="CLOSED", no_more_add=True)
    assert closed.phase == "CLOSED"
    # Counterexamples: every illegal nullable combination is rejected.
    with pytest.raises(ValueError, match="armed_requires"):
        _state(armed=True)                                   # arm without a line
    with pytest.raises(ValueError, match="unarmed_requires"):
        _state(protection_stop_price=Decimal("103"))          # line without arm
    with pytest.raises(ValueError, match="price_peak_none_requires"):
        _state(price_peak_source_nano=NANO)                   # half-filled triple
    with pytest.raises(ValueError, match="net_peak_none_requires"):
        _state(net_peak_source_nano=NANO)                     # source without value
    with pytest.raises(TypeError, match="net_peak_source_nano_must_be_int"):
        _state(net_peak_r=Decimal("0.5"))                     # value without source
    with pytest.raises(ValueError, match="not_precede_first_fill"):
        _state(price_peak=Decimal("101"), price_peak_source_nano=NANO - 1,
               price_peak_source_hash=H64_B)
    with pytest.raises(ValueError, match="net_peak_r_must_be_non_negative"):
        _state(net_peak_r=Decimal("-0.1"), net_peak_source_nano=NANO,
               net_peak_source_hash=H64_B)
    with pytest.raises(ValueError, match="qbase_none_requires"):
        _state(closed_lots=1)                                 # closed lots before Qbase
    with pytest.raises(ValueError, match="closed_lots_must_not_exceed"):
        _state(qbase=2, closed_lots=3)
    with pytest.raises(ValueError, match="closed_phase_requires"):
        _state(phase="CLOSED")                                # CLOSED without Qbase
    with pytest.raises(ValueError, match="at_least_1"):
        _state(qbase=0)
    # Ladder steps: requested/completed conservation, non-negative lots.
    ladder = _state(qbase=7, closed_lots=2, ladder_progress=(
        ExitLadderStep(level_index=0, triggered=True, requested_lots=2,
                       completed_lots=2),))
    assert ladder.ladder_progress[0].completed_lots == 2
    with pytest.raises(ValueError, match="completed_lots_must_not_exceed"):
        ExitLadderStep(level_index=0, triggered=True, requested_lots=1,
                       completed_lots=2)
    with pytest.raises(ValueError, match="non_negative"):
        ExitLadderStep(level_index=0, triggered=True, requested_lots=-1,
                       completed_lots=0)
    with pytest.raises(ValueError, match="non_negative"):
        ExitLadderStep(level_index=-1, triggered=False, requested_lots=0,
                       completed_lots=0)
    with pytest.raises(ValueError, match="sorted_unique"):
        _state(qbase=7, ladder_progress=(
            ExitLadderStep(level_index=1, triggered=True, requested_lots=4,
                           completed_lots=0),
            ExitLadderStep(level_index=0, triggered=True, requested_lots=2,
                           completed_lots=0)))
    # Protection record: positive restored actual price, versioned identity.
    record = ExitProtectionRecord(policy_id="SYNTHETIC:exit_trailing_atr_m1_v1",
                                  stop_price=Decimal("103.2"),
                                  line_version="trailing-line-2",
                                  effective_from_nano=NANO + 1)
    assert record.stop_price == Decimal("103.2")
    with pytest.raises(ValueError, match="stop_price_must_be_positive"):
        ExitProtectionRecord(policy_id="SYNTHETIC:exit_trailing_atr_m1_v1",
                             stop_price=Decimal("0"), line_version="line",
                             effective_from_nano=NANO)
    with pytest.raises(TypeError):
        ExitProtectionRecord(policy_id="SYNTHETIC:exit_trailing_atr_m1_v1",
                             stop_price=103, line_version="line",
                             effective_from_nano=NANO)  # int is not Decimal


def test_p0_state_binding_hash_bytes_and_roundtrip() -> None:
    """OQ-4 byte-exact binding identity carried by every committed state."""
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
    assert exit_binding_content_hash(
        binding.strategy_id, binding.entry_spec_sha256,
        (H64_C, H64_B), binding.sizing_spec_sha256, binding.priority,
        binding.legacy_rules_version) == binding.binding_sha256
    state = _state(armed=True, protection_stop_price=Decimal("103"),
                   protection_line_version="trailing-line-1",
                   protection_effective_from_nano=NANO + 1,
                   confirmations=(ExitConfirmation(
                       exit_policy_id="SYNTHETIC:exit_signal_reversal_m1_v1",
                       rule_id="trigger", count=1, last_observation_id=SLOT),))
    assert state.exit_binding_sha256 == binding.binding_sha256
    decoded = decode_record(ExitState, encode_record(state))
    assert decoded == state
    # Byte-stable re-encode: recovery replays the identical committed bytes.
    assert encode_record(decoded) == encode_record(state)
    with pytest.raises(ValueError, match="64_lowercase_hex"):
        _state(exit_binding_sha256="not-a-hash")
    # Frozen dataclasses: replace() rebuilds through full validation.
    advanced = dataclasses.replace(state, realized_gross_cny=Decimal("40"),
                                   fees_paid_cny=Decimal("4"))
    assert advanced.realized_gross_cny == Decimal("40")
    assert advanced.exit_binding_sha256 == state.exit_binding_sha256

# ═══ P1 behavior fixtures (M3C-owned SYNTHETIC builders; P0 tests above
# stay untouched). Geometry follows 08 §10: S0=98/T0=106, A0=2, R0=100,
# multiplier 10, tick 0.2, 1 CNY per lot fees, zero slippage.

import dataclasses  # noqa: E402
import datetime  # noqa: E402

from decimal import Decimal  # noqa: E402
from ak_trading.v2.contracts import (  # noqa: E402
    AccountSnapshot, DataQuality, ExitContext, ExitPolicyBinding, ExitState,
    FeeLeg, Fill, LayerSpec, PositionLot, RoundBudget, SessionWindow,
    StrategyState, StressPolicy, TradingRule, ValuationMark,
    exit_binding_content_hash,
)
from ak_trading.v2.strategies.registry import strategy_registry_v2  # noqa: E402
from ak_trading.v2.exits.features import (  # noqa: E402
    build_exit_feature_snapshot, collect_exit_facts,
)
from ak_trading.v2.exits.registry import (  # noqa: E402
    exit_registry_specs, exit_spec_sha256,
)

MINUTE = 60_000_000_000
HOUR = 60 * MINUTE
DAY1 = datetime.date(2026, 1, 8)
DAY2 = datetime.date(2026, 1, 9)
SPECS_M1 = exit_registry_specs("MINUTE")
LEGACY_SPEC = next(s for s in SPECS_M1 if s.family == "LEGACY")
TRAILING_SPEC = next(s for s in SPECS_M1 if s.family == "TRAILING_STOP")
TIME_DECAY_SPEC = next(s for s in SPECS_M1 if s.family == "TIME_DECAY")
ENTRY_SPEC = strategy_registry_v2("MINUTE")["trend_m1_v2"].spec
H64A = "a" * 64
DQ_READY = DataQuality(status="READY", reasons=(), temporal_basis="observed",
                       input_hash=H64A)


def _fx_rule() -> TradingRule:
    return TradingRule(
        rule_id="SYN-RULE-1", contract_id="SYN.rb2609",
        effective_from_nano=NANO - 10 ** 12,
        effective_until_nano=NANO + 10 ** 15,
        available_at_nano=NANO - 10 ** 12, multiplier=Decimal("10"),
        price_tick=Decimal("0.2"), lot_step=1, min_open_lots=1,
        open_fee=FeeLeg(Decimal("1"), Decimal("0"), True),
        close_fee=FeeLeg(Decimal("1"), Decimal("0"), True),
        close_today_fee=FeeLeg(Decimal("1"), Decimal("0"), True),
        long_margin_rate=Decimal("0.1"), short_margin_rate=Decimal("0.1"),
        limit_up=None, limit_down=None, slippage_ticks=Decimal("0"),
        impact_ticks=Decimal("0"), participation_rate=Decimal("1"),
        evidence="VERIFIED", source_sha256=H64A)


def _fx_mark(price, as_of_nano, status="CURRENT"):
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


def _fx_session(start, end, day=DAY1, sid="SYN-SESS-1"):
    return SessionWindow(session_id=sid, product="SYN", trading_day=day,
                         start_nano=start, end_nano=end, kind="DAY",
                         calendar_version="cal-v1", evidence_status="VERIFIED")


def _fx_fill(fill_id="SYN-FILL-OPEN-1", intent_id="SYN-INT-OPEN-1",
             offset="OPEN", lots=3, price="100", fee=None, nano=0, side=1):
    return Fill(fill_id=fill_id, intent_id=intent_id, contract_id="SYN.rb2609",
                strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
                execution_nano=NANO + nano, trading_day=DAY1, side=side,
                lots=lots, offset=offset, reference_price=Decimal(price),
                execution_price=Decimal(price),
                fee_cny=Decimal(lots) if fee is None else Decimal(fee),
                slippage_cny=Decimal("0"), rule_id="SYN-RULE-1",
                evidence="VERIFIED")


def _fx_lot(lots=3, price="100", side=1, lot_id="SYN-LOT-1"):
    return PositionLot(lot_id=lot_id, contract_id="SYN.rb2609",
                       strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
                       side=side, lots=lots, opened_at_nano=NANO,
                       opened_trading_day=DAY1, entry_price=Decimal(price),
                       opening_fee_cny=Decimal(lots))


def _fx_account(now, lots=(), quality=None):
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


def _fx_budget(latest_offset=30 * MINUTE, planned_loss="100", side=1,
               stop="98", target="106", initial_atr="2"):
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


def _fx_binding(specs, priority=None):
    values = dict(strategy_id="SYNTHETIC:entry-1", entry_spec_sha256=H64A,
                  exit_spec_sha256s=tuple(sorted({exit_spec_sha256(s) for s in specs})),
                  sizing_spec_sha256=None,
                  priority=priority or tuple(s.exit_policy_id for s in specs),
                  legacy_rules_version="m3b-legacy-rules-v2.1.2")
    return ExitPolicyBinding(binding_sha256=exit_binding_content_hash(**values),
                             **values)


def _fx_state(binding, **overrides):
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


def _fx_entry_state(budget, active_exit_offset=30 * MINUTE):
    return StrategyState(
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        contract_id="SYN.rb2609", phase="HOLDING", setup=None, last_regime=None,
        direction=1, filled_layers=(0,), layer_filled_lots=(3,),
        pending_layer_index=None, active_exit_nano=NANO + active_exit_offset,
        pending_exit_nano=None, pending_intent_ids=(), budget=budget,
        confirmation_count=0, cooldown_until_nano=None,
        last_event_id="SYN-FILL-OPEN-1")


def _fx_context(now_offset, mark, *, specs=None, budget=None, lots=None,
                fills=None, sessions=None, previous=None, account=None,
                active_exit_offset=30 * MINUTE):
    specs = specs or (TRAILING_SPEC,)
    budget = budget or _fx_budget()
    lots = [_fx_lot()] if lots is None else lots
    fills = [_fx_fill()] if fills is None else fills
    sessions = sessions if sessions is not None else [
        _fx_session(NANO - MINUTE, NANO + 60 * MINUTE)]
    now = NANO + int(now_offset)
    return ExitContext(
        now_nano=now, account_id="SYN-ACCOUNT-1", contract_id="SYN.rb2609",
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        exit_binding=_fx_binding(specs), exit_specs=tuple(specs),
        market_features=(), forecasts=(), entry_spec=ENTRY_SPEC,
        entry_state=_fx_entry_state(budget, active_exit_offset), budget=budget,
        account=account or _fx_account(now, lots), lots=tuple(lots),
        marks=(mark,) if mark is not None else (),
        episode_fills=tuple(fills), rules=(_fx_rule(),),
        sessions=tuple(sessions),
        previous_exit_state=previous, continuation=None)


# ═══ P1 behavior: the three commit boundaries, execution receipts, restore ══

from ak_trading.v2.contracts import (  # noqa: E402
    ExecutionResult, ExitExecutionLink, LedgerCheckpoint, RunPolicy,
)
from ak_trading.v2.exits.state import (  # noqa: E402
    ExitStateError, apply_exit_execution, base_state_for,
    commit_selected_exit, commit_exit_observation, initialize_exit_state,
    validate_restored_exit_state,
)
from ak_trading.v2.exits.trailing import TrailingStopPolicy  # noqa: E402


def test_p1_state_initialize_from_first_fill_only() -> None:
    binding = _fx_binding((TRAILING_SPEC,))
    context = _fx_context(10 * MINUTE, _fx_mark("102", NANO + 10 * MINUTE),
                          previous=None)
    state = initialize_exit_state(context, _fx_fill())
    assert state.qbase == 3
    assert state.fees_paid_cny == Decimal("3")
    assert state.phase == "MANAGING"
    assert state.state_prefix_hash != H64A
    # Deterministic genesis: two initializations are byte-identical.
    again = initialize_exit_state(context, _fx_fill())
    assert again == state
    with pytest.raises(Exception):
        initialize_exit_state(
            context, _fx_fill(offset="CLOSE", lots=1, price="104", fee="1"))
    # No committed state and no real fill: evaluation is refused (08 §4.2 —
    # never age an episode from an unexecuted proposal).
    empty = _fx_context(10 * MINUTE, _fx_mark("102", NANO + 10 * MINUTE),
                        fills=(), lots=())
    with pytest.raises(ExitStateError):
        base_state_for(empty, None)
    # base_state_for synthesizes the same genesis from the fill ledger.
    assert base_state_for(context, None) == state


def test_p1_state_observation_boundary_purity() -> None:
    # Counterexample 13: commit_exit_observation advances observation facts
    # only — cashflows, qbase, closed lots, phase and no_more_add never move.
    binding = _fx_binding((TRAILING_SPEC,))
    base = _fx_state(binding)
    policy = TrailingStopPolicy(TRAILING_SPEC)
    context = _fx_context(22 * MINUTE, _fx_mark("102", NANO + 22 * MINUTE),
                          previous=base)
    proposal = policy.evaluate(context, base)
    committed = commit_exit_observation(base, proposal)
    assert committed.armed is True
    for name in ("realized_gross_cny", "fees_paid_cny", "qbase",
                 "closed_lots", "phase", "no_more_add"):
        assert getattr(committed, name) == getattr(base, name)
    # A candidate that smuggles a selection fact is rejected outright.
    from ak_trading.v2.exits.state import advance_state
    smuggled = advance_state(base, proposal.proposal_id, no_more_add=True)
    with pytest.raises(ExitStateError):
        commit_exit_observation(
            base, dataclasses.replace(proposal, next_state=smuggled))
    # Tampering after hashing breaks the chain advance.
    tampered = dataclasses.replace(proposal.next_state, price_peak=Decimal("999"))
    with pytest.raises(ExitStateError):
        commit_exit_observation(
            base, dataclasses.replace(proposal, next_state=tampered))
    # A candidate hashed under a different event identity cannot commit.
    forged = advance_state(base, "SYN-FORGED", armed=True,
                           protection_stop_price=Decimal("100"),
                           protection_line_version="v",
                           protection_effective_from_nano=NANO + 22 * MINUTE,
                           price_peak=Decimal("102"),
                           price_peak_source_nano=NANO + 22 * MINUTE,
                           price_peak_source_hash=H64A)
    with pytest.raises(ExitStateError):
        commit_exit_observation(
            base, dataclasses.replace(proposal, next_state=forged))


def test_p1_state_selected_boundary_latches() -> None:
    binding = _fx_binding((TRAILING_SPEC,))
    base = _fx_state(binding)
    policy = TrailingStopPolicy(TRAILING_SPEC)
    context = _fx_context(22 * MINUTE, _fx_mark("102", NANO + 22 * MINUTE),
                          previous=base)
    keep = policy.evaluate(context, base)  # armed but not breached
    link_keep = ExitExecutionLink(
        link_id="SYN-LINK-KEEP", exit_proposal_id=keep.proposal_id,
        strategy_proposal_id="SYN-PROP-1", decision_id="SYN-DEC-1",
        intent_id="SYN-INT-1", exit_binding_sha256=binding.binding_sha256,
        action="EXIT", target_lots=0, requested_lots=3,
        execution_kind="NEXT_WINDOW_CLOSE")
    with pytest.raises(ExitStateError):
        commit_selected_exit(base, keep, (link_keep,))  # KEEP not selectable
    with pytest.raises(ExitStateError):
        commit_selected_exit(base, keep, ())  # links required
    exit_proposal = dataclasses.replace(keep, action="EXIT", target_lots=0)
    with pytest.raises(ExitStateError):
        commit_selected_exit(base, exit_proposal, ())  # empty links
    wrong_action = ExitExecutionLink(
        link_id="SYN-LINK-2", exit_proposal_id=exit_proposal.proposal_id,
        strategy_proposal_id="SYN-PROP-1", decision_id="SYN-DEC-1",
        intent_id="SYN-INT-1", exit_binding_sha256=binding.binding_sha256,
        action="REDUCE", target_lots=1, requested_lots=2,
        execution_kind="NEXT_WINDOW_CLOSE")
    with pytest.raises(ExitStateError):
        commit_selected_exit(base, exit_proposal, (wrong_action,))
    selected = commit_selected_exit(base, exit_proposal, (link_keep,))
    assert selected.no_more_add is True
    assert selected.phase == "LIQUIDATING"
    assert selected.state_prefix_hash != base.state_prefix_hash


def test_p1_state_execution_receipt_boundaries() -> None:
    # Counterexample 13/14: only real fills advance cashflows and closed
    # lots; receipts without fills are pure no-ops; guards reject OPEN
    # fills, missing multipliers and over-close.
    binding = _fx_binding((TRAILING_SPEC,))
    selected = _fx_state(binding, phase="LIQUIDATING", no_more_add=True)
    link = ExitExecutionLink(
        link_id="SYN-LINK-1", exit_proposal_id="SYN-EXIT-1",
        strategy_proposal_id="SYN-PROP-1", decision_id="SYN-DEC-1",
        intent_id="SYN-INT-CLOSE-1", exit_binding_sha256=binding.binding_sha256,
        action="EXIT", target_lots=0, requested_lots=3,
        execution_kind="NEXT_WINDOW_CLOSE")
    now = NANO + 25 * MINUTE
    pre_receipt = _fx_account(now, (_fx_lot(lots=3),))
    close = _fx_fill(fill_id="SYN-FILL-CLOSE-1", intent_id="SYN-INT-CLOSE-1",
                     offset="CLOSE", lots=1, price="104", fee="1",
                     nano=23 * MINUTE)
    result = ExecutionResult("SYN-INT-CLOSE-1", (close,), 2, "PARTIAL", ())
    # multiplier is a REQUIRED keyword-only argument (007 no-defaults).
    with pytest.raises(TypeError):
        apply_exit_execution(selected, result, link, pre_receipt, now)
    with pytest.raises(ExitStateError):
        apply_exit_execution(selected, result, link, pre_receipt, now,
                             multiplier=Decimal("0"))
    partial = apply_exit_execution(selected, result, link, pre_receipt, now,
                                   multiplier=Decimal("10"))
    assert partial.closed_lots == 1
    assert partial.realized_gross_cny == Decimal("40")
    assert partial.fees_paid_cny == Decimal("4")
    assert partial.phase == "LIQUIDATING"  # remaining != 0 -> not CLOSED
    # The second leg flattens the episode from the 2-lot pre-receipt basis.
    final_close = _fx_fill(fill_id="SYN-FILL-CLOSE-2",
                           intent_id="SYN-INT-CLOSE-2", offset="CLOSE",
                           lots=2, price="100", fee="2", nano=24 * MINUTE)
    final_link = ExitExecutionLink(
        link_id="SYN-LINK-2", exit_proposal_id="SYN-EXIT-1",
        strategy_proposal_id="SYN-PROP-1", decision_id="SYN-DEC-1",
        intent_id="SYN-INT-CLOSE-2",
        exit_binding_sha256=binding.binding_sha256,
        action="EXIT", target_lots=0, requested_lots=2,
        execution_kind="NEXT_WINDOW_CLOSE")
    final_result = ExecutionResult("SYN-INT-CLOSE-2", (final_close,), 0,
                                   "FILLED", ())
    pre_final = _fx_account(now, (_fx_lot(lots=2),))
    closed = apply_exit_execution(partial, final_result, final_link,
                                  pre_final, now, multiplier=Decimal("10"))
    assert closed.closed_lots == 3 == closed.qbase
    assert closed.phase == "CLOSED"
    assert closed.realized_gross_cny == Decimal("40")
    # Guards: OPEN fills never ride an exit link; over-close and missing
    # multiplier are refused; a fills-free BLOCKED receipt changes nothing.
    open_fill = _fx_fill(fill_id="SYN-FILL-OPEN-2", intent_id="SYN-INT-2",
                         offset="OPEN", lots=1)
    with pytest.raises(ExitStateError):
        apply_exit_execution(selected,
                             ExecutionResult("SYN-INT-2", (open_fill,), 0,
                                             "FILLED", ()), link, pre_receipt,
                             now, multiplier=Decimal("10"))
    over = _fx_fill(fill_id="SYN-FILL-CLOSE-3", intent_id="SYN-INT-3",
                    offset="CLOSE", lots=3, price="100")
    with pytest.raises(ExitStateError):
        apply_exit_execution(partial,
                             ExecutionResult("SYN-INT-3", (over,), 0,
                                             "FILLED", ()), link, pre_receipt,
                             now, multiplier=Decimal("10"))
    blocked = ExecutionResult("SYN-INT-CLOSE-1", (), 3, "BLOCKED",
                              ("EXIT_PENDING",))
    unchanged = apply_exit_execution(selected, blocked, link, pre_receipt, now,
                                     multiplier=Decimal("10"))
    assert unchanged is selected


def test_p1_state_restore_validation() -> None:
    # Counterexample 14: a restored state must be the committed instance in
    # the checkpoint, bound to a declared binding, and CLOSED consistency is
    # re-verified — the checkpoint is the only source of committed truth.
    specs = (LEGACY_SPEC, TRAILING_SPEC, TIME_DECAY_SPEC)
    binding = _fx_binding(specs)
    final = _fx_state(binding, phase="CLOSED", closed_lots=3,
                      last_committed_event_id="SYN-INT-CLOSE-2")
    checkpoint = LedgerCheckpoint(
        sequence=1, contract_lock_sha256=H64A, registry_sha256=H64A,
        exit_registry_sha256=H64A,
        run_policy=RunPolicy(
            purpose="RESEARCH_EVALUATION", routing_mode="BEST_EV", seed=7,
            fixed_strategy_id=None, fixed_exit_binding_sha256=None,
            research_policy_version="research-policy-v2.1.0",
            macro_policy_version="macro-risk-v2.1.0", macro_mode="PRICE_ONLY",
            cost_scenario="BASE"),
        rule_hashes={}, model_hashes={"forecast": H64A, "payoff": H64A,
                                      "regime_reference": H64A},
        qualification_hashes={}, account=_fx_account(NANO + 30 * MINUTE),
        valuation_marks=(), states=(), exit_bindings=(binding,),
        exit_states=(final,), exit_execution_links=(), pending_intents=(),
        pending_remaining_lots={}, processed_event_ids=(),
        processed_fill_hashes={}, capacity_used={}, previous_hash=H64A,
        checkpoint_hash=H64A)
    validate_restored_exit_state(final, checkpoint)
    mutated = dataclasses.replace(final, closed_lots=2)
    with pytest.raises(ExitStateError):
        validate_restored_exit_state(mutated, checkpoint)
    foreign = dataclasses.replace(final, episode_id="SYN-EP-9")
    with pytest.raises(Exception):
        validate_restored_exit_state(foreign, checkpoint)
