from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest

from ak_trading.v2.contracts import (
    CalibrationEvidence, FactorValue, FeatureSnapshot, FrozenRange, Horizon, LayerSpec,
    MacroContext, MarketForecast, PayoffEstimate, PositionSnapshot, RegimeAssessment,
    SessionWindow, StrategyContext, StrategyProposal, StrategyQualification, TimeProbability,
)
from ak_trading.v2.policy import confidence_policy, research_policy, stress_policy
from ak_trading.v2.schemas import calibration_geometry_key
from ak_trading.v2.strategies.registry import build_strategy_spec
from ak_trading.v2.decision.gates import evaluate_gate, non_payoff_evidence_reasons
from ak_trading.v2.decision.pipeline import legacy_exit_binding
from test_v2_m4_ledger import DAY, HASH, MODELS, NS, account, bar, budget, quality, rule, run_policy, state


def example(family="TREND"):
    spec=build_strategy_spec(family,"MINUTE")
    now=100*NS
    r=rule()
    fs=FeatureSnapshot("features","rb","SHFE.rb2605","SHFE.RB",DAY,"session","DAY",now,now,60,
        ("bar-rb-5940000000000",),D(100),D(1),tuple(FactorValue(k,D(1),now,quality())
        for k in spec.required_factor_keys)+(FactorValue("META:price_tick:v2.1.0",D(".1"),now,quality()),),None,quality())
    fr=None if family=="TREND" else FrozenRange("range",now,D(98),D(101),D(104),D(94),D(108),fs.snapshot_id)
    regime=RegimeAssessment("regime",fs.snapshot_id,"reference",now,
        "UP_TREND" if family=="TREND" else "ORDERED_RANGE",(),fr,"ALIGNED",3,(),quality())
    horizon=Horizon("TRADING_MINUTE",30)
    cp=confidence_policy(family)
    ev=[("PATH", "PATH", "ALL"),("PERSIST:"+("UP_TREND" if family=="TREND" else "ORDERED_RANGE"),
        "UP_TREND" if family=="TREND" else "ORDERED_RANGE","[0.85,0.90)")]
    if family!="TREND":
        ev.append(("RETURN_BEFORE_BREAK","RETURN_BEFORE_BREAK","[0.85,0.90)"))
    if family=="MARTINGALE":
        ev.append(("STOP_TOUCH:LONG","STOP_TOUCH","[0.05,0.10]"))
    evidence=tuple(CalibrationEvidence("e-"+event,"model",cp.policy_id,"SHFE.RB","DAY",60,regime.base,
        "ATR_1" if event=="PATH" else "COMMON",calibration_geometry_key(fs,fr,geometry),event,horizon,
        "path-label-v2.1.0",bucket,600,80,.01,.1,NS,2*NS,300*NS,"VALID") for event,geometry,bucket in ev)
    keys=("Q05","Q25","Q50","Q75","Q95")
    forecast=MarketForecast("forecast","request",fs.snapshot_id,"rb","SHFE.RB",DAY,"session","DAY",60,
        regime.assessment_id,regime.base,"reference",now,now,102*NS,horizon,101*NS,131*NS,"ATR_1",
        D(100),D(1),D(1),D(1),fr,"path-label-v2.1.0","model",{"UP":.6,"DOWN":.3,"NEUTRAL":.1},
        dict(zip(keys,[-.02,-.01,0.,.01,.02])),dict(zip(keys,map(D,[-2,-1,0,1,2]))),
        dict(zip(keys,[-2.,-1.,0.,1.,2.])),{"UP_FIRST":.6,"DOWN_FIRST":.4,"NEITHER":0.,"AMBIGUOUS":0.},
        (TimeProbability("RETURN_FIRST",30,.85,.01),) if fr else (),
        {"UP_TREND" if family=="TREND" else "ORDERED_RANGE":.85},
        {"LONG":.1,"SHORT":.1} if fr else {},.85 if fr else None,evidence,False,quality())
    b=replace(budget(reserve="150"),frozen_at_nano=now,latest_exit_nano=131*NS,
        layers=(LayerSpec(0,D(100),D(100),1),),maximum_lots=1,planned_loss_cny=D(63),stress_loss_cny=D(105),
        frozen_range=fr,range_id=fr.range_id if fr else None,target_price=fr.center if fr else D(104))
    nxt=replace(state(b,(0,),"WAIT_TRIGGER"),strategy_id=spec.strategy_id,pending_exit_nano=131*NS)
    p=StrategyProposal("proposal",spec.strategy_id,b.episode_id,"rb",now,"OPEN",1,0,D(100),b.target_price,
        b.stop_price,101*NS,102*NS,forecast.forecast_id,regime.assessment_id,b,None,nxt,())
    geom=calibration_geometry_key(fs,fr,"UP_TREND" if family=="TREND" else "RETURN_BEFORE_BREAK")
    payoff=PayoffEstimate("payoff",p.proposal_id,"payoff:"+HASH,horizon,"ATR_1",geom,"opportunity",
        legacy_exit_binding(spec).binding_sha256,"OPEN_INITIAL",3*NS,
        600,80,D(15),D(10),.8,D(30),D(100),None,0.,"READY",())
    p=replace(p,payoff=payoff)
    c=StrategyContext(now,spec,legacy_exit_binding(spec),fs,(forecast,),regime,MacroContext("macro",now,(),"macro-risk-v2.1.0",(),"PRICE_ONLY",quality()),
        tuple(bar(t=i*NS) for i in range(80,100)),
        (SessionWindow("session","SHFE.RB",DAY,NS,300*NS,"DAY","synthetic","SCENARIO"),),
        account("100000"),None,r,cp,stress_policy(),run_policy(),None,MODELS,200*NS)
    return p,c


def test_full_ordinary_gate_and_exact_probability_boundaries():
    p,c=example()
    assert evaluate_gate(p,c,research_policy(),checked_at_nano=c.now_nano).eligible
    f=c.forecasts[0]
    for value,blocked in ((.699999,True),(.70,False)):
        evidence=tuple(replace(e,bucket="[0.70,0.75)") if e.event.startswith("PERSIST") else e for e in f.evidence)
        changed=replace(f,persistence_probabilities={"UP_TREND":value},evidence=evidence)
        gate=evaluate_gate(p,replace(c,forecasts=(changed,)),research_policy(),checked_at_nano=c.now_nano)
        assert ("PERSISTENCE_BELOW_THRESHOLD" in gate.reasons)==blocked
        if not blocked:
            assert gate.eligible


def test_martingale_joint_085_010_and_stop_identity():
    p,c=example("MARTINGALE")
    assert evaluate_gate(p,c,research_policy(),checked_at_nano=c.now_nano).eligible
    for value in (.100001,.6):
        f=replace(c.forecasts[0],stop_touch_probabilities={"LONG":value,"SHORT":.1})
        assert "STOP_TOUCH_ABOVE_CAP" in evaluate_gate(p,replace(c,forecasts=(f,)),research_policy(),checked_at_nano=c.now_nano).reasons
    wrong=replace(c.forecasts[0],evidence=tuple(replace(e,policy_id="confidence-ordinary-v2.1.0") for e in c.forecasts[0].evidence))
    assert "EVIDENCE_IDENTITY_MISMATCH" in evaluate_gate(p,replace(c,forecasts=(wrong,)),research_policy(),checked_at_nano=c.now_nano).reasons


@pytest.mark.parametrize("field,value,reason",[("sample_count",199,"INSUFFICIENT_SAMPLES"),
    ("effective_blocks",29,"INSUFFICIENT_BLOCKS"),("calibration_error",.050001,"CALIBRATION_FAILED"),
    ("status","FAILED","CALIBRATION_FAILED"),("model_id","wrong","EVIDENCE_IDENTITY_MISMATCH"),
    ("geometry_key","wrong","EVIDENCE_IDENTITY_MISMATCH")])
def test_each_evidence_failure_is_independent(field,value,reason):
    p,c=example()
    f=replace(c.forecasts[0],evidence=tuple(replace(e,**{field:value}) for e in c.forecasts[0].evidence))
    gate=evaluate_gate(p,replace(c,forecasts=(f,)),research_policy(),checked_at_nano=c.now_nano)
    assert not gate.eligible and reason in gate.reasons


def test_zero_ev_and_missing_path_mass_accumulate_reasons():
    p,c=example()
    p=replace(p,payoff=replace(p.payoff,conservative_ev_cny=D(0),missing_probability_mass=.1))
    g=evaluate_gate(p,replace(c,rule=None),research_policy(),checked_at_nano=c.now_nano)
    assert {"NON_POSITIVE_EV","UNSUPPORTED_PATH_MASS","COST_UNKNOWN"}<=set(g.reasons)


def test_qualification_mismatch_is_gate_rejection_exit_still_eligible():
    p,c=example()
    _binding=legacy_exit_binding(c.spec)
    _exit_hash=_binding.exit_spec_sha256s[0]
    q=StrategyQualification("qualification",c.spec.strategy_id,c.spec.version,c.spec.parameter_version,c.spec.code_sha256,
        "d"*64,_binding.binding_sha256,(_exit_hash,),
        "SHFE.RB",("rb",),"MINUTE",(c.spec.max_holding,),"PRICE_ONLY","research-policy-v2.1.0",
        "macro-risk-v2.1.0",("BASE","FEE_X2","SLIPPAGE_X2","MARGIN_X1_5","GAP","NO_STOP"),
        {**MODELS,"payoff":"b"*64},HASH,"PASS",HASH,HASH,NS,500*NS,())
    c=replace(c,run_policy=replace(c.run_policy,purpose="SHADOW_OPERATIONAL"),qualification=q)
    assert "QUALIFICATION_MISMATCH" in evaluate_gate(p,c,research_policy(),checked_at_nano=c.now_nano).reasons
    p=replace(p,action="EXIT",target_lots=0,payoff=None)
    c=replace(c,position=PositionSnapshot("rb",p.strategy_id,p.episode_id,1,D(100),True),
        forecasts=(),rule=None,regime=replace(c.regime,base="UNKNOWN"))
    assert evaluate_gate(p,c,research_policy(),checked_at_nano=c.now_nano).eligible


def test_exact_range_and_atr_not_same_geometry_or_id():
    p,c=example("MARTINGALE")
    f=c.forecasts[0]
    g=evaluate_gate(p,replace(c,forecasts=(replace(f,frozen_range=replace(f.frozen_range,center=D("101.1"))),)),research_policy(),checked_at_nano=c.now_nano)
    assert "EVIDENCE_IDENTITY_MISMATCH" in g.reasons
    f=replace(f,barrier_key="ATR_0.5",upper_distance=D(".5"),lower_distance=D(".5"))
    assert "BARRIER_MISMATCH" in evaluate_gate(p,replace(c,forecasts=(f,)),research_policy(),checked_at_nano=c.now_nano).reasons


def test_late_forecast_and_macro_required_block_even_high_probability():
    p,c=example()
    f=c.forecasts[0]
    late=replace(f,published_at_nano=f.window_start_nano,expires_at_nano=f.window_end_nano,
                 quality=replace(quality("INVALID"),reasons=("FORECAST_LATE",)))
    g=evaluate_gate(p,replace(c,forecasts=(late,),run_policy=replace(c.run_policy,macro_mode="WITH_MACRO")),research_policy(),checked_at_nano=c.now_nano)
    assert {"FORECAST_LATE","MACRO_REQUIRED_MISSING"}<=set(g.reasons)


def test_signed_target_contract_rejects_zero_reversed_and_zero_increment():
    p,c=example()
    zero=replace(p,target_lots=0)
    g=evaluate_gate(zero,c,research_policy(),checked_at_nano=c.now_nano)
    assert not g.eligible and "EVIDENCE_IDENTITY_MISMATCH" in g.reasons
    reversed_open=replace(p,target_lots=-1)
    assert not evaluate_gate(reversed_open,c,research_policy(),checked_at_nano=c.now_nano).eligible
    from ak_trading.v2.contracts import PositionSnapshot
    held=replace(c,position=PositionSnapshot("rb",p.strategy_id,p.episode_id,1,D(100),False))
    flat_add=replace(p,action="ADD",target_lots=1,next_state=replace(p.next_state,phase="HOLDING",
        active_exit_nano=131*NS,pending_exit_nano=None))
    assert not evaluate_gate(flat_add,held,research_policy(),checked_at_nano=c.now_nano).eligible


def test_payoff_model_id_must_match_bound_context_hash():
    p,c=example()
    alien=replace(p,payoff=replace(p.payoff,model_id="payoff:"+"b"*64))
    g=evaluate_gate(alien,c,research_policy(),checked_at_nano=c.now_nano)
    assert not g.eligible and "EVIDENCE_IDENTITY_MISMATCH" in g.reasons


def test_rule_expired_at_check_time_blocks_new_risk():
    p,c=example()
    from test_v2_m4_ledger import rule as make_rule
    expired=make_rule(effective_until_nano=c.now_nano+1)
    g=evaluate_gate(p,replace(c,rule=expired),research_policy(),checked_at_nano=c.now_nano+2)
    assert not g.eligible and "COST_UNKNOWN" in g.reasons
    fresh=evaluate_gate(p,replace(c,rule=expired),research_policy(),checked_at_nano=c.now_nano)
    assert "COST_UNKNOWN" not in fresh.reasons


def test_checked_time_after_computation_and_equal_expiry_cannot_open():
    p,c=example()
    f=c.forecasts[0]
    published=replace(f,published_at_nano=c.now_nano+1)
    early=evaluate_gate(p,replace(c,forecasts=(published,)),research_policy(),checked_at_nano=c.now_nano)
    assert "EVIDENCE_IDENTITY_MISMATCH" in early.reasons
    timely=evaluate_gate(p,replace(c,forecasts=(published,)),research_policy(),checked_at_nano=c.now_nano+2)
    assert timely.eligible and timely.checked_at_nano==c.now_nano+2
    expired=evaluate_gate(p,c,research_policy(),checked_at_nano=f.expires_at_nano)
    assert "MODEL_EXPIRED" in expired.reasons and not expired.eligible


def _add_context(c,position_lots,budget_side,layer_lots=1):
    """ADD context: owned position + budget with one frozen layer on `side`."""
    from ak_trading.v2.contracts import LayerSpec,PositionSnapshot
    b=replace(_p_budget(side=budget_side),layers=(LayerSpec(0,D(100),D(100),layer_lots),),
              maximum_lots=layer_lots,side=budget_side)
    held=replace(c,position=PositionSnapshot("rb","trend_m1_v2","episode",position_lots,D(100),False))
    return b,held

def _p_budget(side):
    p,c=example()
    return replace(p.budget,side=side,stop_price=D(94) if side==1 else D(106),
                   target_price=D(104) if side==1 else D(90))


def test_reverse_position_fake_add_rejected_short_side_and_mirror():
    """F14: current=-2, budget.side=+1, layer 1, target=-1 is a partial close
    posing as an ADD; the long-side mirror (current=+2, side=-1, target=+1)
    must fail identically."""
    p,c=example()
    # Short-held, long budget: the arithmetic target matches but the position
    # shrinks (|-1| < |-2|) against the budget direction.
    b,held=_add_context(c,-2,+1)
    short_add=replace(p,action="ADD",target_lots=-1,layer_index=0,budget=b,
        next_state=replace(p.next_state,phase="HOLDING",layer_filled_lots=(0,),
            active_exit_nano=131*NS,pending_exit_nano=None))
    g=evaluate_gate(short_add,held,research_policy(),checked_at_nano=c.now_nano)
    assert not g.eligible and "POSITION_OWNERSHIP_MISMATCH" in g.reasons
    from ak_trading.v2.decision.replay import intents_for_proposal
    with pytest.raises(ValueError,match="opening_intent_target_invalid"):
        intents_for_proposal(short_add,"d",held.account,c.rule)
    # Mirror: long-held position, short budget.
    b2,held2=_add_context(c,2,-1)
    long_add=replace(short_add,target_lots=1,budget=b2)
    g2=evaluate_gate(long_add,held2,research_policy(),checked_at_nano=c.now_nano)
    assert not g2.eligible and "POSITION_OWNERSHIP_MISMATCH" in g2.reasons
    with pytest.raises(ValueError,match="opening_intent_target_invalid"):
        intents_for_proposal(long_add,"d",held2.account,c.rule)


def test_add_without_position_rejected_and_directional_add_still_passes():
    """Zero-position ADD stays rejected; a true same-direction layer ADD keeps
    passing the strengthened boundary. Sign symmetry of the new condition is
    directly exercised by the long-side mirror rejection above (short budget,
    long position), which trips the same |target|/sign clauses."""
    from ak_trading.v2.contracts import LayerSpec,PositionSnapshot
    from ak_trading.v2.decision.replay import intents_for_proposal
    p,c=example()
    # No position: ADD cannot exist.
    b,_=_add_context(c,0,+1)
    orphan=replace(p,action="ADD",target_lots=1,layer_index=0,budget=b,
        next_state=replace(p.next_state,phase="HOLDING",active_exit_nano=131*NS))
    g=evaluate_gate(orphan,c,research_policy(),checked_at_nano=c.now_nano)
    assert not g.eligible and "POSITION_OWNERSHIP_MISMATCH" in g.reasons
    # Legal long ADD: layer0 (1 lot) filled, layer1 (1 lot) pending; current
    # +1 -> +2 with a two-layer budget whose maximum_lots covers both.
    from test_v2_m4_ledger import ledger,fill as ledger_fill,rule as ledger_rule,NS as LNS
    book=ledger("100000")
    book.apply_fill(ledger_fill("add-base",LNS,"100"),ledger_rule())
    from test_v2_m4_ledger import bar as ledger_bar,DAY
    book.mark({"rb":ledger_bar(t=0,price="100")},LNS,DAY,(ledger_rule(),))
    p,c=example()
    base_budget=_p_budget(side=+1)
    two_layer=replace(base_budget,
        layers=(LayerSpec(0,D(100),D(100),1),LayerSpec(1,D(98),D(98),1)),
        maximum_lots=2,planned_loss_cny=D(200),stress_loss_cny=D(400))
    held3=replace(c,position=PositionSnapshot("rb","trend_m1_v2","episode",1,D(100),False),
                  account=book.snapshot(LNS))
    nxt3=replace(p.next_state,phase="HOLDING",budget=two_layer,layer_filled_lots=(1,0),
        filled_layers=(0,),active_exit_nano=131*NS,pending_exit_nano=None)
    legal=replace(p,action="ADD",target_lots=2,layer_index=1,budget=two_layer,next_state=nxt3)
    g3=evaluate_gate(legal,held3,research_policy(),checked_at_nano=c.now_nano)
    assert g3.eligible and g3.reasons==(),g3.reasons
    i=intents_for_proposal(legal,"d",held3.account,c.rule)
    assert i and i[0].delta_lots==1 and i[0].offset=="OPEN" and i[0].purpose=="ADD"


@pytest.fixture(scope="module")
def natural_daily_range_case():
    """Unmodified SYNTHETIC TREND producer, including its lawful null ranges."""
    from test_v2_m4_causality import daily_proposals
    rows=daily_proposals()
    assert [p.action for _,p,_ in rows[:3]]==["WAIT","WAIT","OPEN"]
    context,proposal,_=rows[2]
    forecast=next(f for f in context.forecasts if f.forecast_id==proposal.forecast_id)
    assert proposal.next_state.confirmation_count==3 and proposal.target_lots>0
    assert proposal.next_state.setup is not None and proposal.next_state.setup.frozen_range is None
    assert proposal.budget is not None and proposal.budget.frozen_range is None
    assert context.regime.frozen_range is not None
    assert forecast.horizon==Horizon("TRADING_DAY",5)
    assert forecast.frozen_range==context.regime.frozen_range
    return proposal,context,forecast


def test_natural_daily_null_setup_range_preserves_measurement_and_ordinary_gate(natural_daily_range_case):
    proposal,context,_=natural_daily_range_case
    reasons,evidence,paywalled=non_payoff_evidence_reasons(
        proposal,context,research_policy(),checked_at_nano=context.now_nano)
    assert reasons==[] and evidence and not paywalled
    ordinary=evaluate_gate(proposal,context,research_policy(),checked_at_nano=context.now_nano)
    assert not ordinary.eligible and ordinary.reasons==("INSUFFICIENT_SAMPLES",)


def test_daily_setup_absent_and_null_fallback_keep_complete_range_identity(natural_daily_range_case):
    proposal,context,forecast=natural_daily_range_case
    frozen=context.regime.frozen_range
    altered=replace(frozen,source_snapshot_id=frozen.source_snapshot_id+"-SYNTHETIC-other")
    assert altered!=frozen and altered.range_id==frozen.range_id
    assert calibration_geometry_key(context.features,altered,"RETURN_BEFORE_BREAK")==\
        calibration_geometry_key(context.features,frozen,"RETURN_BEFORE_BREAK")
    # These legal replacements isolate range identity; only the fixture above
    # supplies the natural OPEN/measurement proof.
    for setup in (None,proposal.next_state.setup):
        candidate=replace(proposal,next_state=replace(proposal.next_state,setup=setup))
        reasons,evidence,_=non_payoff_evidence_reasons(
            candidate,context,research_policy(),checked_at_nano=context.now_nano)
        assert reasons==[] and evidence
        for wrong in (None,altered):
            changed=replace(context,forecasts=tuple(
                replace(f,frozen_range=wrong,
                        stop_touch_probabilities={} if wrong is None else f.stop_touch_probabilities,
                        return_before_break_probability=None if wrong is None else f.return_before_break_probability,
                        time_cdf=() if wrong is None else f.time_cdf)
                if f.forecast_id==forecast.forecast_id else f
                for f in context.forecasts))
            reasons,_,_=non_payoff_evidence_reasons(
                candidate,changed,research_policy(),checked_at_nano=context.now_nano)
            assert "EVIDENCE_IDENTITY_MISMATCH" in reasons
        no_range=replace(context,regime=replace(context.regime,frozen_range=None))
        reasons,_,_=non_payoff_evidence_reasons(
            candidate,no_range,research_policy(),checked_at_nano=context.now_nano)
        assert "EVIDENCE_IDENTITY_MISMATCH" in reasons


def test_daily_nonempty_budget_then_setup_range_priority_cannot_fall_back(natural_daily_range_case):
    proposal,context,forecast=natural_daily_range_case
    frozen=context.regime.frozen_range
    budget_range=replace(frozen,range_id=frozen.range_id+"-SYNTHETIC-budget")
    setup_range=replace(frozen,range_id=frozen.range_id+"-SYNTHETIC-setup")
    assert len({frozen.range_id,budget_range.range_id,setup_range.range_id})==3
    setup=replace(proposal.next_state.setup,frozen_range=setup_range)
    budget=replace(proposal.budget,frozen_range=budget_range,range_id=budget_range.range_id)
    candidates=(
        (replace(proposal,budget=budget,next_state=replace(proposal.next_state,budget=budget,setup=setup)),
         budget_range,(setup_range,frozen)),
        (replace(proposal,next_state=replace(proposal.next_state,setup=setup)),setup_range,(frozen,)),
    )
    for candidate,expected,rejected in candidates:
        for actual in (expected,*rejected):
            changed=replace(context,forecasts=tuple(
                replace(f,frozen_range=actual) if f.forecast_id==forecast.forecast_id else f
                for f in context.forecasts))
            reasons,evidence,_=non_payoff_evidence_reasons(
                candidate,changed,research_policy(),checked_at_nano=context.now_nano)
            if actual==expected:
                assert reasons==[] and evidence
            else:
                assert "EVIDENCE_IDENTITY_MISMATCH" in reasons

