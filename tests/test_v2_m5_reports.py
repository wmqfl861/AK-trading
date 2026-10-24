from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import pytest

from ak_trading.v2.contracts import RunPolicy
from ak_trading.v2.schemas import canonical_json_bytes,decode_shared_fixture
from ak_trading.v2.validation.experiments import (ExperimentSpec,HoldoutScope,digest,COST_SCENARIOS,ExperimentError)
from ak_trading.v2.validation.splits import SplitSpec
from ak_trading.v2.validation.reports import (empty_registered_metrics,build_readiness,write_validation_report,
                                            validate_metrics,build_strategy_qualifications)


def experiment():
    from ak_trading.v2.strategies.registry import strategy_specs_v2,select_exit_binding
    from ak_trading.v2.exits.registry import exit_registry_specs
    from ak_trading.v2.decision.pipeline import exit_registry_hash_for
    _sp=strategy_specs_v2("MINUTE")
    _spec=next((x for x in _sp if x.strategy_id=="FIX-TREND-1"), _sp[0])
    _binding=select_exit_binding(_spec)
    _all=tuple(sorted({x.exit_policy_id:x for x in (*exit_registry_specs("MINUTE"),*exit_registry_specs("DAILY"))}.values(),key=lambda x:x.exit_policy_id))
    scope=HoldoutScope("CN_FUTURES_V2",("SYN.rb",),("SYN.rb2609",),100,200,300,"1"*64)
    policy=RunPolicy("RESEARCH_EVALUATION","FIXED",7,_spec.strategy_id,_binding.binding_sha256,"research-policy-v2.1.0","macro-risk-v2.1.0","PRICE_ONLY","BASE")
    key=canonical_json_bytes(["STRATEGY","fold",digest(policy),_spec.strategy_id,"SYN.rb","SYN.rb2609","MINUTE","TRADING_MINUTE",60,"PRICE_ONLY","BASE",_binding.binding_sha256]).decode().strip()
    return ExperimentSpec("reports","reports",("2"*64,),"3"*64,exit_registry_hash_for(_all),(_binding,),"4"*64,"5"*64,
                          (SplitSpec("fold",0,20,20,50,50,90,60,20,False),),digest(scope),scope,(policy,),(key,),7,
                          "research-policy-v2.1.0","macro-risk-v2.1.0","PRICE_ONLY",400,"a"*64)


def specs():
    f=decode_shared_fixture((Path(__file__).parent/"fixtures/v2/shared.json").read_bytes())
    # Qualification iterates (entry spec x registered binding); the shared fixture's
    # synthetic FIX-TREND-1 has no registered binding, so evaluate the real spec
    # whose binding the experiment registered.
    from ak_trading.v2.strategies.registry import strategy_specs_v2
    return (next(x for x in strategy_specs_v2("MINUTE") if x.strategy_id=="breakout_m1_v2"),)


def models():
    return {"forecast":"6"*64,"payoff":"7"*64,"regime_reference":"8"*64}


def test_required_empty_group_retained_and_engineering_separate(tmp_path):
    e=experiment();m=empty_registered_metrics(e,models())
    r=build_readiness(m,[],specs(),"PASS","OFFLINE_ONLY",("SYNTHETIC","REAL_FEED_NOT_ACCEPTED"))
    assert r["engineering"]=="PASS" and r["statistics"]=="INSUFFICIENT" and r["m5_complete"] is False
    assert r["strategies"][0]["stage"]=="RESEARCH"
    assert "NO_TRADE" in r["strategies"][0]["reasons"]
    h=write_validation_report(tmp_path/"report",e,m,r)
    assert h==digest(m)
    assert "未完成" in (tmp_path/"report/report.md").read_text(encoding="utf-8")
    assert m["strategy_groups"][0]["episodes"]==0


def test_failure_cannot_be_hidden_by_top_level_pass(tmp_path):
    e=experiment();m=empty_registered_metrics(e,models())
    m["strategy_groups"][0]["status"]="FAIL"
    r=build_readiness(m,[],specs(),"PASS","SHADOW_PENDING",("WAITING",))
    assert r["statistics"]=="FAIL"
    r["statistics"]="PASS"
    with pytest.raises(ExperimentError,match="worst"):
        write_validation_report(tmp_path/"bad",e,m,r)


def test_required_scope_and_six_cost_scenarios_cannot_be_deleted():
    e=experiment();m=empty_registered_metrics(e,models())
    m["strategy_groups"]=[]
    with pytest.raises(ExperimentError,match="incomplete"):
        validate_metrics(m,e)
    m=empty_registered_metrics(e,models());m["cost_scenarios"]=["BASE"]
    with pytest.raises(ExperimentError,match="registration"):
        validate_metrics(m,e)


def test_qualification_not_inherited_by_new_model_and_insufficient_stays_closed():
    e=experiment();m=empty_registered_metrics(e,models())
    q=build_strategy_qualifications(specs(),e,models(),m,digest(m),"9"*64,1000)
    assert len(q)==1 and q[0].statistics=="INSUFFICIENT"
    assert q[0].cost_scenarios==COST_SCENARIOS
    assert q[0].valid_until_nano-q[0].issued_at_nano==30*86400*10**9
    assert "SIX_SCENARIOS_REQUIRED" in q[0].reasons
    with pytest.raises(ExperimentError,match="not_evaluated"):
        build_strategy_qualifications(specs(),e,{**models(),"forecast":"a"*64},m,digest(m),"9"*64,1000)


def test_cannot_mark_complete_without_real_shadow():
    e=experiment();m=empty_registered_metrics(e,models())
    with pytest.raises(ExperimentError,match="real_shadow"):
        build_readiness(m,[],specs(),"PASS","OFFLINE_ONLY",(),True)
