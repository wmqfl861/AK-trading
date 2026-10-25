from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from ak_trading.v2.contracts import FrozenInput, SessionWindow
from ak_trading.v2.policy import research_policy
from ak_trading.v2.probability.models import ForecastModelSet, default_fit_config
from ak_trading.v2.probability.regimes import RegimeReference
from ak_trading.v2.decision.payoffs import fit_path_payoff_model
from ak_trading.v2.decision.replay import run_decision_replay, intents_for_proposal, replay_payloads
from ak_trading.v2.strategies.registry import build_strategy_spec
from test_v2_m4_ledger import DAY,EXIT_REGISTRY,HASH,NS,account,bar,instrument,rule,run_policy,LEGACY_BINDINGS


def replay_inputs():
    reference=RegimeReference("reference",NS,NS,{"SHFE.RB|DAY|60":(.001,.01,.1)}, {})
    models=ForecastModelSet("models","reference",(),NS,2*NS,1000*NS,
        default_fit_config((),NS,2*NS,1000*NS),{},(),{})
    payoff=fit_path_payoff_model((),NS,"research-policy-v2.1.0")
    bars=tuple(bar(t=i*NS,price=str(D(100)+D(i)*D(".1")+(D(".1") if i%3==0 else D(0)))) for i in range(1,151))
    bundle=FrozenInput(Path("SYNTHETIC"),HASH,(instrument(),),
        (SessionWindow("session","SHFE.RB",DAY,NS,200*NS,"DAY","synthetic","SCENARIO"),),bars,(),(rule(),))
    return bundle,models,payoff,reference


def run(bundle,models,payoff,reference,start=70*NS,end=75*NS,checkpoint=None):
    from test_v2_m4_ledger import exit_fixtures_for
    specs=(build_strategy_spec("WAIT","MINUTE"),)
    exit_specs,bindings,_=exit_fixtures_for(specs)
    return run_decision_replay(bundle,models,payoff,specs,
        account("100000") if checkpoint is None else checkpoint.account,run_policy(),(),{},research_policy(),None,
        reference,HASH,checkpoint,start,end,exit_specs=exit_specs,exit_bindings=bindings)


def test_real_producer_no_evidence_stays_cash_and_half_open_window():
    bundle,models,payoff,reference=replay_inputs()
    result=run(bundle,models,payoff,reference)
    assert result.status=="COMPLETE" and result.decisions and not result.fills
    assert all(70*NS<=d.decision_nano<75*NS for d in result.decisions)
    assert all(a.cash_cny==D(100000) for a in result.equity)
    assert all(d.selected_proposal_id is None for d in result.decisions)
    assert result.reconciliation["status"]=="PASS"


def test_resume_same_events_does_not_repeat_cash_or_decisions():
    bundle,models,payoff,reference=replay_inputs()
    first=run(bundle,models,payoff,reference,end=73*NS)
    resumed=run(bundle,models,payoff,reference,start=73*NS,checkpoint=first.checkpoints[-1])
    continuous=run(bundle,models,payoff,reference)
    assert first.decisions+resumed.decisions==continuous.decisions
    assert first.equity+resumed.equity==continuous.equity
    assert first.checkpoints+resumed.checkpoints==continuous.checkpoints


def test_signed_short_intent_uses_frozen_worst_price():
    from test_v2_m4_gates import example
    p,c=example()
    p=replace(p,target_lots=-1,budget=replace(p.budget,side=-1))
    i=intents_for_proposal(p,"decision",c.account,c.rule)[0]
    assert i.delta_lots==-1 and i.limit_price==D("99.9")


def test_m4_artifacts_have_actual_rows_exact_columns_and_lossless_account():
    import csv,io,json
    from ak_trading.v2.contracts import AccountSnapshot
    from ak_trading.v2.schemas import decode_record_file
    bundle,models,payoff,reference=replay_inputs()
    result=run(bundle,models,payoff,reference)
    files=replay_payloads(result,payoff)
    assert set(files)=={"decisions.json","decisions.csv","intents.json","fills.csv","fills.json","equity.csv",
        "equity.json","execution_results.json","checkpoints.json","payoff_model.json","payoff_estimates.json","risk_report.json"}
    assert decode_record_file(files["equity.json"],AccountSnapshot,"ak_probability_equity_v2.1.0")==result.equity
    decision_rows=list(csv.DictReader(io.StringIO(files['decisions.csv'].decode())))
    assert len(decision_rows)==len(result.decisions)>0
    assert json.loads(files['risk_report.json'])["maximum_inventory"]==0


def test_cumulative_partial_receipts_use_real_m3b_new_fills_and_restore():
    import pytest
    from ak_trading.v2.contracts import ExecutionResult,LayerSpec,OrderIntent
    from ak_trading.v2.decision.ledger import ContinuousLedger
    from ak_trading.v2.decision.replay import apply_receipt
    from test_v2_m4_ledger import budget,fill,ledger,state,MODELS
    book=ledger("100000")
    b=replace(budget(),layers=(LayerSpec(0,D(100),D(100),3),),maximum_lots=3)
    intent=OrderIntent("entry","decision","trend_m1_v2","episode","rb",0,NS,10*NS,3,"OPEN","ENTRY",0,None)
    s=state(b,(0,),"WAIT_TRIGGER",("entry",))
    f1=replace(fill("f1",NS,"100"),intent_id=intent.intent_id)
    f2=replace(fill("f2",2*NS,"100"),intent_id=intent.intent_id)
    f3=replace(fill("f3",3*NS,"100"),intent_id=intent.intent_id)
    s,new=apply_receipt(book,s,intent,ExecutionResult("entry",(f1,),2,"PARTIAL",()),3,(rule(),),NS)
    assert s.layer_filled_lots==(1,) and s.pending_intent_ids==("entry",) and new==(f1,)
    cp=book.checkpoint((s,),(intent,),MODELS,{},pending_remaining_lots={"entry":2},
                         exit_registry_sha256=EXIT_REGISTRY,exit_bindings=LEGACY_BINDINGS,exit_states=(),exit_execution_links=())
    restored=ContinuousLedger.restore(cp,(instrument(),),(rule(),),run_policy(),HASH,HASH,MODELS,{},exit_registry_sha256=EXIT_REGISTRY)
    before=restored.snapshot(NS)
    corrupted=replace(f1,fee_cny=D(2))
    with pytest.raises(ValueError,match="conflicting_duplicate"):
        apply_receipt(restored,s,intent,ExecutionResult("entry",(f2,corrupted),1,"PARTIAL",()),2,(rule(),),2*NS)
    assert restored.snapshot(NS)==before
    s,new=apply_receipt(restored,s,intent,ExecutionResult("entry",(f1,f2),1,"PARTIAL",()),2,(rule(),),2*NS)
    assert new==(f2,) and s.layer_filled_lots==(2,) and restored.snapshot(2*NS).fees_cny==2
    s,new=apply_receipt(restored,s,intent,ExecutionResult("entry",(f1,f2,f3),0,"FILLED",()),1,(rule(),),3*NS)
    assert s.layer_filled_lots==(3,) and s.filled_layers==(0,) and not s.pending_intent_ids
    assert restored.snapshot(3*NS).fees_cny==3 and intent.delta_lots==3


def test_blocked_exit_keeps_pending_then_negative_partials_reduce_real_lots():
    from ak_trading.v2.contracts import ExecutionResult,LayerSpec,OrderIntent
    from ak_trading.v2.decision.replay import apply_receipt
    from test_v2_m4_ledger import budget,fill,ledger,state
    book=ledger("100000")
    book.apply_fill(fill("open-three",NS,"100",lots=3),rule())
    b=replace(budget(),layers=(LayerSpec(0,D(100),D(100),3),),maximum_lots=3)
    s=state(b,(3,),"EXITING",("exit",))
    i=OrderIntent("exit","decision","trend_m1_v2","episode","rb",NS,2*NS,10*NS,-3,"CLOSE_TODAY","STOP",None,None)
    blocked=ExecutionResult("exit",(),-3,"BLOCKED",("LIMIT_LOCKED","EXIT_PENDING"))
    s,new=apply_receipt(book,s,i,blocked,-3,(rule(),),2*NS)
    assert s.phase=="EXITING" and s.pending_intent_ids==("exit",) and not new
    first=replace(fill("close-one",3*NS,"90",-1,"CLOSE_TODAY"),intent_id="exit")
    s,new=apply_receipt(book,s,i,ExecutionResult("exit",(first,),-2,"PARTIAL",()),-3,(rule(),),3*NS)
    assert s.pending_intent_ids==("exit",) and sum(l.lots for l in book.snapshot(3*NS).lots)==2
    assert s.layer_filled_lots==(3,)
    last=replace(fill("close-two",4*NS,"89",-1,"CLOSE_TODAY",lots=2),intent_id="exit")
    s,new=apply_receipt(book,s,i,ExecutionResult("exit",(first,last),0,"FILLED",()),-2,(rule(),),4*NS)
    assert s.phase=="PAUSED" and not book.snapshot(4*NS).lots and not s.pending_intent_ids
    assert book.snapshot(4*NS).realized_gross_cny==D(-320) and book.snapshot(4*NS).fees_cny==D(6)


def test_scheduler_trades_real_fixture_only_after_decision_and_reconciles(monkeypatch):
    from test_v2_m4_gates import example
    from test_v2_m4_ledger import MODELS
    from ak_trading.v2.decision.pipeline import registry_hash_for,model_hashes_for
    from ak_trading.v2.decision.ledger import ContinuousLedger
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model,condition_key,PayoffObservation
    import ak_trading.v2.decision.replay as replay_module
    from ak_trading.v2.contracts import FactorValue
    base,c=example()
    bundle,models,empty,reference=replay_inputs()
    bars=tuple(bar(t=t*NS,price="100") for t in range(70,110))
    # The first close103 is visible only after entry101. At106 the next exit open is locked.
    bars=tuple(replace(b,open=D(1),high=D(1),low=D(1),close=D(1)) if b.start_nano==106*NS else b for b in bars)
    bundle=replace(bundle,bars=bars)
    bound={"payoff":None}
    def fixture_context(bundle,contract,now,account,previous,fm,pm,spec,seasonal,ref,rp,qs,*,exit_binding,forecast_provider):
        _p,ctx=example()
        ctx=replace(ctx,exit_binding=exit_binding)
        fs=replace(ctx.features,snapshot_id=f"features-{now}",decision_nano=now,available_at_nano=now,
                   reference_price=D(100),values=tuple(replace(v,available_at_nano=now) for v in ctx.features.values))
        reg=replace(ctx.regime,assessment_id=f"regime-{now}",snapshot_id=fs.snapshot_id,decision_nano=now)
        f=replace(ctx.forecasts[0],forecast_id=f"forecast-{now}",snapshot_id=fs.snapshot_id,assessment_id=reg.assessment_id,
            decision_nano=now,published_at_nano=now,expires_at_nano=now+2*NS,window_start_nano=now+NS,window_end_nano=now+31*NS)
        # Scheduler fixture, not a model PASS: actual M1 immutable records and real receipt state.
        from ak_trading.v2.decision.execution import position_for
        hashes={**MODELS,"payoff":bound["payoff"]}
        return replace(ctx,now_nano=now,features=fs,forecasts=(f,),regime=reg,account=account,
            spec=spec,run_policy=rp,position=position_for(account,contract),model_hashes=hashes,
            macro=replace(ctx.macro,decision_nano=now),bars=tuple(b for b in bundle.bars if b.end_nano<=now))
    class FixtureStrategy:
        def __init__(self,spec): self.spec=spec
        def evaluate(self,ctx,previous):
            p,_=example()
            now=ctx.now_nano
            if previous is not None and previous.episode_id:
                p=replace(p,proposal_id=f"exit-{now}",decision_nano=now,action="EXIT",target_lots=0,
                    earliest_execution_nano=now+NS,valid_until_nano=now+2*NS,next_state=replace(previous,phase="EXITING"),
                    budget=previous.budget,forecast_id=ctx.forecasts[0].forecast_id,assessment_id=ctx.regime.assessment_id)
            elif now==100*NS:
                p=replace(p,forecast_id=ctx.forecasts[0].forecast_id,assessment_id=ctx.regime.assessment_id)
            else:
                from ak_trading.v2.strategies.registry import fresh_state
                p=replace(p,proposal_id=f"wait-{now}",decision_nano=now,action="WAIT",target_lots=0,budget=None,
                    episode_id=None,next_state=previous or fresh_state(self.spec.strategy_id,"rb"),
                    earliest_execution_nano=now+NS,valid_until_nano=now+2*NS,forecast_id=None,payoff=None)
            return p
    monkeypatch.setattr(replay_module,"build_strategy_context",fixture_context)
    monkeypatch.setattr(replay_module,"strategy_registry_v2",lambda mode:{c.spec.strategy_id:FixtureStrategy(c.spec)})
    from test_v2_m4_payoffs import observations
    pm=fit_path_payoff_model(observations(base,c,profits=("80","-30")),3*NS,c.run_policy.research_policy_version)
    bound["payoff"]=pm.content_hash()
    from test_v2_m4_ledger import exit_fixtures_for
    _es,_bd,_=exit_fixtures_for((c.spec,))
    result=run_decision_replay(bundle,models,pm,(c.spec,),account("100000"),run_policy(),(),{},research_policy(),None,
        reference,HASH,None,100*NS,106*NS,exit_specs=_es,exit_bindings=_bd)
    assert result.fills and result.fills[0].execution_nano==101*NS
    assert all(f.execution_nano>next(i.created_at_nano for i in result.intents if i.intent_id==f.intent_id) for f in result.fills)
    assert result.equity[-1].cash_cny<=D(100000) and not result.equity[-1].lots
    prefix=replace(bundle,bars=tuple(replace(b,open=D(1),high=D(1),low=D(1),close=D(1))
                                   if b.start_nano in (102*NS,103*NS,104*NS) else b for b in bars))
    blocked=run_decision_replay(prefix,models,pm,(c.spec,),account("100000"),run_policy(),(),{},research_policy(),None,
        reference,HASH,None,100*NS,105*NS,exit_specs=_es,exit_bindings=_bd)
    assert blocked.status=="INCOMPLETE" and blocked.equity[-1].lots
    assert any("EXIT_PENDING" in r.reasons for r in blocked.execution_results)
    cp=blocked.checkpoints[-1]
    resumed=run_decision_replay(prefix,models,pm,(c.spec,),cp.account,run_policy(),(),{},research_policy(),None,
        reference,HASH,cp,105*NS,108*NS,exit_specs=_es,exit_bindings=_bd)
    assert resumed.fills and resumed.fills[0].execution_price==D(100)
    assert not resumed.equity[-1].lots


def test_new_fill_outside_intent_window_is_rejected_atomically():
    import pytest
    from ak_trading.v2.contracts import ExecutionResult,LayerSpec,OrderIntent
    from ak_trading.v2.decision.replay import apply_receipt
    from test_v2_m4_ledger import budget,fill,ledger,rule,state
    book=ledger("100000")
    b=replace(budget(),layers=(LayerSpec(0,D(100),D(100),1),),maximum_lots=1)
    i=OrderIntent("entry","d","trend_m1_v2","episode","rb",2*NS,3*NS,10*NS,1,"OPEN","ENTRY",0,D("100.1"))
    s=state(b,(0,),"WAIT_TRIGGER",("entry",))
    before=book.snapshot(0)
    early=replace(fill("early",NS,"100"),intent_id="entry")
    late=replace(fill("late",11*NS,"100"),intent_id="entry")
    for bad in (early,late):
        with pytest.raises(ValueError,match="outside_intent_execution_window"):
            apply_receipt(book,s,i,ExecutionResult("entry",(bad,),0,"FILLED",()),1,(rule(),),NS)
        assert book.snapshot(0)==before
    legal=replace(fill("legal",3*NS,"100"),intent_id="entry")
    s2,new=apply_receipt(book,s,i,ExecutionResult("entry",(legal,),0,"FILLED",()),1,(rule(),),3*NS)
    assert new==(legal,) and s2.phase=="HOLDING"
    s3,new=apply_receipt(book,s2,i,ExecutionResult("entry",(legal,),0,"FILLED",()),0,(rule(),),9*NS)
    assert new==() and s3==s2


def _scheduler_fixture(monkeypatch,c,bars,entry_valid_until_nano=None,exit_on_position=False):
    import ak_trading.v2.decision.replay as replay_module
    from ak_trading.v2.strategies.registry import fresh_state
    from ak_trading.v2.decision.execution import position_for
    from test_v2_m4_gates import example
    bundle,models,empty,reference=replay_inputs()
    bundle=replace(bundle,bars=tuple(bars))
    bound={"payoff":None}
    def fixture_context(bundle,contract,now,account,previous,fm,pm,spec,seasonal,ref,rp,qs,*,exit_binding,forecast_provider):
        _p,ctx=example()
        ctx=replace(ctx,exit_binding=exit_binding)
        fs=replace(ctx.features,snapshot_id=f"features-{now}",decision_nano=now,available_at_nano=now,
                   values=tuple(replace(v,available_at_nano=now) for v in ctx.features.values))
        reg=replace(ctx.regime,assessment_id=f"regime-{now}",snapshot_id=fs.snapshot_id,decision_nano=now)
        f=replace(ctx.forecasts[0],forecast_id=f"forecast-{now}",snapshot_id=fs.snapshot_id,
            assessment_id=reg.assessment_id,decision_nano=now,published_at_nano=now,
            expires_at_nano=now+2*NS,window_start_nano=now+NS,window_end_nano=now+31*NS)
        from test_v2_m4_ledger import MODELS
        hashes={**MODELS,"payoff":bound["payoff"]}
        return replace(ctx,now_nano=now,features=fs,forecasts=(f,),regime=reg,account=account,
            spec=spec,run_policy=rp,position=position_for(account,contract),model_hashes=hashes,
            macro=replace(ctx.macro,decision_nano=now),bars=tuple(b for b in bundle.bars if b.end_nano<=now))
    class FixtureStrategy:
        def __init__(self,spec): self.spec=spec
        def evaluate(self,ctx,previous):
            p,_=example()
            now=ctx.now_nano
            if previous is not None and previous.episode_id:
                if previous.phase=="EXITING" or (exit_on_position and ctx.position is not None):
                    p=replace(p,proposal_id=f"exit-{now}",decision_nano=now,action="EXIT",target_lots=0,
                        earliest_execution_nano=now+NS,valid_until_nano=now+2*NS,
                        next_state=replace(previous,phase="EXITING"),budget=previous.budget,
                        forecast_id=ctx.forecasts[0].forecast_id,assessment_id=ctx.regime.assessment_id,
                        reasons=("STOP_TOUCHED",))
                else:
                    p=replace(p,proposal_id=f"hold-{now}",decision_nano=now,action="HOLD",target_lots=1,
                        earliest_execution_nano=now+NS,valid_until_nano=now+2*NS,
                        next_state=previous,budget=previous.budget,payoff=None,forecast_id=None)
            elif now==100*NS:
                if entry_valid_until_nano is not None:
                    p=replace(p,valid_until_nano=entry_valid_until_nano)
                p=replace(p,forecast_id=ctx.forecasts[0].forecast_id,assessment_id=ctx.regime.assessment_id)
            else:
                p=replace(p,proposal_id=f"wait-{now}",decision_nano=now,action="WAIT",target_lots=0,budget=None,
                    episode_id=None,next_state=previous or fresh_state(self.spec.strategy_id,"rb"),
                    earliest_execution_nano=now+NS,valid_until_nano=now+2*NS,forecast_id=None,payoff=None)
            return p
    monkeypatch.setattr(replay_module,"build_strategy_context",fixture_context)
    monkeypatch.setattr(replay_module,"strategy_registry_v2",lambda mode:{c.spec.strategy_id:FixtureStrategy(c.spec)})
    return bundle,models,reference,bound


def test_blocked_open_keeps_pending_and_fills_at_next_legal_minute(monkeypatch):
    from ak_trading.v2.decision.replay import run_decision_replay
    from test_v2_m4_gates import example
    from test_v2_m4_payoffs import observations
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model
    base,c=example()
    bars=list(bar(t=t*NS,price="100") for t in range(70,110))
    bars[101-70]=replace(bars[101-70],open=D(200),high=D(200),low=D(200),close=D(200))
    bundle,models,reference,bound=_scheduler_fixture(monkeypatch,c,bars,entry_valid_until_nano=104*NS)
    pm=fit_path_payoff_model(observations(base,c,profits=("80","-30")),3*NS,c.run_policy.research_policy_version)
    bound["payoff"]=pm.content_hash()
    from test_v2_m4_ledger import exit_fixtures_for
    _es,_bd,_=exit_fixtures_for((c.spec,))
    result=run_decision_replay(bundle,models,pm,(c.spec,),account("100000"),run_policy(),(),{},research_policy(),None,
        reference,HASH,None,100*NS,102*NS,exit_specs=_es,exit_bindings=_bd)
    blocked=[r for r in result.execution_results if r.status=="BLOCKED"]
    assert blocked and all(r.remaining_lots==1 for r in blocked)
    # The checkpoint taken right after the blocked minute still owes the entry.
    cp=result.checkpoints[-1]
    assert cp.pending_intents and cp.pending_remaining_lots[cp.pending_intents[0].intent_id]==1
    assert result.status=="INCOMPLETE"
    resumed=run_decision_replay(bundle,models,pm,(c.spec,),cp.account,run_policy(),(),{},research_policy(),None,
        reference,HASH,cp,102*NS,103*NS,exit_specs=_es,exit_bindings=_bd)
    assert resumed.fills and resumed.fills[0].execution_price==D(100)
    assert not resumed.checkpoints[-1].pending_intents


def test_pre_registered_stop_intent_fills_stop_first_in_default_replay(monkeypatch):
    from ak_trading.v2.decision.replay import run_decision_replay
    from test_v2_m4_gates import example
    from test_v2_m4_payoffs import observations
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model
    base,c=example()
    bars=list(bar(t=t*NS,price="100") for t in range(70,110))
    bars[102-70]=replace(bars[102-70],high=D(106),low=D(93))
    bundle,models,reference,bound=_scheduler_fixture(monkeypatch,c,bars,exit_on_position=True)
    pm=fit_path_payoff_model(observations(base,c,profits=("80","-30")),3*NS,c.run_policy.research_policy_version)
    bound["payoff"]=pm.content_hash()
    from test_v2_m4_ledger import exit_fixtures_for
    _es,_bd,_=exit_fixtures_for((c.spec,))
    result=run_decision_replay(bundle,models,pm,(c.spec,),account("100000"),run_policy(),(),{},research_policy(),None,
        reference,HASH,None,100*NS,103*NS,exit_specs=_es,exit_bindings=_bd)
    stop_fills=[f for f in result.fills if f.offset!="OPEN"]
    assert stop_fills and stop_fills[-1].execution_price==D(94)
    assert any("STOP_FIRST_AMBIGUITY" in r.reasons for r in result.execution_results)


def test_forged_mark_source_hash_fails_restore_inside_replay():
    import pytest
    from dataclasses import replace as dc_replace
    from ak_trading.v2.decision.replay import run_decision_replay
    from ak_trading.v2.decision.ledger import _checkpoint_hash
    from ak_trading.v2.strategies.registry import build_strategy_spec
    from test_v2_m4_ledger import budget,fill,ledger,rule,state,MODELS
    book=ledger("100000")
    book.apply_fill(fill("a",NS,"100"),rule())
    book.mark({"rb":bar(t=0,price="100")},NS,DAY,(rule(),))
    s=state(budget(),(1,0,0),"HOLDING")
    cp=book.checkpoint((s,),(),MODELS,{},pending_remaining_lots={},exit_registry_sha256=EXIT_REGISTRY,
                         exit_bindings=LEGACY_BINDINGS,exit_states=(),exit_execution_links=())
    forged=dc_replace(cp,valuation_marks=tuple(
        dc_replace(m,source_bar_sha256="b"*64) if m.contract_id=="rb" else m for m in cp.valuation_marks))
    forged=dc_replace(forged,checkpoint_hash=_checkpoint_hash(forged))
    bundle,models,payoff,reference=replay_inputs()
    with pytest.raises(ValueError,match="restore_source_bar_verification_failed"):
        from test_v2_m4_ledger import exit_fixtures_for
        _specs=(build_strategy_spec("WAIT","MINUTE"),)
        _es,_bd,_=exit_fixtures_for(_specs)
        run_decision_replay(bundle,models,payoff,_specs,forged.account,
            run_policy(),(),{},research_policy(),None,reference,HASH,forged,70*NS,75*NS,
            exit_specs=_es,exit_bindings=_bd)


def test_committed_prefix_forward_foreign_keys_reject_missing_or_wrong_originals():
    import pytest
    from ak_trading.v2.contracts import OrderIntent
    from ak_trading.v2.decision.replay import verify_committed_prefix
    from ak_trading.v2.decision.ledger import record_hash
    from test_v2_m4_ledger import MODELS,budget,fill,ledger,rule,state
    book=ledger("100000")
    booked=fill("prefix",NS,"100")
    book.apply_fill(booked,rule())
    cp=book.checkpoint((state(budget(),(1,0,0),"HOLDING"),),(),MODELS,{},pending_remaining_lots={},
                         exit_registry_sha256=EXIT_REGISTRY,exit_bindings=LEGACY_BINDINGS,exit_states=(),exit_execution_links=())
    original=OrderIntent(booked.intent_id,"d0",booked.strategy_id,booked.episode_id,
        booked.contract_id,0,NS,600*NS,1,"OPEN","ENTRY",None,None)
    # Missing originals entirely: FAILED, never silently accepted.
    with pytest.raises(ValueError,match="missing_original_fill"):
        verify_committed_prefix(cp,(),(),())
    # Same intent_id but wrong owner: FAILED.
    wrong=OrderIntent(booked.intent_id,"other","other-strategy","other-episode","other-contract",
        0,NS,600*NS,-1,"CLOSE_TODAY","STOP",None,None)
    with pytest.raises(ValueError,match="intent_owner_mismatch"):
        verify_committed_prefix(cp,(),(booked,),(wrong,))
    # Rule chain: an intent referencing a rule the checkpoint never consumed.
    unknown_rule=OrderIntent(booked.intent_id,"d0",booked.strategy_id,booked.episode_id,
        booked.contract_id,0,NS,600*NS,1,"OPEN","ENTRY",None,None)
    tampered_fill=replace(booked,rule_id="never-consumed")
    tampered_cp=replace(cp,processed_fill_hashes={**dict(cp.processed_fill_hashes),
        tampered_fill.fill_id:record_hash(tampered_fill)})
    with pytest.raises(ValueError,match="rule_not_consumed"):
        verify_committed_prefix(tampered_cp,(),(tampered_fill,),(unknown_rule,))
    # Valid originals: accepted.
    verify_committed_prefix(cp,(),(booked,),(original,))


def test_default_replay_intents_all_resolve_to_published_decisions(monkeypatch):
    """F12: every OrderIntent published by the default replay — including the
    risk-timer hard-exit path — resolves to a published DecisionRecord."""
    from ak_trading.v2.decision.replay import run_decision_replay
    from test_v2_m4_gates import example
    from test_v2_m4_payoffs import observations
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model
    base,c=example()
    bars=list(bar(t=t*NS,price="100") for t in range(70,110))
    bundle,models,reference,bound=_scheduler_fixture(monkeypatch,c,bars,entry_valid_until_nano=110*NS)
    pm=fit_path_payoff_model(observations(base,c,profits=("80","-30")),3*NS,c.run_policy.research_policy_version)
    bound["payoff"]=pm.content_hash()
    from test_v2_m4_ledger import exit_fixtures_for
    _es,_bd,_=exit_fixtures_for((c.spec,))
    result=run_decision_replay(bundle,models,pm,(c.spec,),account("100000"),run_policy(),(),{},research_policy(),None,
        reference,HASH,None,100*NS,132*NS,exit_specs=_es,exit_bindings=_bd)
    known={d.decision_id for d in result.decisions}
    assert result.intents and {i.decision_id for i in result.intents}<=known
    timer=[d for d in result.decisions if "RISK_TIMER_EXIT" in d.reasons]
    assert timer and timer[0].outcome=="MANAGE_POSITION"
    timer_intents=[i for i in result.intents if i.decision_id==timer[0].decision_id]
    assert timer_intents and all(i.offset!="OPEN" for i in timer_intents)


def _exit_state_for(acct,binding,first_nano,fees=D(1)):
    from ak_trading.v2.contracts import ExitState
    from ak_trading.v2.artifacts import canonical_json_bytes,sha256_hex
    prefix=sha256_hex(canonical_json_bytes([acct.account_id,"rb","trend_m1_v2","episode",
                                            str(first_nano),binding.binding_sha256,"initial"]))
    return ExitState(acct.account_id,"rb","trend_m1_v2","episode",first_nano,
        binding.binding_sha256,D(0),fees,None,None,None,None,None,None,False,None,None,None,
        (),False,None,0,(),"MANAGING",None,prefix)


def test_exit_routing_kinds_and_purposes_p1():
    """Soft TIME_DECAY EXIT -> NEXT_WINDOW_CLOSE TIME_EXIT legs; hard-risk
    (no triggering policy) keeps LEGACY_PROTECTIVE STOP."""
    from dataclasses import replace as dc_replace
    from ak_trading.v2.contracts import ExitProposal
    from ak_trading.v2.decision.pipeline import (evaluate_exit_proposal,
        exit_execution_links,legacy_exit_spec)
    from test_v2_m4_gates import example
    from test_v2_m4_ledger import ledger,rule,fill,NS
    from ak_trading.v2.decision.ledger import record_hash
    from ak_trading.v2.schemas import encode_record
    from ak_trading.v2.artifacts import sha256_hex,canonical_json_bytes
    p,c=example()
    book=ledger("100000")
    book.apply_fill(fill("p1",NS,"100"),rule())
    acct=book.snapshot(NS)
    binding=c.exit_binding
    specs=(legacy_exit_spec("MINUTE"),)
    state=_exit_state_for(acct,binding,NS)
    prop=ExitProposal("exit-p1","trend_m1_v2","episode","rb",binding.binding_sha256,"EXIT",0,
        "exit_time_decay_full","9"*64,NS,2*NS,None,state,("TIME_DECAY_SOFT",))
    routed,intents=evaluate_exit_proposal(prop,acct,specs,binding,p)
    links=exit_execution_links(prop,routed,intents,specs)
    assert routed.action=="EXIT" and routed.target_lots==0
    assert {i.purpose for i in intents}=={"REDUCE"}
    assert {l.execution_kind for l in links}=={"NEXT_WINDOW_CLOSE"}
    assert all(l.exit_proposal_id=="exit-p1" and l.requested_lots==1 for l in links)
    roll=dc_replace(prop,reasons=("ROLL_EXIT",))
    _,roll_intents=evaluate_exit_proposal(roll,acct,specs,binding,p)
    assert {i.purpose for i in roll_intents}=={"ROLL_EXIT"}
    prop2=dc_replace(prop,triggering_policy_id=None,reasons=("STOP_TOUCHED",))
    routed2,intents2=evaluate_exit_proposal(prop2,acct,specs,binding,p)
    links2=exit_execution_links(prop2,routed2,intents2,specs)
    assert {i.purpose for i in intents2}=={"STOP"}
    assert {l.execution_kind for l in links2}=={"LEGACY_PROTECTIVE"}
    # binding mismatch and non-owned position rejected
    from ak_trading.v2.decision.pipeline import legacy_exit_binding
    other=legacy_exit_binding(dc_replace(c.spec,strategy_id="wait_m1_v2"))
    import pytest as _pytest
    with _pytest.raises(ValueError,match="binding_mismatch"):
        evaluate_exit_proposal(prop,acct,specs,other,p)
    book3=ledger("100000")
    book3.apply_fill(fill("p2",NS,"100",lots=2),rule())
    acct3=book3.snapshot(NS)
    prop3=dc_replace(prop,target_lots=1,action="REDUCE")
    routed3,intents3=evaluate_exit_proposal(prop3,acct3,specs,binding,p)
    assert routed3.target_lots==1 and intents3
    with _pytest.raises(ValueError,match="exit_target_must_strictly_reduce"):
        evaluate_exit_proposal(dc_replace(prop3,target_lots=2),acct3,specs,binding,p)
    with _pytest.raises(ValueError,match="exit_target_cannot_reverse_or_increase"):
        evaluate_exit_proposal(dc_replace(prop3,target_lots=3),acct3,specs,binding,p)
    with _pytest.raises(ValueError,match="exit_target_cannot_reverse_or_increase"):
        evaluate_exit_proposal(dc_replace(prop3,target_lots=-1),acct3,specs,binding,p)


def test_exit_bindings_and_states_survive_restart_consistently():
    """Checkpoint-carried exit bindings/states restore byte-identically and a
    conflicting binding set is rejected (007 §5.2 cross-restart identity)."""
    import pytest
    from test_v2_m4_gates import example
    from test_v2_m4_ledger import (MODELS,EXIT_REGISTRY,LEGACY_BINDINGS,ledger,rule,
        fill,state,budget,NS)
    from ak_trading.v2.decision.ledger import ContinuousLedger
    p,c=example()
    book=ledger("100000")
    book.apply_fill(fill("p1",NS,"100"),rule())
    acct=book.snapshot(NS)
    s=state(budget(),(1,0,0),"HOLDING")
    cp=book.checkpoint((s,),(),MODELS,{},pending_remaining_lots={},
                       exit_registry_sha256=EXIT_REGISTRY,exit_bindings=LEGACY_BINDINGS,
                       exit_states=(),exit_execution_links=())
    restored=ContinuousLedger.restore(cp,(instrument(),),(rule(),),run_policy(),HASH,HASH,
                                      MODELS,{},exit_registry_sha256=EXIT_REGISTRY)
    assert cp.exit_bindings==LEGACY_BINDINGS
    assert [es.exit_binding_sha256 for es in cp.exit_states]==[LEGACY_BINDINGS[0].binding_sha256]
    assert cp.exit_states[0].phase=="MANAGING" and cp.exit_states[0].first_fill_nano==NS
    # restored ledger re-checkpoints with same bindings, producing identical
    # synthesized exit states for the same held scope
    cp2=restored.checkpoint((s,),(),MODELS,{},pending_remaining_lots={},
                            exit_registry_sha256=EXIT_REGISTRY,exit_bindings=LEGACY_BINDINGS,
                            exit_states=(),exit_execution_links=())
    assert cp2.exit_states==cp.exit_states
    # a conflicting registry binding is rejected on restore
    with pytest.raises(ValueError,match="dependency"):
        ContinuousLedger.restore(cp,(instrument(),),(rule(),),run_policy(),HASH,HASH,
                                 MODELS,{},exit_registry_sha256="e"*64)


# --- M4-REPLAY-PLAN §3/§4: qualification-evidence wiring into the real verifier ---
# SYNTHETIC/SCENARIO in-memory fixtures only: chains are built from the frozen
# public types with canonical encoding and the production hash functions, and
# every acceptance below is engineering validation of the verifier wiring, not
# a real research or independent product opinion.

SHADOW_SIX=("BASE","FEE_X2","SLIPPAGE_X2","MARGIN_X1_5","GAP","NO_STOP")
_REVIEW_CHECK_KEYS=("scope_registered","groups_complete","metrics_recomputed",
                    "thresholds_unchanged","holdout_once","causality_passed",
                    "source_hashes_match")


def _qualification_chain(spec,*,exit_registry,binding,model_hashes,
                         issued_at_nano=1,valid_until_nano=1000*NS):
    """Private in-memory evidence-chain helper (M1 `_evidence_chain` pattern).

    Returns (evidence, qualification, contract_lock_sha256, registry_sha256,
    metrics, review, experiment) where evidence carries the original
    experiment/metrics/review bytes under their real SHA-256 keys plus the
    contract-lock and strategy-registry payloads of this run, so the REAL
    verifier accepts the chain byte-for-byte."""
    import json
    from ak_trading.v2 import artifacts as a
    from ak_trading.v2 import contracts as ct
    from ak_trading.v2.schemas import encode_record
    horizon=spec.max_holding
    bundle=a.sha256_hex(a.canonical_json_bytes(dict(model_hashes)))
    def group_key(scenario,strategy=None,product="SHFE.RB"):
        return json.dumps(["STRATEGY","split-shadow","3"*64,strategy or spec.strategy_id,
            product,"rb" if strategy is None else "other-contract",spec.mode,
            horizon.unit,horizon.value,"PRICE_ONLY",scenario])
    six=[group_key(x) for x in SHADOW_SIX]
    other=group_key("BASE",strategy="other-strat",product="SHFE.CU")
    def group(key,scenario,status="PASS",strategy=None,product="SHFE.RB",fixed=True):
        return {"group_key":key,"split_id":"split-shadow","model_bundle_sha256":bundle,
            "run_policy_sha256":"3"*64,"routing_mode":"FIXED",
            "fixed_strategy_id":strategy or spec.strategy_id,"account_id":"acc-shadow",
            "strategy_id":strategy or spec.strategy_id,"product":product,
            "contract_id":"rb" if strategy is None else "other-contract","mode":spec.mode,
            "horizon_unit":horizon.unit,"horizon_value":horizon.value,
            "macro_mode":"PRICE_ONLY","cost_scenario":scenario,"episodes":10,"blocks":5,
            "admission_rate":0.5,"no_trade_rate":0.5,"net_pnl_cny":"100",
            "conservative_ev_cny":"10","max_drawdown_ratio":0.01,"cvar95_cny":"50",
            "maximum_stress_loss_ratio":0.01,"margin_shortfall_cny":"0",
            "peak_margin_cny":"1000","max_inventory":3,"longest_recovery_days":2,
            "status":status,"reasons":[],"exit_binding_sha256":binding.binding_sha256,
            "exit_spec_sha256s":list(binding.exit_spec_sha256s),
            "fixed_exit_binding_sha256":binding.binding_sha256 if fixed else None}
    metrics={"schema_version":"ak_probability_metrics_v2.1.4",
        "research_policy_version":"research-policy-v2.1.0",
        "macro_policy_version":"macro-risk-v2.1.0","cost_scenarios":list(SHADOW_SIX),
        "required_group_keys":[*six,other],"model_bundles":{bundle:dict(model_hashes)},
        "forecast_groups":[],
        "strategy_groups":[group(key,x) for key,x in zip(six,SHADOW_SIX)]+[group(other,"BASE",
            status="FAIL",strategy="other-strat",product="SHFE.CU",fixed=False)],
        "exclusions":{},"bootstrap":{"method":"TRADING_DAY_BLOCK","replicates":1000,"seed":7},
        "exit_registry_sha256":exit_registry,
        "exit_bindings":{binding.binding_sha256:encode_record(binding)},
        "exit_groups":[],
        "replay_summaries":[{"account_id":"acc-shadow","initial_equity_cny":"1000000",
            "terminal_equity_cny":"1000600","closed_episode_net_pnl_cny":"600",
            "open_episode_realized_net_pnl_cny":None,"terminal_unrealized_cny":None,
            "open_episode_ids":[],"pending_intent_ids":[],"reconciliation_status":"PASS",
            "status":"COMPLETE","reasons":[],"split_id":"split-shadow",
            "run_policy_sha256":"3"*64,"model_bundle_sha256":bundle}]}
    metrics_bytes=a.canonical_json_bytes(metrics)
    experiment={"schema_version":"ak_probability_experiment_v2.1.1","spec":{
        "experiment_id":"exp-shadow","run_id":"run-shadow","input_manifest_hashes":["6"*64],
        "contract_lock_sha256":None,"source_sha256":"f"*64,"parameter_sha256":"7"*64,
        "split_specs":[],"holdout_id":"hold-shadow","holdout_scope":{},"run_policies":[],
        "required_group_keys":[*six,other],"seed":7,
        "research_policy_version":"research-policy-v2.1.0",
        "macro_policy_version":"macro-risk-v2.1.0","macro_mode":"PRICE_ONLY",
        "evaluation_as_of_nano":1,"exit_registry_sha256":exit_registry,
        "exit_bindings":[encode_record(binding)]}}
    lock_bytes=b"aktr-synthetic-contract-lock-SCENARIO\n"
    contract_lock=a.sha256_hex(lock_bytes)
    experiment["spec"]["contract_lock_sha256"]=contract_lock
    experiment_bytes=a.canonical_json_bytes(experiment)
    review={"schema_version":"ak_probability_qualification_review_v2.1.1",
        "scope":"OFFLINE_STATISTICS","verdict":"PASS","independent":True,
        "reviewer_id":"reviewer-shadow","report_sha256":a.sha256_hex(metrics_bytes),
        "contract_lock_sha256":contract_lock,"registry_sha256":None,
        "experiment_sha256":a.sha256_hex(experiment_bytes),"model_hashes":dict(model_hashes),
        "research_policy_version":"research-policy-v2.1.0",
        "macro_policy_version":"macro-risk-v2.1.0","cost_scenarios":list(SHADOW_SIX),
        "source_hashes":{"code":spec.code_sha256},"exit_registry_sha256":exit_registry,
        "exit_binding_sha256s":[binding.binding_sha256],"required_group_keys":[*six,other],
        "checks":{key:"PASS" for key in _REVIEW_CHECK_KEYS},"reasons":[]}
    registry_payload=a.canonical_json_bytes([encode_record(spec)])
    registry=a.sha256_hex(registry_payload)
    review["registry_sha256"]=registry
    review_bytes=a.canonical_json_bytes(review)
    evidence={a.sha256_hex(metrics_bytes):metrics_bytes,a.sha256_hex(review_bytes):review_bytes,
        a.sha256_hex(experiment_bytes):experiment_bytes,contract_lock:lock_bytes,
        registry:registry_payload}
    qualification=ct.StrategyQualification(
        qualification_id="qual-shadow",strategy_id=spec.strategy_id,
        strategy_version=spec.version,parameter_version=spec.parameter_version,
        code_sha256=spec.code_sha256,exit_registry_sha256=exit_registry,
        exit_binding_sha256=binding.binding_sha256,
        exit_spec_sha256s=binding.exit_spec_sha256s,product="SHFE.RB",contract_ids=("rb",),
        mode=spec.mode,horizons=(horizon,),macro_mode="PRICE_ONLY",
        research_policy_version="research-policy-v2.1.0",
        macro_policy_version="macro-risk-v2.1.0",cost_scenarios=SHADOW_SIX,
        model_hashes=dict(model_hashes),experiment_sha256=a.sha256_hex(experiment_bytes),
        statistics="PASS",report_sha256=a.sha256_hex(metrics_bytes),
        review_sha256=a.sha256_hex(review_bytes),issued_at_nano=issued_at_nano,
        valid_until_nano=valid_until_nano,reasons=())
    return evidence,qualification,contract_lock,registry,metrics,review,experiment


def _shadow_scheduler_fixture(monkeypatch,c,bars,entry_nano=100*NS):
    """Deterministic SHADOW scheduler fixture: same frozen forecast/strategy
    inputs as `_scheduler_fixture`, with the qualification attached to the
    context exactly as the real `build_strategy_context` selects it
    (strategy_id + product match) and the single OPEN attempt at entry_nano.
    The qualification verifier, gates and routing stay real."""
    import ak_trading.v2.decision.replay as replay_module
    from ak_trading.v2.strategies.registry import fresh_state
    from ak_trading.v2.decision.execution import position_for
    from test_v2_m4_gates import example
    from test_v2_m4_ledger import MODELS
    bundle,models,empty,reference=replay_inputs()
    bundle=replace(bundle,bars=tuple(bars))
    bound={"payoff":None}
    def fixture_context(bundle,contract,now,account,previous,fm,pm,spec,seasonal,ref,rp,qs,*,exit_binding,forecast_provider):
        _p,ctx=example()
        qualification=next((q for q in qs if q.strategy_id==spec.strategy_id
                             and q.product=="SHFE.RB"),None)
        ctx=replace(ctx,exit_binding=exit_binding,qualification=qualification)
        fs=replace(ctx.features,snapshot_id=f"features-{now}",decision_nano=now,available_at_nano=now,
                   values=tuple(replace(v,available_at_nano=now) for v in ctx.features.values))
        reg=replace(ctx.regime,assessment_id=f"regime-{now}",snapshot_id=fs.snapshot_id,decision_nano=now)
        f=replace(ctx.forecasts[0],forecast_id=f"forecast-{now}",snapshot_id=fs.snapshot_id,
            assessment_id=reg.assessment_id,decision_nano=now,published_at_nano=now,
            expires_at_nano=now+2*NS,window_start_nano=now+NS,window_end_nano=now+31*NS)
        hashes={**MODELS,"payoff":bound["payoff"]}
        return replace(ctx,now_nano=now,features=fs,forecasts=(f,),regime=reg,account=account,
            spec=spec,run_policy=rp,position=position_for(account,contract),model_hashes=hashes,
            macro=replace(ctx.macro,decision_nano=now),bars=tuple(b for b in bundle.bars if b.end_nano<=now))
    class FixtureStrategy:
        def __init__(self,spec): self.spec=spec
        def evaluate(self,ctx,previous):
            p,_=example()
            now=ctx.now_nano
            if previous is not None and previous.episode_id:
                p=replace(p,proposal_id=f"hold-{now}",decision_nano=now,action="HOLD",target_lots=1,
                    earliest_execution_nano=now+NS,valid_until_nano=now+2*NS,
                    next_state=previous,budget=previous.budget,payoff=None,forecast_id=None)
            elif now==entry_nano:
                p=replace(p,decision_nano=now,earliest_execution_nano=now+NS,
                    valid_until_nano=now+2*NS,forecast_id=ctx.forecasts[0].forecast_id,
                    assessment_id=ctx.regime.assessment_id)
            else:
                p=replace(p,proposal_id=f"wait-{now}",decision_nano=now,action="WAIT",target_lots=0,budget=None,
                    episode_id=None,next_state=previous or fresh_state(self.spec.strategy_id,"rb"),
                    earliest_execution_nano=now+NS,valid_until_nano=now+2*NS,forecast_id=None,payoff=None)
            return p
    monkeypatch.setattr(replay_module,"build_strategy_context",fixture_context)
    monkeypatch.setattr(replay_module,"strategy_registry_v2",lambda mode:{c.spec.strategy_id:FixtureStrategy(c.spec)})
    return bundle,models,reference,bound


def _run_shadow(c,bundle,models,reference,pm,qualifications,evidence,exit_specs,exit_bindings,
                contract_lock,start=100*NS,end=106*NS,checkpoint=None):
    from ak_trading.v2.decision.replay import run_decision_replay
    from ak_trading.v2.policy import research_policy
    from test_v2_m4_ledger import account,run_policy
    return run_decision_replay(bundle,models,pm,(c.spec,),account("100000"),
        replace(run_policy(),purpose="SHADOW_OPERATIONAL"),tuple(qualifications),dict(evidence),
        research_policy(),None,reference,contract_lock,checkpoint,start,end,
        exit_specs=exit_specs,exit_bindings=tuple(exit_bindings))


def _shadow_setup(monkeypatch,bar_minutes=range(70,110),entry_nano=100*NS):
    from test_v2_m4_gates import example
    from test_v2_m4_payoffs import observations
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model
    from test_v2_m4_ledger import exit_fixtures_for,MODELS
    base,c=example()
    bars=tuple(bar(t=t*NS,price="100") for t in bar_minutes)
    bundle,models,reference,bound=_shadow_scheduler_fixture(monkeypatch,c,bars,entry_nano=entry_nano)
    pm=fit_path_payoff_model(observations(base,c,profits=("80","-30")),3*NS,c.run_policy.research_policy_version)
    bound["payoff"]=pm.content_hash()
    exit_specs,bindings,exit_registry=exit_fixtures_for((c.spec,))
    return c,bundle,models,reference,pm,bound,exit_specs,bindings,exit_registry,{**MODELS,"payoff":pm.content_hash()}


def test_valid_qualification_evidence_admits_shadow_open_through_real_verifier(monkeypatch):
    """P0-2 positive control: a complete nonempty chain is first accepted by
    the REAL verifier with this run's exit registry/binding, then the REAL
    replay in SHADOW_OPERATIONAL mode admits the OPEN — the qualification is
    not misrecorded missing/invalid by a verifier call defect."""
    from ak_trading.v2 import artifacts as a
    from ak_trading.v2.artifacts import verify_qualification_evidence
    from ak_trading.v2.decision.pipeline import exit_registry_hash_for,registry_hash_for
    c,bundle,models,reference,pm,_bound,exit_specs,bindings,exit_registry,model_hashes=_shadow_setup(monkeypatch)
    evidence,qualification,contract_lock,registry,_met,_rev,_exp=_qualification_chain(
        c.spec,exit_registry=exit_registry,binding=bindings[0],model_hashes=model_hashes,
        issued_at_nano=100*NS,valid_until_nano=1000*NS)
    assert registry==registry_hash_for((c.spec,))
    assert qualification.exit_registry_sha256==exit_registry==exit_registry_hash_for(exit_specs)
    assert {qualification.report_sha256,qualification.review_sha256,
            qualification.experiment_sha256}<=set(evidence)
    for key in (qualification.report_sha256,qualification.review_sha256,qualification.experiment_sha256):
        assert a.sha256_hex(evidence[key])==key
    verify_qualification_evidence(qualification,c.spec,evidence,contract_lock,registry,
        exit_registry_sha256=exit_registry,exit_binding=bindings[0])
    result=_run_shadow(c,bundle,models,reference,pm,(qualification,),evidence,
        exit_specs,bindings,contract_lock,start=100*NS,end=102*NS)
    entry=next(d for d in result.decisions if d.decision_nano==100*NS)
    assert entry.purpose=="SHADOW_OPERATIONAL" and entry.selected_proposal_id is not None
    assert entry.qualification_ids==(qualification.qualification_id,)
    proposal=next(p for p in entry.proposals if p.proposal_id==entry.selected_proposal_id)
    assert entry.weights[proposal.proposal_id]==1.0 and entry.weights["CASH"]==0.0
    assert result.fills and result.fills[0].offset=="OPEN" and result.fills[0].execution_nano==101*NS
    tokens=("QUALIFICATION_MISMATCH","QUALIFICATION_EVIDENCE_MISSING","QUALIFICATION_EVIDENCE_INVALID",
            "QUALIFICATION_EXPIRED","STATISTICS_NOT_PASSED")
    assert not [r for d in result.decisions for g in d.gates for r in g.reasons if r in tokens]


def test_broken_qualification_evidence_rejected_by_real_verifier_with_zero_new_risk(monkeypatch):
    """Negative controls on the same tradable fixture: missing files, tampered
    bytes, semantically narrowed re-hashed scopes and exit-dependency
    mismatches are rejected by the REAL verifier with a visible reason, and
    the replay marks them invalid with zero added risk weight."""
    import pytest
    from dataclasses import replace as dc_replace
    from ak_trading.v2 import artifacts as a
    from ak_trading.v2.artifacts import ArtifactsError,verify_qualification_evidence
    from ak_trading.v2.contracts import ExitPolicyBinding,exit_binding_content_hash
    from ak_trading.v2.decision.pipeline import registry_hash_for
    from ak_trading.v2.strategies.registry import build_strategy_spec as _bss
    c,bundle,models,reference,pm,_bound,exit_specs,bindings,exit_registry,model_hashes=_shadow_setup(monkeypatch)
    def chain(**overrides):
        kwargs=dict(exit_registry=exit_registry,binding=bindings[0],model_hashes=model_hashes,
                    issued_at_nano=100*NS,valid_until_nano=1000*NS)
        kwargs.update(overrides)
        return _qualification_chain(c.spec,**kwargs)
    forged_values=dict(strategy_id=c.spec.strategy_id,
        entry_spec_sha256=_bss("WAIT","MINUTE").code_sha256,
        exit_spec_sha256s=bindings[0].exit_spec_sha256s,sizing_spec_sha256=None,
        priority=bindings[0].priority,legacy_rules_version=bindings[0].legacy_rules_version)
    forged=ExitPolicyBinding(binding_sha256=exit_binding_content_hash(**forged_values),**forged_values)
    cases=[]
    evidence,qualification,lock,reg,_m,_r,_e=chain()
    del evidence[qualification.experiment_sha256]
    cases.append(("missing_file",evidence,qualification,bindings,
                  "required_evidence_file_missing","QUALIFICATION_EVIDENCE_MISSING"))
    evidence,qualification,lock,reg,metrics,review,experiment=chain()
    evidence[qualification.report_sha256]=b"tampered-synthetic-bytes"
    cases.append(("tampered_bytes",evidence,qualification,bindings,
                  "evidence_hash_mismatch","QUALIFICATION_EVIDENCE_INVALID"))
    evidence,qualification,lock,reg,metrics,review,experiment=chain()
    groups=list(metrics["strategy_groups"])
    groups[0]={**groups[0],"status":"FAIL"}
    report=a.canonical_json_bytes({**metrics,"strategy_groups":groups})
    review2={**review,"report_sha256":a.sha256_hex(report)}
    review_bytes2=a.canonical_json_bytes(review2)
    evidence={a.sha256_hex(report):report,a.sha256_hex(review_bytes2):review_bytes2,
        qualification.experiment_sha256:evidence[qualification.experiment_sha256],
        lock:evidence[lock],reg:evidence[reg]}
    qualification=dc_replace(qualification,report_sha256=a.sha256_hex(report),
        review_sha256=a.sha256_hex(review_bytes2))
    cases.append(("rehashed_scope_narrowed",evidence,qualification,bindings,
                  "scope_group_not_passing","QUALIFICATION_EVIDENCE_INVALID"))
    evidence,qualification,lock,reg,_m,_r,_e=chain(binding=forged)
    cases.append(("wrong_entry_attribution",evidence,qualification,(forged,),
                  "exit_binding_entry_spec_mismatch","QUALIFICATION_EVIDENCE_INVALID"))
    evidence,qualification,lock,reg,_m,_r,_e=chain()
    cases.append(("other_exit_version",evidence,dc_replace(qualification,exit_binding_sha256="9"*64),
                  bindings,"qualification_exit_binding_mismatch","QUALIFICATION_EVIDENCE_INVALID"))
    evidence,qualification,lock,reg,_m,_r,_e=chain()
    cases.append(("other_exit_registry",evidence,dc_replace(qualification,exit_registry_sha256="9"*64),
                  bindings,"qualification_exit_registry_mismatch","QUALIFICATION_EVIDENCE_INVALID"))
    evidence,qualification,lock,reg,_m,_r,_e=chain()
    cases.append(("other_strategy_spec",evidence,dc_replace(qualification,code_sha256="0"*64),
                  bindings,"qualification_spec_mismatch","QUALIFICATION_EVIDENCE_INVALID"))
    registry=registry_hash_for((c.spec,))
    for label,ev,qual,run_bindings,verifier_reason,replay_token in cases:
        with pytest.raises(ArtifactsError,match=verifier_reason):
            verify_qualification_evidence(qual,c.spec,ev,lock,registry,
                exit_registry_sha256=exit_registry,exit_binding=run_bindings[0])
        result=_run_shadow(c,bundle,models,reference,pm,(qual,),ev,exit_specs,run_bindings,lock,
                           start=100*NS,end=102*NS)
        decision=next(d for d in result.decisions if d.decision_nano==100*NS)
        gate=decision.gates[0]
        assert not gate.eligible,(label)
        assert {"QUALIFICATION_MISMATCH",replay_token}<=set(gate.reasons),(label)
        assert decision.selected_proposal_id is None,(label)
        assert decision.weights["CASH"]==1.0,(label)
        assert decision.weights[decision.proposals[0].proposal_id]==0.0,(label)
        assert result.intents==() and result.fills==(),(label)


def test_exit_bindings_missing_extra_or_folded_duplicates_rejected_at_boundary():
    """Replay boundary rejects binding sets that are missing, extra, or carry
    two bindings for one strategy — the last would otherwise be silently
    folded by a dict and masquerade as a unique binding (007 §5.4)."""
    import pytest
    from ak_trading.v2.contracts import ExitPolicyBinding,exit_binding_content_hash
    from ak_trading.v2.decision.pipeline import legacy_exit_binding
    from ak_trading.v2.decision.replay import run_decision_replay
    from ak_trading.v2.policy import research_policy
    from test_v2_m4_ledger import account,run_policy,exit_fixtures_for
    specs=(build_strategy_spec("WAIT","MINUTE"),)
    exit_specs,bindings,_registry=exit_fixtures_for(specs)
    bundle,models,payoff,reference=replay_inputs()
    def attempt(exit_bindings):
        return run_decision_replay(bundle,models,payoff,specs,account("100000"),run_policy(),(),{},
            research_policy(),None,reference,HASH,None,70*NS,75*NS,
            exit_specs=exit_specs,exit_bindings=exit_bindings)
    with pytest.raises(ValueError,match="exit_bindings_must_match_specs_exactly"):
        attempt(())
    with pytest.raises(ValueError,match="exit_bindings_must_match_specs_exactly"):
        attempt(tuple(bindings)+(legacy_exit_binding(build_strategy_spec("TREND","MINUTE")),))
    with pytest.raises(ValueError,match="duplicate_exit_bindings"):
        attempt(tuple(bindings)+tuple(bindings))
    folded_values=dict(strategy_id=specs[0].strategy_id,entry_spec_sha256=specs[0].code_sha256,
        exit_spec_sha256s=bindings[0].exit_spec_sha256s,sizing_spec_sha256=None,
        priority=("exit-legacy-folded-counterexample",),
        legacy_rules_version=bindings[0].legacy_rules_version)
    folded=ExitPolicyBinding(binding_sha256=exit_binding_content_hash(**folded_values),**folded_values)
    with pytest.raises(ValueError,match="duplicate_exit_bindings"):
        attempt((bindings[0],folded))


def test_qualification_validity_window_boundaries_block_only_new_risk(monkeypatch):
    """`issued_at <= decision < valid_until` per the original contract: expired
    or not-yet-effective qualifications add no risk weight (inclusive lower,
    exclusive upper bound); the evidence chain itself stays verifier-valid."""
    from ak_trading.v2.artifacts import verify_qualification_evidence
    from ak_trading.v2.decision.pipeline import registry_hash_for
    c,bundle,models,reference,pm,_bound,exit_specs,bindings,exit_registry,model_hashes=_shadow_setup(
        monkeypatch,bar_minutes=range(70,110),entry_nano=103*NS)
    registry=registry_hash_for((c.spec,))
    def windowed(issued,valid):
        return _qualification_chain(c.spec,exit_registry=exit_registry,binding=bindings[0],
            model_hashes=model_hashes,issued_at_nano=issued,valid_until_nano=valid)
    evidence,expired,lock,_reg,_m,_r,_e=windowed(1,103*NS)
    verify_qualification_evidence(expired,c.spec,evidence,lock,registry,
        exit_registry_sha256=exit_registry,exit_binding=bindings[0])
    for qualification in (expired,windowed(104*NS,200*NS)[1]):
        ev,qual,lock2,_r,_m,_x,_e=windowed(qualification.issued_at_nano,qualification.valid_until_nano)
        result=_run_shadow(c,bundle,models,reference,pm,(qual,),ev,exit_specs,bindings,lock2,
                           start=103*NS,end=105*NS)
        decision=next(d for d in result.decisions if d.decision_nano==103*NS)
        assert "QUALIFICATION_EXPIRED" in decision.gates[0].reasons
        assert decision.selected_proposal_id is None
        assert decision.weights[decision.proposals[0].proposal_id]==0.0
        assert decision.weights["CASH"]==1.0
        assert result.intents==() and result.fills==()
    evidence,inclusive,lock,_reg,_m,_r,_e=windowed(103*NS,106*NS)
    result=_run_shadow(c,bundle,models,reference,pm,(inclusive,),evidence,exit_specs,bindings,lock,
                       start=103*NS,end=106*NS)
    assert result.fills and result.fills[0].offset=="OPEN" and result.fills[0].execution_nano==104*NS
    evidence,edge,lock,_reg,_m,_r,_e=windowed(103*NS,104*NS)
    result=_run_shadow(c,bundle,models,reference,pm,(edge,),evidence,exit_specs,bindings,lock,
                       start=103*NS,end=106*NS)
    admitted=[d for d in result.decisions if d.selected_proposal_id is not None]
    assert admitted and admitted[0].weights["CASH"]==0.0
    assert not result.fills
    assert any("MODEL_EXPIRED" in r.reasons for r in result.execution_results)


def test_expired_qualification_still_leaves_timer_exit_channel_open(monkeypatch):
    """After mid-run expiry no new OPEN is added, the blocked gates show the
    rejection, and the pre-deadline risk-timer exit still closes the held
    position — the REDUCE/EXIT safe channel never depended on qualification."""
    c,bundle,models,reference,pm,_bound,exit_specs,bindings,exit_registry,model_hashes=_shadow_setup(
        monkeypatch,bar_minutes=range(70,140))
    evidence,qualification,lock,_reg,_m,_r,_e=_qualification_chain(
        c.spec,exit_registry=exit_registry,binding=bindings[0],model_hashes=model_hashes,
        issued_at_nano=100*NS,valid_until_nano=102*NS)
    result=_run_shadow(c,bundle,models,reference,pm,(qualification,),evidence,exit_specs,bindings,lock,
                       start=100*NS,end=134*NS)
    opens=[f for f in result.fills if f.offset=="OPEN"]
    closes=[f for f in result.fills if f.offset!="OPEN"]
    assert opens and opens[0].execution_nano==101*NS
    assert all(f.execution_nano<102*NS for f in opens)
    # no new entry risk is even proposed after expiry (the gate's SHADOW
    # qualification block only ever applied to OPEN/ADD; HOLD/WAIT gates
    # return early by original contract).
    assert not [i for i in result.intents if i.offset=="OPEN" and i.created_at_nano>=102*NS]
    timer=[d for d in result.decisions if "RISK_TIMER_EXIT" in d.reasons]
    assert timer and timer[0].outcome=="MANAGE_POSITION"
    assert closes and all(f.execution_nano>=timer[0].decision_nano+NS for f in closes)
    assert not result.equity[-1].lots and result.status=="COMPLETE"
