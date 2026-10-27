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


# ---------------------------------------------------------------------------
# T7 CPU equivalence regressions (t7-cpu-owner-001, 2026-10-26): the batch
# snapshot builder's hoisted per-duration specs, the per-contract daily
# background reverse index, population/order preservation, and cold/hot
# byte identity across all six frozen feature durations.
# ---------------------------------------------------------------------------

def _t7cpu_bundle(days: int, slots: int, late_daily_day: int | None = None):
    """A small SYNTHETIC minute+daily world; day ``late_daily_day``'s daily
    bar only becomes available 30 minutes into the next session."""
    from datetime import datetime, timezone
    from decimal import Decimal as D
    from ak_trading.v2.contracts import (DataQuality, FeeLeg, FrozenInput,
                                         Instrument, MarketBar, TradingRule)
    ns = 10 ** 9
    quality = DataQuality("READY", ("SYNTHETIC",), "reconstructed", "a" * 64)
    sessions = tuple(SessionWindow(
        f"day-{i}", "SHFE.RB", date(2026, 1, 5) + timedelta(days=i),
        (i * 1440 + 60) * MINUTE, (i * 1440 + 60 + slots) * MINUTE,
        "DAY", "SYNTHETIC", "SCENARIO") for i in range(days))
    contract = "RB2610"
    leg = FeeLeg(D(1), D(0), False)
    rule = TradingRule("t7cpu-rule", contract, sessions[0].start_nano,
                       sessions[-1].end_nano + 1, sessions[0].start_nano,
                       D(10), D('.1'), 1, 1, leg, leg, leg, D('.1'), D('.1'),
                       D(1000), D(1), D(0), D(0), D('.1'), "SCENARIO", "1" * 64)
    bars = []
    for i, session in enumerate(sessions):
        for j in range(slots):
            start = session.start_nano + j * MINUTE
            close = D(100 + i * 3 + j % 7)
            bars.append(MarketBar(
                f"m-{i}-{j}", contract, "rb2610", "SHFE.RB", 60, start, start,
                start + MINUTE, session.trading_day, session.session_id,
                start + MINUTE, start + MINUTE, close, close + D(1),
                close - D(1), close, D(100 + i), D(1000 + i * 10),
                D(1000 + i * 10), True, 1, 1, "SYNTHETIC", quality))
        available = session.end_nano
        if late_daily_day is not None and i == late_daily_day:
            available = sessions[i + 1].start_nano + 30 * MINUTE
        closes = [b.close for b in bars if b.trading_day == session.trading_day
                  and b.duration_seconds == 60]
        bars.append(MarketBar(
            f"d-{i}", contract, "rb2610", "SHFE.RB", 86400,
            session.start_nano, session.start_nano, session.end_nano,
            session.trading_day, session.session_id, available, available,
            closes[0], max(closes) + D(1), min(closes) - D(1), closes[-1],
            D(100 + i), D(1000 * slots), D(1000 * slots), True, slots, slots,
            "SYNTHETIC", quality))
    instrument = Instrument(contract, "rb2610", "SHFE.RB", "synthetic",
                            ("synthetic",), sessions[0].start_nano, None,
                            None, None, quality)
    bundle = FrozenInput(Path("SYNTHETIC"), "e" * 64,
                         (instrument,), sessions, tuple(bars), (), (rule,))
    return bundle, sessions, contract


def _t7cpu_split(sessions):
    return SplitSpec("t7cpu-fold", sessions[0].start_nano,
                     sessions[-3].end_nano, sessions[-3].end_nano,
                     sessions[-2].end_nano, sessions[-2].end_nano,
                     sessions[-1].end_nano + 1, 60, 1, False)


def _t7cpu_clear_caches():
    import tests.test_v2_m2_causality as m2
    m2._clear_t7cpu_caches()


def test_t7cpu_build_snapshots_background_index_specs_hoist_and_order():
    """The per-contract daily background reverse index and the hoisted
    per-duration spec registry produce exactly the original reverse-scan
    semantics.  Daily durations are processed first per contract, so minute
    snapshots select the latest strictly-previous trading day whose daily
    snapshot is already available — including the late-availability
    fallback to the day before — and day-one minutes have no background."""
    from ak_trading.v2.schemas import encode_record
    from ak_trading.v2.validation.experiments import _build_snapshots
    days, slots = 4, 60
    bundle, sessions, contract = _t7cpu_bundle(days, slots, late_daily_day=1)
    split = _t7cpu_split(sessions)
    config = {"contract_ids": [contract, contract],
              "feature_durations": [60, 86400],
              "model_fit_configs": {"t7cpu-fold": {"feature_keys": ["META:price_tick:v2.1.0"]}}}
    _t7cpu_clear_caches()
    snapshots, _seasonal, _exclusions = _build_snapshots(bundle, config, split)

    # population: each pass emits one snapshot per minute decision plus one
    # per daily decision; ordering is (decision, contract, duration).
    minute_total = days * slots
    assert len(snapshots) == 2 * (minute_total + days)
    assert list(snapshots) == sorted(
        snapshots, key=lambda f: (f.decision_nano, f.contract_id, f.duration_seconds))

    daily_by_day = {f.trading_day: f for f in snapshots
                    if f.duration_seconds == 86400}
    assert len(daily_by_day) == days
    day2_late_available = sessions[2].start_nano + 30 * MINUTE

    none_group = with_group = 0
    for f in snapshots:
        if f.duration_seconds != 60:
            continue
        if f.daily_background_id is None:
            none_group += 1
            assert f.trading_day == sessions[0].trading_day
            continue
        with_group += 1
        day_index = (f.trading_day - date(2026, 1, 5)).days
        # latest strictly-previous trading day with available_at <= decision
        expected_day = None
        for previous in range(day_index):
            daily = daily_by_day[sessions[previous].trading_day]
            if daily.available_at_nano <= f.decision_nano:
                expected_day = daily
        # day 2's daily is late: early day-3 minutes must fall back to day 1
        if day_index == 2 and f.decision_nano < day2_late_available:
            assert expected_day is daily_by_day[sessions[0].trading_day]
        assert expected_day is not None
        assert f.daily_background_id == expected_day.snapshot_id
    assert none_group == 2 * slots and with_group == 2 * minute_total - 2 * slots

    # cold/hot rerun byte identity (specs hoist and caches are pure reuse)
    encoded = [encode_record(f) for f in snapshots]
    _t7cpu_clear_caches()
    again, _, _ = _build_snapshots(bundle, config, split)
    assert [encode_record(f) for f in again] == encoded


def test_t7cpu_six_duration_snapshots_cold_hot_byte_identical():
    """All six frozen feature durations produce the identical complete
    FeatureSnapshot file bytes on a cold run and after a full cache clear,
    with the exact per-duration population and canonical ordering."""
    from ak_trading.v2.schemas import encode_record_file
    from ak_trading.v2.validation.experiments import _build_snapshots
    days, slots = 3, 240
    bundle, sessions, contract = _t7cpu_bundle(days, slots)
    split = _t7cpu_split(sessions)
    config = {"contract_ids": [contract],
              "feature_durations": [60, 300, 900, 1800, 3600, 86400],
              "model_fit_configs": {"t7cpu-fold": {"feature_keys": ["META:price_tick:v2.1.0"]}}}
    _t7cpu_clear_caches()
    snapshots, _seasonal, _exclusions = _build_snapshots(bundle, config, split)
    counts = {}
    for f in snapshots:
        counts[f.duration_seconds] = counts.get(f.duration_seconds, 0) + 1
    assert counts == {60: days * slots, 300: days * (slots // 5),
                      900: days * (slots // 15), 1800: days * (slots // 30),
                      3600: days * (slots // 60), 86400: days}
    assert list(snapshots) == sorted(
        snapshots, key=lambda f: (f.decision_nano, f.contract_id, f.duration_seconds))
    first = encode_record_file("FeatureSnapshot", "ak_probability_features_v2.1.0",
                               snapshots)
    _t7cpu_clear_caches()
    again, _, _ = _build_snapshots(bundle, config, split)
    second = encode_record_file("FeatureSnapshot", "ak_probability_features_v2.1.0",
                                again)
    assert second == first


# ---------------------------------------------------------------------------
# T7 memory consumer wiring regressions (t7-wiring-owner-001/002, 2026-10-26,
# MEMORY-DISPOSITION §3/§6): the fold's real logical collections live in the
# compact FoldRecords store; these tests pin baseline-vs-view equivalence in
# complete canonical bytes/SHA and model identity, and the store lifecycle.
# ---------------------------------------------------------------------------

def _t7w_world(days: int = 14, slots: int = 30):
    """Small real SYNTHETIC minute+daily world that drives ``_fit_fold`` to a
    real model set (tuned in t7-wiring-owner-002/work/probe_world.py)."""
    from decimal import Decimal as D
    from ak_trading.v2.contracts import (DataQuality, FeeLeg, FrozenInput,
                                         Instrument, MarketBar, TradingRule)
    quality = DataQuality("READY", ("SYNTHETIC",), "reconstructed", "a" * 64)
    sessions = tuple(SessionWindow(
        f"day-{i}", "SHFE.RB", date(2026, 1, 5) + timedelta(days=i),
        (i * 1440 + 60) * MINUTE, (i * 1440 + 60 + slots) * MINUTE,
        "DAY", "SYNTHETIC", "SCENARIO") for i in range(days))
    contract = "RB2610"
    leg = FeeLeg(D(1), D(0), False)
    rule = TradingRule("t7w-rule", contract, sessions[0].start_nano,
                       sessions[-1].end_nano + 1, sessions[0].start_nano,
                       D(10), D('.1'), 1, 1, leg, leg, leg, D('.1'), D('.1'),
                       D(1000), D(1), D(0), D(0), D('.1'), "SCENARIO", "1" * 64)
    bars = []
    for i, session in enumerate(sessions):
        for j in range(slots):
            start = session.start_nano + j * MINUTE
            close = D(100 + i) + D(j) / D(4) + (D("0.05") if j % 2 else D(0))
            bars.append(MarketBar(
                f"m-{i}-{j}", contract, "rb2610", "SHFE.RB", 60, start, start,
                start + MINUTE, session.trading_day, session.session_id,
                start + MINUTE, start + MINUTE, close, close + D(1),
                close - D(1), close, D(100 + i), D(1000 + i), D(1000 + i),
                True, 1, 1, "SYNTHETIC", quality))
        minute_closes = [b.close for b in bars if b.duration_seconds == 60
                         and b.trading_day == session.trading_day]
        bars.append(MarketBar(
            f"d-{i}", contract, "rb2610", "SHFE.RB", 86400,
            session.start_nano, session.start_nano, session.end_nano,
            session.trading_day, session.session_id, session.end_nano,
            session.end_nano, minute_closes[0], max(minute_closes) + D(1),
            min(minute_closes) - D(1), minute_closes[-1], D(100 + i),
            D(1000 * slots), D(1000 * slots), True, slots, slots,
            "SYNTHETIC", quality))
    instrument = Instrument(contract, "rb2610", "SHFE.RB", "synthetic",
                            ("synthetic",), sessions[0].start_nano, None,
                            None, None, quality)
    return FrozenInput(Path("SYNTHETIC"), "f" * 64, (instrument,), sessions,
                       tuple(bars), (), (rule,)), sessions, contract


def test_t7w_fit_fold_view_wiring_baseline_equivalence():
    """Real fold through the wired views versus the original tuple path:
    identical model set, content hash, evidence, fitted models and source
    hashes (public tuple entry over the fold's own materialized segments),
    complete canonical file bytes/SHA equality between ``encode_record_file``
    and the streaming writer over the views, real populations and order, and
    the store lifecycle (close never fakes an empty collection)."""
    import hashlib
    import io as _io
    from ak_trading.v2._fold_records import RecordView, StoreClosedError, stream_record_file
    from ak_trading.v2.contracts import FeatureSnapshot, ForecastRequest
    from ak_trading.v2.probability.models import default_fit_config, fit_forecast_models
    from ak_trading.v2.schemas import decode_record_file, encode_record_file
    from ak_trading.v2.strategies.registry import build_strategy_spec
    from ak_trading.v2.validation.experiments import _fit_fold
    from ak_trading.v2.validation.splits import SplitSpec

    bundle, sessions, contract = _t7w_world(14, 30)
    train_end = sessions[6].end_nano
    cal_end = sessions[10].end_nano
    test_end = sessions[-1].end_nano + 1
    split = SplitSpec("t7w-fold", sessions[0].start_nano, train_end,
                      train_end, cal_end, cal_end, test_end, 60, 1, False)
    fit = dict(default_fit_config(("META:price_tick:v2.1.0",), train_end,
                                  cal_end, test_end))
    fit.update({"inner_split_fraction": 0.6, "inner_minimum_train_days": 2,
                "inner_minimum_validation_days": 1, "inner_embargo_days": 1})
    config = {"contract_ids": [contract], "feature_durations": [60, 86400],
              "macro_mode": "PRICE_ONLY", "synthetic": True,
              "minute_horizons": [1, 5], "daily_horizons": [1],
              "evaluation_as_of_nano": test_end,
              "model_fit_configs": {"t7w-fold": fit}}
    specs = (build_strategy_spec("TREND", "MINUTE"),)
    fold = _fit_fold(bundle, config, split, specs)

    snapshots_view = fold["snapshots"]
    requests_view = fold["requests"]
    assert isinstance(snapshots_view, RecordView) and snapshots_view.record_kind == "FeatureSnapshot"
    assert isinstance(requests_view, RecordView) and requests_view.record_kind == "ForecastRequest"
    store = fold["record_store"]
    assert store is snapshots_view.store is requests_view.store
    assert fold["models"] is not None, "probe world must fit a real model set"
    expected_snapshots = 14 * 30 + 14
    assert len(snapshots_view) == expected_snapshots and len(requests_view) > 0
    order = [snapshots_view.meta(i) for i in range(len(snapshots_view))]
    keys = [(m.decision_nano, m.contract_id, m.duration_seconds) for m in order]
    assert keys == sorted(keys)

    snapshots_tuple = tuple(snapshots_view)
    requests_tuple = tuple(requests_view)
    features_bytes = encode_record_file(
        "FeatureSnapshot", "ak_probability_features_v2.1.0", snapshots_tuple)
    buffer = _io.BytesIO()
    count, sha = stream_record_file(
        "FeatureSnapshot", "ak_probability_features_v2.1.0", snapshots_view, buffer)
    assert buffer.getvalue() == features_bytes
    assert count == len(features_bytes)
    assert sha == hashlib.sha256(features_bytes).hexdigest()
    requests_bytes = encode_record_file(
        "ForecastRequest", "ak_probability_requests_v2.1.0", requests_tuple)
    buffer = _io.BytesIO()
    count, sha = stream_record_file(
        "ForecastRequest", "ak_probability_requests_v2.1.0", requests_view, buffer)
    assert buffer.getvalue() == requests_bytes and count == len(requests_bytes)
    assert sha == hashlib.sha256(requests_bytes).hexdigest()
    assert decode_record_file(features_bytes, FeatureSnapshot,
                              "ak_probability_features_v2.1.0") == snapshots_tuple
    assert decode_record_file(requests_bytes, ForecastRequest,
                              "ak_probability_requests_v2.1.0") == requests_tuple

    # baseline: the original tuple orchestration over the fold's own outputs
    by_partition = {a.sample_id: a.partition for a in fold["assignments"]}
    labels_map = {l.request_id: l for l in fold["labels"]}

    def segment(name):
        chosen = [r for r in requests_tuple
                  if by_partition[r.request_id] == name
                  and labels_map.get(r.request_id) is not None
                  and labels_map[r.request_id].status == "MATURE"]
        return (tuple(chosen), tuple(labels_map[r.request_id] for r in chosen))

    train_requests, train_labels = segment("TRAIN")
    calibration_requests, calibration_labels = segment("CALIBRATION")
    assert train_requests and calibration_requests

    def fs(requests):
        return tuple({r.features.snapshot_id: r.features for r in requests}.values())

    def rs(requests):
        return tuple(fold["regimes"][f.snapshot_id] for f in fs(requests))

    baseline = fit_forecast_models(
        fs(train_requests), rs(train_requests), train_requests, train_labels,
        fs(calibration_requests), rs(calibration_requests),
        calibration_requests, calibration_labels, fit, fold["reference"],
        seasonality=None)
    wired = fold["models"]
    assert baseline.model_set_id == wired.model_set_id
    assert baseline.content_hash() == wired.content_hash()
    assert baseline.evidence == wired.evidence
    assert dict(baseline.models) == dict(wired.models)
    assert dict(baseline.source_hashes) == dict(wired.source_hashes)

    # lifecycle: close releases handles, never fakes the collections
    store.seal()
    store.close()
    with pytest.raises(StoreClosedError):
        snapshots_view[0]
    assert len(snapshots_view) == expected_snapshots
    assert len(requests_view) == len(requests_tuple)
    store.close()  # idempotent


def test_t7w_execute_validation_streams_canonical_bytes(tmp_path, monkeypatch):
    """Real smoke through the streaming artifact pipeline: every manifest
    entry matches the re-hashed bytes on disk, published record files carry
    exactly the canonical envelope bytes (``encode_record_file`` equality,
    including the non-empty streamed TradingRule file), shared artifacts are
    byte-identical across two runs on the same frozen inputs, and no
    streaming temp file or directory is left behind.  The expensive real
    inner fold is replaced by its own documented insufficient return shape
    (the accepted ``_empty_fold`` pattern): with the config-frozen daily
    horizons the embargo rule needs a >20-trading-day calibration segment,
    so any small real outer fit is the miniworld-scale cold chain that is
    capacity-blocked here — the real inner path stays covered by the T7
    cold-chain obligations, not by this unit."""
    import hashlib
    from ak_trading.v2.schemas import encode_record_file, parse_json_bytes
    from ak_trading.v2.validation import experiments as ex
    from test_v2_m5_payoff_integration import _bypass_lock, _cached_world, _config
    bundle, sessions, models, reference = _cached_world()
    _bypass_lock(monkeypatch)

    empty_inner = {"snapshots": (), "seasonal": None, "reference": reference,
                   "requests": (), "labels": (), "assignments": (), "models": None,
                   "forecasts": (), "exclusions": []}
    frozen_outer = {"snapshots": (), "seasonal": None, "reference": reference,
                    "requests": (), "labels": (), "assignments": (), "models": models,
                    "forecasts": (), "exclusions": []}

    def dispatcher(bundle_, config_, split_, specs_):
        return frozen_outer if not split_.split_id.endswith("-payoff-inner") \
            else empty_inner

    monkeypatch.setattr(ex, "_fit_fold", dispatcher)
    outputs = []
    # Short one-level output names: pytest's temp base is deep, and the
    # published accounts/<64-hex>/... artifact paths must stay under the
    # Windows MAX_PATH limit (the pre-existing atomic_write boundary).
    for tag in ("a", "b"):
        config = _config(bundle, sessions, models, run_id="t7w-stream-" + tag)
        monkeypatch.setattr(ex, "RUN_ROOT", tmp_path / ("r" + tag))
        output = tmp_path / tag
        ex._execute_validation(bundle, config, output, "smoke")
        outputs.append(output)
    for output in outputs:
        manifest = parse_json_bytes((output / "manifest.json").read_bytes())
        actual = {p.relative_to(output).as_posix()
                  for p in output.rglob("*")
                  if p.is_file() and p != output / "manifest.json"}
        assert set(manifest["files"]) == actual
        for rel, sha in manifest["files"].items():
            assert hashlib.sha256((output / rel).read_bytes()).hexdigest() == sha, rel
        assert not [p for p in output.rglob("*") if p.suffix == ".tmp"]
        assert not [p for p in output.parent.iterdir() if "streaming" in p.name]
    left, right = outputs
    # experiment.json/readiness-adjacent files embed the differing run_id
    # identity; every other artifact must be byte-identical across runs.
    identity = {"manifest.json", "experiment.json", "report.md"}
    shared = sorted(p.relative_to(left).as_posix() for p in left.rglob("*")
                    if p.is_file() and p.name not in identity)
    assert shared
    for rel in shared:
        assert (left / rel).read_bytes() == (right / rel).read_bytes(), rel
    for rel in ("folds/r4-unit-fold/features.json", "folds/r4-unit-fold/requests.json",
                "splits.csv", "model_activations.json", "macro_comparison.json"):
        assert (left / rel).is_file() and (left / rel).read_bytes() == (right / rel).read_bytes()
    # published record files are exactly the canonical encoder's bytes
    assert (left / "folds/r4-unit-fold/features.json").read_bytes() == encode_record_file(
        "FeatureSnapshot", "ak_probability_features_v2.1.0", ())
    assert (left / "folds/r4-unit-fold/requests.json").read_bytes() == encode_record_file(
        "ForecastRequest", "ak_probability_requests_v2.1.0", ())
    streamed_rules = sorted((left).rglob("accounts/*/rules.json"))
    assert streamed_rules, "replay accounts must publish streamed rule files"
    for rules_path in streamed_rules:
        from ak_trading.v2.schemas import decode_record_file
        from ak_trading.v2.contracts import TradingRule
        decoded = decode_record_file(rules_path.read_bytes(), TradingRule,
                                     "ak_probability_rules_v2.1.0")
        assert decoded and rules_path.read_bytes() == encode_record_file(
            "TradingRule", "ak_probability_rules_v2.1.0", decoded)


# ---------------------------------------------------------------------------
# author007 regressions (t7-wiring-owner-004): W1 fs(name) exact dict
# semantics, and the real inner consumer lifecycle through
# ``_fit_training_payoffs`` → actual inner split/config → real ``_fit_fold``.
# ---------------------------------------------------------------------------

def _t7w_fs_snapshot(snapshot_id: str, price: str, decision: int):
    """Hand-built FeatureSnapshot with a free-text id and exact Decimal
    representation (equal-value/different-encoding duplicates by design)."""
    from decimal import Decimal as D
    from ak_trading.v2.contracts import DataQuality, FactorValue, FeatureSnapshot
    quality = DataQuality("READY", (), "observed", "0" * 64)
    values = (FactorValue("META:price_tick:v2.1.0", D("1"), 1, quality),)
    return FeatureSnapshot(
        snapshot_id=snapshot_id, contract_id="RB2610", raw_symbol="rb2610",
        product="SHFE.RB", trading_day=date(2026, 1, 5), session_id="s0",
        session_kind="DAY", decision_nano=decision, available_at_nano=decision,
        duration_seconds=60, source_bar_ids=("b0", "b1"),
        reference_price=D(price), atr_points=D("2"), values=values,
        daily_background_id=None, quality=quality)


def _t7w_fs_request(request_id: str, features, decision: int):
    from decimal import Decimal as D
    from ak_trading.v2.contracts import ForecastRequest, Horizon
    return ForecastRequest(
        request_id=request_id, features=features,
        horizon=Horizon("TRADING_MINUTE", 1), window_start_nano=decision,
        window_end_nano=decision + MINUTE, barrier_key="ATR_1",
        upper_distance=D("2"), lower_distance=D("2"), frozen_range=None)


def test_t7w_partition_feature_view_first_key_last_real_representation():
    """W1: order A1, B, A2 yields key order A, B with values A2, B; a same-id
    record outside the partition never influences the selection; equal-value
    different-encoding duplicates keep the ORIGINAL LAST representation
    (exact canonical bytes, not id/hash equality); same-id different-value
    also takes the last request's value; missing ids and unrepresentable
    encodings fail loudly.  The store populations never change."""
    from ak_trading.v2._fold_records import FoldRecords
    from ak_trading.v2.schemas import canonical_json_bytes, encode_record
    from ak_trading.v2.validation.experiments import _partition_feature_view

    def record_bytes(record):
        return canonical_json_bytes(encode_record(record))

    a1 = _t7w_fs_snapshot("A", "100.0", 10)
    b = _t7w_fs_snapshot("B", "50.0", 20)
    a2 = _t7w_fs_snapshot("A", "100.00", 30)
    a_outside = _t7w_fs_snapshot("A", "100.000", 40)
    assert a1.reference_price == a2.reference_price == a_outside.reference_price
    assert record_bytes(a1) != record_bytes(a2) != record_bytes(a_outside)

    store = FoldRecords(cache_max_records=2, cache_max_bytes=100_000)
    for snapshot in (a1, b, a2, a_outside):
        store.append_snapshot(snapshot)
    for request in (_t7w_fs_request("r1", a1, 11), _t7w_fs_request("r2", b, 21),
                    _t7w_fs_request("r3", a2, 31),
                    _t7w_fs_request("r-out", a_outside, 41)):
        store.append_request(request)
    partition = store.request_view().select([0, 1, 2])  # excludes r-out

    view, order = _partition_feature_view(store, partition)
    assert order == ["A", "B"]
    assert len(view) == 2
    assert [view.meta(i).snapshot_id for i in range(len(view))] == ["A", "B"]
    assert record_bytes(view[0]) == record_bytes(a2)          # last in-partition value
    assert record_bytes(view[0]) != record_bytes(a1)          # not the first store slot
    assert record_bytes(view[0]) != record_bytes(a_outside)   # outside records never win
    assert record_bytes(view[1]) == record_bytes(b)
    assert store.logical_snapshot_count == 4 and store.logical_request_count == 4

    # same id, DIFFERENT value: the last request's value wins (dict semantics)
    other = FoldRecords(cache_max_records=2, cache_max_bytes=100_000)
    v1 = _t7w_fs_snapshot("A", "100.0", 10)
    v2 = _t7w_fs_snapshot("A", "200.00", 30)
    other.append_snapshot(v1); other.append_snapshot(v2)
    other.append_request(_t7w_fs_request("r1", v1, 11))
    other.append_request(_t7w_fs_request("r2", v2, 31))
    view, order = _partition_feature_view(other, other.request_view())
    assert order == ["A"] and record_bytes(view[0]) == record_bytes(v2)

    # an id never appended as a snapshot fails loudly
    ghost_store = FoldRecords(cache_max_records=2, cache_max_bytes=100_000)
    ghost = _t7w_fs_snapshot("Z", "1.0", 10)
    ghost_store.append_snapshot(_t7w_fs_snapshot("A", "1.0", 10))
    ghost_store.append_request(_t7w_fs_request("rz", ghost, 11))
    with pytest.raises(Exception, match="request_features_snapshot_missing_from_store"):
        _partition_feature_view(ghost_store, ghost_store.request_view())

    # a nested representation that no appended snapshot carries fails loudly
    unrep = FoldRecords(cache_max_records=2, cache_max_bytes=100_000)
    unrep.append_snapshot(_t7w_fs_snapshot("A", "100.0", 10))
    unrep.append_request(_t7w_fs_request("r1", _t7w_fs_snapshot("A", "77.7", 30), 31))
    with pytest.raises(Exception, match="request_features_representation_missing_from_store"):
        _partition_feature_view(unrep, unrep.request_view())


def _t7w_payoff_world(days: int = 44, slots: int = 30):
    """Small ramp world whose REAL inner payoff chain fits the budget: the
    actual inner split exists (V=24/E=1 over the 41-day outer train window),
    the real inner ``_fit_fold`` produces a model set (1269 snapshots /
    17664 requests at the frozen probe), and the payoff windows really
    replay (bounded probe: t7-wiring-owner-004/work/probe-44d*)."""
    from decimal import Decimal as D
    from ak_trading.v2.contracts import (DataQuality, FeeLeg, FrozenInput,
                                         Instrument, MarketBar, TradingRule)
    quality = DataQuality("READY", ("SYNTHETIC",), "reconstructed", "a" * 64)
    sessions = tuple(SessionWindow(
        f"day-{i}", "SHFE.RB", date(2026, 1, 5) + timedelta(days=i),
        (i * 1440 + 60) * MINUTE, (i * 1440 + 60 + slots) * MINUTE,
        "DAY", "SYNTHETIC", "SCENARIO") for i in range(days))
    contract = "RB2610"
    leg = FeeLeg(D(1), D(0), False)
    rule = TradingRule("t7w-payoff-rule", contract, sessions[0].start_nano,
                       sessions[-1].end_nano + 1, sessions[0].start_nano,
                       D(10), D('.1'), 1, 1, leg, leg, leg, D('.1'), D('.1'),
                       D(1000), D(1), D(0), D(0), D('.1'), "SCENARIO", "1" * 64)
    bars = []
    for i, session in enumerate(sessions):
        for j in range(slots):
            start = session.start_nano + j * MINUTE
            close = D(100) + D("0.5") * D(i * slots + j)
            bars.append(MarketBar(
                f"m-{i}-{j}", contract, "rb2610", "SHFE.RB", 60, start, start,
                start + MINUTE, session.trading_day, session.session_id,
                start + MINUTE, start + MINUTE, close, close + D(1),
                close - D(1), close, D(100), D(1000), D(1000),
                True, 1, 1, "SYNTHETIC", quality))
        closes = [b.close for b in bars if b.duration_seconds == 60
                  and b.trading_day == session.trading_day]
        bars.append(MarketBar(
            f"d-{i}", contract, "rb2610", "SHFE.RB", 86400,
            session.start_nano, session.start_nano, session.end_nano,
            session.trading_day, session.session_id, session.end_nano,
            session.end_nano, closes[0], max(closes) + D(1),
            min(closes) - D(1), closes[-1], D(100),
            D(1000 * slots), D(1000 * slots), True, slots, slots,
            "SYNTHETIC", quality))
    instrument = Instrument(contract, "rb2610", "SHFE.RB", "synthetic",
                            ("synthetic",), sessions[0].start_nano, None,
                            None, None, quality)
    return FrozenInput(Path("SYNTHETIC"), "d" * 64, (instrument,), sessions,
                       tuple(bars), (), (rule,)), sessions, contract


def test_t7w_fit_training_payoffs_real_inner_consumer_lifecycle(monkeypatch):
    """The real inner chain (author007 §3): ``_fit_training_payoffs`` derives
    its actual inner split/config and calls the REAL ``_fit_fold`` (observed
    by a record-only wrapper) reaching a real model with non-empty records;
    the store is readable while real consumers run, released after success
    AND after an in-chain error, closed views keep their real population,
    and the extracted payoff/payload consumers stay valid after close.
    Natural OPENs are NOT fabricated here — honest NO_CANDIDATE windows are
    reported as-is (positive T7 evidence remains a later obligation)."""
    from ak_trading.v2._fold_records import StoreClosedError
    from ak_trading.v2.probability.models import default_fit_config
    from ak_trading.v2.strategies.registry import build_strategy_spec
    from ak_trading.v2.validation import experiments as ex
    from ak_trading.v2.validation.splits import SplitSpec

    bundle, sessions, contract = _t7w_payoff_world(44, 30)
    train_end = sessions[40].end_nano
    cal_end = sessions[42].end_nano
    test_end = sessions[-1].end_nano + 1
    split = SplitSpec("t7w-payoff", sessions[0].start_nano, train_end,
                      train_end, cal_end, cal_end, test_end, 60, 1, False)
    fit = dict(default_fit_config(("META:price_tick:v2.1.0",), train_end,
                                  cal_end, test_end))
    fit.update({"seed": 7, "inner_split_fraction": 0.3,
                "inner_minimum_train_days": 2, "inner_minimum_validation_days": 24,
                "inner_embargo_days": 1})
    config = {"contract_ids": [contract], "feature_durations": [60, 86400],
              "macro_mode": "PRICE_ONLY", "synthetic": True,
              "minute_horizons": [1, 5, 15, 30, 60], "daily_horizons": [1, 5, 20],
              "evaluation_as_of_nano": test_end,
              "model_fit_configs": {"t7w-payoff": fit},
              "initial_cash_cny": "1000000", "run_policies": [], "seed": 7,
              "research_policy_version": "research-policy-v2.1.0",
              "macro_policy_version": "macro-risk-v2.1.0"}
    specs = (build_strategy_spec("TREND", "MINUTE"),)
    inner, days = ex._payoff_inner_split(bundle, config, split)
    assert inner is not None and days and len(days) == 41
    planned, skipped = ex._payoff_training_plans(bundle, config, split, specs)
    assert planned, "the real inner-OOS windows must exist"

    captured = {}
    real_fit = ex._fit_fold

    def observer(bundle_, config_, split_, specs_):
        fold = real_fit(bundle_, config_, split_, specs_)
        if split_.split_id.endswith("-payoff-inner"):
            store = fold["record_store"]
            snapshots_view = fold["snapshots"]
            captured.update({
                "snapshots": len(snapshots_view),
                "requests": len(fold["requests"]),
                "models_none": fold["models"] is None,
                "store_open_during": not store.closed,
                "meta_readable_during":
                    [snapshots_view.meta(i).snapshot_id for i in range(3)],
                "store": store, "snapshots_view": snapshots_view,
                "models": fold["models"], "reference": fold["reference"]})
        return fold

    monkeypatch.setattr(ex, "_fit_fold", observer)
    payoff, payloads, exclusions = ex._fit_training_payoffs(
        bundle, config, split, specs, "1" * 64, plans=(planned, skipped))

    # real inner fold, real models, real populations (frozen probe numbers)
    assert captured["models_none"] is False
    assert captured["snapshots"] == 1269 and captured["requests"] == 17664
    assert captured["store_open_during"] and len(captured["meta_readable_during"]) == 3
    # released after success; closed views keep the real population
    store = captured["store"]
    assert store.closed
    assert len(captured["snapshots_view"]) == captured["snapshots"]
    with pytest.raises(StoreClosedError):
        captured["snapshots_view"][0]
    # extracted consumers remain valid after close
    assert payoff is not None
    assert captured["models"] is not None and captured["reference"] is not None
    window_files = {name.rsplit("/", 1)[-1] for name in payloads}
    assert {"plan.json", "path.json", "opportunity.json", "manifest.json"} <= window_files
    assert [name for name in payloads if name.endswith("opportunity.json")], \
        "every planned window records its opportunity"

    # error exit: a failure in a real post-fold consumer (payoff window)
    # still releases the delivered store and propagates (no silent
    # completion); an in-fold failure is released by _fit_fold's own guard.
    captured.clear()

    def failing_opportunity(*args, **kwargs):
        raise RuntimeError("T7W_LIFECYCLE_ERROR_PROBE")

    monkeypatch.setattr(ex, "_training_opportunity", failing_opportunity)
    with pytest.raises(RuntimeError, match="T7W_LIFECYCLE_ERROR_PROBE"):
        ex._fit_training_payoffs(bundle, config, split, specs, "1" * 64,
                                 plans=(planned, skipped))
    assert store.closed

    # in-fold failure: an exception raised inside the real ``_fit_fold``
    # (after the store exists) is released by the fold's own guard; the
    # store is captured at creation through a record-only factory wrapper.
    created = []
    real_store_factory = ex._new_record_store

    def recording_store():
        created_store = real_store_factory()
        created.append(created_store)
        return created_store

    from ak_trading.v2.probability import models as models_module

    def failing_from_views(*args, **kwargs):
        raise RuntimeError("T7W_INFOLD_ERROR_PROBE")

    monkeypatch.setattr(ex, "_new_record_store", recording_store)
    monkeypatch.setattr(models_module, "_fit_forecast_models_from_views",
                        failing_from_views)
    with pytest.raises(RuntimeError, match="T7W_INFOLD_ERROR_PROBE"):
        ex._fit_training_payoffs(bundle, config, split, specs, "1" * 64,
                                 plans=(planned, skipped))
    assert created and created[-1].closed
