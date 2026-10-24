"""Offline fixed-candidate payoff-path measurement (E-P1 RULING 2/3).

This module implements the arbitration-selected data-production interface
``replay_payoff_training_window`` (decision v2-ep1-training-path-20260917-r1
RULING 2.1/2.2/2.3 and RULING 3): a pure offline counterfactual economic
measurement of ONE pre-frozen real M3B candidate per selection window,
executed through the real matching, ledger, state-commit and exit machinery.

It is NOT a trading decision and never an admission approval:

* no ``GateDecision(eligible=True)`` is ever constructed here (the only
  intermediate gates handed to ``enforce_account_risk`` are explicitly
  NOT-eligible stubs whose reasons are empty -- the risk module's added
  reasons are independent of the input gate's reason list);
* no production ``DecisionRecord`` / ``selected_proposal_id`` exists inside
  this package; orders resolve only to ``PayoffPathDecision`` records;
* the payoff dependency is an HONEST empty model
  (``fit_path_payoff_model((), ...)``) used solely to keep
  ``StrategyContext.model_hashes`` on its frozen key set -- nothing here
  estimates, consumes or attaches a payoff;
* the account lives in the isolated ``payoff-path:`` namespace derived from
  the plan hash, starts flat with the plan's cash, and never restores or
  publishes as a normal run.

The module is pure offline computation: no file IO, no registration, no
feed/broker connection, no label or payoff-model parameters.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from ak_trading.v2.artifacts import canonical_json_bytes, sha256_hex, stable_id
from ak_trading.v2.contracts import (
    ExecutionResult, ExitPolicyBinding, ExitSpec, Fill, ForecastRequest,
    FrozenInput, GateDecision, JsonValue, MarketBar, MarketForecast,
    PayoffPathCheckpoint, PayoffPathDecision, PayoffPathOrder, PayoffPathResult,
    PayoffReceiptCommit, PayoffTrainingPlan, RegimeAssessment, RunPolicy,
    StrategyProposal, StrategySpec, TradingRule,
    PAYOFF_PATH_ACCOUNT_ID_PREFIX, PAYOFF_PATH_CHECK_DOMAINS,
    PAYOFF_TRAINING_PLAN_SELECTION_RULE,
    payoff_path_decision_id, payoff_path_decision_source_sha256, payoff_path_id,
    payoff_training_plan_sha256,
)
from ak_trading.v2.data import visible_bars, visible_rule
from ak_trading.v2.decision.execution import (
    capacity_before, position_for, simulate_execution, simulate_protective_exit,
)
from ak_trading.v2.decision.gates import evaluate_gate, non_payoff_evidence_reasons
from ak_trading.v2.decision.ledger import ContinuousLedger, record_hash
from ak_trading.v2.decision.payoffs import P1_OPEN_FAMILIES, fit_path_payoff_model
from ak_trading.v2.decision.pipeline import (
    build_strategy_context, exit_registry_hash_for, model_hashes_for, registry_hash_for,
)
from ak_trading.v2.decision.replay import _hard_exit, apply_receipt, intents_for_proposal
from ak_trading.v2.decision.risk import enforce_account_risk
from ak_trading.v2.probability.forecast import predict_market
from ak_trading.v2.probability.models import ForecastModelSet
from ak_trading.v2.probability.regimes import RegimeReference
from ak_trading.v2.schemas import encode_record
from ak_trading.v2.seasonality import SeasonalityModel
from ak_trading.v2.strategies.registry import strategy_registry_v2
from ak_trading.v2.strategies.state import commit_observation, commit_selected_proposal

NS = 60_000_000_000
ZERO = Decimal("0")
ZERO_HASH = "0" * 64

# §3.1.5: the explicit out-of-support marker of the measurement protocol.
P1_DOMAIN_CODE = "DOMAIN_NOT_P1_SINGLE_LAYER_OPEN"

# §3.1.3: the frozen projection of every ordinary non-payoff gate/risk reason
# onto the seven measurement domains. Every code the shared gate computation
# or enforce_account_risk can emit is mapped exactly once; an unmapped code is
# an integrity contradiction (raising), never a silent waiver.
_REASON_DOMAINS: Mapping[str, str] = MappingProxyType({
    # IDENTITY_TIME -- evidence/identity and (shadow) qualification identity.
    "EVIDENCE_IDENTITY_MISMATCH": "IDENTITY_TIME",
    "STATISTICS_NOT_PASSED": "IDENTITY_TIME",
    "QUALIFICATION_EXPIRED": "IDENTITY_TIME",
    "QUALIFICATION_MISMATCH": "IDENTITY_TIME",
    # DATA_CLOCK -- PIT/completeness of bars, factors and valuations.
    "DATA_STALE": "DATA_CLOCK",
    "DATA_INVALID": "DATA_CLOCK",
    "CLOCK_UNKNOWN": "DATA_CLOCK",
    "CURRENT_DAY_DAILY_FORBIDDEN": "DATA_CLOCK",
    "PRICE_MISSING": "DATA_CLOCK",
    "PRICE_STALE": "DATA_CLOCK",
    # REGIME_MACRO -- regime admissibility and macro overlay.
    "REGIME_UNKNOWN": "REGIME_MACRO",
    "REGIME_INELIGIBLE": "REGIME_MACRO",
    "UNVALIDATED_TIMEFRAME_CONFLICT": "REGIME_MACRO",
    "MACRO_REQUIRED_MISSING": "REGIME_MACRO",
    "MACRO_EVENT_RISK": "REGIME_MACRO",
    "MACRO_COVERAGE_UNKNOWN": "REGIME_MACRO",
    # FORECAST_CONFIDENCE -- publication, validity, calibration and sample
    # sufficiency of the M3A forecast evidence (NOT the payoff sample counts).
    "FORECAST_LATE": "FORECAST_CONFIDENCE",
    "MODEL_EXPIRED": "FORECAST_CONFIDENCE",
    "OOD": "FORECAST_CONFIDENCE",
    "AMBIGUITY_ABOVE_CAP": "FORECAST_CONFIDENCE",
    "BARRIER_MISMATCH": "FORECAST_CONFIDENCE",
    "PERSISTENCE_BELOW_THRESHOLD": "FORECAST_CONFIDENCE",
    "STOP_TOUCH_ABOVE_CAP": "FORECAST_CONFIDENCE",
    "CALIBRATION_FAILED": "FORECAST_CONFIDENCE",
    "INSUFFICIENT_SAMPLES": "FORECAST_CONFIDENCE",
    "INSUFFICIENT_BLOCKS": "FORECAST_CONFIDENCE",
    # TRIGGER_DOMAIN -- the native trigger/confirmation/window and support domain.
    "TRIGGER_NOT_MET": "TRIGGER_DOMAIN",
    "HOLDING_WINDOW_TOO_SHORT": "TRIGGER_DOMAIN",
    "EXIT_PENDING": "TRIGGER_DOMAIN",
    "POSITION_OWNERSHIP_MISMATCH": "TRIGGER_DOMAIN",
    P1_DOMAIN_CODE: "TRIGGER_DOMAIN",
    # COST_BUDGET -- contract/lot/tick, frozen loss and stress budget facts.
    "COST_UNKNOWN": "COST_BUDGET",
    "ROUND_BUDGET_EXCEEDED": "COST_BUDGET",
    "TAIL_LOSS_LIMIT": "COST_BUDGET",
    "RULE_MISSING": "COST_BUDGET",
    # ACCOUNT_CAPACITY -- day loss, drawdown, margin, concentration, capacity.
    "MARGIN_LIMIT": "ACCOUNT_CAPACITY",
    "DAY_LOSS_LIMIT": "ACCOUNT_CAPACITY",
    "DRAWDOWN_LIMIT": "ACCOUNT_CAPACITY",
    "CAPACITY_UNKNOWN": "ACCOUNT_CAPACITY",
    "CAPACITY_EXHAUSTED": "ACCOUNT_CAPACITY",
    "CONCENTRATION_LIMIT": "ACCOUNT_CAPACITY",
})


def payoff_path_account_id(plan_sha256: str) -> str:
    """§2.2 isolated initial-account id: derived directly from the plan hash
    (the derivation the in-tree M5 consumer ``_training_opportunity`` uses).
    """
    if (not isinstance(plan_sha256, str) or len(plan_sha256) != 64
            or any(char not in "0123456789abcdef" for char in plan_sha256)):
        raise ValueError("plan_sha256_must_be_64_lowercase_hex")
    return PAYOFF_PATH_ACCOUNT_ID_PREFIX + plan_sha256


def _domain_facts(reasons) -> dict[str, tuple[str, ...]]:
    """Project a reason collection onto the seven frozen domains."""
    facts: dict[str, list[str]] = {domain: [] for domain in PAYOFF_PATH_CHECK_DOMAINS}
    for reason in sorted(set(reasons)):
        domain = _REASON_DOMAINS.get(reason)
        if domain is None:
            # Unknown evidence code: an integrity contradiction of the shared
            # computation, never a silent domain waiver.
            raise ValueError(f"payoff_path_unmapped_gate_reason:{reason}")
        facts[domain].append(reason)
    return {domain: tuple(values) for domain, values in facts.items()}


def _path_order(intent) -> PayoffPathOrder:
    """Field-for-field conversion OrderIntent -> PayoffPathOrder (no economic
    transformation; the only replacement is decision_id -> path_decision_id)."""
    return PayoffPathOrder(
        intent_id=intent.intent_id, path_decision_id=intent.decision_id,
        strategy_id=intent.strategy_id, episode_id=intent.episode_id,
        contract_id=intent.contract_id, created_at_nano=intent.created_at_nano,
        not_before_nano=intent.not_before_nano, expires_at_nano=intent.expires_at_nano,
        delta_lots=intent.delta_lots, offset=intent.offset, purpose=intent.purpose,
        layer_index=intent.layer_index, limit_price=intent.limit_price)


def _path_checkpoint_hash(checkpoint: PayoffPathCheckpoint) -> str:
    """checkpoint_hash recomputed inside the ak_probability_payoff_path_v1 domain."""
    payload = encode_record(checkpoint)
    del payload["checkpoint_hash"]
    return sha256_hex(canonical_json_bytes(payload))


def _to_path_checkpoint(checkpoint, path_id: str, previous_hash: str) -> PayoffPathCheckpoint:
    """LedgerCheckpoint -> PayoffPathCheckpoint, field-for-field, with the
    pending intents strictly re-typed and the hash AND predecessor chain
    recomputed inside the ak_probability_payoff_path_v1 domain. A converted
    checkpoint never restores as a normal checkpoint."""
    base = PayoffPathCheckpoint(
        sequence=checkpoint.sequence, contract_lock_sha256=checkpoint.contract_lock_sha256,
        registry_sha256=checkpoint.registry_sha256,
        exit_registry_sha256=checkpoint.exit_registry_sha256,
        run_policy=checkpoint.run_policy, rule_hashes=checkpoint.rule_hashes,
        model_hashes=checkpoint.model_hashes,
        qualification_hashes=checkpoint.qualification_hashes,
        account=checkpoint.account, valuation_marks=checkpoint.valuation_marks,
        states=checkpoint.states, exit_bindings=checkpoint.exit_bindings,
        exit_states=checkpoint.exit_states,
        exit_execution_links=checkpoint.exit_execution_links,
        pending_intents=tuple(_path_order(intent) for intent in checkpoint.pending_intents),
        pending_remaining_lots=checkpoint.pending_remaining_lots,
        processed_event_ids=checkpoint.processed_event_ids,
        processed_fill_hashes=checkpoint.processed_fill_hashes,
        capacity_used=checkpoint.capacity_used, previous_hash=previous_hash,
        checkpoint_hash=ZERO_HASH, path_id=path_id)
    return replace(base, checkpoint_hash=_path_checkpoint_hash(base))


def _released(checkpoint) -> bool:
    """Episode-free, pending-free and reservation-free committed state."""
    return (not checkpoint.account.lots and not checkpoint.pending_intents
            and checkpoint.account.reserved_margin_cny == ZERO
            and not any(s.budget is not None or s.episode_id is not None
                        or s.pending_intent_ids for s in checkpoint.states))


def _unique(rows, key):
    seen = {}
    for row in rows:
        identity = getattr(row, key)
        if identity in seen and seen[identity] != row:
            raise ValueError("conflicting_output_identity")
        seen[identity] = row
    return tuple(seen.values())


def replay_payoff_training_window(
    bundle: FrozenInput,
    forecast_models: ForecastModelSet,
    spec: StrategySpec,
    initial_account,
    run_policy: RunPolicy,
    policy: Mapping[str, JsonValue],
    seasonality_model: SeasonalityModel | None,
    regime_reference: RegimeReference,
    contract_lock_sha256: str,
    plan: PayoffTrainingPlan,
    *,
    exit_specs: tuple[ExitSpec, ...],
    exit_binding: ExitPolicyBinding,
) -> PayoffPathResult:
    """Measure ONE pre-frozen real M3B candidate through the real execution
    chain (E-P1 RULING 2.1). Pure offline; no payoff/label/forced-candidate
    parameters exist on this entry point."""
    # -- §2.2: run policy is an execution constraint and identity source only.
    if (run_policy.purpose != "RESEARCH_EVALUATION" or run_policy.routing_mode != "FIXED"
            or run_policy.macro_mode != "PRICE_ONLY" or run_policy.cost_scenario != "BASE"):
        raise ValueError("payoff_path_run_policy_must_be_research_fixed_price_only_base")
    if spec.strategy_id != plan.strategy_id:
        raise ValueError("payoff_path_spec_strategy_must_match_plan")
    if plan.selection_rule != PAYOFF_TRAINING_PLAN_SELECTION_RULE:
        raise ValueError("payoff_path_plan_selection_rule_invalid")
    if (exit_binding.strategy_id != spec.strategy_id
            or exit_binding.binding_sha256 != plan.exit_binding_sha256):
        raise ValueError("payoff_path_exit_binding_must_match_plan")
    if (run_policy.fixed_strategy_id != spec.strategy_id
            or run_policy.fixed_exit_binding_sha256 != exit_binding.binding_sha256):
        raise ValueError("payoff_path_run_policy_fixed_identity_mismatch")
    from ak_trading.v2.exits.registry import exit_spec_sha256
    registered = {exit_spec_sha256(item) for item in exit_specs}
    if not set(exit_binding.exit_spec_sha256s) <= registered:
        raise ValueError("payoff_path_binding_specs_not_in_exit_registry")
    instrument_ids = {item.contract_id for item in bundle.instruments}
    if not set(plan.contract_ids) <= instrument_ids:
        raise ValueError("payoff_path_plan_contract_missing_from_input")
    # The frozen models must be the inner-prefix fit the plan declares.
    if (forecast_models.training_end_nano > plan.inner_training_end_nano
            or forecast_models.calibration_end_nano > plan.inner_calibration_end_nano):
        raise ValueError("payoff_path_models_not_frozen_on_inner_prefix")

    plan_sha256 = payoff_training_plan_sha256(plan)
    if initial_account.account_id != payoff_path_account_id(plan_sha256):
        raise ValueError("payoff_path_initial_account_id_must_derive_from_plan")
    if initial_account.as_of_nano != plan.warmup_start_nano:
        raise ValueError("payoff_path_initial_account_clock_must_equal_warmup_start")

    # §2.1: honest empty payoff dependency handle -- identity only, never an
    # estimate; the normal gate on the same candidate still rejects for lack
    # of payoff support (INSUFFICIENT_SAMPLES / NON_POSITIVE_EV downstream).
    empty_payoff = fit_path_payoff_model((), plan.inner_training_end_nano,
                                         run_policy.research_policy_version)
    registry = registry_hash_for((spec,))
    exit_registry = exit_registry_hash_for(exit_specs)
    hashes = dict(model_hashes_for(forecast_models, empty_payoff, regime_reference,
                                   seasonality_model))
    path_id = payoff_path_id(plan_sha256, bundle.manifest_sha256, initial_account,
                             contract_lock_sha256, registry, exit_registry, hashes)

    strategies = dict(strategy_registry_v2(spec.mode))
    if spec.strategy_id not in strategies:
        raise ValueError("unknown_strategy_spec")
    strategy = type(strategies[spec.strategy_id])(spec)
    by_contract = {item.contract_id: item for item in bundle.instruments}

    ledger = ContinuousLedger(initial_account, bundle.instruments, run_policy,
                              contract_lock_sha256, registry,
                              exit_registry_sha256=exit_registry)
    states: dict[tuple[str, str], object] = {}
    pending: dict[str, object] = {}
    pending_remaining: dict[str, int] = {}
    cumulative_capacity: dict[str, int] = {}

    features_rows: list = []
    forecast_rows: list = []
    regime_rows: list = []
    candidate_rows: list[StrategyProposal] = []
    candidate_checks: dict[str, dict[str, tuple[str, ...]]] = {}
    path_decisions: list[PayoffPathDecision] = []
    order_rows: list[PayoffPathOrder] = []
    fill_rows: list[Fill] = []
    equity_rows: list = []
    checkpoint_rows: list[PayoffPathCheckpoint] = []
    execution_rows: list[ExecutionResult] = []
    commit_rows: list[PayoffReceiptCommit] = []
    exclusions: list[str] = []
    used_bars: dict[str, MarketBar] = {}
    selected: PayoffPathDecision | None = None
    previous_path_hash: list[str] = [ZERO_HASH]

    def provider(models, request, regime):
        return predict_market(models, request, regime, request.features.decision_nano)

    def measure_checks(proposal: StrategyProposal, context, now: int):
        """Seven-domain facts: shared non-payoff gate evidence + account risk."""
        reasons, _evidence, _paywalled = non_payoff_evidence_reasons(
            proposal, context, policy, checked_at_nano=now)
        reasons = list(reasons)
        if (proposal.action != "OPEN" or proposal.episode_id is None
                or proposal.budget is None or len(proposal.budget.layers) != 1
                or proposal.layer_index != 0 or proposal.forecast_id is None
                or context.spec.family not in P1_OPEN_FAMILIES):
            reasons.append(P1_DOMAIN_CODE)
        account_now = ledger.snapshot(now)
        contract = context.features.contract_id
        pending_owner = any(intent.contract_id == contract for intent in pending.values())
        other_held = any(
            lot.contract_id != contract
            and by_contract[lot.contract_id].product == by_contract[contract].product
            for lot in account_now.lots)
        if pending_owner or other_held:
            reasons.append("EXIT_PENDING")
        rule = context.rule
        cap = capacity_before(context.bars, contract,
                              60 if spec.mode == "MINUTE" else 86400, now, rule)
        capacity = {} if cap is None else {contract: cap}
        # An explicitly NOT-eligible stub: enforce_account_risk's added
        # reasons are independent of the input gate's reason list, and no
        # fake eligible GateDecision is ever constructed here.
        stub = (GateDecision(proposal.proposal_id, False, (), (), now),)
        risk_snapshot = ledger._fork().checkpoint(
            tuple(states.values()), tuple(pending.values()), hashes, {},
            pending_remaining_lots=pending_remaining,
            exit_registry_sha256=exit_registry, exit_bindings=(exit_binding,),
            exit_states=(), exit_execution_links=())
        risked = enforce_account_risk(
            (proposal,), stub, risk_snapshot.account, bundle.instruments,
            risk_snapshot.states, risk_snapshot.pending_intents, bundle.rules,
            capacity, policy, valuations=risk_snapshot.valuation_marks)
        reasons.extend(risked[0].reasons)
        return _domain_facts(reasons)

    def receipt(intent, result, now, event_commits):
        key = (intent.contract_id, intent.strategy_id)
        states[key], new_fills = apply_receipt(ledger, states[key], intent, result,
                                               pending_remaining[intent.intent_id],
                                               bundle.rules, now)
        fill_rows.extend(new_fills)
        execution_rows.append(result)
        event_commits.append((len(execution_rows) - 1, now))
        if result.status in ("FILLED", "CANCELLED", "EXPIRED"):
            pending.pop(intent.intent_id, None)
            pending_remaining.pop(intent.intent_id, None)
        else:
            pending_remaining[intent.intent_id] = result.remaining_lots

    def path_decision(action, proposal, context, request, checks, now) -> PayoffPathDecision:
        account_before = ledger.snapshot(now)
        source = payoff_path_decision_source_sha256(
            plan_sha256, proposal, context, request, record_hash(account_before),
            checks, action)
        return PayoffPathDecision(
            path_decision_id=payoff_path_decision_id(source), plan_sha256=plan_sha256,
            observed_at_nano=now, proposal=proposal, context=context, request=request,
            account_before_sha256=record_hash(account_before), checks=checks,
            action=action, source_sha256=source)

    # -- event calendar: session timers plus bar open/visible boundaries.
    events: dict[int, dict] = {}
    for session in bundle.sessions:
        for t in range(session.start_nano, session.end_nano + 1, NS):
            if plan.warmup_start_nano <= t < plan.training_end_nano:
                events.setdefault(t, {"open": [], "visible": []})
    for b in bundle.bars:
        if b.duration_seconds not in (60, 86400):
            continue
        if plan.warmup_start_nano <= b.start_nano < plan.training_end_nano:
            events.setdefault(b.start_nano, {"open": [], "visible": []})["open"].append(b)
        at = b.available_at_nano
        if (b.complete and at is not None and b.end_nano <= at
                and plan.warmup_start_nano <= at < plan.training_end_nano):
            events.setdefault(at, {"open": [], "visible": []})["visible"].append(b)

    for now, event in sorted(events.items()):
        event_id = stable_id("payoff_path_event",
                             canonical_json_bytes([now, record_hash(run_policy), registry]))
        a = ledger.snapshot(now)
        event_commits: list[tuple[int, int]] = []
        # Expiration cancels risk additions only; owed exits re-issue later.
        for intent in sorted(tuple(pending.values()), key=lambda i: i.intent_id):
            next_day = next((b.trading_day for b in event["open"]
                             if b.contract_id == intent.contract_id), None)
            stale_offset = (intent.offset == "CLOSE_TODAY" and next_day is not None
                            and next_day != ledger.snapshot(now).trading_day)
            if now >= intent.expires_at_nano or stale_offset:
                receipt(intent, ExecutionResult(intent.intent_id, (),
                        pending_remaining[intent.intent_id], "EXPIRED", ("INTENT_EXPIRED",)),
                        now, event_commits)
        event_day = max((b.trading_day for b in event["open"]
                         if b.trading_day is not None), default=a.trading_day)
        if event_day > a.trading_day:
            ledger.mark({}, now, event_day, bundle.rules)
            a = ledger.snapshot(now)
        # Risk locks, deadlines and lifecycle generate hard exits on clock events.
        for key, s in sorted(tuple(states.items())):
            position = position_for(a, s.contract_id)
            if position is None or s.budget is None:
                continue
            inst = by_contract[s.contract_id]
            deadlines = [x for x in (s.active_exit_nano, inst.delivery_cutoff_nano,
                                     inst.last_trade_nano) if x is not None]
            due = bool(deadlines and now >= min(deadlines))
            mark = ledger._marks.get(s.contract_id)
            stale = mark is None or mark.status != "CURRENT" or (
                spec.mode == "MINUTE" and now - mark.as_of_nano > 120_000_000_000)
            if not (a.killed or due or stale or s.phase == "EXITING"):
                continue
            for intent in tuple(pending.values()):
                if intent.episode_id == s.episode_id and intent.offset == "OPEN":
                    receipt(intent, ExecutionResult(intent.intent_id, (),
                            pending_remaining[intent.intent_id], "CANCELLED", ("RISK_CANCEL",)),
                            now, event_commits)
            s = states[key]
            if any(intent.episode_id == s.episode_id and intent.offset != "OPEN"
                   for intent in pending.values()):
                continue
            starts = [t for sess in bundle.sessions if sess.product == inst.product
                      for t in range(sess.start_nano, sess.end_nano, NS) if t > now]
            if not starts:
                states[key] = replace(s, phase="EXITING")
                continue
            p = _hard_exit(s, a, now, min(starts),
                           "ROLL_EXIT" if inst.delivery_cutoff_nano is not None
                           and now >= inst.delivery_cutoff_nano else "TIME_EXIT")
            exit_context = build_strategy_context(
                bundle, s.contract_id, now, a, s, forecast_models, empty_payoff, spec,
                seasonality_model, regime_reference, run_policy, (),
                exit_binding=exit_binding, forecast_provider=provider)
            if exit_context.rule is not None:
                ledger._consume_rule(exit_context.rule)
            # Hard risk/clock exit: protocol-mandated management, no new risk.
            decision = path_decision("MANAGE_EXIT", p, exit_context, None,
                                     {domain: () for domain in PAYOFF_PATH_CHECK_DOMAINS}, now)
            path_decisions.append(decision)
            intents = intents_for_proposal(p, decision.path_decision_id, a,
                                           visible_rule(bundle, s.contract_id, now))
            states[key] = commit_selected_proposal(s, p, intents)
            for intent in intents:
                order_rows.append(_path_order(intent))
                pending[intent.intent_id] = intent
                pending_remaining[intent.intent_id] = intent.delta_lots
        for intent in sorted(tuple(pending.values()), key=lambda i: i.intent_id):
            if intent.offset != "OPEN":
                continue
            if now >= forecast_models.valid_until_nano:
                receipt(intent, ExecutionResult(intent.intent_id, (),
                        pending_remaining[intent.intent_id], "CANCELLED", ("MODEL_EXPIRED",)),
                        now, event_commits)
        used: dict[str, int] = {}
        # Only the execution layer sees not-yet-complete execution bars.
        for b in sorted(event["open"], key=lambda b: (b.contract_id, b.duration_seconds, b.bar_id)):
            if b.contract_id not in plan.contract_ids:
                continue
            matching = sorted((intent for intent in pending.values()
                               if intent.contract_id == b.contract_id and (
                                   "MINUTE" if b.duration_seconds == 60 else "DAILY") == spec.mode),
                              key=lambda i: (i.offset == "OPEN", i.created_at_nano, i.intent_id))
            if not matching:
                continue
            used_bars[b.bar_id] = b
            rule = visible_rule(bundle, b.contract_id, now)
            capacity_key = f"{b.contract_id}|{now}|{run_policy.cost_scenario}"
            previous = visible_bars(bundle, b.contract_id, b.duration_seconds, now, False)
            cap = capacity_before(previous, b.contract_id, b.duration_seconds, now, rule)
            remaining = max(0, (cap or 0) - cumulative_capacity.get(capacity_key, 0))
            for intent in matching:
                if intent.not_before_nano > now or intent.created_at_nano >= now:
                    continue
                position = position_for(ledger.snapshot(now), b.contract_id)
                if rule is None:
                    result = ExecutionResult(intent.intent_id, (),
                                             pending_remaining[intent.intent_id], "BLOCKED",
                                             ("COST_UNKNOWN", "EXIT_PENDING"))
                else:
                    execution_intent = replace(intent, delta_lots=pending_remaining[intent.intent_id])
                    state = states.get((b.contract_id, intent.strategy_id))
                    budget = None if state is None else state.budget
                    if (intent.purpose in ("STOP", "TARGET") and position is not None
                            and budget is not None):
                        result = simulate_protective_exit(execution_intent, b, rule, remaining,
                                                          position, budget)
                    else:
                        result = simulate_execution(execution_intent, b, rule, remaining, position)
                if result.status == "BLOCKED" and not result.fills and intent.offset == "OPEN":
                    # Transiently blocked entry: recorded once, retried later;
                    # state stays untouched (mirrors the ordinary scheduler).
                    execution_rows.append(result)
                    event_commits.append((len(execution_rows) - 1, now))
                else:
                    receipt(intent, result, now, event_commits)
                count = sum(f.lots for f in result.fills)
                remaining -= count
                used[capacity_key] = used.get(capacity_key, 0) + count
                cumulative_capacity[capacity_key] = cumulative_capacity.get(capacity_key, 0) + count
        current = ledger.snapshot(now)
        # Latest visible complete closes only.
        prices = {}
        trading_day = current.trading_day
        for b in sorted(event["visible"], key=lambda b: (b.end_nano, b.contract_id, b.duration_seconds)):
            if b.close is not None and b.quality.status not in ("INVALID", "MISSING"):
                prices[b.contract_id] = b
                if b.trading_day is not None:
                    trading_day = max(trading_day, b.trading_day)
        if prices or current.lots:
            ledger.mark(prices, now, trading_day, bundle.rules)
        # Observation submission for the real M3B strategy; candidate checks
        # and management measurement happen at the same legal boundary.
        activated = sorted({(b.contract_id, "MINUTE" if b.duration_seconds == 60 else "DAILY")
                            for b in event["visible"] if b.contract_id in plan.contract_ids})
        for contract, mode in activated:
            if mode != spec.mode:
                continue
            key = (contract, spec.strategy_id)
            context = build_strategy_context(
                bundle, contract, now, ledger.snapshot(now), states.get(key),
                forecast_models, empty_payoff, spec, seasonality_model,
                regime_reference, run_policy, (), exit_binding=exit_binding,
                forecast_provider=provider)
            if context.rule is not None:
                ledger._consume_rule(context.rule)
            proposal = strategy.evaluate(context, states.get(key))
            states[key] = commit_observation(states.get(key), proposal)
            features_rows.append(context.features)
            forecast_rows.extend(context.forecasts)
            regime_rows.append(context.regime)
            if selected is None:
                if (proposal.action == "OPEN"
                        and plan.window_start_nano <= now < plan.window_end_nano):
                    checks = measure_checks(proposal, context, now)
                    candidate_checks[proposal.proposal_id] = checks
                    candidate_rows.append(proposal)
                    if not any(checks.values()):
                        # FIRST_FEASIBLE_P1_OPEN_PER_WINDOW: frozen at selection.
                        request = _candidate_request(context, proposal)
                        decision = path_decision("MEASURE_ENTRY", proposal, context,
                                                 request, checks, now)
                        path_decisions.append(decision)
                        selected = decision
                        intents = intents_for_proposal(
                            proposal, decision.path_decision_id, ledger.snapshot(now),
                            visible_rule(bundle, contract, now))
                        states[key] = commit_selected_proposal(states.get(key), proposal, intents)
                        for intent in intents:
                            order_rows.append(_path_order(intent))
                            pending[intent.intent_id] = intent
                            pending_remaining[intent.intent_id] = intent.delta_lots
            elif proposal.action in ("EXIT", "REDUCE") and proposal.episode_id is not None:
                held = states.get(key)
                if held is None or held.episode_id != proposal.episode_id:
                    continue
                gate = evaluate_gate(proposal, context, policy, checked_at_nano=now)
                checks = _domain_facts(gate.reasons)
                if not gate.eligible:
                    continue
                decision = path_decision("MANAGE_EXIT", proposal, context, None, checks, now)
                path_decisions.append(decision)
                for old in tuple(pending.values()):
                    if old.episode_id == proposal.episode_id and old.offset == "OPEN":
                        receipt(old, ExecutionResult(old.intent_id, (),
                                pending_remaining[old.intent_id], "CANCELLED", ("RISK_CANCEL",)),
                                now, event_commits)
                if not any(intent.episode_id == proposal.episode_id for intent in pending.values()):
                    intents = intents_for_proposal(
                        proposal, decision.path_decision_id, ledger.snapshot(now),
                        visible_rule(bundle, contract, now))
                    states[key] = commit_selected_proposal(states.get(key), proposal, intents)
                    for intent in intents:
                        order_rows.append(_path_order(intent))
                        pending[intent.intent_id] = intent
                        pending_remaining[intent.intent_id] = intent.delta_lots
            ledger.sync_reservations(tuple(states.values()), bundle.rules, now)
        ledger.record_event(event_id, used)
        equity_rows.append(ledger.snapshot(now))
        ledger_checkpoint = ledger.checkpoint(
            tuple(states.values()), tuple(pending.values()), hashes, {},
            pending_remaining_lots=pending_remaining, exit_registry_sha256=exit_registry,
            exit_bindings=(exit_binding,), exit_states=(), exit_execution_links=())
        path_checkpoint = _to_path_checkpoint(ledger_checkpoint, path_id,
                                              previous_path_hash[0])
        previous_path_hash[0] = path_checkpoint.checkpoint_hash
        checkpoint_rows.append(path_checkpoint)
        for index, observed in event_commits:
            # Every receipt (including no-Fill terminal states) is proven
            # committed by the checkpoint that actually completed it.
            commit_rows.append(PayoffReceiptCommit(index, observed,
                                                   path_checkpoint.checkpoint_hash))

    # -- consumed originals: rules exactly as consumed, bars, calendar.
    consumed: dict[str, str] = {}
    for checkpoint in checkpoint_rows:
        for rule_id, digest in checkpoint.rule_hashes.items():
            if rule_id in consumed and consumed[rule_id] != digest:
                raise ValueError("payoff_path_consumed_rule_digest_conflict")
            consumed[rule_id] = digest
    bars_by_id = {b.bar_id: b for b in bundle.bars}
    for checkpoint in checkpoint_rows:
        for mark in checkpoint.valuation_marks:
            if mark.source_bar_id is None:
                continue
            bar = bars_by_id.get(mark.source_bar_id)
            if bar is None or record_hash(bar) != mark.source_bar_sha256:
                raise ValueError("payoff_path_mark_bar_original_missing")
            used_bars[bar.bar_id] = bar
    originals = []
    for rule_id, digest in sorted(consumed.items()):
        matches = [r for r in bundle.rules if r.rule_id == rule_id]
        original = next((r for r in matches if record_hash(r) == digest), None)
        if original is None:
            raise ValueError("payoff_path_consumed_rule_original_missing")
        originals.append(original)
    rule_originals = tuple(sorted(originals, key=lambda r: (r.rule_id, record_hash(r))))
    bar_originals = tuple(sorted(used_bars.values(), key=lambda b: (b.start_nano, b.bar_id)))
    products = {item.product for item in bundle.instruments if item.contract_id in plan.contract_ids}
    path_sessions = tuple(sorted((s for s in bundle.sessions if s.product in products),
                                 key=lambda s: (s.start_nano, s.session_id)))
    # The frozen plan binds the WHOLE input calendar (the derivation the M5
    # plan builder uses); the result carries the product-scoped originals the
    # path actually consumed.
    if sha256_hex(canonical_json_bytes([encode_record(s) for s in bundle.sessions])) \
            != plan.calendar_sha256:
        raise ValueError("payoff_path_calendar_mismatch")

    reconciliation = dict(ledger.reconcile())
    if selected is None:
        status = "NO_CANDIDATE"
        exclusions.append("NO_TRIGGERED_BUDGETED_PATH")
        exclusions.extend(sorted({reason for checks in candidate_checks.values()
                                  for reasons in checks.values() for reason in reasons}))
    else:
        last = checkpoint_rows[-1] if checkpoint_rows else None
        residues = bool(ledger.snapshot(max(plan.warmup_start_nano,
                                            plan.training_end_nano - 1)).lots or pending
                        or reconciliation["status"] != "PASS"
                        or (last is not None and not _released(last)))
        status = "INCOMPLETE" if residues else "COMPLETE"
        if status == "INCOMPLETE":
            exclusions.append("PATH_NOT_RELEASED_BEFORE_TRAINING_END")

    return PayoffPathResult(
        source_kind="P1_ACTION_CONDITIONAL_REPLAY", path_id=path_id, plan=plan,
        input_manifest_sha256=bundle.manifest_sha256, initial_account=initial_account,
        features=_unique(features_rows, "snapshot_id"),
        forecasts=_unique(forecast_rows, "forecast_id"),
        regimes=_unique(regime_rows, "assessment_id"),
        proposals=tuple(candidate_rows), candidate_checks=candidate_checks,
        exit_specs=exit_specs, exit_binding=exit_binding, exit_features=(),
        exit_proposals=(), path_decisions=tuple(path_decisions),
        orders=tuple(order_rows), fills=tuple(fill_rows), equity=tuple(equity_rows),
        checkpoints=tuple(checkpoint_rows), execution_results=tuple(execution_rows),
        receipt_commits=tuple(commit_rows), rule_originals=rule_originals,
        bar_originals=bar_originals, sessions=path_sessions,
        exclusions=tuple(sorted(set(exclusions))), reconciliation=reconciliation,
        status=status)


def _candidate_request(context, proposal: StrategyProposal) -> ForecastRequest:
    """Rebuild the original decision-time ForecastRequest from the frozen
    candidate context -- the same field-for-field projection the forecast
    publisher validated, so the bytes equal the request the strategy actually
    consumed at that boundary."""
    matches = [f for f in context.forecasts if f.forecast_id == proposal.forecast_id]
    if len(matches) != 1:
        raise ValueError("payoff_path_candidate_forecast_not_unique")
    forecast = matches[0]
    return ForecastRequest(forecast.request_id, context.features, forecast.horizon,
                           forecast.window_start_nano, forecast.window_end_nano,
                           forecast.barrier_key, forecast.upper_distance,
                           forecast.lower_distance, forecast.frozen_range)
