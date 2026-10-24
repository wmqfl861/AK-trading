from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import multiprocessing
from pathlib import Path
import pytest

from ak_trading.v2.contracts import RunPolicy, SessionWindow
from ak_trading.v2.schemas import parse_json_bytes
from ak_trading.v2.validation.experiments import (
    ExperimentSpec, HoldoutScope, ExperimentError, HoldoutConsumedError, StageIncompleteError,
    begin_final_holdout, consume_final_holdout, fail_final_holdout, reserve_final_holdout,
    recover_final_holdouts, register_experiment, read_chain, digest, import_observed_scope,
    validate_holdout_calendar,
)
from ak_trading.v2.validation.splits import SplitSpec, MINUTE


def experiment(name="test",scope=None):
    from ak_trading.v2.strategies.registry import strategy_specs_v2,select_exit_binding
    from ak_trading.v2.exits.registry import exit_registry_specs
    from ak_trading.v2.decision.pipeline import exit_registry_hash_for
    _specs=strategy_specs_v2("MINUTE")
    _bindings=tuple(sorted({select_exit_binding(s).binding_sha256:select_exit_binding(s) for s in _specs}.values(),key=lambda b:b.binding_sha256))
    _all=tuple(sorted({x.exit_policy_id:x for x in (*exit_registry_specs("MINUTE"),*exit_registry_specs("DAILY"))}.values(),key=lambda s:s.exit_policy_id))
    scope=scope or HoldoutScope("CN_FUTURES_V2",("SHFE.RB",),("rb2609",),100,200,300,"1"*64)
    # EP1-M5: ExperimentSpec carries the strict payoff_training_plan_sha256
    # registration field (schema ak_probability_experiment_v2.1.3).
    return ExperimentSpec(name,name,("2"*64,),"3"*64,exit_registry_hash_for(_all),_bindings,"4"*64,"5"*64,
                          (SplitSpec("fold",0,20,20,50,50,90,60,20,False),),digest(scope),scope,
                          (RunPolicy("RESEARCH_EVALUATION","CASH",7,None,None,"research-policy-v2.1.0","macro-risk-v2.1.0","PRICE_ONLY","BASE"),),
                          (),7,"research-policy-v2.1.0","macro-risk-v2.1.0","PRICE_ONLY",400,"a"*64)


def test_registration_duplicate_and_chain(tmp_path):
    path=tmp_path/"experiment-registry.jsonl"
    register_experiment(experiment(),path)
    with pytest.raises(ExperimentError,match="already_registered"):
        register_experiment(experiment(),path)
    assert len(read_chain(path))==1
    assert (tmp_path/"registered-experiments/test.json").is_file()


def test_begin_is_durable_before_label_access_and_failure_consumes(tmp_path):
    e=experiment()
    token=reserve_final_holdout(e,tmp_path)
    begin_final_holdout(e,token,tmp_path)
    begin_final_holdout(e,token,tmp_path)
    record=parse_json_bytes((tmp_path/(e.holdout_id+".json")).read_bytes())
    assert record["status"]=="CONSUMED_IN_PROGRESS"
    assert token not in str(record)
    assert len(read_chain(tmp_path/"consumed-intervals.jsonl"))==1
    # The first label read would occur only after these assertions.
    fail_final_holdout(e,token,tmp_path,"LABEL_READER_CRASH")
    with pytest.raises(HoldoutConsumedError):
        reserve_final_holdout(experiment("other"),tmp_path)


def test_success_idempotent_but_different_report_and_out_of_order_rejected(tmp_path):
    e=experiment()
    token=reserve_final_holdout(e,tmp_path)
    with pytest.raises(HoldoutConsumedError):
        consume_final_holdout(e,token,tmp_path,"6"*64)
    begin_final_holdout(e,token,tmp_path)
    consume_final_holdout(e,token,tmp_path,"6"*64)
    consume_final_holdout(e,token,tmp_path,"6"*64)
    with pytest.raises(HoldoutConsumedError):
        consume_final_holdout(e,token,tmp_path,"7"*64)


@pytest.mark.parametrize("start,end,tail",[(150,250,350),(300,400,500),(99,199,299)])
def test_global_scope_ignores_contract_product_input_hash_and_equal_endpoint(tmp_path,start,end,tail):
    e=experiment()
    import_observed_scope(e.holdout_scope,tmp_path,"v1-observed",400)
    scope=HoldoutScope("CN_FUTURES_V2",("INE.SC",),("sc-new",),start,end,tail,"9"*64)
    with pytest.raises(HoldoutConsumedError,match="observed"):
        reserve_final_holdout(experiment("other",scope),tmp_path)


def _orphan(path,begin):
    e=experiment("orphan")
    token=reserve_final_holdout(e,Path(path))
    if begin:
        begin_final_holdout(e,token,Path(path))


@pytest.mark.parametrize("begin",[False,True])
def test_process_death_orphan_conservatively_consumed(tmp_path,begin):
    p=multiprocessing.get_context("spawn").Process(target=_orphan,args=(str(tmp_path),begin))
    p.start();p.join(15)
    assert p.exitcode==0
    recover_final_holdouts(tmp_path,500)
    record=parse_json_bytes((tmp_path/(experiment().holdout_id+".json")).read_bytes())
    assert record["status"]=="FAILED_CONSUMED"
    assert read_chain(tmp_path/"consumed-intervals.jsonl")


def test_recovery_does_not_consume_live_owner(tmp_path):
    e=experiment()
    token=reserve_final_holdout(e,tmp_path)
    with pytest.raises(ExperimentError,match="active_holdout"):
        recover_final_holdouts(tmp_path,500)
    fail_final_holdout(e,token,tmp_path,"TEST_CLEANUP")


def _compete(path,name,queue):
    try:
        e=experiment(name)
        token=reserve_final_holdout(e,Path(path))
        begin_final_holdout(e,token,Path(path))
        consume_final_holdout(e,token,Path(path),"a"*64)
        queue.put("began")
    except HoldoutConsumedError:
        queue.put("refused")


def test_two_processes_only_one_can_begin(tmp_path):
    ctx=multiprocessing.get_context("spawn")
    queue=ctx.Queue()
    workers=[ctx.Process(target=_compete,args=(str(tmp_path),f"p{i}",queue)) for i in range(2)]
    for p in workers:p.start()
    for p in workers:p.join(15)
    assert all(p.exitcode==0 for p in workers)
    assert sorted(queue.get(timeout=2) for _ in workers)==["began","refused"]


def test_reservation_freezes_all_experiment_parameters(tmp_path):
    e=experiment();token=reserve_final_holdout(e,tmp_path)
    try:
        with pytest.raises(ExperimentError,match="experiment_changed"):
            begin_final_holdout(replace(e,parameter_sha256="b"*64),token,tmp_path)
    finally:
        fail_final_holdout(e,token,tmp_path,"TEST_CLEANUP")


def test_cost_scenarios_apply_multiplier_once_and_require_real_stress_path():
    from decimal import Decimal as D
    from ak_trading.v2.schemas import decode_shared_fixture
    from ak_trading.v2.validation.experiments import scenario_rules
    from ak_trading.v2.decision.execution import fee_amount
    f=decode_shared_fixture((Path(__file__).parent/"fixtures/v2/shared.json").read_bytes())
    rule=f["rules"][0]
    doubled=scenario_rules((rule,),"FEE_X2")[0]
    assert fee_amount(doubled.open_fee,D(100),1,doubled.multiplier)==2*fee_amount(rule.open_fee,D(100),1,rule.multiplier)
    margin=scenario_rules((rule,),"MARGIN_X1_5")[0]
    assert margin.long_margin_rate==min(D(1),rule.long_margin_rate*D('1.5'))
    slip=scenario_rules((rule,),"SLIPPAGE_X2")[0]
    assert slip.slippage_ticks==2*rule.slippage_ticks and slip.open_fee==rule.open_fee
    with pytest.raises(ExperimentError,match="already_applied"):
        scenario_rules((doubled,),"FEE_X2")
    for name in ("GAP","NO_STOP"):
        with pytest.raises(ExperimentError,match="path_simulation"):
            scenario_rules((rule,),name)


def test_m4_stress_paths_use_base_rules_and_actual_trading_days(tmp_path):
    from decimal import Decimal as D
    from ak_trading.v2.contracts import RoundBudget,LayerSpec,FeeLeg,TradingRule
    from ak_trading.v2.policy import stress_policy
    from ak_trading.v2.decision.execution import simulate_stress_paths
    from ak_trading.v2.schemas import canonical_json_bytes
    from ak_trading.v2.validation.experiments import content
    leg=FeeLeg(D(1),D(0),False)
    rule=TradingRule("base","rb",0,10000*MINUTE,0,D(10),D('.1'),1,1,leg,leg,leg,D('.1'),D('.1'),D(200),D(1),D(0),D(0),D('.1'),"SCENARIO","a"*64)
    budget=RoundBudget("b","e","rb",0,None,None,stress_policy(),D(1),1,
                       tuple(LayerSpec(i,D(p),D(p),q) for i,(p,q) in enumerate(((100,1),(98,2),(96,4)))),
                       D(94),D(104),100*MINUTE,7,D(241),D(535),D(300),D(2000),D(100000))
    sessions=tuple(SessionWindow(str(i),"SHFE.RB",date(2026,1,1)+timedelta(days=i),i*1440*MINUTE,(i*1440+60)*MINUTE,
                                 "DAY","synthetic","SCENARIO") for i in range(4))
    paths=simulate_stress_paths(budget,rule,sessions)
    by_name={p.scenario:p for p in paths}
    assert by_name["GAP"].reference_prices==(D(91),)
    assert by_name["NO_STOP"].reference_prices==(D(93),D(92),D(91),D(90))
    assert len(by_name["NO_STOP"].trading_days)==4
    assert by_name["GAP"].loss_cny==D(465) and by_name["NO_STOP"].loss_cny==D(535)
    (tmp_path/"actual_m4_stress_paths.json").write_bytes(canonical_json_bytes(content(paths)))


def test_configuration_cash_is_positive_decimal_not_hardcoded_million():
    from ak_trading.v2.validation.experiments import parse_config
    from ak_trading.v2.schemas import parse_json_bytes
    c=parse_json_bytes((Path(__file__).parent/"fixtures/v2/m5_research.json").read_bytes())
    parse_config({**c,"initial_cash_cny":"123456.78"})
    for bad in ("0","-1","NaN","Infinity",1.0):
        with pytest.raises((ExperimentError,ValueError)):
            parse_config({**c,"initial_cash_cny":bad})


def test_real_seasonality_applies_only_legal_prefix_and_frozen_calibration_model():
    from decimal import Decimal as D
    from ak_trading.v2.contracts import MarketBar,FrozenInput,Instrument,DataQuality,TradingRule,FeeLeg
    from ak_trading.v2.validation.experiments import _build_snapshots
    ns=10**9;dn=86400*ns;base=1700000000*ns
    quality=DataQuality("READY",("SYNTHETIC",),"reconstructed","a"*64)
    sessions=tuple(SessionWindow(f"s{i}","SHFE.RB",date(2024,1,1)+timedelta(days=i),base+i*dn,base+i*dn+2*MINUTE,"DAY","SYNTHETIC","SCENARIO") for i in range(28))
    instrument=Instrument("unit","SHFE.rb9901","SHFE.RB","synthetic",("synthetic",),base,None,None,None,quality)
    leg=FeeLeg(D(1),D(0),False)
    rule=TradingRule("rule","unit",base,base+30*dn,base,D(10),D('.1'),1,1,leg,leg,leg,D('.1'),D('.1'),D(1000),D(1),D(0),D(0),D('.1'),"SCENARIO","b"*64)
    bars=[]
    for i,s in enumerate(sessions):
        for j in range(2):
            start=s.start_nano+j*MINUTE;price=D(100)+D(i)/10+D(j)/100
            bars.append(MarketBar(f"m{i}-{j}","unit","SHFE.rb9901","SHFE.RB",60,start,start,start+MINUTE,s.trading_day,s.session_id,
                                  start+MINUTE,start+MINUTE,price,price+D('.1'),price-D('.1'),price,D(100) if i<22 else D(120),D(1000),D(1000),True,1,1,"SYNTHETIC",quality))
    bundle=FrozenInput(Path("SYNTHETIC"),"c"*64,(instrument,),sessions,tuple(bars),(),(rule,))
    split=SplitSpec("seasonal",base,sessions[22].start_nano,sessions[22].start_nano,sessions[25].start_nano,
                    sessions[25].start_nano,sessions[-1].end_nano+1,60,20,False)
    key="SEASONAL:volume_relative:v2.1.1:d60"
    config={"contract_ids":["unit"],"feature_durations":[60],"model_fit_configs":{"seasonal":{"feature_keys":[key]}}}
    snapshots,model,_=_build_snapshots(bundle,config,split)
    assert model.fit_end_nano==split.train_end_nano
    after=[f for f in snapshots if f.trading_day>=sessions[22].trading_day]
    assert after and all(next(v for v in f.values if v.factor_key==key).value==D('.2') for f in after)
    assert all(next(v for v in f.values if v.factor_key==key).available_at_nano>=model.fit_end_nano for f in after)
    early=[f for f in snapshots if f.trading_day<sessions[5].trading_day]
    assert all(not any(v.factor_key==key and v.value is not None for v in f.values) for f in early)


def test_sixty_days_and_twenty_day_tail_not_interchangeable():
    sessions=tuple(SessionWindow(str(i),"SHFE.RB",date(2026,1,1)+timedelta(days=i),
                                 i*1440*MINUTE,(i*1440+60)*MINUTE,"DAY","synthetic","SCENARIO") for i in range(80))
    scope=HoldoutScope("CN_FUTURES_V2",("SHFE.RB",),("rb",),0,60*1440*MINUTE,sessions[-1].end_nano,digest(sessions))
    validate_holdout_calendar(scope,sessions,sessions[-1].end_nano)
    with pytest.raises(StageIncompleteError):
        validate_holdout_calendar(scope,sessions,sessions[-1].end_nano-1)
    with pytest.raises(StageIncompleteError):
        validate_holdout_calendar(replace(scope,decision_end_nano=61*1440*MINUTE),sessions,sessions[-1].end_nano)


# --- arbitration 2026-09-17 T6/T8/T9 (workorder E2) --------------------------

def test_t6_fixed_nine_field_policy_single_binding_identity_chain(monkeypatch):
    """T6: the nine-field FIXED training policy, the replay exit_bindings, the
    strategy-context binding and the payoff condition key's 17th item all come
    from one M3B ``select_exit_binding`` original; removing any required
    binding parameter fails. No RunPolicy/producer mocking."""
    import json as _json
    from decimal import Decimal as D
    from ak_trading.v2.contracts import (AccountSnapshot,RunPolicy,StrategyProposal,StrategyState,RoundBudget,LayerSpec)
    from ak_trading.v2.policy import research_policy,stress_policy
    from ak_trading.v2.probability.forecast import predict_market
    from ak_trading.v2.decision.payoffs import condition_key,fit_path_payoff_model
    from ak_trading.v2.decision.pipeline import build_strategy_context
    from ak_trading.v2.decision.replay import run_decision_replay
    from ak_trading.v2.strategies.registry import build_strategy_spec,select_exit_binding,strategy_specs_v2
    from ak_trading.v2.validation.experiments import _exit_pack,_training_fixed_policy
    from test_v2_m5_payoff_integration import _cached_world,CONTRACT,MINUTE as _MINUTE
    bundle,sessions,models,reference=_cached_world()
    spec=build_strategy_spec("TREND","MINUTE")
    binding=select_exit_binding(spec)
    config={"seed":7,"research_policy_version":"research-policy-v2.1.0","macro_policy_version":"macro-risk-v2.1.0"}
    policy=_training_fixed_policy(config,spec,binding)
    assert policy.purpose=="RESEARCH_EVALUATION" and policy.routing_mode=="FIXED"
    assert (policy.seed,policy.fixed_strategy_id)==(7,spec.strategy_id)
    assert policy.fixed_exit_binding_sha256==binding.binding_sha256
    assert (policy.research_policy_version,policy.macro_policy_version)==("research-policy-v2.1.0","macro-risk-v2.1.0")
    assert (policy.macro_mode,policy.cost_scenario)==("PRICE_ONLY","BASE")
    # registered-binding pack equality (the same original that enters
    # ExperimentSpec.exit_bindings and the replay keyword)
    _reg,_es,_bs=_exit_pack((spec,))
    replay_binding=next(b for b in _bs if b.strategy_id==spec.strategy_id)
    registered=tuple(sorted({select_exit_binding(s).binding_sha256:select_exit_binding(s)
                             for s in strategy_specs_v2("MINUTE")}.values(),key=lambda b:b.binding_sha256))
    assert replay_binding.binding_sha256==binding.binding_sha256
    assert binding.binding_sha256 in {b.binding_sha256 for b in registered}
    # P-3: real build_strategy_context carries the same original; the
    # production forecast provider runs for real (the frozen mini-model is
    # fitted on the spec's max holding horizon).
    empty=fit_path_payoff_model((),models.training_end_nano,policy.research_policy_version)
    # EP1-M5 v3 world: the frozen mini models calibrate through channel day
    # 143; the context decision must be a channel day after that for the
    # models to be available (day 145 is the last channel day).
    session=sessions[145]
    decision=session.end_nano-_MINUTE
    cash=D(1000000);zero=D(0)
    account=AccountSnapshot("t6-account",decision,session.trading_day,cash,cash,zero,zero,zero,zero,
                            cash,zero,zero,cash,zero,cash,(),False,bundle.bars[0].quality)
    _provider=lambda m,r,g:predict_market(m,r,g,r.features.decision_nano)
    context=build_strategy_context(bundle,CONTRACT,decision,account,None,models,empty,spec,None,
                                   reference,policy,(),forecast_provider=_provider,
                                   exit_binding=binding)
    assert context.exit_binding.binding_sha256==binding.binding_sha256
    assert context.forecasts and context.rule is not None
    # item 17 of the canonical 18-part condition key is the same original
    price=context.features.reference_price or D(100)
    budget=RoundBudget("t6-budget","t6-episode",CONTRACT,decision,None,None,stress_policy(),D(1),1,
                       (LayerSpec(0,price,price,1),),price-D(2),price+D(2),decision+30*_MINUTE,1,
                       D(20),D(50),D(100),D(20000),D(1000000))
    next_state=StrategyState(spec.strategy_id,"t6-episode",CONTRACT,"WAIT_TRIGGER",None,None,1,(),(0,),
                             None,None,None,(),budget,0,None,None)
    proposal=StrategyProposal("t6-proposal",spec.strategy_id,"t6-episode",CONTRACT,decision,"OPEN",1,0,
                              price,price+D(2),price-D(2),decision+_MINUTE,decision+10*_MINUTE,
                              context.forecasts[0].forecast_id,"assessment-"+digest("t6"),budget,None,
                              next_state,("T6_SYNTHETIC_SINGLE_INPUT",))
    key=_json.loads(condition_key(proposal,context))
    assert len(key)==18 and key[16]==binding.binding_sha256==policy.fixed_exit_binding_sha256
    # a real replay accepts the pack; deleting any required binding parameter fails
    replay_account=AccountSnapshot("t6-replay",session.start_nano,session.trading_day,cash,cash,zero,
                                   zero,zero,zero,cash,zero,zero,cash,zero,cash,(),False,bundle.bars[0].quality)
    result=run_decision_replay(bundle,models,empty,(spec,),replay_account,policy,(),{},research_policy(),None,
                               reference,"1"*64,None,session.start_nano,session.end_nano,
                               exit_specs=_es,exit_bindings=_bs)
    assert result.status is not None
    with pytest.raises(TypeError):
        run_decision_replay(bundle,models,empty,(spec,),replay_account,policy,(),{},research_policy(),None,
                            reference,"1"*64,None,session.start_nano,session.end_nano,exit_specs=_es)
    with pytest.raises(TypeError):
        run_decision_replay(bundle,models,empty,(spec,),replay_account,policy,(),{},research_policy(),None,
                            reference,"1"*64,None,session.start_nano,session.end_nano,exit_bindings=_bs)
    with pytest.raises(TypeError):
        build_strategy_context(bundle,CONTRACT,decision,account,None,models,empty,spec,None,
                               reference,policy,(),forecast_provider=_provider)
    with pytest.raises(ValueError,match="FIXED_mode_requires_fixed_exit_binding_sha256"):
        RunPolicy(purpose="RESEARCH_EVALUATION",routing_mode="FIXED",seed=7,
                  fixed_strategy_id=spec.strategy_id,fixed_exit_binding_sha256=None,
                  research_policy_version="research-policy-v2.1.0",
                  macro_policy_version="macro-risk-v2.1.0",macro_mode="PRICE_ONLY",cost_scenario="BASE")


def _gate_config():
    from ak_trading.v2.schemas import parse_json_bytes
    config=parse_json_bytes((Path(__file__).parent/"fixtures/v2/m5_research.json").read_bytes())
    config["contract_lock_path"]="data/research_runs/v2/contracts-2.1.3/contract-lock.p3-r1.json"
    return config


def _failure_validation_input(tmp_path, monkeypatch, run_id):
    """SYNTHETIC source-only preflight; no producer, fit or input acceptance."""
    import hashlib
    import ak_trading.v2
    from ak_trading.v2 import artifacts
    from ak_trading.v2.contracts import FrozenInput
    from ak_trading.v2.schemas import decode_shared_fixture
    from ak_trading.v2.validation import experiments
    fixture_path = Path(__file__).parent / "fixtures/v2/shared.json"
    raw = fixture_path.read_bytes()
    fixture = decode_shared_fixture(raw)
    bundle = FrozenInput(fixture_path.parent, hashlib.sha256(raw).hexdigest(),
                         fixture["instruments"], fixture["sessions"], fixture["bars"],
                         fixture["macro_items"], fixture["rules"])
    config = _gate_config()
    config["run_id"] = run_id
    config["holdout_scope"]["calendar_sha256"] = digest(bundle.sessions)
    config["holdout_id"] = digest(config["holdout_scope"])
    lock = Path(__file__).resolve().parents[1] / config["contract_lock_path"]
    monkeypatch.setattr(artifacts, "verify_contract_lock", lambda *args: None)
    monkeypatch.setattr(ak_trading.v2, "DOCUMENT_VERSION", parse_json_bytes(lock.read_bytes())["document_version"])
    monkeypatch.setattr(experiments, "RUN_ROOT", tmp_path / "r")
    return bundle, config


@pytest.mark.parametrize("mode", ["smoke", "walk-forward"])
@pytest.mark.parametrize("failure_kind", ["no_model", "fit_error"])
def test_validation_failure_leaves_no_registration(tmp_path, monkeypatch, mode, failure_kind):
    """Real validation entry must leave no registration after a failed fit."""
    from ak_trading.v2.validation import experiments
    bundle, config = _failure_validation_input(tmp_path, monkeypatch,
                                               "fail-" + mode[0] + "-" + failure_kind[0])
    calls = []

    def failed_fold(*args):
        calls.append("fit")
        if failure_kind == "fit_error":
            raise RuntimeError("SYNTHETIC_FIT_FAILURE_PROBE")
        return {"models": None}

    monkeypatch.setattr(experiments, "_fit_fold", failed_fold)
    expected = StageIncompleteError if failure_kind == "no_model" else RuntimeError
    reason = "TRAIN_OR_CALIBRATION_INSUFFICIENT_NO_MODEL_ARTIFACT" if failure_kind == "no_model" else "SYNTHETIC_FIT_FAILURE_PROBE"
    output = tmp_path / "out"
    with pytest.raises(expected, match=reason):
        experiments._execute_validation(bundle, config, output, mode)
    assert calls == ["fit"]
    assert not output.exists()
    assert not (experiments.RUN_ROOT / "experiment-registry.jsonl").exists()
    assert not (experiments.RUN_ROOT / "registered-experiments").exists()
    assert not (experiments.RUN_ROOT / "experiment-registry.lock").exists()


@pytest.mark.parametrize("existing_kind", ["registered", "incomplete_frozen"])
def test_validation_duplicate_registration_rejected_before_fit(tmp_path, monkeypatch, existing_kind):
    from ak_trading.v2.validation import experiments
    bundle, config = _failure_validation_input(tmp_path, monkeypatch, "dup")
    registry = experiments.RUN_ROOT / "experiment-registry.jsonl"
    if existing_kind == "registered":
        register_experiment(experiment(config["run_id"]), registry)
        original = registry.read_bytes()
        reason = "experiment_or_run_already_registered"
    else:
        frozen = registry.parent / "registered-experiments" / (config["run_id"] + ".json")
        frozen.parent.mkdir(parents=True)
        original = b"SYNTHETIC incomplete registration evidence\n"
        frozen.write_bytes(original)
        reason = "registration_incomplete_requires_review"

    def forbidden_fit(*args):
        raise AssertionError("duplicate registration must be rejected before fit")

    monkeypatch.setattr(experiments, "_fit_fold", forbidden_fit)
    with pytest.raises(ExperimentError, match=reason):
        experiments._execute_validation(bundle, config, tmp_path / "out", "smoke")
    assert (registry if existing_kind == "registered" else frozen).read_bytes() == original
    assert not (tmp_path / "out").exists()


def test_t8_missing_producer_fails_smoke_before_expensive_fit(tmp_path,monkeypatch):
    """T8 first half (smoke entry): with the public producer symbol absent,
    _execute_validation fails as an engineering stage error ahead of any
    expensive _fit_fold, registration and holdout consumption."""
    from ak_trading.v2.decision import payoffs
    from ak_trading.v2.validation import experiments
    if hasattr(payoffs,"observation_from_replay"):
        monkeypatch.delattr(payoffs,"observation_from_replay")
    monkeypatch.setattr(experiments,"RUN_ROOT",tmp_path/"runroot")
    def _forbidden(*args,**kwargs):
        raise AssertionError("expensive _fit_fold must not run when the producer is missing")
    monkeypatch.setattr(experiments,"_fit_fold",_forbidden)
    with pytest.raises(experiments.StageIncompleteError,
                       match="PAYOFF_TRAINING_DEPENDENCY_MISSING.*observation_from_replay"):
        experiments._execute_validation(object(),_gate_config(),tmp_path/"out","smoke")
    assert not (tmp_path/"runroot"/"experiment-registry.jsonl").exists()


def test_t8_missing_producer_fails_shadow_replay_before_expensive_fit(tmp_path,monkeypatch):
    """T8 first half (shadow-replay entry): same gate on run_shadow_replay_experiment."""
    from ak_trading.v2.decision import payoffs
    from ak_trading.v2.validation import experiments
    if hasattr(payoffs,"observation_from_replay"):
        monkeypatch.delattr(payoffs,"observation_from_replay")
    def _forbidden(*args,**kwargs):
        raise AssertionError("expensive _fit_fold must not run when the producer is missing")
    monkeypatch.setattr(experiments,"_fit_fold",_forbidden)
    with pytest.raises(experiments.StageIncompleteError,
                       match="PAYOFF_TRAINING_DEPENDENCY_MISSING.*observation_from_replay"):
        experiments.run_shadow_replay_experiment(object(),_gate_config(),tmp_path/"out",None)


def test_t9_new_run_id_registers_once_and_old_bytes_stay_unchanged(tmp_path):
    from ak_trading.v2.schemas import parse_json_bytes
    registry=tmp_path/"experiment-registry.jsonl"
    first=experiment("t9-first")
    register_experiment(first,registry)
    frozen=(tmp_path/"registered-experiments"/"t9-first.json").read_bytes()
    chain_before=read_chain(registry)
    register_experiment(experiment("t9-second"),registry)
    assert (tmp_path/"registered-experiments"/"t9-first.json").read_bytes()==frozen
    chain=read_chain(registry)
    assert len(chain)==2 and chain[0]==chain_before[0]
    # re-registering the same run_id/experiment_id is refused and rewrites nothing
    with pytest.raises(ExperimentError,match="already_registered"):
        register_experiment(experiment("t9-first"),registry)
    assert (tmp_path/"registered-experiments"/"t9-first.json").read_bytes()==frozen
    assert len(read_chain(registry))==2
    assert parse_json_bytes(frozen)["spec"]["run_id"]=="t9-first"


def test_t9_error_directory_is_not_completion_and_cannot_be_resumed(tmp_path):
    """A directory holding only error.json is a failed attempt, never a
    completed run: the CLI refuses it as an output directory and leaves the
    old failure bytes untouched."""
    import importlib.util
    from ak_trading.v2.schemas import canonical_json_bytes
    spec_=importlib.util.spec_from_file_location("m5_cli_t9",
        Path(__file__).resolve().parents[1]/"scripts/research_probability_v2.py")
    module=importlib.util.module_from_spec(spec_);spec_.loader.exec_module(module)
    failed=tmp_path/"failed-run";failed.mkdir()
    original=canonical_json_bytes({"mode":"ERROR","exit_code":3})
    (failed/"error.json").write_bytes(original)
    code=module.main(["--mode","smoke","--fixture",str(Path(__file__).parent/"fixtures/v2/shared.json"),
                      "--config",str(Path(__file__).parent/"fixtures/v2/m5_research.json"),
                      "--output",str(failed)])
    assert code==2
    assert (failed/"error.json").read_bytes()==original
    assert list(failed.iterdir())==[failed/"error.json"]


def test_t9_old_relay_starter_stays_inert():
    """T9: the chunk relay starter that continued past non-zero exits was
    renamed inert by E0; no live run-chunks.sh may return on that path."""
    run_dir=Path(__file__).resolve().parents[1]/"data/research_runs/v2/m5-development-20260914T041258Z"
    assert not (run_dir/"run-chunks.sh").exists()
    assert (run_dir/"run-chunks.sh.inert-20260917T0300-E0").is_file()


# --- E-P1 EP1-M5 (2026-09-17): measurement-path consumption discipline -------

def test_ep1_exclusions_match_complete_frozen_codes_not_prefix():
    """RULING 3.4: only COMPLETE producer refusal codes may become per-window
    exclusions. An unknown suffix sharing the PAYOFF_SAMPLE_UNAVAILABLE:
    prefix must keep blocking (propagate), as must every integrity code."""
    from ak_trading.v2.validation.experiments import (
        _payoff_excludable, payoff_producer_excludable_codes,
    )
    codes = payoff_producer_excludable_codes()
    assert codes  # non-empty: imported from the producer module
    for code in codes:
        assert _payoff_excludable(ValueError(code))
    # same prefix, unknown suffix -> NOT excludable (would propagate and block)
    assert not _payoff_excludable(ValueError("PAYOFF_SAMPLE_UNAVAILABLE:NOT_A_FROZEN_SUFFIX"))
    assert not _payoff_excludable(ValueError("PAYOFF_INTEGRITY:DECISION_CHAIN_MISSING"))
    assert not _payoff_excludable(ValueError("PAYOFF_INTEGRITY_PROGRAM:BLOCK_ID_NOT_TRADING_DAY"))
    assert not _payoff_excludable(ValueError("unrelated"))


def test_ep1_research_config_v2_1_3_requires_real_plan_set_hash():
    from ak_trading.v2.schemas import parse_json_bytes
    from ak_trading.v2.validation.experiments import parse_config
    c = parse_json_bytes((Path(__file__).parent / "fixtures/v2/m5_research.json").read_bytes())
    upgraded = {**c, "schema_version": "ak_probability_research_config_v2.1.3",
                "payoff_training_plan_sha256": "c" * 64}
    parse_config(upgraded)  # exactly one additional strict field, real hash
    with pytest.raises(ExperimentError, match="invalid_sha256"):
        parse_config({**upgraded, "payoff_training_plan_sha256": "not-a-hash"})
    with pytest.raises(ExperimentError, match="schema"):
        parse_config({k: v for k, v in upgraded.items() if k != "payoff_training_plan_sha256"})
    with pytest.raises(ExperimentError, match="schema"):
        parse_config({**upgraded, "extra_field": 1})


def test_ep1_plan_set_is_deterministic_window_bounded_and_warmup_legal():
    """The pre-frozen plan set is calendar/split/term driven only: identical
    inputs hash identically, every plan satisfies the strict §2.2 chain, the
    window end never passes the outer training cutoff, and the first
    inner-OOS day (no legal warmup between calibration and the window) is
    skipped honestly instead of being backdated."""
    from decimal import Decimal as D
    from datetime import date, timedelta
    from ak_trading.v2.contracts import DataQuality, FeeLeg, FrozenInput, Instrument, MarketBar, TradingRule
    from ak_trading.v2.strategies.registry import build_strategy_spec, select_exit_binding
    from ak_trading.v2.validation.experiments import (
        _payoff_inner_split, _payoff_training_plans, digest,
        payoff_training_plan_set_bytes, payoff_training_plan_set_sha256,
    )
    ns = 10 ** 9; dn = 86400 * ns; base = 1700000000 * ns
    quality = DataQuality("READY", ("SYNTHETIC",), "reconstructed", "a" * 64)
    sessions = tuple(SessionWindow(f"d{i}", "SHFE.RB", date(2024, 1, 1) + timedelta(days=i),
                                   base + i * dn, base + i * dn + 30 * MINUTE,
                                   "DAY", "SYNTHETIC", "SCENARIO") for i in range(40))
    instrument = Instrument("unit", "SHFE.rb9901", "SHFE.RB", "synthetic", ("synthetic",),
                            base, None, None, None, quality)
    leg = FeeLeg(D(1), D(0), False)
    rule = TradingRule("r", "unit", base, base + 40 * dn, base, D(10), D('.1'), 1, 1,
                       leg, leg, leg, D('.1'), D('.1'), D(1000), D(1), D(0), D(0), D('.1'),
                       "SCENARIO", "1" * 64)
    bundle = FrozenInput(Path("SYNTHETIC"), digest("plan-set"), (instrument,), sessions, (), (), (rule,))
    spec = build_strategy_spec("TREND", "MINUTE")
    split = SplitSpec("plan-fold", sessions[0].start_nano, sessions[29].end_nano,
                      sessions[30].start_nano, sessions[33].end_nano,
                      sessions[34].start_nano, sessions[39].end_nano + 1, 60, 1, False)
    config = {"contract_ids": ["unit"], "initial_cash_cny": "1000000",
              "model_fit_configs": {"plan-fold": {"inner_split_fraction": 0.5,
                                                  "inner_minimum_train_days": 2,
                                                  "inner_minimum_validation_days": 1,
                                                  "inner_embargo_days": 1}}}
    planned, skipped = _payoff_training_plans(bundle, config, split, (spec,))
    assert planned, "expected at least one legal window"
    inner, _days = _payoff_inner_split(bundle, config, split)
    binding = select_exit_binding(spec)
    first_window = min(p[1] for p in planned)
    # the first inner-test day has no legal warmup and is skipped honestly
    assert all(s[2] == "PAYOFF_PLAN_WINDOW_UNPLANNED:NO_LEGAL_WARMUP" for s in skipped)
    assert skipped and skipped[0][1] == inner.test_start_nano
    for s, ws, we, plan in planned:
        assert plan.strategy_id == spec.strategy_id
        assert plan.exit_binding_sha256 == binding.binding_sha256
        assert plan.outer_split_id == split.split_id and plan.inner_split_id == inner.split_id
        assert plan.inner_training_end_nano < plan.inner_calibration_end_nano \
            < plan.warmup_start_nano <= plan.window_start_nano \
            < plan.window_end_nano <= plan.training_end_nano == split.train_end_nano
        assert plan.window_start_nano == ws and plan.window_end_nano == we
        assert plan.warmup_start_nano > inner.calibration_end_nano
        assert plan.contract_ids == ("unit",) and plan.calendar_sha256 == digest(sessions)
        assert plan.initial_cash_cny == D(1000000)
    plans = [p[3] for p in planned]
    # determinism: same frozen inputs -> identical canonical bytes and hash
    again, _ = _payoff_training_plans(bundle, config, split, (spec,))
    assert payoff_training_plan_set_bytes(plans) == payoff_training_plan_set_bytes([p[3] for p in again])
    assert payoff_training_plan_set_sha256(plans) == payoff_training_plan_set_sha256([p[3] for p in again])
    # a changed window boundary changes the plan-set identity
    shifted = [replace_plan_window(p, 1) if i == 0 else p for i, p in enumerate(plans)]
    assert payoff_training_plan_set_sha256(shifted) != payoff_training_plan_set_sha256(plans)


def replace_plan_window(plan, shift):
    from dataclasses import replace
    from ak_trading.v2.contracts import PayoffTrainingPlan
    return replace(plan, warmup_start_nano=plan.warmup_start_nano + shift,
                   window_start_nano=plan.window_start_nano + shift,
                   window_end_nano=plan.window_end_nano + shift)
