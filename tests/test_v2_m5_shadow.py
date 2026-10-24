from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import asyncio
import pytest

from ak_trading.v2.contracts import FrozenInput
from ak_trading.v2.schemas import decode_shared_fixture
from ak_trading.v2.validation.shadow import (FeedEvent,FeedMonitor,ReplayClock,SECOND,
                                           group_acceptance,validate_clock_evidence)
from ak_trading.v2.validation.experiments import digest


def fixture():
    p=Path(__file__).parent/"fixtures/v2/shared.json"
    f=decode_shared_fixture(p.read_bytes())
    return f,FrozenInput(p.parent,digest(p.read_bytes().hex()),f["instruments"],f["sessions"],f["bars"],f["macro_items"],f["rules"])


def bar_event(sequence=1):
    f,b=fixture()
    bar=f["bars"][0]
    return b,FeedEvent(f"event{sequence}",sequence,"BAR",bar.end_nano,bar.end_nano,bar,(),"SYNTHETIC_M5")


def test_feed_protocol_no_order_surface_and_bar_shape():
    b,e=bar_event()
    with pytest.raises(ValueError,match="BAR_requires"):
        replace(e,bar=None)
    with pytest.raises(ValueError,match="BAR_requires"):
        replace(e,kind="TIMER")
    assert not any("order" in n or "trade" in n for n in dir(e))


def test_source_zero_delay_cannot_replace_receiver_latency():
    b,e=bar_event()
    c=ReplayClock(e.bar.end_nano+121*SECOND)
    row=FeedMonitor(b).receive(e,c)
    assert "DATA_STALE" in row.reasons
    assert row.visible_bar.available_at_nano==c.current
    assert row.event.bar.available_at_nano==e.bar.available_at_nano
    assert row.event.received_at_nano==e.received_at_nano
    assert not row.clock_verified


def test_duplicate_conflict_and_unfinished_last_bar():
    b,e=bar_event()
    monitor=FeedMonitor(b);c=ReplayClock(e.bar.end_nano+SECOND)
    monitor.receive(e,c)
    assert "DUPLICATE" in monitor.receive(e,c).reasons
    with pytest.raises(ValueError,match="conflicting_duplicate"):
        monitor.receive(replace(e,source="changed"),c)
    assert "BAR_INCOMPLETE" in FeedMonitor(b).receive(replace(e,bar=replace(e.bar,complete=False)),c).reasons


def test_identity_mapping_clock_reversal_and_disconnect():
    b,e=bar_event();c=ReplayClock(e.bar.end_nano+SECOND);m=FeedMonitor(b)
    row=m.receive(replace(e,bar=replace(e.bar,raw_symbol="wrong")),c)
    assert row.visible_bar is None and "IDENTITY_INVALID" in row.reasons
    m.last_bar_end[e.bar.contract_id]=e.bar.end_nano
    assert m.can_add_risk(e.bar.contract_id,c.current)
    m.receive(FeedEvent("down",2,"DISCONNECT",c.current,c.current,None,(),e.source),c)
    assert not m.can_add_risk(e.bar.contract_id,c.current)
    c.current-=1
    with pytest.raises(ValueError,match="clock_reversed"):
        m.receive(e,c)


def test_two_second_buffer_only_unpublished_events():
    b,e=bar_event();m=FeedMonitor(b);c=ReplayClock(e.bar.end_nano+SECOND)
    row=m.receive(e,c)
    assert m.buffered(row)==()
    c.advance(e.bar.end_nano+3*SECOND)
    timer=FeedEvent("timer",2,"TIMER",c.current,c.current,None,(),e.source)
    flushed=m.buffered(m.receive(timer,c))
    assert flushed[0].event.event_id==e.event_id
    assert len(flushed)==2


def test_fixed_publication_denominator_missing_and_retry():
    f,b=fixture();p=f["forecasts"][0];m=FeedMonitor(b)
    m.register_request(p.request_id,p.window_start_nano,p.window_end_nano)
    m.register_request("missing",p.window_start_nano,p.window_end_nano)
    late=replace(p,published_at_nano=p.window_start_nano,expires_at_nano=p.window_end_nano,
                 quality=replace(p.quality,status="INVALID",reasons=("FORECAST_LATE",)))
    m.publish(late);m.publish(p);m.publish(late)
    assert m.publication_counts()==(0,2)
    with pytest.raises(ValueError,match="window_changed"):
        m.register_request(p.request_id,p.window_start_nano+1,p.window_end_nano+1)


def test_each_session_group_must_pass_twenty_days_and_p99():
    days=set(range(20))
    good=group_acceptance("SHFE.RB","DAY",days,1000,1000,days,[1.0]*995,995,995,True)
    assert good["passed"]
    night=group_acceptance("SHFE.RB","NIGHT",days,1000,1000,set(range(19)),[1.0]*994+[9.0]*6,1000,1000,True)
    assert not night["passed"]
    unverified=group_acceptance("SHFE.RB","DAY",days,1000,1000,days,[0.0]*1000,1000,1000,False)
    assert not unverified["passed"]
    empty=group_acceptance("SHFE.RB","DAY",days,1000,1000,set(),[],0,0,False)
    assert "NO_LATENCY_OBSERVATIONS" in empty["reasons"]


def test_raw_clock_evidence_hash_and_synthetic_never_verified():
    c=ReplayClock(1_700_000_000*SECOND)
    e=dict(c.evidence())
    assert validate_clock_evidence(e,c.current)[1] is False
    e["raw_evidence"]="forged"
    with pytest.raises(ValueError,match="raw_evidence_hash"):
        validate_clock_evidence(e,c.current)


def initial_checkpoint():
    from ak_trading.v2.contracts import LedgerCheckpoint,RunPolicy
    f,b=fixture()
    p=RunPolicy("RESEARCH_EVALUATION","CASH",7,None,None,"research-policy-v2.1.0","macro-risk-v2.1.0","PRICE_ONLY","BASE")
    cp=LedgerCheckpoint(0,"1"*64,"2"*64,"6"*64,p,{}, {"forecast":"3"*64,"payoff":"4"*64,"regime_reference":"5"*64},{},
                        f["accounts"][0],(),(),(),(),(),(),{},(),{},{},"0"*64,"0"*64)
    return replace(cp,checkpoint_hash=digest({k:v for k,v in __import__('ak_trading.v2.validation.experiments',fromlist=['content']).content(cp).items() if k!="checkpoint_hash"}))


def test_journal_only_committed_checkpoint_restores_and_old_bytes_unchanged(tmp_path):
    from ak_trading.v2.validation.shadow import ShadowJournal,recover_segment,verify_committed_entities
    from ak_trading.v2.validation.experiments import atomic_write
    from ak_trading.v2.schemas import canonical_json_bytes
    f,b=fixture();cp=initial_checkpoint();segment=tmp_path/"segment"
    journal=ShadowJournal(segment,cp,cp.run_policy)
    clock=ReplayClock(cp.account.as_of_nano+1);monitor=FeedMonitor(b)
    event=FeedEvent("timer",1,"TIMER",clock.current,clock.current,None,(),"SYNTHETIC")
    row=monitor.receive(event,clock)
    atomic_write(segment/"clock_evidence"/(row.clock_evidence_sha256+".json"),canonical_json_bytes(clock.evidence()))
    committed=replace(cp,sequence=1,processed_event_ids=("timer",))
    committed=replace(committed,checkpoint_hash=digest({k:v for k,v in __import__('ak_trading.v2.validation.experiments',fromlist=['content']).content(committed).items() if k!="checkpoint_hash"}))
    journal.commit(row,committed,{})
    # A fully written candidate with no event commit must never become state.
    orphan=replace(committed,sequence=2,checkpoint_hash="f"*64)
    journal.append_records("checkpoints","uncommitted",(orphan,))
    with (segment/"fills.jsonl").open("ab") as handle:handle.write(b'{"event_id":"uncommitted","record":{"fill_id":"conflict"}}\nBROKEN_TAIL')
    before={p.name:p.read_bytes() for p in segment.glob("*.jsonl")}
    restored,last,tail=recover_segment(segment)
    assert restored==committed and tail is not None
    verify_committed_entities(restored,(segment,),b)
    assert {p.name:p.read_bytes() for p in segment.glob("*.jsonl")}==before
    assert restored.account==cp.account


def test_recovery_missing_fill_original_is_failure(tmp_path):
    from ak_trading.v2.validation.shadow import ShadowJournal,verify_committed_entities
    cp=initial_checkpoint();f,b=fixture();segment=tmp_path/"segment"
    ShadowJournal(segment,cp,cp.run_policy)
    cp=replace(cp,processed_fill_hashes={"closed-ancestor-fill":"a"*64})
    with pytest.raises(ValueError,match="original_set"):
        verify_committed_entities(cp,(segment,),b)


def test_model_activation_external_injection_forbidden_and_sources_frozen():
    from ak_trading.v2.validation.shadow import ModelActivationRequest
    b,e=bar_event();clock=ReplayClock(e.received_at_nano)
    activation=replace(e,kind="MODEL_ACTIVATION",bar=None,source="M5:model-activation")
    with pytest.raises(ValueError,match="external_feed"):
        FeedMonitor(b).receive(activation,clock)
    hashes={"forecast":"1"*64,"payoff":"2"*64,"regime_reference":"3"*64}
    request=ModelActivationRequest("4"*64,100,"5"*64,hashes,hashes,{"q":"6"*64},{"experiment":"5"*64})
    hashes["forecast"]="7"*64
    assert request.old_model_hashes["forecast"]=="1"*64
    with pytest.raises(ValueError,match="new_qualifications"):
        replace(request,new_qualification_hashes={})


def test_actual_timer_without_bar_keeps_position_and_creates_exit_intent():
    from decimal import Decimal as D
    from ak_trading.v2.contracts import Fill,LayerSpec,RoundBudget,StrategyState
    from ak_trading.v2.policy import stress_policy,research_policy
    from ak_trading.v2.decision.ledger import ContinuousLedger
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model
    from ak_trading.v2.probability.models import ForecastModelSet,default_fit_config
    from ak_trading.v2.probability.regimes import RegimeReference
    from ak_trading.v2.macro import MacroCache
    from ak_trading.v2.strategies.registry import build_strategy_spec
    from ak_trading.v2.validation.shadow import _process_event
    f,b=fixture();cp=initial_checkpoint();t=cp.account.as_of_nano;spec=build_strategy_spec("TREND","MINUTE")
    rule=replace(b.rules[0],slippage_ticks=D(0),impact_ticks=D(0),limit_down=D(2900),limit_up=D(3200))
    price=D(100);leg=rule.open_fee
    fee=leg.per_lot_cny+price*rule.multiplier*leg.notional_rate
    fill=Fill("timer-entry","entry",rule.contract_id,spec.strategy_id,"timer-episode",t,cp.account.trading_day,1,1,"OPEN",price,price,fee,D(0),rule.rule_id,"SCENARIO")
    from ak_trading.v2.strategies.registry import select_exit_binding as _seb2
    _eb2=_seb2(spec)
    book=ContinuousLedger(replace(cp.account,as_of_nano=t-1),b.instruments,cp.run_policy,cp.contract_lock_sha256,cp.registry_sha256,exit_registry_sha256=cp.exit_registry_sha256)
    book.record_event("seed-timer",{})
    _seed=book.checkpoint((),(),cp.model_hashes,{},pending_remaining_lots={},exit_registry_sha256=cp.exit_registry_sha256,exit_bindings=(_eb2,))
    book=ContinuousLedger.restore(_seed,b.instruments,b.rules,cp.run_policy,cp.contract_lock_sha256,cp.registry_sha256,cp.model_hashes,{},exit_registry_sha256=cp.exit_registry_sha256)
    assert book.apply_fill(fill,rule)
    budget=RoundBudget("timer-budget","timer-episode",rule.contract_id,t,None,None,stress_policy(),D(1),1,(LayerSpec(0,price,price,1),),
                       D(99),D(102),t+60*SECOND,1,D(100),D(500),D(1000),D(20000),D(1000000))
    state=StrategyState(spec.strategy_id,"timer-episode",rule.contract_id,"HOLDING",None,None,1,(0,),(1,),None,t+60*SECOND,None,(),budget,1,None,None)
    original=book.checkpoint((state,),(),cp.model_hashes,{},pending_remaining_lots={},exit_registry_sha256=cp.exit_registry_sha256,exit_bindings=(_eb2,))
    now=t+61*SECOND;clock=ReplayClock(now);monitor=FeedMonitor(b)
    row=monitor.receive(FeedEvent("timer-expired",1,"TIMER",now,now,None,(),"SYNTHETIC"),clock)
    model=ForecastModelSet("UNUSED-TIMER-MODEL","UNUSED-REFERENCE",(),t-20,t-10,t+1000*SECOND,default_fit_config((),t-20,t-10,t+1000*SECOND),{},(),{})
    reference=RegimeReference("UNUSED-REFERENCE",t-20,t-20,{}, {})
    payoff=fit_path_payoff_model((),t-20,"research-policy-v2.1.0")
    states={(state.contract_id,state.strategy_id):state};pending={}
    monitor.exit_registry_sha256=cp.exit_registry_sha256
    result,records=_process_event(row,replace(b,rules=(rule,)),book,states,pending,model,payoff,None,reference,(spec,),cp.run_policy,
                                  research_policy(),(),clock,monitor,MacroCache(()),cp.model_hashes,{},{})
    assert result.account.lots==original.account.lots and result.account.fees_cny==fee
    assert records["intents"] and all(i.offset!="OPEN" for i in records["intents"])
    assert not records.get("fills")
    assert states[(state.contract_id,state.strategy_id)].phase=="EXITING"
    assert states[(state.contract_id,state.strategy_id)].layer_filled_lots==(1,)
    assert states[(state.contract_id,state.strategy_id)].budget==budget


def test_next_completed_bar_execution_never_backfills_trigger_bar():
    from decimal import Decimal as D
    from ak_trading.v2.contracts import OrderIntent,Fill,LayerSpec,RoundBudget,StrategyState
    from ak_trading.v2.policy import stress_policy,research_policy
    from ak_trading.v2.decision.ledger import ContinuousLedger
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model
    from ak_trading.v2.probability.models import ForecastModelSet,default_fit_config
    from ak_trading.v2.probability.regimes import RegimeReference
    from ak_trading.v2.macro import MacroCache
    from ak_trading.v2.strategies.registry import build_strategy_spec
    from ak_trading.v2.validation.shadow import FeedEvent,FeedMonitor,ReplayClock,_process_event
    f,b=fixture();cp=initial_checkpoint();spec=build_strategy_spec("TREND","MINUTE")
    rule=replace(b.rules[0],slippage_ticks=D(0),impact_ticks=D(0),limit_down=D(2900),limit_up=D(3200))
    t=cp.account.as_of_nano;contract=rule.contract_id
    bar_source=f["bars"][0]
    def minute(index):
        start=bar_source.start_nano+index*60*SECOND
        return replace(bar_source,bar_id=f"next-bar-{index}",start_nano=start,end_nano=start+60*SECOND,datetime_nano=start,
                       trading_day=bar_source.trading_day,session_id=bar_source.session_id,
                       available_at_nano=start+60*SECOND,imported_at_nano=start+60*SECOND)
    trigger=replace(minute(21),open=D(3000),high=D(3010),low=D(2990),close=D(3000))
    seen=replace(minute(22),open=D(3001),high=D(3011),low=D(2991),close=D(3001))
    nxt=replace(minute(23),open=D(3002),high=D(3012),low=D(2992),close=D(3002))
    warmup=tuple(replace(minute(21-i),volume=D(1000),open=D(3000+i),high=D(3010+i),low=D(2990+i),close=D(3000+i)) for i in range(20,0,-1))
    instrument=replace(b.instruments[0],contract_id=contract,raw_symbol=bar_source.raw_symbol)
    bundle=replace(b,instruments=(instrument,),rules=(rule,),bars=(*warmup,trigger,seen,nxt))
    intent=OrderIntent("queued","decision",spec.strategy_id,"episode-next",contract,seen.start_nano,
                       seen.start_nano+1,
                       max(nxt.end_nano+2*SECOND,nxt.start_nano+1),1,"OPEN","ENTRY",0,None)
    price=trigger.close
    prior_close=warmup[-1]
    book=ContinuousLedger(replace(cp.account,as_of_nano=min(seen.start_nano,prior_close.end_nano)),(instrument,),cp.run_policy,cp.contract_lock_sha256,cp.registry_sha256,exit_registry_sha256=cp.exit_registry_sha256)
    book.mark({trigger.contract_id:prior_close},max(prior_close.end_nano,seen.start_nano),cp.account.trading_day,(rule,))
    history={**{b.bar_id:b for b in warmup},trigger.bar_id:trigger,seen.bar_id:seen,nxt.bar_id:nxt}
    from ak_trading.v2.strategies.registry import select_exit_binding as _seb3
    _eb3=_seb3(spec)
    book.record_event("seed",{});original=book.checkpoint((),(),cp.model_hashes,{},pending_remaining_lots={},exit_registry_sha256=cp.exit_registry_sha256,exit_bindings=(_eb3,))
    budget=RoundBudget("queued-budget","episode-next",contract,trigger.end_nano,None,None,stress_policy(),D(2),1,
                       (LayerSpec(0,price,price,1),),price-D(2),price+D(2),nxt.end_nano+2*SECOND,1,
                       D(20),D(50),D(100),D(20000),cp.account.equity_cny)
    assert rule.limit_down<=price<=rule.limit_up
    states={(contract,spec.strategy_id):StrategyState(spec.strategy_id,"episode-next",contract,"WAIT_TRIGGER",None,None,1,(),(0,),None,None,None,("queued",),budget,0,None,None)}
    horizon=seen.start_nano+1000*SECOND
    model=ForecastModelSet("UNUSED","UNUSED-REF",(),seen.start_nano-20*SECOND,seen.start_nano-10*SECOND,horizon,default_fit_config((),seen.start_nano-20*SECOND,seen.start_nano-10*SECOND,horizon),{},(),{})
    reference=RegimeReference("UNUSED-REF",seen.start_nano-20*SECOND,seen.start_nano-20*SECOND,{}, {})
    payoff=fit_path_payoff_model((),seen.start_nano-10*SECOND,"research-policy-v2.1.0")
    clock=ReplayClock(seen.start_nano+SECOND);monitor=FeedMonitor(bundle)
    pending={"queued":intent};remaining={"queued":1}
    row=monitor.receive(FeedEvent("trigger",1,"BAR",trigger.end_nano,trigger.end_nano,trigger,(),"SYNTHETIC"),clock)
    monitor.last_bar_end[contract]=trigger.end_nano
    monitor.pending_remaining_lots={"queued":1}
    monitor.last_account_nano=trigger.end_nano
    # The trigger bar precedes the earliest legal time; the next completed slot
    # is what both schedules and validates the intent for execution.
    clock.advance(seen.end_nano+SECOND)
    row=monitor.receive(FeedEvent("trigger-seen",2,"BAR",seen.end_nano,seen.end_nano,seen,(),"SYNTHETIC"),clock)
    monitor.last_bar_end[contract]=seen.end_nano
    checkpoint_trigger,records=_process_event(row,bundle,book,states,pending,model,payoff,None,reference,(spec,),cp.run_policy,
                                              research_policy(),(),clock,monitor,MacroCache(()),cp.model_hashes,{},history)
    assert not records.get("fills") and monitor.execution_queue[(contract,spec.strategy_id)][0].bar_id==seen.bar_id
    # Simulate restart: restore from the committed trigger checkpoint so the
    # pending-remaining mapping is carried by a real checkpoint, not test state.
    from ak_trading.v2.decision.ledger import ContinuousLedger as Ledger
    restored=Ledger.restore(checkpoint_trigger,(instrument,),(rule,),cp.run_policy,
                            checkpoint_trigger.contract_lock_sha256,checkpoint_trigger.registry_sha256,
                            checkpoint_trigger.model_hashes,{},exit_registry_sha256=checkpoint_trigger.exit_registry_sha256)
    states={(s.contract_id,s.strategy_id):s for s in checkpoint_trigger.states}
    pending={i.intent_id:i for i in checkpoint_trigger.pending_intents}
    monitor.pending_remaining_lots=dict(checkpoint_trigger.pending_remaining_lots)
    clock.advance(nxt.end_nano+SECOND)
    row=monitor.receive(FeedEvent("next",3,"BAR",nxt.end_nano,nxt.end_nano,nxt,(),"SYNTHETIC"),clock)
    monitor.last_bar_end[contract]=nxt.end_nano
    checkpoint_next,records=_process_event(row,bundle,restored,states,pending,model,payoff,None,reference,(spec,),cp.run_policy,
                                           research_policy(),(),clock,monitor,MacroCache(()),cp.model_hashes,{},
                                           {**history,row.visible_bar.bar_id:row.visible_bar})
    fills=records.get("fills",())
    assert fills and fills[0].execution_nano==nxt.start_nano
    assert fills[0].execution_nano>=checkpoint_trigger.account.as_of_nano
    assert fills[0].fee_cny==rule.open_fee.per_lot_cny+fills[0].execution_price*rule.multiplier*rule.open_fee.notional_rate
    assert not monitor.execution_queue
    assert "queued" not in pending


def test_actual_m4_checkpoint_activation_preserves_funds_and_refuses_unregistered_change():
    from ak_trading.v2.decision.ledger import ContinuousLedger
    f,b=fixture();initial=initial_checkpoint()
    ledger=ContinuousLedger(initial.account,b.instruments,initial.run_policy,initial.contract_lock_sha256,initial.registry_sha256,exit_registry_sha256=initial.exit_registry_sha256)
    old=ledger.checkpoint((),(),initial.model_hashes,{},pending_remaining_lots={},exit_registry_sha256=initial.exit_registry_sha256)
    new_models={**dict(initial.model_hashes),"forecast":"a"*64}
    with pytest.raises(ValueError):
        ledger.checkpoint((),(),new_models,{},pending_remaining_lots={},exit_registry_sha256=initial.exit_registry_sha256)
    restored=ContinuousLedger.restore(old,b.instruments,b.rules,old.run_policy,old.contract_lock_sha256,old.registry_sha256,old.model_hashes,{},exit_registry_sha256=old.exit_registry_sha256)
    before=restored.snapshot(old.account.as_of_nano)
    restored.record_event("MODEL_ACTIVATION:"+"b"*64,{})
    activated=restored.checkpoint((),(),new_models,{"qualified":"c"*64},pending_remaining_lots={},exit_registry_sha256=initial.exit_registry_sha256)
    assert activated.account==before and activated.processed_fill_hashes==old.processed_fill_hashes
    again=ContinuousLedger.restore(activated,b.instruments,b.rules,activated.run_policy,activated.contract_lock_sha256,activated.registry_sha256,new_models,{"qualified":"c"*64},exit_registry_sha256=activated.exit_registry_sha256)
    assert again.snapshot(before.as_of_nano)==before
    assert activated.processed_event_ids.count("MODEL_ACTIVATION:"+"b"*64)==1


def test_model_activation_rejects_unreleased_resource_and_missing_originals():
    from decimal import Decimal
    from ak_trading.v2.validation.shadow import ModelActivationRequest,verify_activation_request
    cp=initial_checkpoint();hashes=dict(cp.model_hashes)
    request=ModelActivationRequest(cp.checkpoint_hash,cp.account.as_of_nano,"5"*64,hashes,hashes,{"q":"6"*64},{"experiment":"5"*64})
    locked=replace(cp,account=replace(cp.account,reserved_margin_cny=Decimal(1)))
    request=replace(request,previous_checkpoint_hash=locked.checkpoint_hash)
    with pytest.raises(ValueError,match="flat_unreserved"):
        verify_activation_request(request,locked,hashes,(),{},cp.account.as_of_nano,cp.account.as_of_nano+10)
    with pytest.raises(ValueError,match="experiment_original_missing"):
        verify_activation_request(request,cp,hashes,(),{},cp.account.as_of_nano,cp.account.as_of_nano+10)


def test_hanging_macro_transport_does_not_block_minute_cache_snapshots():
    from ak_trading.v2.macro import MacroCache,refresh_macro_cache
    class Gate:
        def __await__(self):
            yield "TRANSPORT_PENDING"
            return {}
    class Transport:
        async def call_tool(self,name,arguments):
            return await Gate()
    cache=MacroCache(())
    pending=refresh_macro_cache(Transport(),cache,100,lambda:101)
    try:
        assert pending.send(None)=="TRANSPORT_PENDING"
        contexts=[cache.snapshot(102+i,"SHFE.RB",True,False) for i in range(10)]
        assert len(contexts)==10 and all(c.mode=="BLOCKED" for c in contexts)
    finally:
        pending.close()
