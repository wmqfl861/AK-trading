"""M3C P0 contract tests: candidate states, execution time, causal boundaries.

OQ-2 Plan A P0 entry ``test_p0_contract_candidate_state_and_execution_time``
(open-questions-resolution §2, OQ-3). stage=P0 / contract-only: the embedded
``next_state`` ownership rules, observation/earliest-execution ordering,
append-only committed history (exit signals must not rewrite it), the
Nt=None → KEEP path that never reaches an execution link while the hard-risk
channel stays constructible, and byte-identical recovery roundtrips. Future
append replay through a real pipeline is P1 work and NOT claimed here. All
fixtures are SYNTHETIC; M1 types are consumed read-only.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from decimal import Decimal

import pytest

from ak_trading.v2.contracts import (
    DataQuality,
    ExitExecutionLink,
    ExitFeatureSnapshot,
    ExitFeatureValue,
    ExitPolicyBinding,
    ExitProposal,
    ExitState,
    exit_binding_content_hash,
)
from ak_trading.v2.schemas import canonical_json_bytes, decode_record, encode_record

H64 = "a" * 64
H64_B = "b" * 64
H64_C = "c" * 64
NANO = 1_767_920_700_000_000_000
MINUTE_NANO = 60_000_000_000
DQ_READY = DataQuality(status="READY", reasons=(), temporal_basis="observed",
                       input_hash=H64)
DQ_MISSING = DataQuality(status="MISSING", reasons=("NLI_UNVALUED",),
                         temporal_basis="unknown", input_hash=H64)
NLI_KEY = "EXITSTATE:episode_net_liquidation_r:v1.0.0:d60"
ELAPSED_KEY = "EXITSTATE:elapsed_fraction:v1.0.0:d60"


def _binding() -> ExitPolicyBinding:
    values = dict(
        strategy_id="SYNTHETIC:entry-1", entry_spec_sha256=H64,
        exit_spec_sha256s=(H64_B, H64_C), sizing_spec_sha256=None,
        priority=("SYNTHETIC:exit_trailing_atr_m1_v1",
                  "SYNTHETIC:exit_time_decay_m1_v1"),
        legacy_rules_version="m3b-legacy-rules-v2.1.2")
    sha = exit_binding_content_hash(**values)
    return ExitPolicyBinding(binding_sha256=sha, **values)


def _state(**overrides) -> ExitState:
    values = dict(
        account_id="SYN-ACCOUNT-1", contract_id="SYN.rb2609",
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        first_fill_nano=NANO, exit_binding_sha256=_binding().binding_sha256,
        realized_gross_cny=Decimal("40"), fees_paid_cny=Decimal("4"),
        price_peak=None, price_peak_source_nano=None, price_peak_source_hash=None,
        net_peak_r=None, net_peak_source_nano=None, net_peak_source_hash=None,
        armed=False, protection_stop_price=None, protection_line_version=None,
        protection_effective_from_nano=None, confirmations=(),
        no_more_add=False, qbase=3, closed_lots=0, ladder_progress=(),
        phase="MANAGING", last_committed_event_id="SYN-EVENT-1",
        state_prefix_hash=H64)
    values.update(overrides)
    return ExitState(**values)


def _proposal(action: str = "EXIT", target_lots: int = 0,
              triggering_policy_id: str | None = "SYNTHETIC:exit_trailing_atr_m1_v1",
              observed: int = NANO + 2 * MINUTE_NANO,
              earliest: int = NANO + 3 * MINUTE_NANO,
              next_state: ExitState | None = None,
              reasons: tuple[str, ...] = ("TRAILING_LINE_BREACHED",)) -> ExitProposal:
    return ExitProposal(
        proposal_id="SYN-EXIT-P1", strategy_id="SYNTHETIC:entry-1",
        episode_id="SYN-EP-1", contract_id="SYN.rb2609",
        exit_binding_sha256=_binding().binding_sha256, action=action,
        target_lots=target_lots, triggering_policy_id=triggering_policy_id,
        input_sha256=H64, observed_at_nano=observed,
        earliest_execution_nano=earliest, protection=None,
        next_state=next_state if next_state is not None else _state(),
        reasons=reasons)


def test_p0_contract_candidate_state_and_execution_time() -> None:
    """OQ-3: full next_state embedding, ownership, and timing constraints."""
    committed = _state()
    # A candidate advances only observation facts; every committed cash/qty
    # fact stays byte-equal (exit signals must not rewrite history).
    candidate = dataclasses.replace(
        committed,
        price_peak=Decimal("105"), price_peak_source_nano=NANO + MINUTE_NANO,
        price_peak_source_hash=H64_B, net_peak_r=Decimal("0.6"),
        net_peak_source_nano=NANO + MINUTE_NANO, net_peak_source_hash=H64_C,
        armed=True, protection_stop_price=Decimal("103"),
        protection_line_version="trailing-line-v1",
        protection_effective_from_nano=NANO + 2 * MINUTE_NANO,
        last_committed_event_id=None)
    for fact in ("first_fill_nano", "realized_gross_cny", "fees_paid_cny",
                 "closed_lots", "no_more_add", "qbase", "exit_binding_sha256"):
        assert getattr(candidate, fact) == getattr(committed, fact)
    proposal = _proposal(next_state=candidate)
    assert proposal.next_state is candidate
    assert proposal.earliest_execution_nano >= proposal.observed_at_nano
    # Counterexamples: ownership and timing rejections.
    with pytest.raises(ValueError, match="zero_target_lots"):
        _proposal(action="EXIT", target_lots=2)               # EXIT must be flat
    with pytest.raises(ValueError, match="reduce_action_requires"):
        _proposal(action="REDUCE", target_lots=0)             # REDUCE keeps risk
    with pytest.raises(ValueError, match="precede_observation"):
        _proposal(earliest=NANO + MINUTE_NANO)                # same-bar retro claim
    with pytest.raises(ValueError, match="scope_must_match"):
        _proposal(next_state=_state(episode_id="SYN-EP-OTHER"))
    with pytest.raises(ValueError, match="scope_must_match"):
        _proposal(next_state=_state(strategy_id="SYNTHETIC:entry-2"))
    with pytest.raises(ValueError, match="scope_must_match"):
        _proposal(next_state=_state(contract_id="SYN.hc2609"))
    with pytest.raises(TypeError, match="next_state_must_be"):
        dataclasses.replace(proposal, next_state=None)
    # Hard risk may carry no triggering M3C policy at all (M3C-H03 shape).
    hard = _proposal(triggering_policy_id=None, reasons=("HOLDING_DEADLINE",),
                     observed=NANO + 2 * MINUTE_NANO)
    assert hard.triggering_policy_id is None
    assert decode_record(ExitProposal, encode_record(hard)) == hard


def test_p0_causality_future_append_does_not_rewrite_history() -> None:
    """Later observations extend the chain; committed bytes never change."""
    first = _state(last_committed_event_id="SYN-EVENT-1")
    first_bytes = canonical_json_bytes(encode_record(first))
    first_prefix = first.state_prefix_hash
    # A descendant state built on `first` (later observation, later event).
    second = dataclasses.replace(
        first, net_peak_r=Decimal("0.6"), net_peak_source_nano=NANO + MINUTE_NANO,
        net_peak_source_hash=H64_B, last_committed_event_id="SYN-EVENT-2",
        state_prefix_hash=hashlib.sha256(
            first_bytes + first_prefix.encode("ascii")).hexdigest())
    assert second.last_committed_event_id != first.last_committed_event_id
    assert second.state_prefix_hash != first.state_prefix_hash
    # The ancestor is untouched: identical bytes, identical prefix hash, and a
    # lossless byte-identical roundtrip (recovery replays the same history).
    assert canonical_json_bytes(encode_record(first)) == first_bytes
    assert first.state_prefix_hash == first_prefix
    assert canonical_json_bytes(encode_record(
        decode_record(ExitState, encode_record(first)))) == first_bytes
    assert canonical_json_bytes(encode_record(second)) != first_bytes
    # Any single byte change in the ancestor flips the descendant prefix:
    tampered = dataclasses.replace(first, realized_gross_cny=Decimal("41"))
    tampered_prefix = hashlib.sha256(
        canonical_json_bytes(encode_record(tampered))
        + first_prefix.encode("ascii")).hexdigest()
    assert tampered_prefix != second.state_prefix_hash
    # Visibility boundary: a source that becomes available after the snapshot
    # bound cannot enter a decision snapshot at that time (future append into
    # a past decision is rejected at the contract gate).
    late_value = ExitFeatureValue(feature_key=ELAPSED_KEY, value=Decimal("0.8"),
                                  observed_at_nano=NANO, available_at_nano=NANO + 1,
                                  quality=DQ_READY, source_hashes={"SYN:1": H64})
    with pytest.raises(ValueError, match="exceed_snapshot"):
        ExitFeatureSnapshot(
            snapshot_id="SYN-SNAP-1", account_id="SYN-ACCOUNT-1",
            contract_id="SYN.rb2609", strategy_id="SYNTHETIC:entry-1",
            episode_id="SYN-EP-1", exit_binding_sha256=_binding().binding_sha256,
            duration_seconds=60, observed_at_nano=NANO, available_at_nano=NANO,
            values=(late_value,), source_state_sha256=H64)
    with pytest.raises(ValueError, match="sorted_unique"):
        ExitFeatureSnapshot(
            snapshot_id="SYN-SNAP-1", account_id="SYN-ACCOUNT-1",
            contract_id="SYN.rb2609", strategy_id="SYNTHETIC:entry-1",
            episode_id="SYN-EP-1", exit_binding_sha256=_binding().binding_sha256,
            duration_seconds=60, observed_at_nano=NANO, available_at_nano=NANO,
            values=(late_value, late_value), source_state_sha256=H64)


def test_p0_causality_nt_none_keep_and_recovery_consistency() -> None:
    """Nt=None keeps the position (no link) while hard risk stays available."""
    unvalued = ExitFeatureValue(feature_key=NLI_KEY, value=None,
                                observed_at_nano=NANO, available_at_nano=NANO,
                                quality=DQ_MISSING, source_hashes={"SYN:1": H64})
    assert unvalued.value is None and unvalued.quality.reasons
    with pytest.raises(ValueError, match="quality_reasons"):
        ExitFeatureValue(feature_key=NLI_KEY, value=None, observed_at_nano=NANO,
                         available_at_nano=NANO, quality=DQ_READY,
                         source_hashes={"SYN:1": H64})
    snapshot = ExitFeatureSnapshot(
        snapshot_id="SYN-SNAP-2", account_id="SYN-ACCOUNT-1",
        contract_id="SYN.rb2609", strategy_id="SYNTHETIC:entry-1",
        episode_id="SYN-EP-1", exit_binding_sha256=_binding().binding_sha256,
        duration_seconds=60, observed_at_nano=NANO, available_at_nano=NANO,
        values=(unvalued,), source_state_sha256=H64)
    assert decode_record(ExitFeatureSnapshot, encode_record(snapshot)) == snapshot
    # Soft branch on unknown Nt: KEEP with an explicit reason, and KEEP can
    # never become an execution link — no OrderIntent may originate from it.
    keep = _proposal(action="KEEP", target_lots=3,
                     triggering_policy_id="SYNTHETIC:exit_time_decay_m1_v1",
                     reasons=("NLI_UNVALUED",), next_state=_state())
    assert keep.action == "KEEP"
    with pytest.raises(ValueError, match="reduce_or_exit"):
        ExitExecutionLink(
            link_id="SYN-LINK-1", exit_proposal_id=keep.proposal_id,
            strategy_proposal_id="SYN-PROP-1", decision_id="SYN-DEC-1",
            intent_id="SYN-INT-1", exit_binding_sha256=keep.exit_binding_sha256,
            action="KEEP", target_lots=3, requested_lots=3,
            execution_kind="NEXT_WINDOW_CLOSE")
    # Hard risk at the same instant remains fully constructible: valuation
    # failure blocks neither the hard deadline exit nor its link.
    hard = _proposal(triggering_policy_id=None, reasons=("HOLDING_DEADLINE",))
    link = ExitExecutionLink(
        link_id="SYN-LINK-2", exit_proposal_id=None,
        strategy_proposal_id="SYN-PROP-1", decision_id="SYN-DEC-1",
        intent_id="SYN-INT-1", exit_binding_sha256=hard.exit_binding_sha256,
        action="EXIT", target_lots=0, requested_lots=1,
        execution_kind="NEXT_WINDOW_CLOSE")
    assert link.exit_proposal_id is None      # hard risk needs no M3C candidate
    with pytest.raises(ValueError, match="zero_target_lots"):
        dataclasses.replace(link, target_lots=1)
    with pytest.raises(ValueError, match="requested_lots"):
        dataclasses.replace(link, requested_lots=0)
    # Recovery consistency: byte-identical roundtrips for state, proposal and
    # link over the identical binding identity (OQ-4 byte roundtrip).
    for record in (snapshot, keep, hard, link):
        encoded = canonical_json_bytes(encode_record(record))
        assert canonical_json_bytes(encode_record(
            decode_record(type(record), encode_record(record)))) == encoded
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
    assert keep.exit_binding_sha256 == hard.exit_binding_sha256 == \
        link.exit_binding_sha256 == binding.binding_sha256
    # A different binding identity (priority order swapped) hashes differently
    # and the link rejects malformed hash strings outright.
    different = exit_binding_content_hash(
        "SYNTHETIC:entry-1", H64, (H64_B, H64_C), None,
        ("SYNTHETIC:exit_time_decay_m1_v1", "SYNTHETIC:exit_trailing_atr_m1_v1"),
        "m3b-legacy-rules-v2.1.2")
    assert different != binding.binding_sha256
    with pytest.raises(ValueError, match="64_lowercase_hex"):
        dataclasses.replace(link, exit_binding_sha256="not-a-hash")

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


# ═══ P1 behavior: causality, visibility and replay determinism ═════════════

from ak_trading.v2.exits.registry import (  # noqa: E402
    arbitrate_exit, evaluate_exit,
)
from ak_trading.v2.exits.state import (  # noqa: E402
    ExitStateError, commit_exit_observation,
)
from ak_trading.v2.exits.trailing import TrailingStopPolicy  # noqa: E402


def test_p1_causality_deterministic_and_forward_only() -> None:
    # Identical inputs produce identical candidates (pure functions); after
    # a commit the chain has advanced and a stale candidate can never be
    # replayed onto the advanced state (no rewriting of committed history).
    binding = _fx_binding((TRAILING_SPEC,))
    base = _fx_state(binding)
    policy = TrailingStopPolicy(TRAILING_SPEC)
    context = _fx_context(22 * MINUTE, _fx_mark("102", NANO + 22 * MINUTE),
                          previous=base)
    first = policy.evaluate(context, base)
    replay = policy.evaluate(context, base)
    assert first == replay  # deterministic candidate, same chain advance
    committed = commit_exit_observation(base, first)
    assert committed.state_prefix_hash != base.state_prefix_hash
    later_context = _fx_context(23 * MINUTE, _fx_mark("102.4", NANO + 23 * MINUTE),
                                previous=committed)
    second = policy.evaluate(later_context, committed)
    assert second.proposal_id != first.proposal_id
    # Re-committing the SAME candidate is an idempotent no-op (the event
    # identity is already the chain head) — never a double advance.
    assert commit_exit_observation(committed, first) is committed
    # A DIFFERENT stale candidate hashed on the pre-commit chain cannot be
    # replayed onto the advanced state (committed history is never rewritten).
    from ak_trading.v2.exits.state import advance_state
    stale = advance_state(base, "SYN-STALE-EVENT", price_peak=Decimal("999"),
                          price_peak_source_nano=NANO + 22 * MINUTE,
                          price_peak_source_hash=H64A)
    with pytest.raises(ExitStateError):
        commit_exit_observation(committed, dataclasses.replace(
            first, next_state=stale))


def test_p1_causality_future_mark_invisible() -> None:
    # Counterexample 6: a mark dated after the decision time is invisible —
    # it cannot arm, value or trigger anything (visible-prefix rule).
    binding = _fx_binding((TRAILING_SPEC,))
    base = _fx_state(binding)
    context = _fx_context(10 * MINUTE, _fx_mark("999", NANO + 20 * MINUTE),
                          previous=base)
    proposal = TrailingStopPolicy(TRAILING_SPEC).evaluate(context, base)
    assert proposal.action == "KEEP"
    assert proposal.next_state.armed is False
    facts = None
    from ak_trading.v2.exits.features import collect_exit_facts
    facts = collect_exit_facts(context, base)
    assert facts.mark is None and facts.unrealized is None


def test_p1_causality_observation_before_execution_window() -> None:
    # Counterexample 2/6: the completed observation at t only produces a
    # request whose earliest execution is the NEXT legal window; nothing
    # claims an intra-bar fill and the candidate state stays pre-fill.
    binding = _fx_binding((TRAILING_SPEC,))
    armed = _fx_state(
        binding, armed=True, price_peak=Decimal("102"),
        price_peak_source_nano=NANO + 5 * MINUTE, price_peak_source_hash=H64A,
        protection_stop_price=Decimal("100"),
        protection_line_version="exit_trailing_atr_m1_v1:line:100",
        protection_effective_from_nano=NANO + 5 * MINUTE)
    context = _fx_context(10 * MINUTE, _fx_mark("99.9", NANO + 10 * MINUTE),
                          previous=armed)
    proposal = TrailingStopPolicy(TRAILING_SPEC).evaluate(context, armed)
    assert proposal.action == "EXIT"
    assert proposal.observed_at_nano == NANO + 10 * MINUTE
    assert proposal.earliest_execution_nano > proposal.observed_at_nano
    assert (proposal.earliest_execution_nano - proposal.observed_at_nano) \
        % (60 * 1_000_000_000) == 0  # lands on a minute boundary
    assert proposal.next_state.phase == "MANAGING"
    assert proposal.next_state.closed_lots == 0
    snapshot = build_exit_feature_snapshot(context, armed)
    assert snapshot.available_at_nano == snapshot.observed_at_nano
    for value in snapshot.values:
        assert value.available_at_nano <= snapshot.available_at_nano


def test_p1_causality_legacy_delegates_and_word_ban_holds() -> None:
    # The LEGACY adapter never adds an exit; every reason across all
    # candidates stays free of the legacy purpose words (no mis-routing).
    specs = (LEGACY_SPEC, TRAILING_SPEC, TIME_DECAY_SPEC)
    binding = _fx_binding(specs)
    for price in ("99.9", "102"):
        context = _fx_context(
            10 * MINUTE, _fx_mark(price, NANO + 10 * MINUTE), specs=specs,
            previous=_fx_state(binding))
        candidates = evaluate_exit(context, context.previous_exit_state)
        legacy = [p for p in candidates
                  if p.triggering_policy_id == "exit_legacy_m1_v1"]
        assert len(legacy) == 1 and legacy[0].action == "KEEP"
        assert "LEGACY_DEFERS_TO_ORIGINAL_CHANNELS" in legacy[0].reasons
        for proposal in candidates:
            for word in ("STOP", "TARGET", "ROLL"):
                assert all(word not in reason for reason in proposal.reasons)
        winner = arbitrate_exit(candidates, context)
        if winner.action == "EXIT":
            assert winner.target_lots == 0


def test_p1_replay_logic_only_deterministic() -> None:
    # P1 review finding #2: exits/replay.py completes the nine first-batch
    # files. The logical replay is deterministic, never a financial NAV
    # (financial_result is null) and advances only the observation chain.
    from ak_trading.v2.exits.replay import (
        LogicalExitReplay, logical_exit_replay_bytes, replay_exit_policy,
    )
    binding = _fx_binding((TRAILING_SPEC,))
    base = _fx_state(binding)
    first_ctx = _fx_context(22 * MINUTE, _fx_mark("102", NANO + 22 * MINUTE),
                            previous=base)
    second_ctx = _fx_context(23 * MINUTE, _fx_mark("99.9", NANO + 23 * MINUTE),
                             previous=None)
    replay = replay_exit_policy((first_ctx, second_ctx), base)
    assert isinstance(replay, LogicalExitReplay)
    assert replay.financial_result is None
    assert len(replay.proposals) == 2
    assert replay.final_state.armed is True
    assert replay.final_state.state_prefix_hash != base.state_prefix_hash
    again = replay_exit_policy((first_ctx, second_ctx), base)
    assert logical_exit_replay_bytes(replay) == logical_exit_replay_bytes(again)
    # Mixed scopes are refused.
    foreign = dataclasses.replace(second_ctx, episode_id="SYN-EP-9")
    with pytest.raises(Exception):
        replay_exit_policy((first_ctx, foreign), base)
