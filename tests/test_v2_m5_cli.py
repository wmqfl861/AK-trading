from __future__ import annotations

from pathlib import Path
import importlib.util
import pytest

from ak_trading.v2.schemas import parse_json_bytes,canonical_json_bytes
from ak_trading.v2.validation.experiments import parse_config,ExperimentError

ROOT=Path(__file__).resolve().parents[1]


def cli():
    spec=importlib.util.spec_from_file_location("m5_cli",ROOT/"scripts/research_probability_v2.py")
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def config():
    return parse_json_bytes((ROOT/"tests/fixtures/v2/m5_research.json").read_bytes())


@pytest.fixture(scope="module")
def trained_sparse_world(tmp_path_factory):
    """SYNTHETIC sparse unit cohort, not full-grid research or feed acceptance."""
    from datetime import date,timedelta
    from decimal import Decimal as D
    from ak_trading.v2.contracts import (DataQuality,FactorValue,FeatureSnapshot,ForecastRequest,PathLabel,RegimeAssessment,Horizon,MarketBar,SessionWindow)
    from ak_trading.v2.probability.models import default_fit_config,fit_forecast_models
    from ak_trading.v2.probability.regimes import fit_regime_reference
    from ak_trading.v2.probability.forecast import build_forecast_requests
    from ak_trading.v2.probability.labels import label_forecast
    from ak_trading.v2.validation.experiments import digest
    ns=10**9;day_ns=86400*ns;base=1700000000*ns
    product="SHFE.RB";contract="SYNTHETIC_UNIT:rb"
    sessions=tuple(SessionWindow(f"day-{i}",product,date(2024,1,1)+timedelta(days=i),base+i*day_ns,base+i*day_ns+180*60*ns,"DAY","SYNTHETIC-SPARSE-UNIT","SCENARIO") for i in range(320))
    records=[]
    for i,session in enumerate(sessions):
        at=session.start_nano+60*ns-1
        momentum=(-1.0,0.0,1.0)[i%3]
        q=DataQuality("READY",("SYNTHETIC_SPARSE_UNIT",),"reconstructed",digest([i,momentum]))
        features=FeatureSnapshot(f"snapshot-{i}",contract,"SHFE.rb9901",product,session.trading_day,session.session_id,"DAY",at,at,60,(),D(100),D(2),
                                 (FactorValue("META:price_tick:v2.1.0",D('.1'),at,q),FactorValue("UNIT:momentum",momentum,at,q)),None,q)
        reqs=build_forecast_requests(features,(Horizon("TRADING_MINUTE",1),),None,sessions)
        start,end=reqs[0].window_start_nano,reqs[0].window_end_nano
        close=D(100)+D(str(momentum))*D(3)
        bar=MarketBar(f"bar-{i}",contract,"SHFE.rb9901",product,60,start,start,end,session.trading_day,session.session_id,end,end,D(100),max(D(100),close)+D('.1'),min(D(100),close)-D('.1'),close,D(100),D(100),D(100),True,1,1,"SYNTHETIC_UNIT",q)
        labels=tuple(label_forecast(r,(bar,),sessions,(),end) for r in reqs)
        records.append((features,reqs,labels))
    def parts(lo,hi):
        selected=records[lo:hi]
        return tuple(x[0] for x in selected),tuple(r for x in selected for r in x[1]),tuple(l for x in selected for l in x[2])
    training=parts(0,252);calibration=parts(253,313)
    train_end=max(l.available_at_nano for l in training[2]);cal_end=max(l.available_at_nano for l in calibration[2])
    reference=fit_regime_reference(training[0],train_end,train_end)
    def regimes(fs,ref):
        return tuple(RegimeAssessment("regime-"+f.snapshot_id,f.snapshot_id,ref,f.decision_nano,"UNKNOWN",(),None,"UNKNOWN",0,("SYNTHETIC",),f.quality) for f in fs)
    fit=default_fit_config(("UNIT:momentum",),train_end,cal_end,cal_end+30*day_ns)
    models=fit_forecast_models(training[0],regimes(training[0],"inner-prefix"),training[1],training[2],
                               calibration[0],regimes(calibration[0],reference.reference_id),calibration[1],calibration[2],fit,reference,seasonality=None)
    feature,requests,labels=records[315]
    regime=regimes((feature,),reference.reference_id)[0]
    from ak_trading.v2.schemas import encode_record_file
    from ak_trading.v2.validation.experiments import content
    artifacts=tmp_path_factory.mktemp("m5-real-components")
    (artifacts/"models.json").write_bytes(canonical_json_bytes({"schema_version":"ak_probability_models_v2.1.0","model_set":content(models)}))
    (artifacts/"regime_reference.json").write_bytes(canonical_json_bytes({"schema_version":"ak_probability_regime_reference_v2.1.0","reference":content(reference)}))
    (artifacts/"training_features.json").write_bytes(encode_record_file("FeatureSnapshot","ak_probability_features_v2.1.0",training[0]))
    (artifacts/"training_requests.json").write_bytes(encode_record_file("ForecastRequest","ak_probability_requests_v2.1.0",training[1]))
    (artifacts/"training_labels.json").write_bytes(encode_record_file("PathLabel","ak_probability_labels_v2.1.0",training[2]))
    (artifacts/"calibration_labels.json").write_bytes(encode_record_file("PathLabel","ak_probability_labels_v2.1.0",calibration[2]))
    (artifacts/"evidence_scope.json").write_bytes(canonical_json_bytes({"status":"DRAFT_NOT_ACCEPTED","dataset_kind":"SYNTHETIC",
        "scope":"SPARSE_COMPONENT_UNIT","training_days":252,"calibration_days":60,"full_minute_grid":False,
        "statistics":"INSUFFICIENT","runtime":"OFFLINE_ONLY","m5_complete":False,"contract_lock_sha256":None}))
    print("M5_COMPONENT_ARTIFACTS="+str(artifacts))
    return models,reference,feature,requests,labels,regime,sessions


def test_real_fit_publish_and_score_sparse_engineering_path(trained_sparse_world,tmp_path):
    from ak_trading.v2.probability.forecast import publish_market_forecast
    from ak_trading.v2.validation.metrics import evaluate_forecasts
    models,reference,features,requests,labels,regime,sessions=trained_sparse_world
    assert models.models and any(str(m["fit_status"]).startswith("SELECTED") for m in models.models.values())
    forecasts=tuple(publish_market_forecast(models,r,regime,clock_nano=lambda:features.decision_nano) for r in requests)
    assert len(forecasts)==3 and all(f.model_id==models.model_set_id for f in forecasts)
    metrics=evaluate_forecasts(forecasts,labels,{r.request_id:str(features.trading_day) for r in requests},7)
    assert metrics["forecast_groups"] and all(g["status"]=="INSUFFICIENT" for g in metrics["forecast_groups"])
    assert not any(g["blocks"]>1 for g in metrics["forecast_groups"])
    from ak_trading.v2.schemas import encode_record_file
    (tmp_path/"forecasts.json").write_bytes(encode_record_file("MarketForecast","ak_probability_forecasts_v2.1.0",forecasts))
    (tmp_path/"labels.json").write_bytes(encode_record_file("PathLabel","ak_probability_labels_v2.1.0",labels))
    (tmp_path/"actual_forecast_metrics.json").write_bytes(canonical_json_bytes(metrics))


def test_real_publication_clock_after_compute_preserves_late_window(trained_sparse_world):
    from ak_trading.v2.probability.forecast import publish_market_forecast
    from ak_trading.v2.validation.metrics import evaluate_forecasts
    models,_,features,requests,_,regime,_=trained_sparse_world
    request=requests[1]
    calls=[]
    def clock():
        calls.append("publication")
        return request.window_end_nano+1
    late=publish_market_forecast(models,request,regime,clock_nano=clock)
    assert calls==["publication"]
    assert late.quality.status=="INVALID" and "FORECAST_LATE" in late.quality.reasons
    assert late.expires_at_nano==request.window_end_nano
    assert evaluate_forecasts((late,),(),{},7)["exclusions"]=={"FORECAST_LATE":1}


def test_real_model_m4_gates_reject_statistical_failure_without_fake_qualification(trained_sparse_world):
    from dataclasses import replace
    from decimal import Decimal as D
    from ak_trading.v2.contracts import (AccountSnapshot,DataQuality,MacroContext,StrategyContext,RunPolicy)
    from ak_trading.v2.policy import confidence_policy,stress_policy,research_policy
    from ak_trading.v2.probability.forecast import publish_market_forecast
    from ak_trading.v2.strategies.registry import build_strategy_spec,strategy_registry_v2,select_exit_binding
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model,estimate_proposal_payoff
    from ak_trading.v2.decision.pipeline import model_hashes_for
    from ak_trading.v2.decision.gates import evaluate_gate
    from ak_trading.v2.decision.routing import choose_proposal
    from ak_trading.v2.validation.experiments import digest
    models,reference,features,requests,_,regime,sessions=trained_sparse_world
    forecasts=tuple(publish_market_forecast(models,r,regime,clock_nano=lambda:features.decision_nano) for r in requests)
    spec=build_strategy_spec("TREND","MINUTE")
    p=RunPolicy("SHADOW_OPERATIONAL","FIXED",7,spec.strategy_id,select_exit_binding(spec).binding_sha256,"research-policy-v2.1.0","macro-risk-v2.1.0","PRICE_ONLY","BASE")
    cash=D(1000000);zero=D(0);q=features.quality
    account=AccountSnapshot("SYNTHETIC_UNIT",features.decision_nano,features.trading_day,cash,cash,zero,zero,zero,zero,cash,zero,zero,cash,zero,cash,(),False,q)
    macro=MacroContext("price-only",features.decision_nano,(),p.macro_policy_version,(),"PRICE_ONLY",q)
    payoff=fit_path_payoff_model((),models.training_end_nano,p.research_policy_version)
    from ak_trading.v2.strategies.registry import select_exit_binding as _seb6
    context=StrategyContext(features.decision_nano,spec,_seb6(spec),features,forecasts,regime,macro,(),sessions,account,None,None,
                            confidence_policy(spec.family),stress_policy(),p,None,model_hashes_for(models,payoff,reference,None),
                            features.decision_nano+3600*10**9)
    proposal=strategy_registry_v2("MINUTE")[spec.strategy_id].evaluate(context,None)
    estimate=estimate_proposal_payoff(payoff,proposal,context,"opportunity")
    proposal=replace(proposal,payoff=estimate)
    gate=evaluate_gate(proposal,context,research_policy(),checked_at_nano=features.decision_nano)
    decision=choose_proposal((proposal,),(gate,),account,"opportunity",features.decision_nano,p,())
    assert estimate.status=="INVALID" and decision.outcome=="NO_TRADE"
    assert "EVIDENCE_IDENTITY_MISMATCH" in estimate.reasons
    assert proposal.action=="WAIT"
    assert decision.selected_proposal_id is None


def test_real_m2_to_m3a_request_labels_all_eight_horizons(trained_sparse_world):
    from dataclasses import replace
    from decimal import Decimal as D
    from ak_trading.v2.contracts import MarketBar,Horizon,TradingRule,FeeLeg
    from ak_trading.v2.factor_specs import factor_registry_v2,baseline_registry_v2
    from ak_trading.v2.factors import build_feature_snapshot
    from ak_trading.v2.baseline import baseline_values_v2
    from ak_trading.v2.probability.forecast import build_forecast_requests
    from ak_trading.v2.probability.labels import label_forecast
    _,_,features,_,_,_,sessions=trained_sparse_world
    current=sessions[315];ns=10**9
    rule=TradingRule("synthetic-rule",features.contract_id,sessions[0].start_nano,sessions[-1].end_nano+1,sessions[0].start_nano,D(10),D('.1'),1,1,
                     FeeLeg(D(1),D(0),False),FeeLeg(D(1),D(0),False),FeeLeg(D(1),D(0),False),D('.1'),D('.1'),D(1000),D(1),D('.1'),D(0),D('.1'),"SCENARIO","1"*64)
    bars=[]
    for i in range(120):
        start=current.start_nano+i*60*ns;close=D(100)+D(i%9)/10
        bars.append(MarketBar(f"m2-{i}",features.contract_id,features.raw_symbol,features.product,60,start,start,start+60*ns,current.trading_day,current.session_id,
                              start+60*ns,start+60*ns,close,close+D('.2'),close-D('.2'),close,D(100+i%5),D(1000),D(1000),True,1,1,"SYNTHETIC_UNIT",features.quality))
    decision=bars[80].end_nano
    snapshot=build_feature_snapshot(features.contract_id,tuple(bars),decision,factor_registry_v2(60)+baseline_registry_v2(60),None,None,rule,sessions)
    assert snapshot.atr_points is not None
    requested={s.factor_key for s in factor_registry_v2(60)+baseline_registry_v2(60)}
    assert len(requested)==25 and requested<={v.factor_key for v in snapshot.values}
    assert "BASE:opening_gap_points:v2.1.0:d60" in requested
    assert len({v.factor_key for v in snapshot.values})==len(snapshot.values)
    baseline=baseline_values_v2(tuple(bars),decision,sessions=sessions)
    assert len(baseline)==15
    minute=build_forecast_requests(snapshot,tuple(Horizon("TRADING_MINUTE",h) for h in (1,5,15,30,60)),None,sessions)
    assert len(minute)==15
    labels=tuple(label_forecast(r,tuple(b for b in bars if r.window_start_nano<=b.start_nano<r.window_end_nano),sessions,(),bars[-1].end_nano) for r in minute)
    assert any(l.status=="MATURE" for l in labels)
    # Daily bars retain their own calendar days; no minute sample fills a daily target.
    daily=[]
    for i,session in enumerate(sessions[:100]):
        close=D(100)+D(i%9)/10
        daily.append(MarketBar(f"daily-{i}",features.contract_id,features.raw_symbol,features.product,86400,session.start_nano,session.start_nano,session.end_nano,
                               session.trading_day,None,session.end_nano,session.end_nano,close,close+D('.2'),close-D('.2'),close,D(1000),D(10000),D(10000),True,180,180,"SYNTHETIC_UNIT",features.quality))
    decision=daily[80].end_nano
    snapshot=build_feature_snapshot(features.contract_id,tuple(daily),decision,factor_registry_v2(86400)+baseline_registry_v2(86400),None,None,rule,sessions)
    assert snapshot.atr_points is not None
    daily_requests=build_forecast_requests(snapshot,tuple(Horizon("TRADING_DAY",h) for h in (1,5,20)),None,sessions)
    assert len(daily_requests)==9
    labels=tuple(label_forecast(r,tuple(b for b in daily if r.window_start_nano<=b.start_nano<r.window_end_nano),sessions,(),daily[-1].end_nano) for r in daily_requests)
    assert {l.horizon.value for l in labels}=={1,5,20}
    assert any(l.status=="UNMATURED" for l in labels if l.horizon.value==20)


def test_replay_fold_receives_actual_configured_initial_cash(trained_sparse_world):
    from decimal import Decimal as D
    from ak_trading.v2.contracts import FrozenInput,Instrument,RunPolicy
    from ak_trading.v2.strategies.registry import build_strategy_spec,select_exit_binding
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model
    from ak_trading.v2.validation.experiments import _replay_fold,digest
    from ak_trading.v2.validation.splits import SplitSpec
    models,reference,f,_,_,_,sessions=trained_sparse_world
    instrument=Instrument(f.contract_id,f.raw_symbol,f.product,"synthetic",("synthetic",),sessions[0].start_nano,None,None,None,f.quality)
    bundle=FrozenInput(Path("SYNTHETIC_UNIT"),digest("empty-input"),(instrument,),sessions,(),(),())
    spec=build_strategy_spec("WAIT","MINUTE")
    policy=RunPolicy("RESEARCH_EVALUATION","CASH",7,None,None,"research-policy-v2.1.0","macro-risk-v2.1.0","PRICE_ONLY","BASE")
    start=sessions[315].start_nano
    split=SplitSpec("funds",sessions[0].start_nano,models.training_end_nano,models.training_end_nano+1,models.calibration_end_nano,
                    start,start+60*10**9,60,20,False)
    payoff=fit_path_payoff_model((),models.training_end_nano,policy.research_policy_version)
    result,_=_replay_fold(bundle,split,{"models":models,"reference":reference},payoff,(spec,),policy,"1"*64,D('123456.78'))
    assert result.equity and all(a.initial_cash_cny==D('123456.78') and a.cash_cny==D('123456.78') for a in result.equity)


def test_actual_shadow_unsupported_horizon_keeps_request_and_no_trade(trained_sparse_world,tmp_path):
    from dataclasses import replace
    from decimal import Decimal as D
    from ak_trading.v2.contracts import (Instrument,FrozenInput,MarketBar,TradingRule,FeeLeg,RunPolicy,AccountSnapshot)
    from ak_trading.v2.decision.ledger import ContinuousLedger
    from ak_trading.v2.decision.pipeline import model_hashes_for,registry_hash_for
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model
    from ak_trading.v2.strategies.registry import build_strategy_spec,select_exit_binding
    from ak_trading.v2.policy import research_policy
    from ak_trading.v2.macro import MacroCache
    from ak_trading.v2.validation.shadow import FeedEvent,FeedMonitor,ReplayClock,_process_event,ShadowJournal,verify_committed_entities
    from ak_trading.v2.validation.experiments import digest,atomic_write
    models,reference,f,_,_,_,sessions=trained_sparse_world
    session=sessions[315];ns=10**9
    instrument=Instrument(f.contract_id,f.raw_symbol,f.product,"synthetic",("synthetic",),sessions[0].start_nano,None,None,None,f.quality)
    leg=FeeLeg(D(1),D(0),False)
    rule=TradingRule("unit-rule",f.contract_id,sessions[0].start_nano,sessions[-1].end_nano+1,sessions[0].start_nano,D(10),D('.1'),1,1,
                     leg,leg,leg,D('.1'),D('.1'),D(1000),D(1),D(0),D(0),D('.1'),"SCENARIO","1"*64)
    bars=[]
    for i in range(90):
        start=session.start_nano+i*60*ns;close=D(100)+D(i%9)/10
        bars.append(MarketBar(f"shadow-{i}",f.contract_id,f.raw_symbol,f.product,60,start,start,start+60*ns,session.trading_day,session.session_id,
                              start+60*ns,start+60*ns,close,close+D('.2'),close-D('.2'),close,D(1000),D(1000),D(1000),True,1,1,"SYNTHETIC_UNIT",f.quality))
    spec=build_strategy_spec("TREND","MINUTE")
    policy=RunPolicy("SHADOW_OPERATIONAL","FIXED",7,spec.strategy_id,select_exit_binding(spec).binding_sha256,"research-policy-v2.1.0","macro-risk-v2.1.0","PRICE_ONLY","BASE")
    payoff=fit_path_payoff_model((),models.training_end_nano,policy.research_policy_version)
    hashes=model_hashes_for(models,payoff,reference,None)
    initial=bars[-2].end_nano;cash=D(1000000);zero=D(0)
    account=AccountSnapshot("shadow-component",initial,session.trading_day,cash,cash,zero,zero,zero,zero,cash,zero,zero,cash,zero,cash,(),False,f.quality)
    bundle=FrozenInput(Path("SYNTHETIC_UNIT"),digest("shadow-component"),(instrument,),sessions,tuple(bars),(),(rule,))
    from ak_trading.v2.validation.experiments import _exit_pack
    _reg,_es,_bs=_exit_pack((spec,))
    ledger=ContinuousLedger(account,(instrument,),policy,"2"*64,registry_hash_for((spec,)),exit_registry_sha256=_reg)
    cp=ledger.checkpoint((),(),hashes,{},pending_remaining_lots={},exit_registry_sha256=_reg,exit_bindings=_bs)
    clock=ReplayClock(bars[-1].end_nano+1);monitor=FeedMonitor(bundle)
    event=FeedEvent("bar-event",1,"BAR",bars[-1].end_nano,bars[-1].end_nano,bars[-1],(),"SYNTHETIC_UNIT")
    row=monitor.receive(event,clock)
    monitor.last_bar_end[f.contract_id]=bars[-1].end_nano
    visible=replace(bundle,bars=tuple((*bars[:-1],row.visible_bar)))
    final,records=_process_event(row,visible,ledger,{}, {},models,payoff,None,reference,(spec,),policy,research_policy(),(),clock,monitor,MacroCache(()),hashes,{},{})
    assert records["decisions"]
    assert all(d.outcome=="NO_TRADE" for d in records["decisions"])
    # M3A rev3 refuses unsupported horizons at the provider boundary. With the
    # sparse fixture model there is no supported minute request for this
    # context, so the registered denominator stays empty and NO_TRADE carries
    # generic model/gate reasons; nothing published, nothing filled either way.
    assert monitor.publication_counts()[0]==0
    assert not records.get("fills") and final.account.cash_cny==cash
    journal=ShadowJournal(tmp_path/"segment",cp,policy)
    atomic_write(tmp_path/"segment/clock_evidence"/(row.clock_evidence_sha256+".json"),canonical_json_bytes(clock.evidence()))
    journal.commit(row,final,records)
    verify_committed_entities(final,(tmp_path/"segment",),bundle)


def test_exact_fixture_config_and_no_hidden_sampling_fields():
    c=config();splits,policies,scope=parse_config(c)
    assert len(policies)==102 and len(splits)==1
    assert c["minute_horizons"]==[1,5,15,30,60] and c["daily_horizons"]==[1,5,20]
    with pytest.raises(ExperimentError,match="schema"):
        parse_config({**c,"hidden_model":{}})
    c["minute_horizons"]=[1]
    with pytest.raises(ExperimentError,match="values_changed"):
        parse_config(c)


def test_cli_mutual_exclusion_unknown_live_flag_and_existing_output(tmp_path):
    module=cli()
    assert module.main(["--mode","smoke","--fixture","x","--input-bundle","y","--config","z","--output",str(tmp_path/"x")])==2
    assert module.main(["--mode","live","--fixture","x","--config","z","--output",str(tmp_path/"x")])==2
    assert module.main(["--mode","smoke","--fixture","x","--config","z","--output",str(tmp_path)])==2
    assert not (tmp_path/"error.json").exists()


def test_missing_lock_is_stage_incomplete_and_never_fabricated(tmp_path):
    module=cli();c=config();c["contract_lock_path"]=str(tmp_path/"never-issued-lock.json")
    conf=tmp_path/"config.json";conf.write_bytes(canonical_json_bytes(c))
    output=tmp_path/"run"
    code=module.main(["--mode","smoke","--fixture",str(ROOT/"tests/fixtures/v2/shared.json"),"--config",str(conf),"--output",str(output)])
    assert code==3
    error=parse_json_bytes((output/"error.json").read_bytes())
    assert error["reason"]=="M1_CONTRACT_LOCK_NOT_PUBLISHED"
    assert not error["m5_complete"]
    assert not (tmp_path/"never-issued-lock.json").exists()


def test_shadow_requires_feed_and_reproduce_requires_reference(tmp_path):
    module=cli()
    common=["--fixture",str(ROOT/"tests/fixtures/v2/shared.json"),"--config",str(ROOT/"tests/fixtures/v2/m5_research.json")]
    assert module.main(["--mode","shadow-replay",*common,"--output",str(tmp_path/"s")])==2
    assert module.main(["--mode","reproduce",*common,"--output",str(tmp_path/"r")])==2


def test_short_fixture_feed_is_replay_only_and_preserves_raw_clock():
    from ak_trading.v2.validation.shadow import JsonlReplayFeed,ReplayClock
    feed=JsonlReplayFeed(ROOT/"tests/fixtures/v2/m5_stream.jsonl",ReplayClock(0))
    result=[]
    async def consume():
        async for event in feed:
            result.append(event)
    coroutine=consume()
    try:
        while True:assert coroutine.send(None) is None
    except StopIteration:pass
    finally:coroutine.close()
    assert len(result)==9
    assert {e.kind for e in result}>={"BAR","TIMER","DISCONNECT","RECONNECT"}
    assert all(e.source.startswith("SYNTHETIC") for e in result)
    assert feed.clock.evidence()["source_kind"]=="SYNTHETIC"


def test_t9_m5_r4_chunk_config_skeletons_are_new_series_with_todo_identity():
    """Arbitration 2026-09-17 RULING 4.2/E2 + E-P1 RULING 2.2 (EP1-M5): six
    frozen m5-r4 skeletons on the v2.1.3 research-config schema carrying the
    strict payoff_training_plan_sha256 registration field, no r2/r3 residue;
    identity fields stay explicit TODO markers until the ep1-r1 lock and the
    final plan set/source digest exist, and a skeleton must not be runnable
    as-is (no accidental start before the freeze)."""
    import json
    base=ROOT/"data/research_runs/v2/m5-development-20260914T041258Z/chunk-configs/m5-r4"
    files=sorted(base.glob("chunk0*.json"))
    assert [f.name for f in files]==[f"chunk{i:02d}.json" for i in range(1,7)]
    locks=set()
    for f in files:
        c=json.loads(f.read_bytes())
        assert c["run_id"]==f"m5-r4-smoke-{f.stem}"
        assert [s["split_id"] for s in c["split_specs"]]==[f"r4-{f.stem}"]
        assert set(c["model_fit_configs"])=={f"r4-{f.stem}"}
        # E-P1 RULING 2.2: the strict plan-set registration field exists on
        # the v2.1.3 schema and is itself a freeze-time TODO (the real value
        # is the 64-hex SHA-256 of the canonical frozen plan-set bytes).
        assert c["schema_version"]=="ak_probability_research_config_v2.1.3"
        assert c["payoff_training_plan_sha256"].startswith("TODO-M5-R4-FREEZE:payoff-training-plan-sha256")
        blob=json.dumps(c)
        assert "m5-r2-smoke" not in blob and "m5-r3-smoke" not in blob
        assert '"r2-chunk' not in blob and '"r2-fold"' not in blob
        assert c["contract_lock_path"].startswith("TODO-M5-R4-FREEZE:")
        locks.add(c["contract_lock_path"])
        fixed=[p for p in c["run_policies"] if p["routing_mode"]=="FIXED"]
        assert fixed and all(p["fixed_exit_binding_sha256"].startswith("TODO-M5-R4-FREEZE:")
                             for p in fixed)
        non_fixed=[p for p in c["run_policies"] if p["routing_mode"]!="FIXED"]
        assert all(p["fixed_exit_binding_sha256"] is None for p in non_fixed)
        strategy_keys=[k for k in c["required_group_keys"] if json.loads(k)[0]=="STRATEGY"]
        assert strategy_keys and all("TODO-M5-R4-FREEZE:run-policy-sha256" in k for k in strategy_keys)
        # a skeleton cannot pass parse_config yet — the TODO plan-set marker,
        # binding and policy markers are not legal hash fields, so nothing
        # can start these runs before the freeze
        from ak_trading.v2.schemas import parse_json_bytes
        with pytest.raises((ExperimentError,ValueError)):
            parse_config(parse_json_bytes(f.read_bytes()))
        # ... and the v2.1.3 schema is strictly one field richer: dropping
        # the plan-set field is a schema mismatch, not a silent legacy read
        legacy={k:v for k,v in c.items() if k!="payoff_training_plan_sha256"}
        with pytest.raises(ExperimentError,match="schema"):
            parse_config(legacy)
    assert len(locks)==1  # all six bind one and the same future lock
