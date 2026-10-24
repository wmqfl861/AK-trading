from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from functools import lru_cache
from pathlib import Path

import pytest

from ak_trading.v2.decision.pipeline import build_strategy_context
from ak_trading.v2.decision.replay import replay_payloads
from ak_trading.v2.strategies.registry import build_strategy_spec
from test_v2_m4_ledger import NS,account,bar,rule,run_policy
from ak_trading.v2.decision.pipeline import legacy_exit_binding
from test_v2_m4_replay import replay_inputs,run


def test_extreme_future_append_keeps_all_historical_record_bytes():
    bundle,models,payoff,reference=replay_inputs()
    prefix=replace(bundle,bars=tuple(b for b in bundle.bars if b.start_nano<80*NS))
    future=replace(bundle,manifest_sha256="f"*64,bars=prefix.bars+(bar(t=81*NS,price="99999"),))
    a=run(prefix,models,payoff,reference)
    b=run(future,models,payoff,reference)
    assert replay_payloads(a,payoff)==replay_payloads(b,payoff)
    assert a.features==b.features and a.forecasts==b.forecasts and a.checkpoints==b.checkpoints


def test_pipeline_consumes_real_m2_baseline_and_does_not_fit():
    bundle,models,payoff,reference=replay_inputs()
    spec=build_strategy_spec("TREND","MINUTE")
    from ak_trading.v2.probability.forecast import predict_market
    c=build_strategy_context(bundle,"rb",70*NS,account("100000"),None,models,payoff,spec,None,reference,run_policy(),(),
        exit_binding=legacy_exit_binding(spec),
        forecast_provider=lambda models,request,regime:predict_market(models,request,regime,request.features.decision_nano))
    values={v.factor_key:v for v in c.features.values}
    assert "BASE:ma20:v2.1.0:d60" in values and "MOM001:v2.1.0:d60:m1-n60" in values
    assert c.features.reference_price==next(b.close for b in bundle.bars if b.end_nano==70*NS)
    assert all(b.end_nano<=70*NS and b.available_at_nano<=70*NS for b in c.bars)
    assert c.model_hashes["forecast"]==models.content_hash()
    from ak_trading.v2.factor_specs import factor_registry_v2,baseline_registry_v2
    from ak_trading.v2.factors import build_feature_snapshot
    from ak_trading.v2.data import visible_bars,visible_rule
    raw=build_feature_snapshot("rb",visible_bars(bundle,"rb",60,70*NS,True),70*NS,
        factor_registry_v2(60)+baseline_registry_v2(60),None,None,visible_rule(bundle,"rb",70*NS),bundle.sessions)
    assert raw==c.features
    from ak_trading.v2.factor_specs import baseline_registry_v2 as _breg
    expected_base={s.factor_key for s in _breg(60)}
    actual_base={v.factor_key for v in c.features.values if v.factor_key.startswith("BASE:")}
    assert actual_base==expected_base
    assert "BASE:opening_gap_points:v2.1.0:d60" in expected_base


def test_missing_seasonal_never_falls_back_to_raw_features():
    bundle,models,payoff,reference=replay_inputs()
    models=replace(models,feature_keys=("SEASONAL:volume_relative:v2.1.1:d60",))
    with pytest.raises(ValueError,match="seasonality_source_missing_for_selected_columns"):
        run(bundle,models,payoff,reference)


def _seasonal_handle():
    from ak_trading.v2.seasonality import fit_seasonality
    from test_v2_m4_ledger import DAY
    bundle,_,_,_=replay_inputs()
    return fit_seasonality(bundle.bars,bundle.sessions,NS,50*NS)


def test_seasonality_binding_four_combinations_and_forged_fourth_key():
    from ak_trading.v2.decision.pipeline import model_hashes_for
    bundle,models,payoff,reference=replay_inputs()
    seasonal=_seasonal_handle()
    bound=replace(models,source_hashes={**dict(models.source_hashes),
        "seasonality":seasonal.content_hash()})
    # B=True / K=False is legal: the transform exists, the predictor skipped it.
    hashes=model_hashes_for(bound,payoff,reference,seasonal)
    assert set(hashes)=={"forecast","payoff","regime_reference","seasonality"}
    assert hashes["seasonality"]==seasonal.content_hash()
    # B=False / handle None: exactly three keys.
    assert set(model_hashes_for(models,payoff,reference,None))=={"forecast","payoff","regime_reference"}
    # Missing fourth key: bound source but no handle.
    with pytest.raises(ValueError,match="bound_seasonality_requires_handle"):
        model_hashes_for(bound,payoff,reference,None)
    # Forged fourth key: recorded value is not the handle's content hash.
    forged=replace(bound,source_hashes={**dict(bound.source_hashes),"seasonality":"e"*64})
    with pytest.raises(ValueError,match="seasonality_source_hash_mismatch"):
        model_hashes_for(forged,payoff,reference,seasonal)
    # B=False while a handle is supplied.
    with pytest.raises(ValueError,match="unbound_seasonality_handle_rejected"):
        model_hashes_for(models,payoff,reference,seasonal)
    # Recorded regime_reference source must match the actual reference content.
    tampered=replace(models,source_hashes={"regime_reference":"c"*64})
    with pytest.raises(ValueError,match="regime_reference_source_hash_mismatch"):
        model_hashes_for(tampered,payoff,reference,None)


def test_daily_shared_collection_keeps_bound_source_without_seasonal_columns():
    from ak_trading.v2.decision.pipeline import build_strategy_context,model_hashes_for
    from ak_trading.v2.probability.forecast import predict_market
    bundle,models,payoff,reference=replay_inputs()
    seasonal=_seasonal_handle()
    bound=replace(models,source_hashes={**dict(models.source_hashes),
        "seasonality":seasonal.content_hash()})
    spec=build_strategy_spec("TREND","DAILY")
    from test_v2_m4_ledger import bar as minute_bar
    daily_bars=(replace(minute_bar(t=40*NS,price="100"),duration_seconds=86400,
        start_nano=40*NS,end_nano=45*NS,available_at_nano=45*NS,session_id=None),)
    from ak_trading.v2.contracts import FrozenInput
    daily_bundle=replace(bundle,bars=bundle.bars+daily_bars)
    c=build_strategy_context(daily_bundle,"rb",70*NS,account("100000"),None,bound,payoff,spec,seasonal,
        reference,run_policy(),(),exit_binding=legacy_exit_binding(spec),
        forecast_provider=lambda m,request,regime:predict_market(m,request,regime,request.features.decision_nano))
    assert not any(v.factor_key.startswith("SEASONAL:") for v in c.features.values)
    assert c.model_hashes["seasonality"]==seasonal.content_hash()
    assert set(model_hashes_for(bound,payoff,reference,seasonal))=={
        "forecast","payoff","regime_reference","seasonality"}


def test_strategy_registry_hash_is_not_a_factor_registry_hash():
    from ak_trading.v2.decision.pipeline import registry_hash_for
    from ak_trading.v2.strategies.registry import strategy_specs_v2
    from ak_trading.v2.factor_specs import factor_registry_v2
    from ak_trading.v2.schemas import encode_record
    from ak_trading.v2.artifacts import canonical_json_bytes,sha256_hex
    specs=strategy_specs_v2("MINUTE")
    base=registry_hash_for(specs)
    assert base==registry_hash_for(tuple(reversed(specs)))
    factor_hash=sha256_hex(canonical_json_bytes([encode_record(s) for s in factor_registry_v2(60)]))
    assert base!=factor_hash
    changed=replace(specs[0],validation_stage="VALIDATED")
    assert registry_hash_for((changed,)+specs[1:])!=base


def test_future_model_and_test_payoff_cannot_enter_decision():
    bundle,models,payoff,reference=replay_inputs()
    with pytest.raises(ValueError,match="model_not_available"):
        run(bundle,replace(models,training_end_nano=71*NS,calibration_end_nano=72*NS),payoff,reference)


def test_provider_is_required_missing_publication_and_late_records():
    from ak_trading.v2.probability.forecast import predict_market,ForecastError
    from ak_trading.v2.contracts import MarketForecast,TimeProbability,DataQuality
    from test_v2_m4_ledger import HASH as FIXED
    bundle,models,payoff,reference=replay_inputs()
    spec=build_strategy_spec("TREND","MINUTE")
    args=(bundle,"rb",70*NS,account("100000"),None,models,payoff,spec,None,reference,run_policy(),())
    kwargs={"exit_binding":legacy_exit_binding(spec)}
    with pytest.raises(TypeError,match="forecast_provider"):
        build_strategy_context(*args,**kwargs)
    calls=[]
    def research_provider(m,request,regime):
        calls.append(request)
        return predict_market(m,request,regime,request.features.decision_nano)
    c=build_strategy_context(*args,**kwargs,forecast_provider=research_provider)
    # The synthetic model cannot publish: recorded as missing publications,
    # never fabricated into forecasts and never crashing the host.
    assert len(calls)==3 and c.forecasts==()
    keys=("Q05","Q25","Q50","Q75","Q95")
    def late_provider(m,request,regime):
        f=request.features
        returns=(-.02,-.01,0.,.01,.02)
        points=[D(str(r))*f.reference_price for r in returns]
        atrs=[float(p/f.atr_points) for p in points]
        return MarketForecast(f"forecast-{request.barrier_key}",request.request_id,f.snapshot_id,
            f.contract_id,f.product,f.trading_day,f.session_id,f.session_kind,f.duration_seconds,
            regime.assessment_id,regime.base,"reference",f.decision_nano,request.window_start_nano+1,
            request.window_end_nano,request.horizon,request.window_start_nano,request.window_end_nano,
            request.barrier_key,f.reference_price,f.atr_points,request.upper_distance,request.lower_distance,
            request.frozen_range,"path-label-v2.1.0","model",{"UP":.6,"DOWN":.3,"NEUTRAL":.1},
            dict(zip(keys,returns)),dict(zip(keys,points)),
            dict(zip(keys,atrs)),{"UP_FIRST":.6,"DOWN_FIRST":.4,"NEITHER":0.,"AMBIGUOUS":0.},
            (),{"UP_TREND":.85}, {"LONG":.05,"SHORT":.05},None,(),False,
            DataQuality("INVALID",("FORECAST_LATE",),"reconstructed",FIXED))
    c2=build_strategy_context(*args,**kwargs,forecast_provider=late_provider)
    assert len(c2.forecasts)==3
    assert all(f.quality.status=="INVALID" and "FORECAST_LATE" in f.quality.reasons for f in c2.forecasts)
    assert all(f.published_at_nano==f.window_start_nano+1 and f.expires_at_nano==f.window_end_nano
               for f in c2.forecasts)


def test_unpublishable_daily_background_records_missing_publication_without_crash(monkeypatch):
    """F10: a present daily background whose forecast cannot be published is a
    recorded missing publication for that original request; the host continues
    toward NO_TRADE instead of crashing (004 §4)."""
    from ak_trading.v2.decision.pipeline import build_strategy_context
    from ak_trading.v2.probability.forecast import predict_market
    from ak_trading.v2.strategies.registry import build_strategy_spec
    from ak_trading.v2.contracts import SessionWindow
    from test_v2_m4_ledger import instrument
    from datetime import date,timedelta
    bundle,models,payoff,reference=replay_inputs()
    days=[]; day=date(2026,3,2)
    while len(days)<23:
        if day.weekday()<5:
            days.append(day)
        day+=timedelta(days=1)
    starts=[int(datetime(d.year,d.month,d.day,1,tzinfo=timezone.utc).timestamp())*10**9 for d in days]
    daily=tuple(replace(bar(t=starts[i],price=str(100+i)),bar_id=f"bg-daily-{i}",duration_seconds=86400,
        trading_day=days[i],session_id=None,end_nano=starts[i]+240*NS,available_at_nano=starts[i]+240*NS,
        high=D(102+i),low=D(98+i)) for i in range(22))
    minute=tuple(replace(bar(t=starts[22]+i*NS,price=str(D(120)+D(i)/10)),session_id="bg-session-22",
        trading_day=days[22],bar_id=f"bg-minute-{i}") for i in range(151))
    sessions=tuple(SessionWindow(f"bg-session-{i}","SHFE.RB",days[i],s,s+240*NS,"DAY","bg-synthetic","SCENARIO")
                   for i,s in enumerate(starts))
    horizon=starts[-1]+241*NS
    bundle=replace(bundle,sessions=sessions,bars=daily+minute,
        instruments=(replace(instrument(),expiry_nano=horizon,last_trade_nano=horizon,
                             delivery_cutoff_nano=horizon),),
        rules=(rule(effective_until_nano=horizon),))
    models=replace(models,valid_until_nano=horizon)
    now=starts[22]+70*NS
    calls=[]
    import ak_trading.v2.decision.pipeline as pipeline
    feature_calls=[]
    real_features=pipeline._features
    def track_features(*args,**kwargs):
        feature_calls.append((args[3],kwargs.get("canonical_daily",False)))
        return real_features(*args,**kwargs)
    monkeypatch.setattr(pipeline,"_features",track_features)
    def provider(m,request,regime):
        calls.append(request.features.duration_seconds)
        return predict_market(m,request,regime,request.features.decision_nano)
    context=build_strategy_context(bundle,"rb",now,account("100000"),None,models,payoff,
        build_strategy_spec("TREND","MINUTE"),None,reference,run_policy(),(),
        exit_binding=legacy_exit_binding(build_strategy_spec("TREND","MINUTE")),forecast_provider=provider)
    assert 86400 in calls
    assert context.forecasts==()
    assert {duration for duration,_ in feature_calls}=={60,86400}
    assert not any(enabled for _,enabled in feature_calls)
    result=run(bundle,models,payoff,reference,start=now,end=now+3*NS)
    assert all(d.selected_proposal_id is None for d in result.decisions)
    assert result.status in ("COMPLETE","INCOMPLETE")


@lru_cache(maxsize=1)
def synthetic_daily_source():
    """Declared SYNTHETIC calendar and candles, available before any fit."""
    from ak_trading.v2.artifacts import sha256_hex
    from ak_trading.v2.contracts import FrozenInput, SessionWindow
    from test_v2_m4_ledger import instrument
    second=1_000_000_000
    days=[]; cursor=date(2022,1,3)
    # A declared SCENARIO closure demonstrates trading-day, not civil-day,
    # age. No claim is made about a historical exchange holiday.
    scenario_closure=date(2024,4,8)
    while len(days)<650:
        if cursor.weekday()<5 and cursor!=scenario_closure:
            days.append(cursor)
        cursor+=timedelta(days=1)
    starts=[int(datetime(d.year,d.month,d.day,1,tzinfo=timezone.utc).timestamp())*second for d in days]
    sessions=tuple(SessionWindow(f"SYNTHETIC-daily-{i}","SHFE.RB",d,starts[i],starts[i]+NS,
                                 "DAY","SYNTHETIC-daily-calendar","SCENARIO") for i,d in enumerate(days))
    bars=tuple(bar(t=starts[i],price=str(D(100)+D(i)/10),bar_id=f"SYNTHETIC-daily-bar-{i}",
                   duration_seconds=86400,trading_day=d,session_id=sessions[i].session_id,
                   # Coherent SYNTHETIC zero-overnight-gap candles: the
                   # next open equals the prior close; growth is intraday.
                   open=D(100)+D(max(i-1,0))/10,
                   high=D(100)+D(i)/10+D(".4"),low=D(100)+D(i)/10-D(".4"),
                   volume=D(10000)) for i,d in enumerate(days))
    until=sessions[-1].end_nano+second
    bundle=FrozenInput(Path("SYNTHETIC-DAILY20-NATURAL-CHAIN"),sha256_hex(b"SYNTHETIC-DAILY20-NATURAL-CHAIN"),
        (replace(instrument(),expiry_nano=until,last_trade_nano=until,delivery_cutoff_nano=until),),
        sessions,bars,(),(rule(effective_until_nano=until,limit_up=D(1000)),))
    return bundle,starts


@lru_cache(maxsize=1)
def synthetic_daily_world():
    """SYNTHETIC source, fitted models and measured calibration statistics.

    Real M2 features, M3A fitting and calibration produce the legal model
    cells. No READY forecast, candidate or payoff observation is injected.
    This fixture supplies engineering evidence only, never historical PIT
    or statistical qualification.
    """
    from ak_trading.v2.contracts import Horizon, MacroContext, PathLabel
    from ak_trading.v2.decision.pipeline import _features
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model
    from ak_trading.v2.probability.forecast import build_forecast_requests
    from ak_trading.v2.probability.labels import LABEL_VERSION
    from ak_trading.v2.probability.models import default_fit_config, fit_forecast_models
    from ak_trading.v2.probability.regimes import RegimeReference, assess_regime, freeze_range
    from test_v2_m4_ledger import quality
    bundle,starts=synthetic_daily_source()
    sessions=bundle.sessions; bars=bundle.bars
    second=1_000_000_000
    until=sessions[-1].end_nano+second
    reference=RegimeReference("SYNTHETIC-daily-reference",starts[0],starts[0],
                             {"SHFE.RB|DAILY|86400":(.001,.01,.1)}, {})
    horizons=tuple(Horizon("TRADING_DAY",n) for n in (1,5,20))
    def segment(indices):
        features=[]; regimes=[]; requests=[]; labels=[]
        for i in indices:
            snapshot,_,_=_features(bundle,"rb",bars[i].available_at_nano,86400,None,None,sessions)
            frozen=freeze_range(snapshot)
            macro=MacroContext(f"SYNTHETIC-training-macro-{i}",snapshot.decision_nano,(),
                               "macro-risk-v2.1.0",(),"PRICE_ONLY",quality())
            regime=assess_regime(snapshot,macro,None,frozen,D(0),reference,None)
            features.append(snapshot); regimes.append(regime)
            for request in build_forecast_requests(snapshot,horizons,frozen,sessions):
                requests.append(request)
                terminal=D(request.horizon.value)/10
                labels.append(PathLabel(request.request_id,"rb",LABEL_VERSION,request.horizon,
                    request.window_start_nano,request.window_end_nano,request.window_end_nano+second,
                    "MATURE",("SYNTHETIC_MODEL_TRAINING",),"UP",float(terminal/snapshot.reference_price),
                    terminal,float(terminal/snapshot.atr_points),"UP_FIRST",1,None,None,None,"NEITHER",
                    {"UP_TREND":True,"DOWN_TREND":False,"ORDERED_RANGE":False,"WIDE_RANGE":False,
                     "COMPRESSION":False,"UP_BREAKOUT":True,"DOWN_BREAKOUT":False},
                    {"LONG":False,"SHORT":True},frozen.range_id))
        return tuple(features),tuple(regimes),tuple(requests),tuple(labels)
    training=segment(range(60,310)); calibration=segment(range(335,535))
    train_end=max(label.available_at_nano for label in training[3])
    calibration_end=max(label.available_at_nano for label in calibration[3])
    # Small optimizer limits keep the deterministic constant-target fixture
    # quick; the frozen confidence/sample/block thresholds remain unchanged.
    config=default_fit_config(("META:price_tick:v2.1.0",),train_end,calibration_end,until,
                              max_iterations=20,tree_count=2)
    models=fit_forecast_models(*training,*calibration,config,reference,seasonality=None)
    payoff=fit_path_payoff_model((),train_end,"research-policy-v2.1.0")
    return bundle,models,payoff,reference


def daily_context(*, index=591, bundle=None, models=None, reference=None, previous=None,
                  family="TREND", transform=None):
    """Real pipeline/provider with explicit negative-only fault injection."""
    from ak_trading.v2.probability.forecast import predict_market
    world=synthetic_daily_world()
    bundle=world[0] if bundle is None else bundle
    models=world[1] if models is None else models
    reference=world[3] if reference is None else reference
    spec=build_strategy_spec(family,"DAILY")
    calls=[]
    def provider(model,request,regime):
        calls.append((request,regime))
        forecast=predict_market(model,request,regime,request.features.decision_nano)
        return transform(forecast,request) if transform is not None and request.horizon.value==20 else forecast
    now=world[0].bars[index].available_at_nano
    context=build_strategy_context(bundle,"rb",now,account("100000"),previous,models,world[2],spec,
        None,reference,run_policy(),(),exit_binding=legacy_exit_binding(spec),forecast_provider=provider)
    return context,calls


def daily_proposals(*, bundle=None, models=None, transform=None):
    from ak_trading.v2.strategies.registry import strategy_registry_v2
    from ak_trading.v2.strategies.state import commit_observation
    previous=None; rows=[]
    strategy=strategy_registry_v2("DAILY")[build_strategy_spec("TREND","DAILY").strategy_id]
    for i in range(589,593):
        context,calls=daily_context(index=i,bundle=bundle,models=models,previous=previous,transform=transform)
        proposal=strategy.evaluate(context,previous)
        previous=commit_observation(previous,proposal)
        rows.append((context,proposal,calls))
    return rows


def test_daily20_real_cell_is_prior_source_and_main_remains_daily5():
    from ak_trading.v2.contracts import Horizon
    from ak_trading.v2.policy import research_policy
    from ak_trading.v2.probability.forecast import predict_market, build_forecast_requests
    context,calls=daily_context()
    bg_request,bg_regime=calls[0]
    bg=predict_market(synthetic_daily_world()[1],bg_request,bg_regime,bg_request.features.decision_nano)
    assert context.features.quality.status=="READY"
    assert context.regime.base=="UP_TREND" and context.regime.cross_timeframe=="ALIGNED"
    assert context.features.daily_background_id==bg_request.features.snapshot_id
    assert bg_request.features.trading_day<context.features.trading_day
    assert bg_request.features.source_bar_ids[-1]=="SYNTHETIC-daily-bar-590"
    assert bg_request.features.decision_nano==synthetic_daily_world()[0].bars[590].available_at_nano
    assert bg.horizon==Horizon("TRADING_DAY",20) and bg.barrier_key=="ATR_1" and bg.quality.status=="READY"
    assert len(calls)==4 and [r.horizon.value for r,_ in calls]==[20,5,5,5]
    assert all(f.horizon==Horizon("TRADING_DAY",5) for f in context.forecasts)
    assert bg.request_id not in {f.request_id for f in context.forecasts}
    assert bg.assessment_id!=context.regime.assessment_id
    assert bg.snapshot_id!=context.features.snapshot_id
    assert bg.window_end_nano>max(f.window_end_nano for f in context.forecasts)
    assert research_policy()["daily_background_max_trading_days"]==1
    # Independently publish DAY1/5/20 from this SAME legal original source;
    # they have distinct cells, requests, windows and calibration evidence.
    requests=build_forecast_requests(bg_request.features,tuple(Horizon("TRADING_DAY",n) for n in (1,5,20)),
                                     bg_request.frozen_range,context.sessions)
    forecasts=[predict_market(synthetic_daily_world()[1],r,bg_regime,r.features.decision_nano)
               for r in requests if r.barrier_key=="ATR_1"]
    assert len({f.request_id for f in forecasts})==len({f.window_end_nano for f in forecasts})==3
    assert all(f.quality.status=="READY" for f in forecasts)
    assert len({e.evidence_id for f in forecasts for e in f.evidence if e.event=="DIRECTION"})==6
    assert all(e.horizon==f.horizon for f in forecasts for e in f.evidence)


def test_daily_natural_confirmation_admission_and_two_final_gates():
    from ak_trading.v2.decision.gates import evaluate_gate, non_payoff_evidence_reasons
    from ak_trading.v2.policy import research_policy
    from ak_trading.v2.strategies.registry import admission_decision
    rows=daily_proposals()
    assert [p.action for _,p,_ in rows[:3]]==["WAIT","WAIT","OPEN"]
    context,proposal,_=rows[2]
    assert proposal.next_state.confirmation_count==3 and proposal.target_lots>0
    assert admission_decision(context,event="PERSIST:UP_TREND",side=1,horizon=context.spec.max_holding).ok
    reasons,evidence,paywalled=non_payoff_evidence_reasons(proposal,context,research_policy(),checked_at_nano=context.now_nano)
    assert reasons==[] and evidence and not paywalled
    ordinary=evaluate_gate(proposal,context,research_policy(),checked_at_nano=context.now_nano)
    # EP1 measurement passes the non-payoff domains; an honest empty payoff
    # dependency still cannot satisfy the ordinary full EV gate.
    assert not ordinary.eligible and ordinary.reasons==("INSUFFICIENT_SAMPLES",)
    opposite=daily_proposals(transform=lambda f,r:replace(f,direction_probabilities={"UP":.1,"DOWN":.8,"NEUTRAL":.1}))
    context,proposal,_=opposite[2]
    assert context.regime.cross_timeframe=="COUNTERTREND" and proposal.action=="OPEN"
    final=evaluate_gate(proposal,context,research_policy(),checked_at_nano=context.now_nano)
    assert not final.eligible and "UNVALIDATED_TIMEFRAME_CONFLICT" in final.reasons


@pytest.mark.parametrize("failure",[
    "missing_cell","missing_evidence","immature","reference_unavailable","unpublishable",
    "expired","late","stale","invalid","ood","tie_up_down","tie_up_neutral","neutral",
    "identity","wrong_model","wrong_product","wrong_label","duplicate_evidence",
    "insufficient_samples","insufficient_blocks","bad_calibration","expired_evidence"])
def test_daily_background_failure_matrix_has_no_fallback(failure):
    from ak_trading.v2.policy import confidence_policy
    from ak_trading.v2.probability.forecast import UnpublishableForecastError
    from ak_trading.v2.strategies.registry import admission_decision
    bundle,models,_,reference=synthetic_daily_world()
    at=bundle.bars[590].available_at_nano; now=bundle.bars[591].available_at_nano
    transform=None
    if failure=="missing_cell":
        models=replace(models,models={k:v for k,v in models.models.items() if "|D20|" not in k})
    elif failure=="missing_evidence":
        models=replace(models,evidence=tuple(e for e in models.evidence if e.horizon.value!=20))
    elif failure=="immature":
        models=replace(models,calibration_end_nano=at)
    elif failure=="reference_unavailable":
        reference=replace(reference,training_end_nano=at+1,available_at_nano=at+1)
        models=replace(models,source_hashes={**dict(models.source_hashes),"regime_reference":reference.content_hash()})
    elif failure in ("insufficient_samples","insufficient_blocks","bad_calibration","expired_evidence"):
        cp=confidence_policy("TREND")
        changes={"insufficient_samples":{"sample_count":cp.minimum_samples-1},
                 "insufficient_blocks":{"effective_blocks":cp.minimum_blocks-1},
                 "bad_calibration":{"calibration_error":cp.maximum_calibration_error+.001},
                 "expired_evidence":{"valid_until_nano":now}}
        rows=tuple(e for e in models.evidence if e.horizon.value==20
                   and e.event=="DIRECTION" and e.policy_id==cp.policy_id)
        assert len(rows)==1
        # Keep the faulted row in the model's frozen evidence, so the real
        # provider preserves its membership and these cases exercise the
        # existing numeric/expiry admission conditions directly.
        models=replace(models,evidence=tuple(replace(e,**changes[failure]) if e in rows else e
                                             for e in models.evidence))
    else:
        def transform(f,r):
            if failure=="unpublishable":
                raise UnpublishableForecastError(r.request_id,("SYNTHETIC_FAULT",))
            if failure=="expired": return replace(f,expires_at_nano=now)
            if failure=="late":
                return replace(f,published_at_nano=r.window_start_nano,expires_at_nano=r.window_end_nano,
                               quality=replace(f.quality,status="INVALID",reasons=("FORECAST_LATE",)))
            if failure in ("stale","invalid"):
                return replace(f,quality=replace(f.quality,status=failure.upper()))
            if failure=="ood": return replace(f,out_of_distribution=True)
            if failure=="tie_up_down": return replace(f,direction_probabilities={"UP":.45,"DOWN":.45,"NEUTRAL":.1})
            if failure=="tie_up_neutral": return replace(f,direction_probabilities={"UP":.45,"DOWN":.1,"NEUTRAL":.45})
            if failure=="neutral": return replace(f,direction_probabilities={"UP":.1,"DOWN":.1,"NEUTRAL":.8})
            if failure=="identity": return replace(f,request_id="SYNTHETIC-mismatched-request")
            if failure=="wrong_model": return replace(f,model_id="SYNTHETIC-other-model")
            if failure=="wrong_product": return replace(f,product="SYNTHETIC.OTHER")
            if failure=="wrong_label": return replace(f,label_version="SYNTHETIC-invalid-label-version")
            evidence=next(e for e in f.evidence if e.event=="DIRECTION" and e.policy_id=="confidence-ordinary-v2.1.0")
            if failure=="duplicate_evidence":
                return replace(f,evidence=f.evidence+(replace(evidence,evidence_id="SYNTHETIC-conflicting-evidence",calibration_error=.5),))
            raise AssertionError("unhandled_synthetic_background_failure:"+failure)
    context,calls=daily_context(bundle=bundle,models=models,reference=reference,transform=transform)
    assert context.features.daily_background_id is not None
    assert context.regime.cross_timeframe=="UNKNOWN"
    assert [r.horizon.value for r,_ in calls] in ([20,5,5,5],[5,5,5])
    gate=admission_decision(context,event="PERSIST:UP_TREND",side=1,horizon=context.spec.max_holding)
    assert not gate.ok and "CROSS_TIMEFRAME_NOT_VERIFIED" in gate.reasons


def test_daily_age_uses_product_trading_days_and_missing_calendar_fails_closed():
    bundle,_,_,_=synthetic_daily_world()
    for index in (next(i for i in range(580,610) if bundle.bars[i].trading_day.weekday()==0),
                  next(i for i,b in enumerate(bundle.bars) if b.trading_day==date(2024,4,9))):
        context,calls=daily_context(index=index)
        assert (bundle.bars[index].trading_day-bundle.bars[index-1].trading_day).days>1
        assert context.regime.cross_timeframe=="ALIGNED" and calls[0][0].horizon.value==20
        overage=replace(bundle,bars=tuple(b for b in bundle.bars if b!=bundle.bars[index-1]))
        context,calls=daily_context(index=index,bundle=overage)
        assert context.features.daily_background_id is None and context.regime.cross_timeframe=="UNKNOWN"
        assert all(r.horizon.value==5 for r,_ in calls)
    missing=replace(bundle,sessions=tuple(s for s in bundle.sessions if s.trading_day!=bundle.bars[590].trading_day))
    context,calls=daily_context(bundle=missing)
    assert context.features.daily_background_id is None and context.regime.cross_timeframe=="UNKNOWN"
    assert all(r.horizon.value==5 for r,_ in calls)


def test_daily_same_future_and_late_bars_cannot_replace_the_prior_background():
    from ak_trading.v2.artifacts import canonical_json_bytes
    from ak_trading.v2.schemas import encode_record
    bundle,_,_,_=synthetic_daily_world()
    context,calls=daily_context()
    assert calls[0][0].features.trading_day!=context.features.trading_day
    now=context.now_nano
    late=replace(bundle.bars[590],bar_id="SYNTHETIC-late-revision",available_at_nano=now+1,close=D(99999),high=D(99999))
    future=replace(bundle.bars[592],bar_id="SYNTHETIC-extreme-future",close=D(99999),high=D(99999))
    prefix=replace(bundle,bars=tuple(b for b in bundle.bars if b.end_nano<=now))
    appended=replace(prefix,manifest_sha256="f"*64,bars=prefix.bars+(late,future))
    earlier,earlier_calls=daily_context(bundle=prefix)
    later,later_calls=daily_context(bundle=appended)
    assert canonical_json_bytes(encode_record(earlier))==canonical_json_bytes(encode_record(later))
    assert canonical_json_bytes([encode_record(r) for r,_ in earlier_calls])==canonical_json_bytes([encode_record(r) for r,_ in later_calls])
    missing=replace(prefix,bars=(bundle.bars[591],))
    missing_append=replace(missing,bars=missing.bars+(late,future))
    a,_=daily_context(bundle=missing); b,_=daily_context(bundle=missing_append)
    assert a.regime.cross_timeframe==b.regime.cross_timeframe=="UNKNOWN"
    assert canonical_json_bytes(encode_record(a))==canonical_json_bytes(encode_record(b))


def test_daily_provider_catch_is_narrow_and_minute_behavior_stays_strict():
    def unrelated(f,r): raise ValueError("SYNTHETIC-unrelated-provider-bug")
    with pytest.raises(ValueError,match="SYNTHETIC-unrelated-provider-bug"):
        daily_context(transform=unrelated)
    bundle,models,payoff,reference=replay_inputs()
    spec=build_strategy_spec("TREND","MINUTE")
    with pytest.raises(ValueError,match="forecast_provider_request_identity_mismatch"):
        build_strategy_context(bundle,"rb",70*NS,account("100000"),None,models,payoff,spec,None,
            reference,run_policy(),(),exit_binding=legacy_exit_binding(spec),forecast_provider=lambda m,r,a:None)


def test_daily_exact_key_source_conflict_propagates_and_future_revision_isolated():
    """Source integrity failure has no context; background stages are not reached."""
    from ak_trading.v2.artifacts import canonical_json_bytes
    from ak_trading.v2.factors import FactorsError
    from ak_trading.v2.probability.forecast import predict_market
    from ak_trading.v2.schemas import encode_record
    bundle,models,payoff,reference=synthetic_daily_world()
    baseline,_=daily_context()
    spec=build_strategy_spec("TREND","DAILY")
    now=baseline.now_nano
    calls=[]
    def provider(m,r,a):
        calls.append(r.request_id)
        return predict_market(m,r,a,r.features.decision_nano)
    def build(candidate,at=now):
        return build_strategy_context(candidate,"rb",at,account("100000"),None,models,payoff,spec,None,
            reference,run_policy(),(),exit_binding=legacy_exit_binding(spec),forecast_provider=provider)
    for index in (590,591):
        revision=replace(bundle.bars[index],close=bundle.bars[index].close+D(".1"))
        conflicting=replace(bundle,bars=bundle.bars+(revision,))
        calls.clear()
        with pytest.raises(FactorsError) as error:
            build(conflicting)
        assert str(error.value)=="conflicting_duplicate_bar" and calls==[]
        # No returned context means the affected decision cannot reach
        # admission, OPEN, execution or observation; they are not executed.
        late=replace(revision,available_at_nano=now+1)
        future=replace(bundle,bars=bundle.bars+(late,))
        historical=build(future)
        assert canonical_json_bytes(encode_record(historical))==canonical_json_bytes(encode_record(baseline))
        calls.clear()
        with pytest.raises(FactorsError) as error:
            build(future,now+1)
        assert str(error.value)=="conflicting_duplicate_bar" and calls==[]
    identical=replace(bundle,bars=bundle.bars+(bundle.bars[591],))
    assert canonical_json_bytes(encode_record(build(identical)))==canonical_json_bytes(encode_record(baseline))


def test_daily_ambiguous_prior_day_without_exact_key_collision_is_unknown():
    bundle,_,_,_=synthetic_daily_world()
    prior=bundle.bars[590]
    ambiguous=replace(prior,bar_id="SYNTHETIC-second-prior-key",datetime_nano=prior.datetime_nano+1,
                      start_nano=prior.start_nano+1)
    context,calls=daily_context(bundle=replace(bundle,bars=bundle.bars+(ambiguous,)))
    assert context.features.daily_background_id is None and context.regime.cross_timeframe=="UNKNOWN"
    assert all(r.horizon.value==5 for r,_ in calls)


@pytest.mark.parametrize("warm",[False,True])
def test_daily_source_preflight_rejects_first_conflict_before_any_provider(monkeypatch,warm):
    """The first cold/warm real context call rejects an equal-start tail.

    Cache isolation is test-only. Production never clears or replaces it.
    Warm-up's actual successful publications remain recorded separately.
    """
    import ak_trading.v2.factors as factors
    from ak_trading.v2.probability.forecast import predict_market
    bundle,models,payoff,reference=synthetic_daily_world()
    monkeypatch.setattr(factors,"_PREFIX_CACHE",{})
    spec=build_strategy_spec("TREND","DAILY")
    now=bundle.bars[591].available_at_nano
    publications=[]
    def provider(model,request,regime):
        publications.append(request.request_id)
        return predict_market(model,request,regime,request.features.decision_nano)
    def build(candidate):
        return build_strategy_context(candidate,"rb",now,account("100000"),None,models,payoff,
            spec,None,reference,run_policy(),(),exit_binding=legacy_exit_binding(spec),forecast_provider=provider)
    warm_publications=()
    if warm:
        assert build(bundle).features.quality.status=="READY"
        warm_publications=tuple(publications)
        assert warm_publications
        assert any(len(state[0])>16 for state in factors._PREFIX_CACHE[("rb",86400)].values())
    count_before=len(publications)
    conflict=replace(bundle.bars[591],close=bundle.bars[591].close+D(".1"))
    with pytest.raises(factors.FactorsError) as error:
        build(replace(bundle,bars=bundle.bars+(conflict,)))
    assert str(error.value)=="conflicting_duplicate_bar"
    assert publications[count_before:]==[]
    assert tuple(publications[:count_before])==warm_publications


def test_daily_source_preflight_consumes_equal_anchor_once_in_snapshot_and_context(monkeypatch):
    import ak_trading.v2.factors as factors
    from ak_trading.v2.artifacts import canonical_json_bytes
    from ak_trading.v2.schemas import encode_record
    bundle,_,_,_=synthetic_daily_world()
    monkeypatch.setattr(factors,"_PREFIX_CACHE",{})
    baseline,baseline_calls=daily_context(bundle=bundle)
    assert any(len(state[0])>16 for state in factors._PREFIX_CACHE[("rb",86400)].values())
    duplicated=replace(bundle,bars=bundle.bars+(bundle.bars[591],))
    actual,actual_calls=daily_context(bundle=duplicated)
    assert len(actual.bars)==len(baseline.bars)==592
    assert actual.bars.count(bundle.bars[591])==1
    assert actual.features.source_bar_ids==baseline.features.source_bar_ids
    assert actual.features.values==baseline.features.values and actual.features.quality==baseline.features.quality
    assert canonical_json_bytes(encode_record(actual))==canonical_json_bytes(encode_record(baseline))
    assert canonical_json_bytes([encode_record(request) for request,_ in actual_calls])==canonical_json_bytes(
        [encode_record(request) for request,_ in baseline_calls])


def test_daily_source_preflight_covers_three_snapshots_at_their_own_boundaries(monkeypatch):
    import ak_trading.v2.decision.pipeline as pipeline
    from ak_trading.v2.data import visible_bars
    bundle,_,_,_=synthetic_daily_world()
    calls=[]; normalized=[]
    real_features=pipeline._features
    real_normalize=pipeline._deduplicate_visible
    def normalize(candidates):
        result=real_normalize(candidates)
        normalized.append((tuple(candidates),result))
        return result
    def features(*args,**kwargs):
        result=real_features(*args,**kwargs)
        calls.append((args[2],args[3],kwargs.get("canonical_daily",False),result))
        return result
    monkeypatch.setattr(pipeline,"_deduplicate_visible",normalize)
    monkeypatch.setattr(pipeline,"_features",features)
    context,_=daily_context(bundle=bundle)
    assert [(at,duration,enabled) for at,duration,enabled,_ in calls]==[
        (bundle.bars[591].available_at_nano,86400,True),
        (bundle.bars[590].available_at_nano,86400,True),
        (bundle.bars[591].available_at_nano,86400,True)]
    assert len(normalized)==3
    for (at,_,_,(snapshot,bars,_)),(candidates,canonical) in zip(calls,normalized):
        assert candidates==visible_bars(bundle,"rb",86400,at,False)
        assert bars is canonical
        assert snapshot.decision_nano==at
        assert all(bar.end_nano<=at and bar.available_at_nano<=at for bar in bars)
    assert context.bars is normalized[-1][1]


def test_daily_historical_preflight_does_not_consume_later_available_distinct_row():
    from ak_trading.v2.artifacts import canonical_json_bytes
    from ak_trading.v2.schemas import encode_record
    bundle,_,_,_=synthetic_daily_world()
    baseline,baseline_calls=daily_context(bundle=bundle)
    row=bundle.bars[588]
    later=replace(row,bar_id="SYNTHETIC-distinct-key-later-visible",datetime_nano=row.datetime_nano+1,
        start_nano=row.start_nano+1,available_at_nano=bundle.bars[591].available_at_nano)
    context,calls=daily_context(bundle=replace(bundle,bars=bundle.bars+(later,)))
    before=next(request for request,_ in baseline_calls if request.horizon.value==20)
    after=next(request for request,_ in calls if request.horizon.value==20)
    assert canonical_json_bytes(encode_record(before))==canonical_json_bytes(encode_record(after))
    assert later in context.bars
    assert later.bar_id not in after.features.source_bar_ids
    assert after.features.decision_nano==bundle.bars[590].available_at_nano<later.available_at_nano

