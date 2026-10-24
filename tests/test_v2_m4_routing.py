from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D
from hashlib import sha256

import pytest

from ak_trading.v2.contracts import GateDecision
from ak_trading.v2.decision.routing import choose_proposal
from ak_trading.v2.decision.ledger import record_hash
from test_v2_m4_gates import example
from test_v2_m4_ledger import account, fill, ledger, NS, rule, run_policy


def test_sampling_exact_hash_weights_order_and_rejection():
    p,c=example()
    p2=replace(p,proposal_id="second",strategy_id="other",payoff=replace(p.payoff,proposal_id="second",conservative_ev_cny=D(30)))
    rejected=replace(p,proposal_id="bad",strategy_id="bad")
    gates=(GateDecision(p.proposal_id,True,(),(),c.now_nano),GateDecision("second",True,(),(),c.now_nano),
           GateDecision("bad",False,("TAIL_LOSS_LIMIT",),(),c.now_nano))
    args=(c.account,"opportunity",c.now_nano,run_policy("PROBABILITY_SAMPLE"),())
    a=choose_proposal((p,p2,rejected),gates,*args)
    b=choose_proposal((rejected,p2,p),tuple(reversed(gates)),*args)
    assert a==b and record_hash(a)==record_hash(b)
    assert a.weights[p.proposal_id]==.225 and a.weights["second"]==.675 and a.weights["bad"]==0 and a.weights["CASH"]==.1
    digest=sha256(b"7|opportunity|research-policy-v2.1.0|PROBABILITY_SAMPLE").digest()
    assert a.random_u==int.from_bytes(digest[:8],"big")/2**64


@pytest.mark.parametrize("mode",["BEST_EV","PROBABILITY_SAMPLE","FIXED","CASH"])
def test_all_policies_share_rejected_gate(mode):
    p,c=example("MARTINGALE")
    policy=run_policy(mode)
    if mode=="FIXED": policy=replace(policy,fixed_strategy_id=p.strategy_id)
    decision=choose_proposal((p,),(GateDecision(p.proposal_id,False,("STOP_TOUCH_ABOVE_CAP",),(),c.now_nano),),
        c.account,"opportunity",c.now_nano,policy,())
    assert decision.selected_proposal_id is None and decision.weights[p.proposal_id]==0 and decision.weights["CASH"]==1.


def test_best_ev_tie_and_different_horizon_refused():
    p,c=example()
    p2=replace(p,proposal_id="second",strategy_id="aaa")
    gates=tuple(GateDecision(x.proposal_id,True,(),(),c.now_nano) for x in (p,p2))
    a=choose_proposal((p,p2),gates,c.account,"opp",c.now_nano,run_policy(),())
    assert a.selected_proposal_id=="second"
    from ak_trading.v2.contracts import Horizon
    p2=replace(p2,payoff=replace(p2.payoff,horizon=Horizon("TRADING_DAY",20)))
    with pytest.raises(ValueError,match="different_horizons"):
        choose_proposal((p,p2),gates,c.account,"opp",c.now_nano,run_policy(),())


def test_cash_still_exits_owned_episode_and_other_strategy_cannot_take_over():
    p,c=example()
    book=ledger("100000")
    book.apply_fill(fill("entry",NS,"100"),rule())
    a=book.snapshot(c.now_nano)
    exitp=replace(p,action="EXIT",target_lots=0)
    other=replace(p,strategy_id="other",proposal_id="other",episode_id="other-episode")
    gates=tuple(GateDecision(x.proposal_id,True,(),(),c.now_nano) for x in (exitp,other))
    d=choose_proposal((exitp,other),gates,a,"opp",c.now_nano,run_policy("CASH"),())
    assert d.selected_proposal_id==exitp.proposal_id and d.outcome=="MANAGE_POSITION" and d.weights["other"]==0
