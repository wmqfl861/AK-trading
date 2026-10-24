"""Four deterministic policies over one already risk-filtered opportunity."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from hashlib import sha256

from ak_trading.v2.artifacts import canonical_json_bytes, stable_id
from ak_trading.v2.contracts import AccountSnapshot, DecisionRecord, GateDecision, RunPolicy, StrategyProposal
from ak_trading.v2.decision.ledger import record_hash
from ak_trading.v2.schemas import encode_record


def opportunity_id(contract_id: str, decision_nano: int, horizon,
                   account: AccountSnapshot) -> str:
    return stable_id("opportunity",canonical_json_bytes([contract_id,decision_nano,
        encode_record(horizon),[encode_record(l) for l in account.lots if l.contract_id==contract_id],
        record_hash(account)]))


def choose_proposal(proposals: tuple[StrategyProposal, ...], gates: tuple[GateDecision, ...],
                    account: AccountSnapshot, opportunity_set_id: str, decision_nano: int,
                    run_policy: RunPolicy, qualification_ids: tuple[str, ...]) -> DecisionRecord:
    ordered=tuple(sorted(proposals,key=lambda p:(p.strategy_id,p.proposal_id)))
    gmap={g.proposal_id:g for g in gates}
    if len({p.proposal_id for p in ordered})!=len(ordered) or len(gmap)!=len(gates) \
            or set(gmap)!={p.proposal_id for p in ordered}:
        raise ValueError("proposal_gate_identity_join")
    if any(p.decision_nano!=decision_nano for p in ordered) or len({p.contract_id for p in ordered})>1:
        raise ValueError("different_opportunities_cannot_be_compared")
    horizons={(p.payoff.horizon.unit,p.payoff.horizon.value) for p in ordered
              if p.payoff is not None and p.action in ("OPEN","ADD")}
    if len(horizons)>1:
        raise ValueError("different_horizons_cannot_be_compared")
    owned={(l.strategy_id,l.episode_id) for l in account.lots
           if ordered and l.contract_id==ordered[0].contract_id}
    if len(owned)>1:
        raise ValueError("conflicting_episode_owners")
    for p in ordered:
        g=gmap[p.proposal_id]
        if owned and (p.strategy_id,p.episode_id) not in owned:
            gmap[p.proposal_id]=replace(g,eligible=False,reasons=tuple(sorted(set(g.reasons)|{"POSITION_OWNERSHIP_MISMATCH"})))
        elif p.action in ("OPEN","ADD") and (p.payoff is None or p.payoff.status!="READY"
                or p.payoff.conservative_ev_cny is None or p.payoff.conservative_ev_cny<=0):
            gmap[p.proposal_id]=replace(g,eligible=False,reasons=tuple(sorted(set(g.reasons)|{"NON_POSITIVE_EV"})))
    eligible=[p for p in ordered if gmap[p.proposal_id].eligible]
    management=[p for p in eligible if p.action in ("EXIT","REDUCE")]
    risk=[p for p in eligible if p.action in ("OPEN","ADD")]
    weights={p.proposal_id:0.0 for p in ordered}
    weights["CASH"]=1.0
    selected=None
    outcome="NO_TRADE"
    random_u=None
    reasons=[]
    if management:
        selected=min(management,key=lambda p:(0 if p.action=="EXIT" else 1,p.strategy_id,p.proposal_id))
        outcome="MANAGE_POSITION"
    elif owned:
        # An episode never changes owner or samples a new strategy every bar.
        if risk and run_policy.routing_mode!="CASH":
            selected=min(risk,key=lambda p:(p.strategy_id,p.proposal_id))
            outcome="MANAGE_POSITION"
    elif risk and run_policy.routing_mode=="BEST_EV":
        selected=min(risk,key=lambda p:(-p.payoff.conservative_ev_cny,p.strategy_id,p.proposal_id))
    elif risk and run_policy.routing_mode=="FIXED":
        fixed=[p for p in risk if p.strategy_id==run_policy.fixed_strategy_id]
        selected=min(fixed,key=lambda p:p.proposal_id) if fixed else None
    elif run_policy.routing_mode=="PROBABILITY_SAMPLE":
        digest=sha256(f"{run_policy.seed}|{opportunity_set_id}|{run_policy.research_policy_version}|{run_policy.routing_mode}".encode()).digest()
        random_u=int.from_bytes(digest[:8],"big")/2**64
        if risk:
            total=sum((p.payoff.conservative_ev_cny for p in risk),Decimal(0))
            weights["CASH"]=0.10
            cumulative=0.0
            for p in risk:
                weight=float(Decimal(".90")*p.payoff.conservative_ev_cny/total)
                weights[p.proposal_id]=weight
                cumulative+=weight
                if selected is None and random_u<cumulative:
                    selected=p
    if selected is not None:
        if outcome!="MANAGE_POSITION":
            outcome="SELECTED"
        if run_policy.routing_mode!="PROBABILITY_SAMPLE" or owned or management:
            weights["CASH"]=0.0
            weights[selected.proposal_id]=1.0
    else:
        reasons.append("NO_ELIGIBLE_PROPOSAL" if not risk else "CASH_SELECTED")
    final_gates=tuple(gmap[p.proposal_id] for p in ordered)
    identity=[opportunity_set_id,decision_nano,record_hash(run_policy),record_hash(account),
              [encode_record(p) for p in ordered],[encode_record(g) for g in final_gates],weights,random_u,
              sorted(qualification_ids)]
    return DecisionRecord(stable_id("decision",canonical_json_bytes(identity)),opportunity_set_id,
        decision_nano,run_policy.research_policy_version,record_hash(run_policy),run_policy.purpose,
        tuple(sorted(set(qualification_ids))),run_policy.routing_mode,ordered,final_gates,weights,
        run_policy.seed,random_u,selected.proposal_id if selected else None,outcome,tuple(reasons),record_hash(account))
