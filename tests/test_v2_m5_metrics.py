from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import pytest

from ak_trading.v2.contracts import DataQuality, PathLabel, PERSISTENCE_EVENTS
from ak_trading.v2.schemas import decode_shared_fixture
from ak_trading.v2.validation.metrics import (binary_brier, multiclass_brier, block_bootstrap,
                                             evaluate_forecasts, worst_status)


def prediction():
    fixture = decode_shared_fixture((Path(__file__).parent / "fixtures/v2/shared.json").read_bytes())
    return fixture["forecasts"][0]


def label(f, direction="UP", status="MATURE"):
    return PathLabel(f.request_id, f.contract_id, f.label_version, f.horizon, f.window_start_nano,
                     f.window_end_nano, f.window_end_nano if status == "MATURE" else None,
                     status, (), direction if status == "MATURE" else None, .001, None, .1,
                     "UP_FIRST" if status == "MATURE" else None, 1, None, None, None, None,
                     {k: None for k in PERSISTENCE_EVENTS}, {}, f.frozen_range.range_id if f.frozen_range else None)


def test_binary_and_multiclass_hand_calculation():
    assert binary_brier([.8,.2],[1,0]) == pytest.approx(.04)
    assert multiclass_brier([{"UP":.8,"DOWN":.1,"NEUTRAL":.1}], ["UP"]) == pytest.approx(.06)
    with pytest.raises(ValueError):
        binary_brier([True],[1])
    with pytest.raises(ValueError):
        binary_brier([float("nan")],[1])


def test_bootstrap_dates_reproducible_and_copying_minutes_does_not_add_blocks():
    assert block_bootstrap([1,2,3],["a","a","a"],7) == (None,None)
    a=block_bootstrap([1,2,3],["a","b","c"],7)
    assert a == block_bootstrap([1,1,2,2,3,3],["a","a","b","b","c","c"],7)
    assert a == block_bootstrap([1,2,3],["a","b","c"],7)


def test_late_counted_before_missing_or_unmatured_label():
    f=prediction()
    f=replace(f,published_at_nano=f.window_start_nano,expires_at_nano=f.window_end_nano,
              quality=replace(f.quality,status="INVALID",reasons=("FORECAST_LATE",)))
    for labels in ((),(label(f,status="UNMATURED"),)):
        result=evaluate_forecasts((f,),labels,{},7)
        assert result["exclusions"] == {"FORECAST_LATE":1}
        assert result["forecast_groups"] == []


def test_late_end_and_after_end_keep_expiry_and_single_request_exclusion():
    f=prediction()
    for published in (f.window_start_nano,f.window_end_nano,f.window_end_nano+1):
        late=replace(f,forecast_id=f"late-{published}",published_at_nano=published,expires_at_nano=f.window_end_nano,
                     quality=replace(f.quality,status="INVALID",reasons=("FORECAST_LATE",)))
        result=evaluate_forecasts((late,late),(),{},7)
        assert result["exclusions"]=={"FORECAST_LATE":1}
        assert result["forecast_groups"]==[]


def test_same_date_thousand_rows_still_one_block():
    f=prediction()
    forecasts=tuple(replace(f,forecast_id=f"f{i}",request_id=f"r{i}",snapshot_id=f"s{i}") for i in range(1000))
    result=evaluate_forecasts(forecasts,tuple(label(x) for x in forecasts),{x.request_id:"2026-01-09" for x in forecasts},7)
    direction=[g for g in result["forecast_groups"] if g["event"]=="DIRECTION"]
    assert all(g["n"]==1000 and g["blocks"]==1 and g["status"]=="INSUFFICIENT" for g in direction)
    blocks={x.request_id:str(i) for i,x in enumerate(forecasts)}
    with pytest.raises(ValueError, match="same_trading_day"):
        evaluate_forecasts(forecasts,tuple(label(x) for x in forecasts),blocks,7)


def test_common_barriers_not_triple_counted():
    f=prediction()
    forecasts=tuple(replace(f,forecast_id=f"f{i}",request_id=f"r{i}",barrier_key=b) for i,b in enumerate(("ATR_0.5","ATR_1","ATR_2")))
    result=evaluate_forecasts(forecasts,tuple(label(x) for x in forecasts),{x.request_id:"day" for x in forecasts},7)
    assert {g["n"] for g in result["forecast_groups"] if g["event"]=="DIRECTION"} == {1}
    assert len([g for g in result["forecast_groups"] if g["event"]=="PATH" and g["contract_id"] is not None])==6
    assert len([g for g in result["forecast_groups"] if g["event"]=="PATH" and g["contract_id"] is None])==6


def test_mismatch_missing_unmatured_and_worst_status():
    f=prediction()
    r=evaluate_forecasts((f,),(label(f,status="UNMATURED"),),{},7)
    assert r["exclusions"] == {"UNMATURED":1}
    with pytest.raises(ValueError, match="identity"):
        evaluate_forecasts((f,),(replace(label(f),contract_id="other"),),{f.request_id:"day"},7)
    assert worst_status(["PASS","FAIL","INSUFFICIENT"])=="FAIL"
    assert worst_status(["PASS","INSUFFICIENT"])=="INSUFFICIENT"
    assert worst_status([])=="INSUFFICIENT"


def replay_case(closed=True):
    from decimal import Decimal as D
    from ak_trading.v2.contracts import (AccountSnapshot,Fill,OrderIntent,ExecutionResult,LedgerCheckpoint,RunPolicy,FeeLeg,TradingRule,ValuationMark,PositionLot)
    from ak_trading.v2.decision.replay import ReplayResult
    from ak_trading.v2.validation.experiments import digest
    f=prediction();q=replace(f.quality,status="READY",reasons=())
    p=RunPolicy("RESEARCH_EVALUATION","FIXED",7,"strategy","2"*64,"research-policy-v2.1.0","macro-risk-v2.1.0","PRICE_ONLY","BASE")
    rule=TradingRule("old",f.contract_id,0,100,0,D(10),D(1),1,1,FeeLeg(D(2),D(0),False),FeeLeg(D(3),D(0),False),FeeLeg(D(3),D(0),False),D('.1'),D('.1'),D(1000),D(1),D(1),D(0),D(1),"SCENARIO","a"*64)
    new=replace(rule,rule_id="new",effective_from_nano=100,effective_until_nano=1000,available_at_nano=100,close_fee=FeeLeg(D(5),D(0),False))
    day=date(2026,1,1)
    fills=(Fill("f1","i1",f.contract_id,"strategy","round",10,day,1,2,"OPEN",D(100),D(101),D(4),D(20),"old","SCENARIO"),
           Fill("f2","i2",f.contract_id,"strategy","round",20,day,-1,1,"CLOSE_TODAY",D(104),D(103),D(3),D(10),"old","SCENARIO"),
           Fill("f3","i3",f.contract_id,"strategy","round",110,day+timedelta(days=1),-1,1,"CLOSE",D(100),D(99),D(5),D(10),"new","SCENARIO"))
    if not closed:fills=fills[:2]
    intents=tuple(OrderIntent(x.intent_id,"decision",x.strategy_id,x.episode_id,x.contract_id,x.execution_nano-2,x.execution_nano-1,x.execution_nano+5,x.side*x.lots,x.offset,"ENTRY" if x.offset=="OPEN" else "REDUCE",0 if x.offset=="OPEN" else None,None) for x in fills)
    executions=tuple(ExecutionResult(x.intent_id,(x,),0,"FILLED",()) for x in fills)
    lots=() if closed else (PositionLot("lot",f.contract_id,"strategy","round",1,1,10,day,D(101),D(2)),)
    cash=D(988) if closed else D(1013);unrealized=D(0) if closed else D(-10)
    account=AccountSnapshot("account",120 if closed else 30,day if not closed else day+timedelta(days=1),D(1000),cash,D(0) if closed else D(20),D(12) if closed else D(7),D(40) if closed else D(30),unrealized,cash+unrealized,D(0) if closed else D(100),D(0),D(1000),cash+unrealized-D(1000),D(1020),lots,False,q)
    marks=() if closed else (ValuationMark(f.contract_id,D(100),25,30,"mark","b"*64,"old","CURRENT",()),)
    from ak_trading.v2.contracts import ExitState
    from ak_trading.v2.schemas import canonical_json_bytes
    import hashlib as _hl
    from ak_trading.v2.strategies.registry import select_exit_binding as _sel
    from ak_trading.v2.strategies.registry import strategy_specs_v2 as _specs
    _spec=next(x for x in _specs("MINUTE") if x.strategy_id=="strategy") if False else None
    # fixture uses synthetic strategy id; declare binding via producer on first spec
    _any=next(iter(_specs("MINUTE")))
    _binding=_sel(_any)
    exit_states=()
    if not closed:
        _prefix=_hl.sha256(canonical_json_bytes(["account",f.contract_id,"strategy","round","10","3"*64,"initial"]).decode().encode()).hexdigest()
        exit_states=(ExitState(account_id="account",contract_id=f.contract_id,strategy_id="strategy",
            episode_id="round",first_fill_nano=10,exit_binding_sha256=_binding.binding_sha256,realized_gross_cny=D(0),
            fees_paid_cny=D(2),price_peak=None,price_peak_source_nano=None,price_peak_source_hash=None,
            net_peak_r=None,net_peak_source_nano=None,net_peak_source_hash=None,armed=False,
            protection_stop_price=None,protection_line_version=None,protection_effective_from_nano=None,
            confirmations=(),no_more_add=False,qbase=None,closed_lots=0,ladder_progress=(),
            phase="MANAGING",last_committed_event_id=None,state_prefix_hash=_prefix),)
    cp=LedgerCheckpoint(1,"c"*64,"d"*64,"9"*64,p,{r.rule_id:digest(r) for r in (rule,new)},
                        {"forecast":"e"*64,"payoff":"f"*64,"regime_reference":"1"*64},{},account,marks,(),
                        (_binding,),exit_states,(),(),{},("committed",),
                        {x.fill_id:digest(x) for x in fills},{},"0"*64,"2"*64)
    result=ReplayResult((),(),(),(),(),intents,fills,(account,),(cp,),executions,{"status":"PASS" if closed else "INCOMPLETE"},"COMPLETE" if closed else "INCOMPLETE")
    return result,(rule,new)


def test_whole_episode_fifo_old_new_rules_fee_once_and_open_round(tmp_path):
    from decimal import Decimal
    from ak_trading.v2.validation.metrics import evaluate_replay
    result,rules=replay_case()
    m=evaluate_replay(result,Decimal(1000),{"round":"2026-01-01"},7,rules=rules)
    assert m["episodes"]==1 and m["net_pnl_cny"]=="-12"
    assert m["replay_summary"]["status"]=="COMPLETE"
    assert m["replay_summary"]["closed_episode_net_pnl_cny"]=="-12"
    from ak_trading.v2.schemas import canonical_json_bytes,encode_record_file
    (tmp_path/"closed_episode_metrics.json").write_bytes(canonical_json_bytes(m))
    (tmp_path/"fills.json").write_bytes(encode_record_file("Fill","ak_probability_fills_v2.1.0",result.fills))
    (tmp_path/"rules.json").write_bytes(encode_record_file("TradingRule","ak_probability_rules_v2.1.0",rules))
    result,rules=replay_case(False)
    m=evaluate_replay(result,Decimal(1000),{},7,rules=rules)
    assert m["episodes"]==0
    assert m["replay_summary"]["open_episode_realized_net_pnl_cny"]=="13"
    assert m["replay_summary"]["terminal_unrealized_cny"]=="-10"
    assert m["replay_summary"]["terminal_equity_cny"]=="1003"


def test_missing_rule_uncommitted_fill_and_conflicting_repeat_never_score():
    from decimal import Decimal
    from ak_trading.v2.validation.metrics import evaluate_replay
    result,rules=replay_case()
    m=evaluate_replay(result,Decimal(1000),{},7,rules=rules[1:])
    assert m["episodes"]==0 and "FILL_RULE_ORIGINAL_MISSING" in m["replay_summary"]["reasons"]
    m=evaluate_replay(replace(result,checkpoints=()),Decimal(1000),{},7,rules=rules)
    assert m["episodes"]==0 and "UNCOMMITTED_FILL" in m["replay_summary"]["reasons"]
    changed=replace(result.fills[0],fee_cny=Decimal(5))
    m=evaluate_replay(replace(result,fills=(*result.fills,changed)),Decimal(1000),{},7,rules=rules)
    assert m["replay_summary"]["status"]=="FAILED"
    m=evaluate_replay(replace(result,fills=(*result.fills,result.fills[0])),Decimal(1000),{"round":"2026-01-01"},7,rules=rules)
    assert m["episodes"]==1


def test_sufficient_bad_calibration_fails_without_lowering_thresholds():
    f=replace(prediction(),direction_probabilities={"UP":.9,"DOWN":.05,"NEUTRAL":.05})
    forecasts=tuple(replace(f,forecast_id=f"f{i}",request_id=f"r{i}",snapshot_id=f"s{i}",
                           trading_day=date(2026,1,1)+timedelta(days=i//5)) for i in range(200))
    result=evaluate_forecasts(forecasts,tuple(label(x,"DOWN") for x in forecasts),{x.request_id:str(x.trading_day) for x in forecasts},7)
    ordinary=[g for g in result["forecast_groups"] if g["event"]=="DIRECTION" and "ordinary" in g["policy_id"]]
    assert ordinary[0]["status"]=="FAIL"
