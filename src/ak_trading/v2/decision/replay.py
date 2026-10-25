"""Offline event scheduler and reproducible decision-run artifacts."""
from __future__ import annotations

from dataclasses import dataclass, replace, fields
from decimal import Decimal
from pathlib import Path
from typing import Literal, Mapping
import csv
import io

from ak_trading.v2.artifacts import canonical_json_bytes, sha256_hex, stable_id, publish_bundle, verify_qualification_evidence
from ak_trading.v2.contracts import (
    AccountSnapshot, DecisionRecord, ExecutionResult, FeatureSnapshot, Fill, FrozenInput,
    GateDecision, JsonValue, LedgerCheckpoint, MarketForecast, OrderIntent, RegimeAssessment,
    RunPolicy, StrategyProposal, StrategyQualification, StrategySpec, StrategyState,
)
from ak_trading.v2.data import visible_bars, visible_rule
from ak_trading.v2.probability.models import ForecastModelSet
from ak_trading.v2.probability.regimes import RegimeReference
from ak_trading.v2.schemas import encode_record, encode_record_file, encode_csv_bytes
from ak_trading.v2.seasonality import SeasonalityModel
from ak_trading.v2.strategies.registry import strategy_registry_v2
from ak_trading.v2.strategies.state import apply_execution, commit_observation, commit_selected_proposal
from ak_trading.v2.decision.execution import (
    capacity_before, position_for, simulate_execution, simulate_protective_exit,
)
from ak_trading.v2.decision.gates import evaluate_gate
from ak_trading.v2.decision.ledger import ContinuousLedger, record_hash
from ak_trading.v2.decision.payoffs import PathPayoffModel, estimate_proposal_payoff, observation_payload
from ak_trading.v2.decision.pipeline import build_strategy_context, model_hashes_for, registry_hash_for
from ak_trading.v2.probability.forecast import predict_market
from ak_trading.v2.decision.risk import enforce_account_risk
from ak_trading.v2.decision.routing import choose_proposal, opportunity_id

NS = 60_000_000_000


@dataclass(frozen=True)
class ReplayResult:
    features: tuple[FeatureSnapshot, ...]
    forecasts: tuple[MarketForecast, ...]
    regimes: tuple[RegimeAssessment, ...]
    proposals: tuple[StrategyProposal, ...]
    decisions: tuple[DecisionRecord, ...]
    intents: tuple[OrderIntent, ...]
    fills: tuple[Fill, ...]
    equity: tuple[AccountSnapshot, ...]
    checkpoints: tuple[LedgerCheckpoint, ...]
    execution_results: tuple[ExecutionResult, ...]
    reconciliation: Mapping[str, Decimal | bool | str]
    status: Literal["COMPLETE", "INCOMPLETE", "FAILED"]


def intents_for_proposal(proposal: StrategyProposal, decision_id: str,
                         account: AccountSnapshot, rule) -> tuple[OrderIntent, ...]:
    """Signed targets become explicit open or FIFO today/yesterday close legs."""
    position=position_for(account,proposal.contract_id)
    current=0 if position is None else position.lots
    delta=proposal.target_lots-current
    if not delta:
        return ()
    if proposal.action in ("OPEN","ADD"):
        if rule is None or proposal.budget is None or proposal.layer_index is None:
            raise ValueError("opening_intent_requires_rule_and_frozen_layer")
        layer=proposal.budget.layers[proposal.layer_index]
        side=proposal.budget.side
        signed_layer=side*layer.lots
        # Signed-target hard boundary (01 §4.1): an entry intent is exactly one
        # frozen layer in the budget's direction; zero, reversed or repeated
        # targets never become orders even if a gate was bypassed. F14: an ADD
        # must also strictly grow the owned position along the budget's
        # direction (|target|>|current|, both signs equal to the budget side) —
        # a reducing or reversed target can never emit an OPEN-offset order.
        if delta!=signed_layer or (proposal.action=="OPEN" and current!=0) or (
                proposal.action=="ADD" and (abs(proposal.target_lots)<=abs(current)
                                            or proposal.target_lots*side<=0 or current*side<=0)):
            raise ValueError("opening_intent_target_invalid")
        limit=layer.reference_entry_price+side*(Decimal(".1")*proposal.budget.initial_atr_points
              +rule.price_tick*(rule.slippage_ticks+rule.impact_ticks))
        # Adverse rounding never expands a budget.
        from decimal import ROUND_FLOOR, ROUND_CEILING
        limit=(limit/rule.price_tick).to_integral_value(rounding=ROUND_FLOOR if side==1 else ROUND_CEILING)*rule.price_tick
        pieces=(("OPEN",delta,limit),)
        purpose="ENTRY" if proposal.action=="OPEN" else "ADD"
    else:
        if position is None or position.lots*delta>=0 or abs(delta)>abs(current):
            raise ValueError("closing_intent_cannot_reverse")
        today=sum(l.lots for l in account.lots if l.contract_id==proposal.contract_id
                  and l.opened_trading_day==account.trading_day)
        old=abs(current)-today
        quantity=abs(delta)
        sign=1 if delta>0 else -1
        old_close=min(quantity,old)
        pieces=tuple((offset,sign*q,None) for offset,q in
                     (("CLOSE",old_close),("CLOSE_TODAY",quantity-old_close)) if q)
        # 007 §5.4 purpose routing: NEXT_WINDOW_CLOSE exits are plain REDUCE;
        # only the legacy pre-registered S0/T0 (and lifecycle roll) keep their
        # protective/roll purposes, matched on exact frozen reason tokens —
        # never a free substring sniff over arbitrary reasons.
        purpose="REDUCE" if proposal.action=="REDUCE" else "TIME_EXIT"
        exact_purpose={"STOP_TOUCHED":"STOP","TARGET_REACHED":"TARGET","ROLL_EXIT":"ROLL_EXIT"}
        for token,purpose_override in exact_purpose.items():
            if token in proposal.reasons:
                purpose=purpose_override
                break
    expires=proposal.valid_until_nano
    if proposal.action in ("REDUCE","EXIT") and rule is not None:
        # An owed protective exit has no forecast TTL. Keep it live within the
        # already published rule interval; a new rule still revalidates execution.
        expires=max(expires,rule.effective_until_nano)
    return tuple(OrderIntent(stable_id("intent",canonical_json_bytes([decision_id,proposal.proposal_id,offset,d])),
        decision_id,proposal.strategy_id,proposal.episode_id,proposal.contract_id,proposal.decision_nano,
        proposal.earliest_execution_nano,expires,d,offset,purpose,proposal.layer_index,
        limit) for offset,d,limit in pieces)


def verify_restore_sources(checkpoint: LedgerCheckpoint, bundle: FrozenInput) -> None:
    """Verify every recoverable original reference before any restore (003 §3/§4).

    Valuation-mark source bars must resolve inside the frozen input to the
    exact canonical content hash; a repackaged checkpoint_hash over a forged
    mark is not a recovery fact. Original Fill/Intent entities are verified by
    verify_committed_prefix when hosts hold them; missing originals are FAILED
    by that host, never silently accepted here.
    """
    bars_by_id={b.bar_id:b for b in bundle.bars}
    for mark in checkpoint.valuation_marks:
        if mark.source_bar_id is None:
            continue
        source=bars_by_id.get(mark.source_bar_id)
        if source is None or record_hash(source)!=mark.source_bar_sha256:
            raise ValueError("restore_source_bar_verification_failed")


def verify_committed_prefix(checkpoint: LedgerCheckpoint, bars: tuple, fills: tuple,
                            intents: tuple) -> None:
    """Full committed-prefix entity verification for hosts holding originals.

    Forward and reverse foreign keys are both enforced (003 §4): every
    processed_fill_hashes entry must resolve to the supplied original Fill
    (missing originals are FAILED, not accepted), each Fill must resolve to
    its original OrderIntent with matching contract/strategy/episode/offset,
    and every Fill.rule_id must resolve to an actually consumed rule in
    checkpoint.rule_hashes. Supplied fills must hash to their booked digests.
    M5 owes the same verification for its shadow prefix before calling restore.
    """
    verify_restore_sources(checkpoint, FrozenInput(Path("VERIFIED"), "0"*64, (), (), tuple(bars), (), ()))
    by_fill={f.fill_id:f for f in fills}
    if len(by_fill)!=len(fills):
        raise ValueError("committed_prefix_duplicate_fill_ids")
    by_intent={i.intent_id:i for i in intents}
    if len(by_intent)!=len(intents):
        raise ValueError("committed_prefix_duplicate_intent_ids")
    for fill_id in checkpoint.processed_fill_hashes:
        if fill_id not in by_fill:
            raise ValueError("committed_prefix_missing_original_fill")
    for fill in fills:
        digest=record_hash(fill)
        recorded=checkpoint.processed_fill_hashes.get(fill.fill_id)
        if recorded is None or recorded!=digest:
            raise ValueError("committed_fill_hash_mismatch")
        intent=by_intent.get(fill.intent_id)
        if intent is None:
            raise ValueError("committed_fill_without_intent")
        if (intent.contract_id!=fill.contract_id or intent.strategy_id!=fill.strategy_id
                or intent.episode_id!=fill.episode_id or intent.offset!=fill.offset):
            raise ValueError("committed_fill_intent_owner_mismatch")
        if fill.rule_id not in checkpoint.rule_hashes:
            raise ValueError("committed_fill_rule_not_consumed")


def _hard_exit(state: StrategyState, account: AccountSnapshot, now: int,
               next_open: int, reason: str) -> StrategyProposal:
    position=position_for(account,state.contract_id)
    if position is None or state.budget is None:
        raise ValueError("risk_exit_requires_position_and_budget")
    nxt=replace(state,phase="EXITING",pending_layer_index=None,pending_exit_nano=None)
    return StrategyProposal(stable_id("hard_exit",canonical_json_bytes([state.episode_id,now,reason])),
        state.strategy_id,state.episode_id,state.contract_id,now,"EXIT",0,None,None,
        state.budget.target_price,state.budget.stop_price,next_open,next_open+NS,
        None,state.last_regime.assessment_id if state.last_regime else "risk-timer",state.budget,None,nxt,(reason,))


def apply_receipt(ledger: ContinuousLedger, state: StrategyState, intent: OrderIntent,
                  result: ExecutionResult, remaining_before: int,
                  rules, now_nano: int) -> tuple[StrategyState, tuple[Fill, ...]]:
    """Validate a whole raw (possibly cumulative) receipt before committing.

    Its caller owns the event boundary and original pending-intent mapping.
    This function leaves the ledger untouched if any Fill or state fails.
    """
    if result.intent_id != intent.intent_id:
        raise ValueError("receipt_intent_mismatch")
    batch = {}
    for fill in result.fills:
        digest = record_hash(fill)
        previous = batch.get(fill.fill_id, ledger._fill_hashes.get(fill.fill_id))
        if previous is not None and previous != digest:
            raise ValueError("conflicting_duplicate_fill")
        batch[fill.fill_id] = digest
        if fill.intent_id != intent.intent_id or fill.contract_id!=intent.contract_id \
                or fill.strategy_id!=intent.strategy_id or fill.episode_id!=intent.episode_id \
                or fill.offset!=intent.offset or fill.side*intent.delta_lots<=0:
            raise ValueError("receipt_fill_intent_mismatch")
    candidate = ledger._fork()
    new = []
    already = set(ledger._fill_hashes)
    for fill in result.fills:
        from ak_trading.v2.decision.execution import rule_at
        if fill.fill_id in already:
            continue  # idempotent replay of an already-booked fill
        # A genuinely new fill must fall inside the originating intent's
        # execution window (05 §7.1): not before its earliest legal time and
        # not after expiry; already-booked history is never re-judged.
        if not (intent.not_before_nano <= fill.execution_nano < intent.expires_at_nano):
            raise ValueError("fill_outside_intent_execution_window")
        rule = rule_at(rules, fill.contract_id, fill.execution_nano)
        if candidate.apply_fill(fill, rule):
            new.append(fill)
    expected = remaining_before - sum(f.side * f.lots for f in new)
    if expected != result.remaining_lots or expected * intent.delta_lots < 0:
        raise ValueError("receipt_remaining_mismatch")
    if result.status == "FILLED" and expected != 0:
        raise ValueError("filled_receipt_has_remaining")
    if result.status in ("PARTIAL", "BLOCKED") and expected == 0:
        raise ValueError("pending_receipt_without_remaining")
    increment = replace(result, fills=tuple(new))
    if not new and result.status in ("FILLED", "PARTIAL"):
        # Cumulative repeated fills contain no new state transition.
        updated = state
    else:
        updated = apply_execution(state, increment, candidate.snapshot(now_nano), now_nano)
    ledger.__dict__.update(candidate.__dict__)
    return updated, tuple(new)


def run_decision_replay(bundle: FrozenInput, forecast_models: ForecastModelSet,
                        payoff_model: PathPayoffModel, specs: tuple[StrategySpec, ...],
                        initial_account: AccountSnapshot, run_policy: RunPolicy,
                        qualifications: tuple[StrategyQualification, ...],
                        qualification_evidence: Mapping[str, bytes], policy: Mapping[str, JsonValue],
                        seasonality_model: SeasonalityModel | None, regime_reference: RegimeReference,
                        contract_lock_sha256: str, initial_checkpoint: LedgerCheckpoint | None,
                        replay_start_nano: int, replay_end_nano: int, *,
                        exit_specs: tuple, exit_bindings: tuple) -> ReplayResult:
    # Each invocation is one explicitly declared offline experiment.
    if run_policy.purpose not in ("RESEARCH_EVALUATION", "SHADOW_OPERATIONAL"):
        raise ValueError("unsupported_run_purpose")
    if replay_start_nano>=replay_end_nano:
        raise ValueError("replay_interval_must_be_nonempty_half_open")
    registry=registry_hash_for(specs)
    from ak_trading.v2.decision.pipeline import exit_registry_hash_for
    exit_registry=exit_registry_hash_for(exit_specs)
    # Duplicate bindings for one strategy must fail here, before the dict below
    # can silently fold them; the exact-spec-set check only catches missing or
    # extra strategies (007 §5.4).
    binding_owners=[b.strategy_id for b in exit_bindings]
    if len(binding_owners)!=len(set(binding_owners)):
        raise ValueError("duplicate_exit_bindings")
    bindings_by_strategy=dict(zip(binding_owners,exit_bindings))
    if set(bindings_by_strategy)!={s.strategy_id for s in specs}:
        raise ValueError("exit_bindings_must_match_specs_exactly")
    hashes=model_hashes_for(forecast_models,payoff_model,regime_reference,seasonality_model)
    qhashes={q.qualification_id:record_hash(q) for q in qualifications}
    if initial_checkpoint is not None:
        if initial_account!=initial_checkpoint.account:
            raise ValueError("initial_account_checkpoint_mismatch")
        verify_restore_sources(initial_checkpoint,bundle)
        ledger=ContinuousLedger.restore(initial_checkpoint,bundle.instruments,bundle.rules,run_policy,
            contract_lock_sha256,registry,hashes,qhashes,exit_registry_sha256=exit_registry)
        states={(s.contract_id,s.strategy_id):s for s in initial_checkpoint.states}
        exit_execution_links=list(initial_checkpoint.exit_execution_links)
        pending={i.intent_id:i for i in initial_checkpoint.pending_intents}
        pending_remaining=dict(initial_checkpoint.pending_remaining_lots)
        processed=set(initial_checkpoint.processed_event_ids)
    else:
        if initial_account.lots:
            raise ValueError("initial_positions_require_checkpoint")
        ledger=ContinuousLedger(initial_account,bundle.instruments,run_policy,contract_lock_sha256,registry,
                                 exit_registry_sha256=exit_registry)
        states={}
        exit_execution_links=[]
        pending={}
        pending_remaining={}
        processed=set()
    strategies={}
    for mode in sorted({s.mode for s in specs}):
        strategies.update(strategy_registry_v2(mode))
    for spec in specs:
        if spec.strategy_id not in strategies:
            raise ValueError("unknown_strategy_spec")
        strategies[spec.strategy_id]=type(strategies[spec.strategy_id])(spec)
    invalid_q={}
    for q in qualifications:
        matching=[s for s in specs if s.strategy_id==q.strategy_id]
        try:
            if len(matching)!=1:
                raise ValueError("qualification_spec_missing")
            # 007 §5.4: evidence is verified against this run's own frozen exit
            # registry and the unique binding of the matching strategy; the
            # qualification's own recorded hashes never substitute as trusted
            # dependencies here.
            verify_qualification_evidence(q,matching[0],qualification_evidence,contract_lock_sha256,registry,
                exit_registry_sha256=exit_registry,
                exit_binding=bindings_by_strategy[q.strategy_id])
        except (ValueError,TypeError):
            required=(q.report_sha256,q.review_sha256,q.experiment_sha256,contract_lock_sha256,registry)
            invalid_q[q.qualification_id]=("QUALIFICATION_EVIDENCE_MISSING" if any(k not in qualification_evidence for k in required)
                                            else "QUALIFICATION_EVIDENCE_INVALID")
    by_contract={i.contract_id:i for i in bundle.instruments}
    events={}
    # Timers are present even when a market bar is missing.
    for session in bundle.sessions:
        for t in range(session.start_nano,session.end_nano+1,NS):
            if replay_start_nano<=t<replay_end_nano:
                events.setdefault(t,{"open":[],"visible":[]})
    for b in bundle.bars:
        if b.duration_seconds not in (60,86400):
            continue
        if replay_start_nano<=b.start_nano<replay_end_nano:
            events.setdefault(b.start_nano,{"open":[],"visible":[]})["open"].append(b)
        at=b.available_at_nano
        if b.complete and at is not None and b.end_nano<=at and replay_start_nano<=at<replay_end_nano:
            events.setdefault(at,{"open":[],"visible":[]})["visible"].append(b)
    features=[]; forecasts=[]; regimes=[]; proposals=[]; decisions=[]; intents=[]
    fills=[]; equity=[]; checkpoints=[]; executions=[]
    cumulative_capacity=dict(initial_checkpoint.capacity_used) if initial_checkpoint else {}
    def receipt(intent, result, now):
        key=(intent.contract_id,intent.strategy_id)
        states[key], new_fills = apply_receipt(ledger, states[key], intent, result,
            pending_remaining[intent.intent_id], bundle.rules, now)
        fills.extend(new_fills)
        executions.append(result)
        if result.status in ("FILLED","CANCELLED","EXPIRED"):
            pending.pop(intent.intent_id,None)
            pending_remaining.pop(intent.intent_id,None)
        else:
            pending_remaining[intent.intent_id]=result.remaining_lots
    for now,event in sorted(events.items()):
        event_id=stable_id("replay_event",canonical_json_bytes([now,record_hash(run_policy),registry]))
        if event_id in processed:
            continue
        a=ledger.snapshot(now)
        # Expiration cancels only risk additions; owed exits are reissued after a receipt.
        for intent in sorted(tuple(pending.values()),key=lambda i:i.intent_id):
            # Once the trading day changes, close-today and close-yesterday
            # must be re-split from actual FIFO lots, not silently relabeled.
            next_day=next((b.trading_day for b in event["open"] if b.contract_id==intent.contract_id),None)
            stale_offset=(intent.offset=="CLOSE_TODAY" and next_day is not None
                          and next_day!=ledger.snapshot(now).trading_day)
            if now>=intent.expires_at_nano or stale_offset:
                receipt(intent,ExecutionResult(intent.intent_id,(),pending_remaining[intent.intent_id],"EXPIRED",("INTENT_EXPIRED",)),now)
        event_day=max((b.trading_day for b in event["open"] if b.trading_day is not None),default=a.trading_day)
        if event_day>a.trading_day:
            ledger.mark({},now,event_day,bundle.rules)
            a=ledger.snapshot(now)
        # Risk locks, active deadlines and contract lifecycle generate exits on clock events.
        for key,s in sorted(tuple(states.items())):
            position=position_for(a,s.contract_id)
            if position is None or s.budget is None:
                continue
            inst=by_contract[s.contract_id]
            deadlines=[x for x in (s.active_exit_nano,inst.delivery_cutoff_nano,inst.last_trade_nano) if x is not None]
            due=bool(deadlines and now>=min(deadlines))
            mark=ledger._marks.get(s.contract_id)
            stale=mark is None or mark.status!="CURRENT" or (
                next(spec for spec in specs if spec.strategy_id==s.strategy_id).mode=="MINUTE"
                and now-mark.as_of_nano>120_000_000_000)
            if not (a.killed or due or stale or s.phase=="EXITING"):
                continue
            for intent in tuple(pending.values()):
                if intent.episode_id==s.episode_id and intent.offset=="OPEN":
                    receipt(intent,ExecutionResult(intent.intent_id,(),pending_remaining[intent.intent_id],"CANCELLED",("RISK_CANCEL",)),now)
            s=states[key]
            if any(i.episode_id==s.episode_id and i.offset!="OPEN" for i in pending.values()):
                continue
            starts=[t for sess in bundle.sessions if sess.product==inst.product
                    for t in range(sess.start_nano,sess.end_nano,NS) if t>now]
            if not starts:
                states[key]=replace(s,phase="EXITING")
                continue
            p=_hard_exit(s,a,now,min(starts),"ROLL_EXIT" if inst.delivery_cutoff_nano is not None
                and now>=inst.delivery_cutoff_nano else "TIME_EXIT")
            # A published intent must resolve to a published decision (05 §8):
            # the risk-timer exit gets its own real DecisionRecord, never a
            # proposal_id posing as a decision_id.
            exit_horizon=next(spec for spec in specs if spec.strategy_id==s.strategy_id).max_holding
            timer=DecisionRecord(stable_id("risk_timer_decision",canonical_json_bytes(
                [s.episode_id,now,p.proposal_id,record_hash(a),record_hash(run_policy)])),
                opportunity_id(s.contract_id,now,exit_horizon,a),
                now,run_policy.research_policy_version,record_hash(run_policy),run_policy.purpose,(),
                run_policy.routing_mode,(p,),(GateDecision(p.proposal_id,True,(),(),now),),
                {p.proposal_id:1.0,"CASH":0.0},run_policy.seed,None,p.proposal_id,"MANAGE_POSITION",
                ("RISK_TIMER_EXIT",),record_hash(a))
            decisions.append(timer)
            new=intents_for_proposal(p,timer.decision_id,a,visible_rule(bundle,s.contract_id,now))
            states[key]=commit_selected_proposal(s,p,new)
            proposals.append(p); intents.extend(new)
            pending.update({i.intent_id:i for i in new})
            pending_remaining.update({i.intent_id:i.delta_lots for i in new})
        for intent in sorted(tuple(pending.values()),key=lambda i:i.intent_id):
            if intent.offset!="OPEN":
                continue
            qualification=next((q for q in qualifications if q.strategy_id==intent.strategy_id
                               and intent.contract_id in q.contract_ids),None)
            invalid=now>=forecast_models.valid_until_nano
            if run_policy.purpose=="SHADOW_OPERATIONAL":
                invalid|=(qualification is None or qualification.qualification_id in invalid_q
                          or not qualification.issued_at_nano<=now<qualification.valid_until_nano)
            if invalid:
                receipt(intent,ExecutionResult(intent.intent_id,(),pending_remaining[intent.intent_id],"CANCELLED",
                                                ("MODEL_EXPIRED",)),now)
        used={}
        # Only the execution module sees a not-yet-complete execution bar.
        for b in sorted(event["open"],key=lambda b:(b.contract_id,b.duration_seconds,b.bar_id)):
            matching=sorted((i for i in pending.values() if i.contract_id==b.contract_id
                             and next(s for s in specs if s.strategy_id==i.strategy_id).mode==(
                                 "MINUTE" if b.duration_seconds==60 else "DAILY")),
                            key=lambda i:(i.offset=="OPEN",i.created_at_nano,i.intent_id))
            if not matching:
                continue
            rule=visible_rule(bundle,b.contract_id,now)
            capacity_key=f"{b.contract_id}|{now}|{run_policy.cost_scenario}"
            previous=visible_bars(bundle,b.contract_id,b.duration_seconds,now,False)
            cap=capacity_before(previous,b.contract_id,b.duration_seconds,now,rule)
            remaining=max(0,(cap or 0)-cumulative_capacity.get(capacity_key,0))
            for intent in matching:
                if intent.not_before_nano>now or intent.created_at_nano>=now:
                    continue
                position=position_for(ledger.snapshot(now),b.contract_id)
                if rule is None:
                    result=ExecutionResult(intent.intent_id,(),pending_remaining[intent.intent_id],"BLOCKED",("COST_UNKNOWN","EXIT_PENDING"))
                else:
                    execution_intent=replace(intent,delta_lots=pending_remaining[intent.intent_id])
                    state=states.get((b.contract_id,intent.strategy_id))
                    budget=None if state is None else state.budget
                    # 007 §5.4: an ExitExecutionLink's execution_kind is the
                    # routing authority when present — NEXT_WINDOW_CLOSE goes
                    # through the plain next-window close even if a legacy
                    # purpose leaked onto the intent.
                    link=None
                    for candidate in exit_execution_links:
                        if candidate.intent_id==intent.intent_id:
                            link=candidate
                            break
                    if link is not None and link.execution_kind=="NEXT_WINDOW_CLOSE":
                        result=simulate_execution(execution_intent,b,rule,remaining,position)
                    elif intent.purpose in ("STOP","TARGET") and position is not None and budget is not None:
                        # Pre-registered protective order: on a dual-barrier bar the
                        # frozen stop wins and the ambiguity cost is recorded (05 §7.1).
                        result=simulate_protective_exit(execution_intent,b,rule,remaining,position,budget)
                    else:
                        result=simulate_execution(execution_intent,b,rule,remaining,position)
                if result.status=="BLOCKED" and not result.fills and intent.offset=="OPEN":
                    # 003 §4: a transiently blocked entry keeps its intent and
                    # signed remainder; only expiry or an independent cancel
                    # event terminates the unfilled part. Record once, retry later.
                    executions.append(result)
                else:
                    receipt(intent,result,now)
                count=sum(f.lots for f in result.fills)
                remaining-=count
                used[capacity_key]=used.get(capacity_key,0)+count
                cumulative_capacity[capacity_key]=cumulative_capacity.get(capacity_key,0)+count
        current=ledger.snapshot(now)
        # Latest visible complete closes only; intraday daily bars never mark a minute account early.
        prices={}
        trading_day=current.trading_day
        for b in sorted(event["visible"],key=lambda b:(b.end_nano,b.contract_id,b.duration_seconds)):
            if b.close is not None and b.quality.status not in ("INVALID","MISSING"):
                prices[b.contract_id]=b
                if b.trading_day is not None:
                    trading_day=max(trading_day,b.trading_day)
        if prices or current.lots:
            ledger.mark(prices,now,trading_day,bundle.rules)
        # Observation submission for all strategies; resource submission only for selected candidates.
        activated=sorted({(b.contract_id,"MINUTE" if b.duration_seconds==60 else "DAILY") for b in event["visible"]})
        for contract,mode in activated:
            pending_owner=any(i.contract_id==contract for i in pending.values())
            other_held=any(l.contract_id!=contract and by_contract[l.contract_id].product==by_contract[contract].product
                           for l in ledger.snapshot(now).lots)
            # Existing episodes and pending original entries own this product until cleared.
            contexts={}
            batch=[]
            for spec in sorted((s for s in specs if s.mode==mode),key=lambda s:s.strategy_id):
                key=(contract,spec.strategy_id)
                context=build_strategy_context(bundle,contract,now,ledger.snapshot(now),states.get(key),
                    forecast_models,payoff_model,spec,seasonality_model,regime_reference,run_policy,qualifications,
                    exit_binding=bindings_by_strategy[spec.strategy_id],
                    forecast_provider=lambda models,request,regime: predict_market(models,request,regime,request.features.decision_nano))
                contexts[spec.strategy_id]=context
                if context.rule is not None:
                    ledger._consume_rule(context.rule)
                p=strategies[spec.strategy_id].evaluate(context,states.get(key))
                states[key]=commit_observation(states.get(key),p)
                features.append(context.features); forecasts.extend(context.forecasts); regimes.append(context.regime)
                batch.append(p)
            # Each complete horizon is a separate comparison opportunity.
            groups={}
            for p in batch:
                c=contexts[p.strategy_id]
                f=next((f for f in c.forecasts if f.forecast_id==p.forecast_id),None)
                horizon=f.horizon if f else c.spec.max_holding
                groups.setdefault((horizon.unit,horizon.value),[]).append((p,horizon))
            for _,members in sorted(groups.items()):
                before=ledger.snapshot(now)
                opp=opportunity_id(contract,now,members[0][1],before)
                attached=[]; gates=[]; qualifications_used=[]
                for p,_h in members:
                    c=replace(contexts[p.strategy_id],account=before)
                    if p.action in ("OPEN","ADD"):
                        p=replace(p,payoff=estimate_proposal_payoff(payoff_model,p,c,opp))
                    gate=evaluate_gate(p,c,policy,checked_at_nano=now)
                    if p.action in ("OPEN","ADD") and (pending_owner or other_held):
                        gate=replace(gate,eligible=False,reasons=tuple(sorted(set(gate.reasons)|{"EXIT_PENDING"})))
                    if c.qualification is not None:
                        qualifications_used.append(c.qualification.qualification_id)
                        if c.qualification.qualification_id in invalid_q and run_policy.purpose=="SHADOW_OPERATIONAL" and p.action in ("OPEN","ADD"):
                            gate=replace(gate,eligible=False,reasons=tuple(sorted(set(gate.reasons)|{"QUALIFICATION_MISMATCH",invalid_q[c.qualification.qualification_id]})))
                    attached.append(p); gates.append(gate)
                rule=visible_rule(bundle,contract,now)
                cap=capacity_before(contexts[attached[0].strategy_id].bars,contract,60 if mode=="MINUTE" else 86400,now,rule)
                capacity={} if cap is None else {contract:cap}
                risk_snapshot=ledger._fork().checkpoint(tuple(states.values()),tuple(pending.values()),hashes,qhashes,
                                                pending_remaining_lots=pending_remaining,
                                                exit_registry_sha256=exit_registry,
                                                exit_bindings=exit_bindings,exit_states=(),exit_execution_links=())
                for mark in risk_snapshot.valuation_marks:
                    if mark.rule_id is not None:
                        consumed=next(r for r in bundle.rules if r.rule_id==mark.rule_id)
                        if record_hash(consumed)!=risk_snapshot.rule_hashes[mark.rule_id]:
                            raise ValueError("valuation_consumed_rule_hash_mismatch")
                risked=enforce_account_risk(tuple(attached),tuple(gates),risk_snapshot.account,bundle.instruments,
                    risk_snapshot.states,risk_snapshot.pending_intents,bundle.rules,capacity,policy,
                    valuations=risk_snapshot.valuation_marks)
                decision=choose_proposal(tuple(attached),risked,before,opp,now,run_policy,tuple(qualifications_used))
                decisions.append(decision); proposals.extend(attached)
                if decision.selected_proposal_id is not None:
                    p=next(p for p in attached if p.proposal_id==decision.selected_proposal_id)
                    key=(contract,p.strategy_id)
                    if p.action in ("EXIT","REDUCE"):
                        for old in tuple(pending.values()):
                            if old.episode_id==p.episode_id and old.offset=="OPEN":
                                receipt(old,ExecutionResult(old.intent_id,(),pending_remaining[old.intent_id],"CANCELLED",("RISK_CANCEL",)),now)
                    if not any(i.episode_id==p.episode_id for i in pending.values()):
                        new=intents_for_proposal(p,decision.decision_id,ledger.snapshot(now),rule)
                        states[key]=commit_selected_proposal(states[key],p,new)
                        intents.extend(new); pending.update({i.intent_id:i for i in new})
                        pending_remaining.update({i.intent_id:i.delta_lots for i in new})
                ledger.sync_reservations(tuple(states.values()),bundle.rules,now)
        ledger.record_event(event_id,used)
        processed.add(event_id)
        equity.append(ledger.snapshot(now))
        checkpoints.append(ledger.checkpoint(tuple(states.values()),tuple(pending.values()),hashes,qhashes,
                                             pending_remaining_lots=pending_remaining,
                                             exit_registry_sha256=exit_registry,
                                             exit_bindings=exit_bindings,exit_states=(),exit_execution_links=()))
    reconciliation=ledger.reconcile()
    incomplete=bool(ledger.snapshot(max(replay_start_nano,replay_end_nano-1)).lots or pending
                    or reconciliation["status"]!="PASS")
    if incomplete:
        reconciliation={**reconciliation,"status":"INCOMPLETE"}
    # Deduplicate identical shared snapshots without changing publication order.
    def unique(rows, key):
        seen={}
        for row in rows:
            identity=getattr(row,key)
            if identity in seen and seen[identity]!=row:
                raise ValueError("conflicting_output_identity")
            seen[identity]=row
        return tuple(seen.values())
    return ReplayResult(unique(features,"snapshot_id"),unique(forecasts,"forecast_id"),unique(regimes,"assessment_id"),
        tuple(proposals),tuple(decisions),tuple(intents),tuple(fills),tuple(equity),tuple(checkpoints),tuple(executions),
        reconciliation,"INCOMPLETE" if incomplete else "COMPLETE")


def replay_payloads(result: ReplayResult, payoff_model: PathPayoffModel, *,
                    rules=()) -> Mapping[str, bytes]:
    """Exact M4 JSON/CSV schemas; no lossy projection is used for recovery."""
    payloads={}
    records={"decisions":("DecisionRecord",result.decisions),"intents":("OrderIntent",result.intents),
        "fills":("Fill",result.fills),"equity":("AccountSnapshot",result.equity),
        "execution_results":("ExecutionResult",result.execution_results),"checkpoints":("LedgerCheckpoint",result.checkpoints),
        "payoff_estimates":("PayoffEstimate",tuple(p.payoff for p in result.proposals if p.payoff is not None))}
    for name,(kind,rows) in records.items():
        version="2.1.2" if name=="checkpoints" else "2.1.0"
        payloads[name+".json"]=encode_record_file(kind,f"ak_probability_{name}_v{version}",rows)
    payloads["fills.csv"]=encode_csv_bytes(Fill,result.fills)
    def projection(columns,rows):
        output=io.StringIO(newline="")
        writer=csv.writer(output,lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            values=[]
            for k in columns:
                value=row[k]
                values.append("" if value is None else canonical_json_bytes(value).decode().strip()
                              if isinstance(value,(list,dict,bool)) else str(value))
            writer.writerow(values)
        return output.getvalue().encode("utf-8")
    columns="decision_id,opportunity_set_id,decision_nano,policy_version,mode,selected_proposal_id,outcome,weights,seed,random_u,reasons,account_before_hash".split(",")
    payloads["decisions.csv"]=projection(columns,[encode_record(r) for r in result.decisions])
    columns="account_id,as_of_nano,trading_day,initial_cash_cny,cash_cny,realized_gross_cny,fees_cny,slippage_cny,unrealized_cny,equity_cny,margin_cny,reserved_margin_cny,day_start_equity_cny,day_net_cny,peak_equity_cny,killed,status".split(",")
    payloads["equity.csv"]=projection(columns,[{**encode_record(r),"status":r.quality.status} for r in result.equity])
    model={"model_id":payoff_model.model_id,"training_end_nano":payoff_model.training_end_nano,
        "observations":[observation_payload(r) for r in payoff_model.observations],
        "policy_version":payoff_model.policy_version,"source_hashes":dict(payoff_model.source_hashes)}
    payloads["payoff_model.json"]=canonical_json_bytes({"schema_version":"ak_probability_payoff_model_v2.1.0","model":model})
    blocked={}
    for d in result.decisions:
        for g in d.gates:
            for reason in g.reasons:
                blocked[reason]=blocked.get(reason,0)+1
    positions={}
    if result.equity:
        for lot in result.equity[-1].lots:
            positions[lot.contract_id]=positions.get(lot.contract_id,0)+lot.side*lot.lots
    stress_losses={}
    shortfalls={}
    if rules:
        from ak_trading.v2.decision.execution import simulate_stress_paths,rule_at
        budgets={p.budget.budget_id:p.budget for p in result.proposals if p.budget is not None}
        for budget in budgets.values():
            rule=rule_at(rules,budget.contract_id,budget.frozen_at_nano)
            if rule is None:
                continue
            for path in simulate_stress_paths(budget,rule,()):
                key=budget.budget_id+"|"+path.scenario
                stress_losses[key]=str(path.loss_cny)
                shortfalls[key]=str(path.margin_shortfall_cny)
    cvars=[p.payoff.loss_cvar95_cny for p in result.proposals if p.payoff is not None and p.payoff.loss_cvar95_cny is not None]
    risk={"schema_version":"ak_probability_risk_report_v2.1.0","status":result.status,
        "maximum_inventory":max((sum(l.lots for l in a.lots) for a in result.equity),default=0),
        "peak_margin_cny":str(max((a.margin_cny for a in result.equity),default=Decimal(0))),
        "loss_cvar95_cny":str(max(cvars)) if cvars else None,"stress_losses":stress_losses,"margin_shortfalls":shortfalls,"blocked_counts":blocked,
        "open_positions":positions,"reconciliation":{k:str(v) if isinstance(v,Decimal) else v for k,v in result.reconciliation.items()}}
    payloads["risk_report.json"]=canonical_json_bytes(risk)
    return payloads
