"""Joint gates record every safely computable rejection, including identity."""
from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from ak_trading.v2.contracts import GateDecision, JsonValue, StrategyContext, StrategyProposal
from ak_trading.v2.policy import confidence_policy, stress_policy
from ak_trading.v2.schemas import calibration_geometry_key, probability_in_bucket
from ak_trading.v2.decision.execution import simulate_stress_paths
from ak_trading.v2.decision.payoffs import payoff_event, proposal_forecast


def non_payoff_evidence_reasons(proposal: StrategyProposal, context: StrategyContext,
                                policy: Mapping[str, JsonValue], *, checked_at_nano: int
                                ) -> tuple[list[str], list[str], bool]:
    """Every non-payoff joint-gate fact as ``(reasons, evidence_ids, paywalled)``.

    Shared internal pure computation (E-P1 arbitration 2026-09-17 RULING 3.1.4):
    the ordinary :func:`evaluate_gate` composes these facts with the payoff
    evidence block below; the offline measurement path
    (``decision/payoff_paths.py``) consumes the very same facts as its seven
    measurement domains. The composition is frozen: every check lives exactly
    where it lived before the extraction, ``paywalled`` marks the action
    families whose original evaluation returned before the payoff block, and
    no reason string is ever filtered out here -- in particular
    ``INSUFFICIENT_SAMPLES`` may come from the M3A calibration evidence in this
    function AND from the payoff block in ``evaluate_gate``; deleting it by
    string would wrongly waive forecast-sample requirements.
    """
    reasons = []
    evidence_ids = []
    now = context.now_nano
    spec = context.spec
    features = context.features
    position = context.position
    if proposal.strategy_id != spec.strategy_id or proposal.contract_id != features.contract_id \
            or proposal.decision_nano != now:
        reasons.append("EVIDENCE_IDENTITY_MISMATCH")
    if proposal.action in ("REDUCE", "EXIT"):
        if position is None or position.strategy_id != proposal.strategy_id \
                or position.episode_id != proposal.episode_id \
                or abs(proposal.target_lots) >= abs(position.lots) \
                or proposal.target_lots * position.lots < 0:
            reasons.append("POSITION_OWNERSHIP_MISMATCH")
        return reasons, evidence_ids, True
    if proposal.action not in ("OPEN", "ADD"):
        reasons.extend(proposal.reasons)
        reasons.append("TRIGGER_NOT_MET")
        return reasons, evidence_ids, True
    cp = confidence_policy(spec.family)
    if context.confidence_policy != cp or context.stress_policy != stress_policy():
        reasons.append("EVIDENCE_IDENTITY_MISMATCH")
    if context.run_policy.research_policy_version != policy["policy_version"]:
        reasons.append("EVIDENCE_IDENTITY_MISMATCH")
    if features.quality.status != "READY" or context.account.quality.status != "READY":
        reasons.append("DATA_STALE" if "STALE" in (features.quality.status,context.account.quality.status) else "DATA_INVALID")
    if features.available_at_nano > now or features.decision_nano != now:
        reasons.append("DATA_INVALID")
    if not context.sessions or any(s.evidence_status=="UNKNOWN" for s in context.sessions):
        reasons.append("CLOCK_UNKNOWN")
    if spec.mode=="MINUTE" and any(b.duration_seconds==86400 and b.trading_day==features.trading_day for b in context.bars):
        reasons.append("CURRENT_DAY_DAILY_FORBIDDEN")
    if any(b.end_nano>now or b.available_at_nano is None or b.available_at_nano>now or not b.complete for b in context.bars):
        reasons.append("DATA_INVALID")
    factors = {v.factor_key:v for v in features.values}
    if len(factors)!=len(features.values) or any(k not in factors or factors[k].value is None
            or factors[k].quality.status!="READY" or factors[k].available_at_nano is None
            or factors[k].available_at_nano>now for k in spec.required_factor_keys):
        reasons.append("DATA_INVALID")
    if spec.mode=="MINUTE" and context.bars:
        last=max(b.end_nano for b in context.bars if b.duration_seconds==60)
        active_age=sum(max(0,min(checked_at_nano,s.end_nano)-max(last,s.start_nano)) for s in context.sessions)
        if active_age>int(policy["minute_freshness_seconds"])*1_000_000_000:
            reasons.append("DATA_STALE")
    if context.regime.base=="UNKNOWN":
        reasons.append("REGIME_UNKNOWN")
    if context.regime.base not in spec.allowed_regimes or not set(context.regime.flags)<=set(spec.allowed_flags):
        reasons.append("REGIME_INELIGIBLE")
    if context.regime.cross_timeframe in ("CONFLICT", "COUNTERTREND"):
        reasons.append("UNVALIDATED_TIMEFRAME_CONFLICT")
    if spec.family in ("BREAKOUT","PULLBACK") and spec.family not in context.regime.flags:
        reasons.append("REGIME_INELIGIBLE")
    if context.run_policy.macro_mode=="WITH_MACRO":
        if context.macro.mode!="WITH_MACRO" or context.macro.quality.status!="READY":
            reasons.append("MACRO_REQUIRED_MISSING")
        if context.macro.risk_tags:
            reasons.append("MACRO_EVENT_RISK")
    if "MACRO_COVERAGE_UNKNOWN" in context.macro.quality.reasons:
        reasons.append("MACRO_COVERAGE_UNKNOWN")
    if context.macro.decision_nano!=now or context.macro.policy_version!=context.run_policy.macro_policy_version:
        reasons.append("EVIDENCE_IDENTITY_MISMATCH")
    if position is not None:
        if position.pending_exit:
            reasons.append("EXIT_PENDING")
        if (position.strategy_id!=proposal.strategy_id or position.episode_id!=proposal.episode_id
                or proposal.action=="OPEN" or proposal.target_lots*position.lots<=0):
            reasons.append("POSITION_OWNERSHIP_MISMATCH")
    elif proposal.action=="ADD":
        reasons.append("POSITION_OWNERSHIP_MISMATCH")
    # Signed-target contract (01 §4.1): OPEN must be the layer's signed absolute
    # target; ADD must strictly increase the owned position by exactly one layer.
    if proposal.budget is not None:
        if proposal.layer_index is None or proposal.layer_index>=len(proposal.budget.layers):
            reasons.append("EVIDENCE_IDENTITY_MISMATCH")
        else:
            signed_layer = proposal.budget.side*proposal.budget.layers[proposal.layer_index].lots
            if proposal.action=="OPEN" and (proposal.target_lots==0 or proposal.target_lots!=signed_layer):
                reasons.append("EVIDENCE_IDENTITY_MISMATCH")
            if proposal.action=="ADD" and position is not None:
                # F14: an ADD must grow the real position along the budget's
                # direction — |target|>|current| and target/side/current all
                # share one sign. A reducing or reversed target is never new
                # risk and is rejected before routing with zero weight.
                if (proposal.target_lots!=position.lots+signed_layer
                        or abs(proposal.target_lots)<=abs(position.lots)
                        or proposal.target_lots*proposal.budget.side<=0
                        or position.lots*proposal.budget.side<=0):
                    reasons.append("POSITION_OWNERSHIP_MISMATCH")
    if proposal.next_state.confirmation_count<cp.confirmation_observations:
        reasons.append("TRIGGER_NOT_MET")
    if proposal.next_state.pending_intent_ids:
        reasons.append("EXIT_PENDING")
    forecast=proposal_forecast(proposal,context)
    if forecast is None:
        reasons.append("EVIDENCE_IDENTITY_MISMATCH")
    else:
        expected_range = (proposal.budget.frozen_range if proposal.budget and proposal.budget.frozen_range
                          else proposal.next_state.setup.frozen_range if proposal.next_state.setup
                          and proposal.next_state.setup.frozen_range is not None
                          else context.regime.frozen_range)
        if (forecast.contract_id!=features.contract_id or forecast.snapshot_id!=features.snapshot_id
                or forecast.product!=features.product or forecast.session_kind!=features.session_kind
                or forecast.session_id!=features.session_id or forecast.trading_day!=features.trading_day
                or forecast.duration_seconds!=features.duration_seconds
                or forecast.assessment_id!=context.regime.assessment_id
                or forecast.decision_nano!=now or forecast.base_regime!=context.regime.base
                or forecast.reference_price!=features.reference_price or forecast.atr_points!=features.atr_points
                or forecast.frozen_range!=expected_range or proposal.assessment_id!=forecast.assessment_id):
            reasons.append("EVIDENCE_IDENTITY_MISMATCH")
        if (forecast.barrier_key!="ATR_1" or forecast.upper_distance!=features.atr_points
                or forecast.lower_distance!=features.atr_points):
            reasons.append("BARRIER_MISMATCH")
        if forecast.quality.status!="READY":
            reasons.append("DATA_INVALID")
        if forecast.published_at_nano>=forecast.window_start_nano or "FORECAST_LATE" in forecast.quality.reasons:
            reasons.append("FORECAST_LATE")
        if forecast.published_at_nano > checked_at_nano:
            reasons.append("EVIDENCE_IDENTITY_MISMATCH")
        if checked_at_nano>=forecast.expires_at_nano:
            reasons.append("MODEL_EXPIRED")
        if forecast.out_of_distribution:
            reasons.append("OOD")
        if forecast.path_probabilities["AMBIGUOUS"]>cp.maximum_ambiguity:
            reasons.append("AMBIGUITY_ABOVE_CAP")
        if forecast.window_end_nano>context.holding_deadline_nano or checked_at_nano>=context.holding_deadline_nano \
                or checked_at_nano>=proposal.valid_until_nano \
                or proposal.earliest_execution_nano<forecast.window_start_nano \
                or proposal.valid_until_nano>forecast.window_end_nano:
            reasons.append("HOLDING_WINDOW_TOO_SHORT")
        if proposal.action=="OPEN" and forecast.horizon!=spec.max_holding:
            reasons.append("EVIDENCE_IDENTITY_MISMATCH")
        if proposal.action=="ADD" and (proposal.next_state.active_exit_nano is None
                or forecast.window_end_nano>proposal.next_state.active_exit_nano):
            reasons.append("HOLDING_WINDOW_TOO_SHORT")
        side = proposal.budget.side if proposal.budget else proposal.next_state.direction
        persist=("ORDERED_RANGE" if spec.family in ("REVERSION","GRID","MARTINGALE")
                 else ("UP" if side==1 else "DOWN")+("_BREAKOUT" if spec.family=="BREAKOUT" else "_TREND"))
        probability=forecast.persistence_probabilities.get(persist)
        if probability is None or probability<cp.minimum_persistence:
            reasons.append("PERSISTENCE_BELOW_THRESHOLD")
        events=[("PATH",None),("PERSIST:"+persist,probability)]
        if spec.family in ("REVERSION","GRID","MARTINGALE"):
            events.append(("RETURN_BEFORE_BREAK",forecast.return_before_break_probability))
            terminals=[t for t in forecast.time_cdf if t.event=="RETURN_FIRST" and t.step==forecast.horizon.value]
            if not terminals or any(t.unknown_probability>cp.maximum_ambiguity for t in terminals):
                reasons.append("AMBIGUITY_ABOVE_CAP")
        if spec.family=="MARTINGALE":
            stop=forecast.stop_touch_probabilities.get("LONG" if side==1 else "SHORT")
            if stop is None or stop>cp.maximum_stop_touch:
                reasons.append("STOP_TOUCH_ABOVE_CAP")
            events.append(("STOP_TOUCH:"+("LONG" if side==1 else "SHORT"),stop))
        for event,value in events:
            geom_event=event.removeprefix("PERSIST:")
            if event.startswith("STOP_TOUCH:"):
                geom_event="STOP_TOUCH"
            geometry=calibration_geometry_key(features,forecast.frozen_range,geom_event)
            matches=[]
            for e in forecast.evidence:
                if e.event!=event:
                    continue
                identity=(e.model_id==forecast.model_id and e.policy_id==cp.policy_id
                    and e.product==features.product and e.session_kind==features.session_kind
                    and e.duration_seconds==features.duration_seconds and e.base_regime==context.regime.base
                    and e.horizon==forecast.horizon and e.label_version==forecast.label_version
                    and e.barrier_key==("ATR_1" if event=="PATH" else "COMMON")
                    and geometry is not None and e.geometry_key==geometry
                    and (e.bucket=="ALL" if event=="PATH" else (value is not None and e.bucket!="ALL"
                         and probability_in_bucket(value,e.bucket))))
                if identity:
                    matches.append(e)
            if not matches:
                reasons.append("EVIDENCE_IDENTITY_MISMATCH")
            for e in matches:
                evidence_ids.append(e.evidence_id)
                if e.training_end_nano>=e.calibration_end_nano or e.calibration_end_nano>=now:
                    reasons.append("EVIDENCE_IDENTITY_MISMATCH")
                if checked_at_nano>=e.valid_until_nano or e.status=="EXPIRED":
                    reasons.append("MODEL_EXPIRED")
                if e.status!="VALID" or e.calibration_error is None or e.calibration_error>cp.maximum_calibration_error:
                    reasons.append("CALIBRATION_FAILED")
                if e.sample_count<cp.minimum_samples:
                    reasons.append("INSUFFICIENT_SAMPLES")
                if e.effective_blocks<cp.minimum_blocks:
                    reasons.append("INSUFFICIENT_BLOCKS")
    rule=context.rule
    if rule is None or rule.evidence=="UNKNOWN" or any(x is None for x in
            (rule.open_fee,rule.close_fee,rule.close_today_fee,rule.long_margin_rate,rule.short_margin_rate)):
        reasons.append("COST_UNKNOWN")
    # Cost-rule validity is tested at the actual check time, not the observation
    # anchor (004 §4): a rule expired before commit must not fund new risk.
    elif not (rule.available_at_nano<=checked_at_nano
              and rule.effective_from_nano<=checked_at_nano<rule.effective_until_nano):
        reasons.append("COST_UNKNOWN")
    budget=proposal.budget
    if budget is None:
        reasons.append("ROUND_BUDGET_EXCEEDED")
    else:
        if proposal.stop_price!=budget.stop_price or proposal.target_price!=budget.target_price \
                or abs(proposal.target_lots)>budget.maximum_lots:
            reasons.append("ROUND_BUDGET_EXCEEDED")
        if budget.planned_loss_cny>budget.equity_at_start_cny*Decimal(str(policy["maximum_round_loss_ratio"])):
            reasons.append("ROUND_BUDGET_EXCEEDED")
        if budget.stress_loss_cny>budget.equity_at_start_cny*Decimal(str(policy["maximum_stress_loss_to_equity"])):
            reasons.append("TAIL_LOSS_LIMIT")
        if rule is not None:
            try:
                paths=simulate_stress_paths(budget,rule,())
                if max(p.loss_cny for p in paths)>budget.stress_loss_cny:
                    reasons.append("ROUND_BUDGET_EXCEEDED")
                if any(p.margin_shortfall_cny>0 for p in paths):
                    reasons.append("MARGIN_LIMIT")
            except ValueError:
                reasons.append("COST_UNKNOWN")
    if context.run_policy.purpose=="SHADOW_OPERATIONAL":
        q=context.qualification
        if q is None or q.statistics!="PASS":
            reasons.append("STATISTICS_NOT_PASSED")
        if q is not None:
            if not q.issued_at_nano<=checked_at_nano<q.valid_until_nano:
                reasons.append("QUALIFICATION_EXPIRED")
            if (q.strategy_id!=spec.strategy_id or q.strategy_version!=spec.version
                    or q.parameter_version!=spec.parameter_version or q.code_sha256!=spec.code_sha256
                    or q.product!=features.product or proposal.contract_id not in q.contract_ids
                    or q.mode!=spec.mode or forecast is None or forecast.horizon not in q.horizons
                    or q.macro_mode!=context.run_policy.macro_mode
                    or q.research_policy_version!=context.run_policy.research_policy_version
                    or q.macro_policy_version!=context.run_policy.macro_policy_version
                    or context.run_policy.cost_scenario not in q.cost_scenarios
                    or dict(q.model_hashes)!=dict(context.model_hashes)):
                reasons.append("QUALIFICATION_MISMATCH")
    return reasons, evidence_ids, False


def evaluate_gate(proposal: StrategyProposal, context: StrategyContext,
                  policy: Mapping[str, JsonValue], *, checked_at_nano: int) -> GateDecision:
    """Joint gate over ALL conditions (E-P1 RULING 3.1.4: composition unchanged).

    The non-payoff evidence facts come from the shared pure computation above;
    this function still combines them with the complete payoff evidence block,
    so identical inputs keep producing the identical full reason set, eligible
    flag and evidence ids as before the extraction.
    """
    if type(checked_at_nano) is not int or checked_at_nano < context.now_nano:
        raise ValueError("gate_clock_invalid")
    reasons, evidence_ids, paywalled = non_payoff_evidence_reasons(
        proposal, context, policy, checked_at_nano=checked_at_nano)
    if paywalled:
        return GateDecision(proposal.proposal_id,not reasons,tuple(sorted(set(reasons))),
                            tuple(sorted(set(evidence_ids))),checked_at_nano)
    cp = confidence_policy(context.spec.family)
    features = context.features
    forecast=proposal_forecast(proposal,context)
    budget=proposal.budget
    now = context.now_nano
    payoff=proposal.payoff
    if payoff is None:
        reasons.append("INSUFFICIENT_SAMPLES")
    else:
        bound_payoff = context.model_hashes["payoff"] if "payoff" in context.model_hashes else None
        expected_geometry=calibration_geometry_key(features,forecast.frozen_range if forecast else None,payoff_event(proposal,context))
        if (payoff.proposal_id!=proposal.proposal_id or forecast is None or payoff.horizon!=forecast.horizon
                or payoff.barrier_key!="ATR_1" or payoff.geometry_key!=expected_geometry
                or payoff.training_end_nano>=now
                or bound_payoff is None or payoff.model_id!="payoff:"+bound_payoff):
            reasons.append("EVIDENCE_IDENTITY_MISMATCH")
        if payoff.missing_probability_mass>0:
            reasons.append("UNSUPPORTED_PATH_MASS")
        if payoff.sample_count<cp.minimum_samples or payoff.status!="READY":
            reasons.append("INSUFFICIENT_SAMPLES")
        if payoff.effective_blocks<cp.minimum_blocks:
            reasons.append("INSUFFICIENT_BLOCKS")
        if payoff.conservative_ev_cny is None or payoff.conservative_ev_cny<=cp.minimum_conservative_ev_cny:
            reasons.append("NON_POSITIVE_EV")
        if payoff.loss_cvar95_cny is None or budget is None or payoff.loss_cvar95_cny>budget.planned_loss_cny:
            reasons.append("TAIL_LOSS_LIMIT")
    return GateDecision(proposal.proposal_id,not reasons,tuple(sorted(set(reasons))),
                        tuple(sorted(set(evidence_ids))),checked_at_nano)
