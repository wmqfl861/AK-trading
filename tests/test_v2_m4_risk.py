from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

from ak_trading.v2.contracts import GateDecision
from ak_trading.v2.policy import research_policy
from ak_trading.v2.decision.risk import enforce_account_risk
from ak_trading.v2.decision.routing import choose_proposal
from test_v2_m4_gates import example
from test_v2_m4_ledger import EXIT_REGISTRY,instrument, rule, state,LEGACY_BINDINGS


def check(proposals,c,states=(),capacity=None,account=None,instruments=None,rules=None):
    return enforce_account_risk(proposals,tuple(GateDecision(p.proposal_id,True,(),(),c.now_nano) for p in proposals),
        account or c.account,instruments or (instrument(),),states,(),rules or (rule(),),
        {"rb":3} if capacity is None else capacity,research_policy(),valuations=())


def test_concentration_gate_precedes_routing_and_zero_weight():
    p,c=example()
    p=replace(p,budget=replace(p.budget,reserved_margin_cny=D(11000)))
    g=check((p,),c)
    assert not g[0].eligible and "CONCENTRATION_LIMIT" in g[0].reasons
    d=choose_proposal((p,),g,c.account,"opp",c.now_nano,c.run_policy,())
    assert d.weights[p.proposal_id]==0 and d.selected_proposal_id is None


def test_mutually_exclusive_alternatives_do_not_sum_reserved_budget():
    p,c=example()
    p=replace(p,budget=replace(p.budget,reserved_margin_cny=D(9000)))
    alternative=replace(p,proposal_id="alternative",strategy_id="other",
        budget=replace(p.budget,budget_id="other-budget",episode_id="other-episode"),episode_id="other-episode")
    assert all(g.eligible for g in check((p,alternative),c))


def test_existing_committed_episode_is_not_counted_again_for_add():
    p,c=example()
    b=replace(p.budget,reserved_margin_cny=D(9000))
    p=replace(p,action="ADD",budget=b)
    a=replace(c.account,reserved_margin_cny=D(9000))
    assert check((p,),c,(state(b,(0,),"WAIT_TRIGGER"),),account=a)[0].eligible


def test_resource_second_contract_sees_first_reservation():
    p,c=example()
    first=replace(p.budget,reserved_margin_cny=D(9000))
    second=replace(p,proposal_id="second",contract_id="rb2",episode_id="second",
        budget=replace(first,budget_id="second",episode_id="second",contract_id="rb2"))
    a=replace(c.account,reserved_margin_cny=D(9000))
    g=check((second,),c,(state(first,(0,),"WAIT_TRIGGER"),),capacity={"rb2":3},account=a,
        instruments=(instrument(),replace(instrument(),contract_id="rb2")),rules=(rule(),rule("rb2")))
    # 18k gross in same strategy and product exceeds both 10% and 15%; no offset netting.
    assert not g[0].eligible and "CONCENTRATION_LIMIT" in g[0].reasons


def test_capacity_unknown_exhausted_kill_and_exit_independent():
    p,c=example()
    assert "CAPACITY_UNKNOWN" in check((p,),c,capacity={})[0].reasons
    assert "CAPACITY_EXHAUSTED" in check((p,),c,capacity={"rb":0})[0].reasons
    killed=replace(c.account,killed=True)
    assert not check((p,),c,account=killed)[0].eligible
    exitp=replace(p,action="EXIT",target_lots=0)
    assert check((exitp,),c,account=killed,capacity={})[0].eligible


def test_current_marks_compute_gross_790_then_680_without_entry_price_guess():
    from test_v2_m4_ledger import ledger,account as new_account,fill,bar,budget,NS,HASH,run_policy,MODELS,DAY
    from ak_trading.v2.decision.ledger import ContinuousLedger
    p,c=example()
    instruments=(instrument("a"),instrument("b"),instrument())
    rules=(rule("a"),rule("b",multiplier=D(5),short_margin_rate=D(".2")),rule())
    book=ContinuousLedger(new_account("100000"),instruments,run_policy(),HASH,HASH,exit_registry_sha256=EXIT_REGISTRY)
    book.apply_fill(fill("a",0,"100",lots=2,contract="a",episode="a"),rules[0])
    book.apply_fill(fill("b",0,"200",side=-1,lots=3,contract="b",episode="b"),rules[1])
    a=book.mark({"a":bar(t=0,price="110",contract="a"),"b":bar(t=0,price="190",contract="b")},NS,DAY,rules)
    assert (a.margin_cny,a.unrealized_cny)==(D(790),D(350))
    sa=replace(state(budget("a","a"),(1,1,0),"EXITING"),contract_id="a")
    sb=replace(state(budget("b","b",side=-1),(1,2,0),"EXITING"),contract_id="b")
    cp=book.checkpoint((sa,sb),(),MODELS,{},pending_remaining_lots={},exit_registry_sha256=EXIT_REGISTRY,
                           exit_bindings=LEGACY_BINDINGS,exit_states=(),exit_execution_links=())
    gates=(GateDecision(p.proposal_id,True,(),(),c.now_nano),)
    assert enforce_account_risk((p,),gates,a,instruments,(sa,sb),(),rules,{"rb":3},research_policy(),valuations=cp.valuation_marks)[0].eligible
    book.apply_fill(fill("a-close",2*NS,"110",-1,"CLOSE_TODAY",contract="a",episode="a"),rules[0])
    a=book.snapshot(2*NS)
    assert a.margin_cny==D(680)
    stale=book.mark({},3*NS,DAY,rules)
    cp=book.checkpoint((sa,sb),(),MODELS,{},pending_remaining_lots={},exit_registry_sha256=EXIT_REGISTRY,
                           exit_bindings=LEGACY_BINDINGS,exit_states=(),exit_execution_links=())
    gate=enforce_account_risk((p,),gates,stale,instruments,(sa,sb),(),rules,{"rb":3},research_policy(),valuations=cp.valuation_marks)[0]
    assert not gate.eligible and "DATA_STALE" in gate.reasons

