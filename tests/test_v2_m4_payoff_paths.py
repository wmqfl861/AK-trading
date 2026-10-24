"""E-P1 EP1-M4: offline fixed-candidate path measurement and the extended
sole payoff producer (decision v2-ep1-training-path-20260917-r1 RULING 2/3).

Every PayoffPathResult here is produced by the real
``replay_payoff_training_window`` over SYNTHETIC minute fixtures around the
real matching/ledger/state-commit machinery (arbitration RULING 6 allows
clearly-labeled SYNTHETIC unit inputs); no PathResult, decision or checkpoint
is ever hand-built, and no PayoffObservation is hand-built either.
"""
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

import ak_trading.v2.decision.payoff_paths as payoff_paths_module
from ak_trading.v2.artifacts import canonical_json_bytes, sha256_hex, stable_id
from ak_trading.v2.contracts import (
    AccountSnapshot, FeeLeg, FrozenInput, Horizon, LedgerCheckpoint, PERSISTENCE_EVENTS,
    PayoffPathResult, PayoffTrainingPlan, PathLabel, RunPolicy, ForecastRequest,
    payoff_training_plan_sha256, PAYOFF_PATH_INTEGRITY_SUFFIXES,
)
from ak_trading.v2.decision.gates import evaluate_gate
from ak_trading.v2.decision.ledger import ContinuousLedger
from ak_trading.v2.decision.payoff_paths import (
    payoff_path_account_id, replay_payoff_training_window,
)
from ak_trading.v2.decision.payoffs import (
    PAYOFF_INTEGRITY_PREFIX, PAYOFF_SAMPLE_UNAVAILABLE_PREFIX,
    observation_from_replay, payoff_observation_source,
)
from ak_trading.v2.decision.replay import replay_payloads
from ak_trading.v2.decision.risk import enforce_account_risk
from ak_trading.v2.decision.routing import choose_proposal
from ak_trading.v2.policy import research_policy
from ak_trading.v2.probability.labels import label_forecast
from ak_trading.v2.schemas import decode_record, encode_record
from ak_trading.v2.strategies.registry import build_strategy_spec
from test_v2_m4_gates import example
from test_v2_m4_ledger import (
    DAY, HASH, NS, bar, exit_fixtures_for, instrument, quality, rule,
)
from test_v2_m4_payoffs import (
    _p1_bars, _p1_side_budget, _p1_side_example, _p1_side_proposal, _p1_evidence,
    _p1_fixture, _p1_model, _p1_run,
)
from test_v2_m4_replay import replay_inputs

ENTRY_NANO = 100 * NS
DEFAULT_TIMES = dict(
    inner_training_end_nano=60 * NS, inner_calibration_end_nano=65 * NS,
    warmup_start_nano=70 * NS, window_start_nano=95 * NS,
    window_end_nano=110 * NS, training_end_nano=200 * NS)


def _daily_plan_times(original,window_end_index=594,selection_end_offset_nano=0,support_end_offset_nano=0):
    """Declare the calendar tail from a fixed selection endpoint, without fitting."""
    from ak_trading.v2.clock import horizon_window
    selection_end=original.bars[window_end_index].available_at_nano+selection_end_offset_nano
    _,support_end=horizon_window(selection_end,original.instruments[0].product,
        Horizon("TRADING_DAY",20),original.sessions)
    return selection_end,support_end+support_end_offset_nano


def _natural_daily_path(*, bundle=None, models=None, window_end_index=594,
                        selection_end_offset_nano=0,support_end_offset_nano=0):
    """SYNTHETIC DAILY chain; actual pipeline, strategy, gate and ledger.

    The M3A dependency is fitted and calibrated from explicit synthetic
    training inputs. There are no patched contexts, forced proposals,
    prebuilt PathResults or hand-built payoff observations in this chain.
    """
    from test_v2_m4_causality import synthetic_daily_source, synthetic_daily_world
    from ak_trading.v2.decision.pipeline import legacy_exit_binding, legacy_exit_spec
    # Freeze this SYNTHETIC tail from the declared selection window before
    # retrieving fitted models or observing candidates, fills or labels.
    original,_=synthetic_daily_source()
    selection_end,training_end=_daily_plan_times(original,window_end_index,
        selection_end_offset_nano,support_end_offset_nano)
    world=synthetic_daily_world()
    bundle=original if bundle is None else bundle
    models=world[1] if models is None else models
    spec=build_strategy_spec("TREND","DAILY")
    binding=legacy_exit_binding(spec)
    plan=make_plan(bundle,binding.binding_sha256,strategy_id=spec.strategy_id,
        inner_training_end_nano=models.training_end_nano,inner_calibration_end_nano=models.calibration_end_nano,
        warmup_start_nano=original.bars[589].start_nano,
        window_start_nano=original.bars[589].available_at_nano,
        window_end_nano=selection_end,training_end_nano=training_end)
    initial=replace(path_account(plan),as_of_nano=plan.warmup_start_nano,
                    trading_day=original.bars[588].trading_day)
    rp=RunPolicy("RESEARCH_EVALUATION","FIXED",7,spec.strategy_id,binding.binding_sha256,
                 "research-policy-v2.1.0","macro-risk-v2.1.0","PRICE_ONLY","BASE")
    result=replay_payoff_training_window(bundle,models,spec,initial,rp,research_policy(),None,world[3],HASH,plan,
        exit_specs=(legacy_exit_spec("DAILY"),),exit_binding=binding)
    return result,bundle


def _natural_daily_observation(result,bundle):
    selected=next(row for row in result.path_decisions if row.action=="MEASURE_ENTRY")
    request=selected.request
    # Future bars enter only the existing label/scoring stage after selection
    # and complete path replay. They never reach candidate or gate inputs.
    future=tuple(b for b in bundle.bars if request.window_start_nano<=b.start_nano<request.window_end_nano)
    label=label_forecast(request,future,bundle.sessions,(),result.plan.training_end_nano)
    block_id=selected.context.features.trading_day.isoformat()
    observation=observation_from_replay(selected.proposal,selected.context,request,label,result,
        result.plan.training_end_nano,block_id,rule_originals=result.rule_originals)
    source=payoff_observation_source(selected.proposal,selected.context,request,label,result,
        result.plan.training_end_nano,block_id,rule_originals=result.rule_originals)
    return selected,label,observation,source


def test_daily20_natural_measurement_fills_closes_and_produces_open_initial():
    result,bundle=_natural_daily_path()
    assert result.status=="COMPLETE" and result.reconciliation["status"]=="PASS"
    assert result.proposals and result.fills and result.orders and result.receipt_commits, \
        tuple((row.status,row.reasons) for row in result.execution_results)
    selected,label,observation,source=_natural_daily_observation(result,bundle)
    assert not any(selected.checks.values())
    assert selected.context.features.daily_background_id is not None
    assert selected.context.regime.cross_timeframe=="ALIGNED"
    assert selected.proposal.action=="OPEN" and selected.proposal.next_state.confirmation_count==3
    assert selected.request.horizon==Horizon("TRADING_DAY",5) and selected.request.barrier_key=="ATR_1"
    assert selected.request.features.snapshot_id==selected.context.features.snapshot_id
    assert label.status=="MATURE"
    assert any(f.offset=="OPEN" for f in result.fills) and any(f.offset!="OPEN" for f in result.fills)
    assert all(f.execution_nano>selected.observed_at_nano for f in result.fills)
    assert not result.checkpoints[-1].account.lots and not result.checkpoints[-1].pending_intents
    assert observation.exit_state_condition=="OPEN_INITIAL" and observation.barrier_key=="ATR_1"
    assert observation.horizon==Horizon("TRADING_DAY",5)
    assert observation.exit_binding_sha256==result.exit_binding.binding_sha256
    assert observation.source_sha256==sha256_hex(source)
    assert observation.ended_at_nano==label.window_end_nano
    assert observation.available_at_nano==label.available_at_nano<result.plan.training_end_nano
    ordinary=evaluate_gate(selected.proposal,selected.context,research_policy(),checked_at_nano=selected.observed_at_nano)
    assert not ordinary.eligible and ordinary.reasons==("INSUFFICIENT_SAMPLES",)


@pytest.mark.parametrize("failure",["missing","expired","overage","confirmation_incomplete"])
def test_daily_ep1_rejection_accounting_never_constructs_observation(failure):
    from test_v2_m4_causality import synthetic_daily_world
    bundle,models,_,_=synthetic_daily_world()
    end=594
    if failure=="missing":
        models=replace(models,models={k:v for k,v in models.models.items() if "|D20|" not in k})
    elif failure=="expired":
        deadline=bundle.bars[589].available_at_nano
        models=replace(models,evidence=tuple(replace(e,valid_until_nano=deadline,status="EXPIRED")
            if e.horizon.value==20 else e for e in models.evidence))
    elif failure=="overage":
        bundle=replace(bundle,bars=tuple(b for i,b in enumerate(bundle.bars) if i not in (588,590,592,594)))
    else:
        end=590
    result,_=_natural_daily_path(bundle=bundle,models=models,window_end_index=end)
    assert result.status=="NO_CANDIDATE"
    assert "NO_TRIGGERED_BUDGETED_PATH" in result.exclusions
    assert not result.fills and not result.orders
    assert not any(row.action=="MEASURE_ENTRY" for row in result.path_decisions)
    assert not result.checkpoints[-1].account.lots
    # NO_CANDIDATE is the actual layer returned to EP1. No producer is called
    # on this branch and zero trades cannot become statistical PASS.


def test_daily_countertrend_real_candidate_is_rejected_without_measurement_or_fills(monkeypatch):
    """Negative-only DAY20 fault; real candidate, gate and training-path loop."""
    predict=payoff_paths_module.predict_market
    backgrounds=[]
    def opposite_background(models,request,regime,published_at_nano):
        forecast=predict(models,request,regime,published_at_nano)
        if request.horizon==Horizon("TRADING_DAY",20):
            backgrounds.append(request)
            return replace(forecast,direction_probabilities={"UP":.1,"DOWN":.8,"NEUTRAL":.1})
        return forecast
    monkeypatch.setattr(payoff_paths_module,"predict_market",opposite_background)
    result,_=_natural_daily_path()
    assert backgrounds and all(r.barrier_key=="ATR_1" for r in backgrounds)
    assert result.regimes and all(r.cross_timeframe=="COUNTERTREND" for r in result.regimes)
    assert result.proposals and all(p.action=="OPEN" for p in result.proposals)
    assert all("UNVALIDATED_TIMEFRAME_CONFLICT" in reasons
               for checks in result.candidate_checks.values()
               for reasons in (tuple(reason for domain in checks.values() for reason in domain),))
    assert result.status=="NO_CANDIDATE" and "NO_TRIGGERED_BUDGETED_PATH" in result.exclusions
    assert not result.path_decisions and not result.orders and not result.fills
    assert not result.checkpoints[-1].account.lots


def test_daily_future_append_preserves_canonical_context_and_observation_source():
    from test_v2_m4_causality import synthetic_daily_world, daily_context
    original,_,_,_=synthetic_daily_world()
    _,end=_daily_plan_times(original)
    prefix=replace(original,bars=tuple(b for b in original.bars if b.start_nano<end))
    later=next(bar for bar in original.bars if bar.start_nano>end)
    extreme=replace(later,bar_id="SYNTHETIC-post-observation-extreme",close=D(99999),high=D(99999))
    # Keep the frozen input manifest identity: it is a provenance input to
    # the path id, unlike append-only provider data beyond its influence.
    appended=replace(prefix,bars=prefix.bars+(extreme,))
    a,_=daily_context(bundle=prefix); b,_=daily_context(bundle=appended)
    assert canonical_json_bytes(encode_record(a))==canonical_json_bytes(encode_record(b))
    before,_=_natural_daily_path(bundle=prefix); after,_=_natural_daily_path(bundle=appended)
    assert before.status==after.status=="COMPLETE"
    before_selected,before_label,before_observation,before_source=_natural_daily_observation(before,prefix)
    after_selected,after_label,after_observation,after_source=_natural_daily_observation(after,appended)
    assert before_selected==after_selected and before_label==after_label
    assert before_observation==after_observation and before_source==after_source
    assert before_observation.source_sha256==sha256_hex(before_source)==sha256_hex(after_source)


@pytest.fixture(scope="module")
def daily_maturity_inputs():
    # Fixed SYNTHETIC selection endpoint: one minute after the third daily
    # observation. It is declared before fit/selection; the tail is exactly
    # its public DAY20 endpoint. No candidate or payoff is supplied to replay.
    path,bundle=_natural_daily_path(window_end_index=591,selection_end_offset_nano=NS)
    entry,label,observation,source=_natural_daily_observation(path,bundle)
    assert path.status=="COMPLETE" and label.status=="MATURE"
    return path,bundle,entry,label,observation,source


def _daily_produce(path,label,*,context=None,opened=None,request=None,producer=observation_from_replay):
    entry=next(row for row in path.path_decisions if row.action=="MEASURE_ENTRY")
    return producer(opened or entry.proposal,context or entry.context,request or entry.request,
        label,path,path.plan.training_end_nano,entry.context.features.trading_day.isoformat(),
        rule_originals=path.rule_originals)


def _daily_future_sessions(path):
    entry=next(row for row in path.path_decisions if row.action=="MEASURE_ENTRY")
    return tuple(session for session in path.sessions if session.product==entry.context.features.product
        and session.start_nano>entry.observed_at_nano)


def test_daily_maturity_support_equality_preserves_actual_day5_facts(daily_maturity_inputs):
    from ak_trading.v2.clock import horizon_window
    path,_,entry,label,observation,source=daily_maturity_inputs
    support_end=horizon_window(entry.observed_at_nano,entry.context.features.product,
        Horizon("TRADING_DAY",20),path.sessions)[1]
    assert path.plan.training_end_nano==support_end
    assert observation.horizon==entry.request.horizon==Horizon("TRADING_DAY",5)
    assert observation.ended_at_nano==label.window_end_nano
    assert observation.available_at_nano==label.available_at_nano<support_end
    assert entry.proposal.budget.latest_exit_nano==label.window_end_nano
    assert observation.source_sha256==sha256_hex(source)
    assert _daily_produce(path,label)==observation
    assert _daily_produce(path,label,producer=payoff_observation_source)==source


def test_daily_maturity_one_ns_before_support_rejects_actual_replay(daily_maturity_inputs):
    from ak_trading.v2.clock import horizon_window
    _,_,baseline,_,_,_=daily_maturity_inputs
    path,bundle=_natural_daily_path(window_end_index=591,selection_end_offset_nano=NS,
        support_end_offset_nano=-1)
    entry=next(row for row in path.path_decisions if row.action=="MEASURE_ENTRY")
    future=tuple(bar for bar in bundle.bars if entry.request.window_start_nano<=bar.start_nano<entry.request.window_end_nano)
    label=label_forecast(entry.request,future,bundle.sessions,(),path.plan.training_end_nano)
    assert path.status=="COMPLETE" and label.status=="MATURE"
    assert entry.observed_at_nano==baseline.observed_at_nano
    support_end=horizon_window(entry.observed_at_nano,entry.context.features.product,
        Horizon("TRADING_DAY",20),path.sessions)[1]
    assert path.plan.training_end_nano==support_end-1
    assert label.available_at_nano<path.plan.training_end_nano
    for producer in (observation_from_replay,payoff_observation_source):
        with pytest.raises(ValueError,match="^PAYOFF_SAMPLE_UNAVAILABLE:MATURITY_WINDOW_BELOW_MINIMUM$"):
            _daily_produce(path,label,producer=producer)


@pytest.mark.parametrize("fault",["nineteen_days","missing_twentieth","foreign_product",
    "same_day_fragments","declared_holiday","no_product_days"])
def test_daily_maturity_calendar_capacity_cannot_be_filled_by_other_slots(daily_maturity_inputs,fault):
    """Explicit SYNTHETIC negative calendars applied to a real completed path."""
    from ak_trading.v2.clock import ClockError, horizon_window
    path,_,entry,label,_,_=daily_maturity_inputs
    future=_daily_future_sessions(path)
    nineteenth,twentieth=future[18:20]
    calendar=tuple(session for session in path.sessions if session.end_nano<=nineteenth.end_nano)
    if fault=="missing_twentieth":
        calendar=tuple(session for session in path.sessions if session.trading_day!=twentieth.trading_day)
    elif fault=="declared_holiday":
        calendar=tuple(session for session in path.sessions if session.trading_day!=future[10].trading_day)
    elif fault=="foreign_product":
        calendar+=tuple(replace(session,session_id="SYNTHETIC-CU-"+session.session_id,
            product="SHFE.CU") for session in future[19:40])
    elif fault=="same_day_fragments":
        calendar+=tuple(replace(session,session_id="SYNTHETIC-fragment-"+session.session_id,
            start_nano=session.end_nano+NS,end_nano=session.end_nano+2*NS,kind="NIGHT") for session in future[:19])
    elif fault=="no_product_days":
        calendar=tuple(replace(session,product="SHFE.CU") for session in path.sessions)
    if fault in ("missing_twentieth","declared_holiday"):
        assert horizon_window(entry.observed_at_nano,entry.context.features.product,
            Horizon("TRADING_DAY",20),calendar)[1]>path.plan.training_end_nano
    else:
        with pytest.raises(ClockError,match="^(horizon_exceeds_calendar|no_trading_days_in_calendar)$"):
            horizon_window(entry.observed_at_nano,entry.context.features.product,Horizon("TRADING_DAY",20),calendar)
    negative=_tampered(path,sessions=calendar)
    for producer in (observation_from_replay,payoff_observation_source):
        with pytest.raises(ValueError,match="^PAYOFF_SAMPLE_UNAVAILABLE:MATURITY_WINDOW_BELOW_MINIMUM$"):
            _daily_produce(negative,label,producer=producer)


@pytest.mark.parametrize("fault",["overlap","wrong_container_type"])
def test_daily_maturity_calendar_errors_keep_blocking_channels(daily_maturity_inputs,fault):
    from ak_trading.v2.clock import ClockError
    path,_,_,label,_,_=daily_maturity_inputs
    calendar=list(path.sessions)
    if fault=="overlap":
        calendar=tuple(calendar)+(replace(_daily_future_sessions(path)[10],session_id="SYNTHETIC-overlap"),)
        error_type,reason=ClockError,"^overlapping_sessions_for_product$"
    else:
        error_type,reason=TypeError,"^sessions_must_be_tuple_of_SessionWindow$"
    negative=_tampered(path,sessions=calendar)
    for producer in (observation_from_replay,payoff_observation_source):
        with pytest.raises(error_type,match=reason) as rejected:
            _daily_produce(negative,label,producer=producer)
        assert not _m5_washes(rejected.value)


def test_daily_maturity_last_session_sets_support_and_weekends_add_no_days(daily_maturity_inputs):
    from ak_trading.v2.clock import horizon_window
    path,bundle,entry,label,_,_=daily_maturity_inputs
    future=_daily_future_sessions(path)
    start,end=horizon_window(entry.observed_at_nano,entry.context.features.product,Horizon("TRADING_DAY",20),path.sessions)
    assert (start,end)==(future[0].start_nano,future[19].end_nano)
    assert all(session.trading_day.weekday()<5 for session in future[:20])
    assert end-entry.observed_at_nano>20*86400*1_000_000_000
    extra=replace(future[19],session_id="SYNTHETIC-final-night-fragment",start_nano=end+NS,
        end_nano=end+2*NS,kind="NIGHT")
    calendar=path.sessions+(extra,)
    assert horizon_window(entry.observed_at_nano,entry.context.features.product,
        Horizon("TRADING_DAY",20),calendar)[1]==extra.end_nano
    for producer in (observation_from_replay,payoff_observation_source):
        with pytest.raises(ValueError,match="^PAYOFF_SAMPLE_UNAVAILABLE:MATURITY_WINDOW_BELOW_MINIMUM$"):
            _daily_produce(_tampered(path,sessions=calendar),label,producer=producer)
    before_closure=next(bar for bar in bundle.bars if bar.trading_day==date(2024,4,5))
    first,_=horizon_window(before_closure.available_at_nano,"SHFE.RB",Horizon("TRADING_DAY",20),bundle.sessions)
    assert before_closure.trading_day==date(2024,4,5)
    assert next(session for session in bundle.sessions if session.start_nano==first).trading_day==date(2024,4,9)


def test_daily_maturity_true_later_label_visibility_keeps_evidence_cutoff(daily_maturity_inputs):
    path,bundle,entry,original,_,_=daily_maturity_inputs
    future=tuple(bar for bar in bundle.bars if entry.request.window_start_nano<=bar.start_nano<entry.request.window_end_nano)
    # Negative-only SYNTHETIC late source: derive actual label availability
    # through label_forecast; it never changes the natural positive input.
    later=path.plan.training_end_nano+1
    late=future[:-1]+(replace(future[-1],available_at_nano=later),)
    label=label_forecast(entry.request,late,bundle.sessions,(),later)
    assert label.status=="MATURE" and label.available_at_nano==later
    assert label.window_end_nano==original.window_end_nano<later
    for producer in (observation_from_replay,payoff_observation_source):
        with pytest.raises(ValueError,match="^PAYOFF_SAMPLE_UNAVAILABLE:EVIDENCE_AFTER_TRAINING_END$"):
            _daily_produce(path,label,producer=producer)


def test_daily_maturity_real_early_close_still_waits_for_original_label(daily_maturity_inputs):
    path,_,entry,label,observation,_=daily_maturity_inputs
    closes=[fill for fill in path.fills if fill.offset!="OPEN"]
    assert closes and max(fill.execution_nano for fill in closes)<label.window_end_nano
    assert observation.horizon==entry.request.horizon==Horizon("TRADING_DAY",5)
    assert observation.ended_at_nano==label.window_end_nano
    assert observation.available_at_nano==label.available_at_nano<path.plan.training_end_nano


@pytest.mark.parametrize("maximum_days",[1,20])
def test_daily_maturity_request_cannot_exempt_different_spec(daily_maturity_inputs,maximum_days):
    path,_,entry,label,_,_=daily_maturity_inputs
    context=replace(entry.context,spec=replace(entry.context.spec,max_holding=Horizon("TRADING_DAY",maximum_days)))
    changed=_tampered(entry,context=context)
    negative=_tampered(path,path_decisions=tuple(changed if row is entry else row for row in path.path_decisions))
    for producer in (observation_from_replay,payoff_observation_source):
        with pytest.raises(ValueError,match="^PAYOFF_SAMPLE_UNAVAILABLE:MATURITY_WINDOW_BELOW_MINIMUM$"):
            _daily_produce(negative,label,producer=producer)


@pytest.mark.parametrize("days",[1,20])
def test_daily_maturity_other_horizons_keep_original_minimum(daily_maturity_inputs,days):
    from ak_trading.v2.probability.forecast import build_forecast_requests, predict_market
    from test_v2_m4_causality import synthetic_daily_world
    path,bundle,entry,_,_,_=daily_maturity_inputs
    request=next(row for row in build_forecast_requests(entry.context.features,(Horizon("TRADING_DAY",days),),
        entry.request.frozen_range,bundle.sessions) if row.barrier_key=="ATR_1")
    forecast=predict_market(synthetic_daily_world()[1],request,entry.context.regime,entry.observed_at_nano)
    context=replace(entry.context,forecasts=(forecast,))
    opened=replace(entry.proposal,forecast_id=forecast.forecast_id)
    changed=_tampered(entry,proposal=opened,context=context,request=request)
    future=tuple(bar for bar in bundle.bars if request.window_start_nano<=bar.start_nano<request.window_end_nano)
    label=label_forecast(request,future,bundle.sessions,(),path.plan.training_end_nano)
    assert label.status=="MATURE"
    sessions=path.sessions if days==1 else tuple(session for session in path.sessions
        if session.end_nano<=_daily_future_sessions(path)[18].end_nano)
    negative=_tampered(path,sessions=sessions,
        forecasts=tuple(forecast if row.forecast_id==entry.proposal.forecast_id else row for row in path.forecasts),
        proposals=tuple(opened if row is entry.proposal else row for row in path.proposals),
        path_decisions=tuple(changed if row is entry else row for row in path.path_decisions))
    for producer in (observation_from_replay,payoff_observation_source):
        with pytest.raises(ValueError,match="^PAYOFF_SAMPLE_UNAVAILABLE:MATURITY_WINDOW_BELOW_MINIMUM$"):
            _daily_produce(negative,label,producer=producer)


@pytest.mark.parametrize("fault",["unreleased","wrong_identity","late_receipt","late_rule"])
def test_daily_maturity_support_cannot_bypass_prior_integrity(daily_maturity_inputs,fault):
    path,_,entry,label,_,_=daily_maturity_inputs
    if fault=="unreleased":
        negative=_tampered(path,status="INCOMPLETE")
        reason="^PAYOFF_SAMPLE_UNAVAILABLE:EPISODE_NOT_CLOSED$"
    elif fault=="wrong_identity":
        changed=_tampered(entry,request=replace(entry.request,request_id="SYNTHETIC-wrong-identity"))
        negative=_tampered(path,path_decisions=tuple(changed if row is entry else row for row in path.path_decisions))
        reason="^PAYOFF_INTEGRITY:CANDIDATE_SOURCE_MISMATCH$"
    elif fault=="late_receipt":
        commits=path.receipt_commits[:-1]+(replace(path.receipt_commits[-1],observed_at_nano=path.plan.training_end_nano+1),)
        negative=_tampered(path,receipt_commits=commits)
        reason="^PAYOFF_SAMPLE_UNAVAILABLE:EVIDENCE_AFTER_TRAINING_END$"
    else:
        original=path.rule_originals[0]
        negative=_tampered(path,rule_originals=(replace(original,available_at_nano=path.plan.training_end_nano+1),))
        reason="^PAYOFF_INTEGRITY:RULE_ORIGINAL_CONFLICT$"
    for producer in (observation_from_replay,payoff_observation_source):
        with pytest.raises(ValueError,match=reason):
            _daily_produce(negative,label,request=entry.request,producer=producer)


def _calendar_sha256(bundle: FrozenInput) -> str:
    # Same derivation as the M5 plan builder (digest over the whole calendar).
    return sha256_hex(canonical_json_bytes([encode_record(s) for s in bundle.sessions]))


def make_plan(bundle, binding_sha256, **overrides) -> PayoffTrainingPlan:
    values = {**DEFAULT_TIMES, "protocol_version": "payoff-training-p1-v1",
              "outer_split_id": "outer-r1", "inner_split_id": "inner-r1",
              "contract_ids": ("rb",), "strategy_id": "trend_m1_v2",
              "exit_binding_sha256": binding_sha256, "initial_cash_cny": D("100000"),
              "calendar_sha256": _calendar_sha256(bundle),
              "selection_rule": "FIRST_FEASIBLE_P1_OPEN_PER_WINDOW", **overrides}
    return PayoffTrainingPlan(**values)


def path_account(plan: PayoffTrainingPlan) -> AccountSnapshot:
    cash = plan.initial_cash_cny
    return AccountSnapshot(payoff_path_account_id(payoff_training_plan_sha256(plan)),
                           plan.warmup_start_nano, DAY, cash, cash, D(0), D(0), D(0), D(0),
                           cash, D(0), D(0), cash, D(0), cash, (), False, quality())


def fixed_run_policy(binding_sha256: str) -> RunPolicy:
    return RunPolicy("RESEARCH_EVALUATION", "FIXED", 7, "trend_m1_v2", binding_sha256,
                     "research-policy-v2.1.0", "macro-risk-v2.1.0", "PRICE_ONLY", "BASE")


def _path_fixture(monkeypatch, bars, *, side=1, latest_exit_nano=200 * NS,
                  entry_valid_until_nano=None, exit_reasons=("STOP_TOUCHED",),
                  open_times=(100 * NS,), post_selection_open=False, night=False,
                  regime_base=None, confirmations=None, persistence=None,
                  future_bar=False, fees_none=False, stale_decision=False, rules=None):
    """SYNTHETIC scheduler fixture driving the real measurement entry point."""
    from ak_trading.v2.decision.execution import position_for
    from ak_trading.v2.data import visible_rule
    from ak_trading.v2.strategies.registry import fresh_state
    bundle, models, _empty, reference = replay_inputs()
    bundle = replace(bundle, bars=tuple(bars))
    if rules is not None:
        bundle = replace(bundle, rules=tuple(rules))
    if fees_none:
        bundle = replace(bundle, rules=tuple(replace(r, open_fee=None)
                                             for r in bundle.rules))
    holder = {}

    def fixture_context(bundle, contract, now, account_, previous, fm, pm, spec,
                        seasonal, ref, rp, qs, *, exit_binding, forecast_provider):
        _p, ctx = _p1_side_example(side)
        ctx = replace(ctx, exit_binding=exit_binding)
        trading_day = date(2026, 1, 13) if night else ctx.features.trading_day
        fs = replace(ctx.features, snapshot_id=f"features-{now}", decision_nano=now,
                     available_at_nano=now, trading_day=trading_day,
                     values=tuple(replace(v, available_at_nano=now) for v in ctx.features.values))
        reg = replace(ctx.regime, assessment_id=f"regime-{now}", snapshot_id=fs.snapshot_id,
                      decision_nano=now)
        if regime_base is not None:
            reg = replace(reg, base=regime_base)
        f = replace(ctx.forecasts[0], forecast_id=f"forecast-{now}", snapshot_id=fs.snapshot_id,
                    assessment_id=reg.assessment_id, decision_nano=now, published_at_nano=now,
                    expires_at_nano=now + 2 * NS, window_start_nano=now + NS,
                    window_end_nano=now + 31 * NS, trading_day=trading_day)
        if persistence is not None:
            event = "PERSIST:" + ("UP_TREND" if side == 1 else "DOWN_TREND")
            f = replace(f, persistence_probabilities={event[8:]: persistence},
                        evidence=tuple(
                            replace(e, bucket="[0.60,0.70)") if e.event == event else e
                            for e in f.evidence))
        visible_bars = tuple(b for b in bundle.bars if b.end_nano <= now)
        if future_bar:
            visible_bars = visible_bars + (bar(t=now, price="100"),)
        rule_used = visible_rule(bundle, contract, now)
        hashes = {"forecast": HASH, "payoff": pm.content_hash(), "regime_reference": HASH}
        return replace(ctx, now_nano=now, features=fs, forecasts=(f,), regime=reg,
                       account=account_, spec=spec, run_policy=rp,
                       position=position_for(account_, contract), model_hashes=hashes,
                       macro=replace(ctx.macro, decision_nano=now), rule=rule_used,
                       bars=visible_bars)

    class FixtureStrategy:
        def __init__(self, spec):
            self.spec = spec

        def evaluate(self, ctx, previous):
            p, _ = _p1_side_example(side)
            now = ctx.now_nano
            if previous is not None and previous.episode_id:
                if previous.phase == "EXITING" or ctx.position is not None:
                    p = replace(p, proposal_id=f"exit-{now}", decision_nano=now, action="EXIT",
                                target_lots=0, earliest_execution_nano=now + NS,
                                valid_until_nano=now + 2 * NS,
                                next_state=replace(previous, phase="EXITING"),
                                budget=previous.budget,
                                forecast_id=ctx.forecasts[0].forecast_id,
                                assessment_id=ctx.regime.assessment_id,
                                reasons=tuple(exit_reasons))
                elif post_selection_open and now in open_times:
                    p = self._open(p, ctx, now)
                else:
                    p = replace(p, proposal_id=f"hold-{now}", decision_nano=now, action="HOLD",
                                target_lots=1, earliest_execution_nano=now + NS,
                                valid_until_nano=now + 2 * NS, next_state=previous,
                                budget=previous.budget, forecast_id=None, payoff=None)
            elif now in open_times:
                p = self._open(p, ctx, now)
            else:
                p = replace(p, proposal_id=f"wait-{now}", decision_nano=now, action="WAIT",
                            target_lots=0, budget=None, episode_id=None,
                            next_state=previous or fresh_state(self.spec.strategy_id, "rb"),
                            earliest_execution_nano=now + NS, valid_until_nano=now + 2 * NS,
                            forecast_id=None, payoff=None)
            return p

        def _open(self, p, ctx, now):
            b = _p1_side_budget(p, latest_exit_nano, side)
            p = _p1_side_proposal(p, b)
            p = replace(p, proposal_id=f"open-{now}", payoff=None,
                        forecast_id=ctx.forecasts[0].forecast_id,
                        assessment_id=ctx.regime.assessment_id)
            if stale_decision:
                p = replace(p, decision_nano=now - NS)
            if confirmations is not None:
                p = replace(p, next_state=replace(p.next_state,
                                                  confirmation_count=confirmations))
            if entry_valid_until_nano is not None:
                p = replace(p, valid_until_nano=entry_valid_until_nano)
            holder["proposal"] = p
            holder["context"] = ctx
            return p

    spec = build_strategy_spec("TREND", "MINUTE")
    monkeypatch.setattr(payoff_paths_module, "build_strategy_context", fixture_context)
    monkeypatch.setattr(payoff_paths_module, "strategy_registry_v2",
                        lambda mode: {spec.strategy_id: FixtureStrategy(spec)})
    return bundle, models, reference, holder, spec


def _run(monkeypatch, bars=None, *, plan_overrides=None, **fixture_kwargs):
    fixture = _path_fixture(monkeypatch, _p1_bars() if bars is None else bars,
                            **fixture_kwargs)
    bundle, models, reference, holder, spec = fixture
    _es, bindings, _hash = exit_fixtures_for((spec,))
    plan = make_plan(bundle, bindings[0].binding_sha256, **(plan_overrides or {}))
    result = replay_payoff_training_window(
        bundle, models, spec, path_account(plan), fixed_run_policy(bindings[0].binding_sha256),
        research_policy(), None, reference, HASH, plan,
        exit_specs=_es, exit_binding=bindings[0])
    return result, holder, bundle, plan


def _entry_of(result):
    return next(d for d in result.path_decisions if d.action == "MEASURE_ENTRY")


def _label_for(result, bundle, training_end=None):
    entry = _entry_of(result)
    request = entry.request
    future = tuple(b for b in bundle.bars if b.contract_id == "rb" and b.duration_seconds == 60
                   and request.window_start_nano <= b.start_nano < request.window_end_nano)
    label = label_forecast(request, future, bundle.sessions, (),
                           training_end if training_end is not None
                           else result.plan.training_end_nano)
    return entry, request, label


def _obs(result, bundle, training_end=None, rule_originals="default"):
    entry, request, label = _label_for(result, bundle, training_end)
    originals = result.rule_originals if rule_originals == "default" else rule_originals
    return observation_from_replay(
        entry.proposal, entry.context, request, label, result,
        training_end if training_end is not None else result.plan.training_end_nano,
        str(entry.context.features.trading_day), rule_originals=originals)


def _tampered(result: PayoffPathResult, **changes) -> PayoffPathResult:
    clone = copy.copy(result)
    for key, value in changes.items():
        object.__setattr__(clone, key, value)
    return clone


def _m5_washes(error) -> bool:
    return str(error).startswith(PAYOFF_SAMPLE_UNAVAILABLE_PREFIX)


# ─── positive complete paths ────────────────────────────────────────────────

def test_long_stop_path_produces_complete_result_and_observation(monkeypatch):
    result, holder, bundle, plan = _run(monkeypatch, _p1_bars(stop_bar=102))
    assert result.status == "COMPLETE"
    assert result.source_kind == "P1_ACTION_CONDITIONAL_REPLAY"
    assert result.path_id.startswith("payoff_path:")
    entry = _entry_of(result)
    assert entry.observed_at_nano == ENTRY_NANO and entry.request is not None
    assert entry.proposal.payoff is None and len(entry.proposal.budget.layers) == 1
    assert all(not reasons for reasons in entry.checks.values())
    assert len(result.fills) == 2 and len(result.orders) == 2
    assert [c.execution_index for c in result.receipt_commits] == list(
        range(len(result.execution_results)))
    # Isolated account: payoff-path namespace, plan cash, flat release.
    assert result.initial_account.account_id.startswith("payoff-path:")
    assert result.initial_account.cash_cny == plan.initial_cash_cny
    assert not result.checkpoints[-1].account.lots
    assert [r.rule_id for r in result.rule_originals] == ["rule-rb"]
    assert result.bar_originals and result.sessions
    obs = _obs(result, bundle)
    assert (obs.traded, obs.net_pnl_cny, obs.peak_margin_cny, obs.maximum_loss_cny) \
        == (True, D(-62), D(100), D(62))
    assert obs.return_before_stop is None
    assert obs.exit_state_condition == "OPEN_INITIAL" and obs.barrier_key == "ATR_1"
    assert obs.started_at_nano == ENTRY_NANO and obs.block_id == str(DAY)
    # Slippage is a memo: cash conservation already holds exactly (-62 net).
    assert result.reconciliation["cash_difference_cny"] == D(0)


def test_short_path_and_same_terminal_different_path(monkeypatch):
    result, holder, bundle, _plan = _run(
        monkeypatch, _p1_bars(stop_bar=102, stop_low="99", stop_high="107", stop_close="106"),
        side=-1)
    obs = _obs(result, bundle)
    assert (obs.traded, obs.net_pnl_cny, obs.peak_margin_cny, obs.maximum_loss_cny) \
        == (True, D(-62), D(100), D(62))
    # Same terminal exit price 94, different mid-path marks: equal net/loss,
    # different actual peak margin, distinct source identity.
    results = []
    for mark in (100, 94):
        r, _h, b, _p = _run(monkeypatch, _p1_bars(mark=mark, stop_bar=102))
        results.append((_obs(r, b), r))
    (a, ra), (c, rc) = results
    assert a.net_pnl_cny == c.net_pnl_cny == D(-62)
    assert a.maximum_loss_cny == c.maximum_loss_cny == D(62)
    assert (a.peak_margin_cny, c.peak_margin_cny) == (D(100), D(94))
    assert a.observation_id != c.observation_id
    assert a.source_sha256 != c.source_sha256


def _two_rules():
    """A mid-path rule switch: rule-rb until 102*NS, then rule-later."""
    leg2 = FeeLeg(D(2), D(0), False)
    rule_later = replace(rule(), rule_id="rule-later", effective_from_nano=102 * NS,
                         effective_until_nano=10000 * NS, available_at_nano=101 * NS,
                         multiplier=D(20), close_fee=leg2, close_today_fee=leg2)
    rule_first = replace(rule(), effective_until_nano=102 * NS)
    return rule_first, rule_later


def test_multi_rule_legs_charge_each_fill_by_its_own_rule(monkeypatch):
    rule_first, rule_later = _two_rules()
    result, holder, bundle, _plan = _run(
        monkeypatch, _p1_bars(stop_bar=102), rules=(rule_first, rule_later))
    assert result.status == "COMPLETE"
    entry_fill = next(f for f in result.fills if f.offset == "OPEN")
    close_fill = next(f for f in result.fills if f.offset != "OPEN")
    assert entry_fill.rule_id == "rule-rb" and entry_fill.fee_cny == D(1)
    assert close_fill.rule_id == "rule-later" and close_fill.fee_cny == D(2)
    assert [r.rule_id for r in result.rule_originals] == ["rule-later", "rule-rb"]
    obs = _obs(result, bundle)
    # Open 100 under rule-rb (mult 10), stop 94 under rule-later (mult 20):
    # realized -120, fees 1 + 2 -> net -123; mark at 101 prices 100 -> 100.
    assert (obs.traded, obs.net_pnl_cny, obs.peak_margin_cny, obs.maximum_loss_cny) \
        == (True, D(-123), D(100), D(123))


def test_night_session_block_uses_trading_day(monkeypatch):
    result, holder, bundle, _plan = _run(monkeypatch, _p1_bars(stop_bar=102), night=True)
    entry = _entry_of(result)
    assert entry.context.features.trading_day == date(2026, 1, 13)
    obs = _obs(result, bundle)
    assert obs.block_id == "2026-01-13"
    with pytest.raises(ValueError, match="BLOCK_ID_NOT_TRADING_DAY"):
        observation_from_replay(entry.proposal, entry.context, entry.request,
                                _label_for(result, bundle)[2], result,
                                result.plan.training_end_nano, "2026-01-12",
                                rule_originals=result.rule_originals)


def test_genuine_zero_path_is_real_zero(monkeypatch):
    result, holder, bundle, _plan = _run(monkeypatch, _p1_bars(blocked=(101, 102)))
    assert result.status == "COMPLETE" and not result.fills
    assert any(r.status == "EXPIRED" for r in result.execution_results)
    obs = _obs(result, bundle)
    assert (obs.traded, obs.net_pnl_cny, obs.peak_margin_cny, obs.maximum_loss_cny,
            obs.return_before_stop) == (False, D(0), D(0), D(0), None)


# ─── candidate-stage rejections ─────────────────────────────────────────────

def _request_of(c):
    f = c.forecasts[0]
    return ForecastRequest(f.request_id, c.features, f.horizon, f.window_start_nano,
                           f.window_end_nano, f.barrier_key, f.upper_distance,
                           f.lower_distance, f.frozen_range)


def _mature_label(c):
    f = c.forecasts[0]
    return PathLabel(f.request_id, c.features.contract_id, "label-v2.1.0", f.horizon,
                     f.window_start_nano, f.window_end_nano, 131 * NS, "MATURE", (),
                     "UP", None, None, None, "UP_FIRST", None, None, None, None, None,
                     {key: None for key in PERSISTENCE_EVENTS}, {}, None)


def test_no_candidate_records_flat_state_without_fabricating(monkeypatch):
    result, holder, bundle, _plan = _run(monkeypatch, _p1_bars(), open_times=())
    assert result.status == "NO_CANDIDATE"
    assert "NO_TRIGGERED_BUDGETED_PATH" in result.exclusions
    assert not result.path_decisions and not result.orders and not result.fills
    assert not result.execution_results and not result.receipt_commits
    assert result.candidate_checks == {} and not result.proposals
    assert not result.checkpoints[-1].account.lots
    p, c = example()
    with pytest.raises(ValueError, match="PATH_DECISION_CHAIN_MISSING") as error:
        observation_from_replay(p, c, _request_of(c), _mature_label(c), result,
                                result.plan.training_end_nano, str(DAY),
                                rule_originals=result.rule_originals)
    assert str(error.value).startswith(PAYOFF_INTEGRITY_PREFIX)


@pytest.mark.parametrize("fixture_kwargs,domain,code", [
    (dict(regime_base="UNKNOWN"), "REGIME_MACRO", "REGIME_UNKNOWN"),
    (dict(confirmations=0), "TRIGGER_DOMAIN", "TRIGGER_NOT_MET"),
    (dict(persistence=.65), "FORECAST_CONFIDENCE", "PERSISTENCE_BELOW_THRESHOLD"),
    (dict(future_bar=True), "DATA_CLOCK", "DATA_INVALID"),
    (dict(fees_none=True), "COST_BUDGET", "COST_UNKNOWN"),
    (dict(stale_decision=True), "IDENTITY_TIME", "EVIDENCE_IDENTITY_MISMATCH"),
])
def test_each_domain_rejection_keeps_the_candidate_unmeasured(
        monkeypatch, fixture_kwargs, domain, code):
    result, holder, bundle, _plan = _run(monkeypatch, _p1_bars(stop_bar=102), **fixture_kwargs)
    assert result.status == "NO_CANDIDATE"
    assert len(result.proposals) == 1
    checks = result.candidate_checks[result.proposals[0].proposal_id]
    assert set(checks) == set(payoff_paths_module.PAYOFF_PATH_CHECK_DOMAINS)
    assert code in checks[domain]
    assert code in result.exclusions
    assert not result.path_decisions


def test_capacity_exhausted_blocks_in_account_capacity_domain(monkeypatch):
    bars = tuple(replace(b, volume=D(1)) for b in _p1_bars(stop_bar=102))
    result, holder, bundle, _plan = _run(monkeypatch, bars)
    assert result.status == "NO_CANDIDATE"
    checks = result.candidate_checks[result.proposals[0].proposal_id]
    assert "CAPACITY_EXHAUSTED" in checks["ACCOUNT_CAPACITY"]


def test_selection_is_frozen_first_candidate_failure_never_switches(monkeypatch):
    # The first candidate's entry is blocked twice and EXPIRES unfilled; the
    # OPENs the strategy keeps emitting at 101/102 are never sampled as
    # candidates and the zero path belongs to the FIRST candidate alone.
    result, holder, bundle, _plan = _run(
        monkeypatch, _p1_bars(blocked=(101, 102)),
        open_times=(100 * NS, 101 * NS, 102 * NS), post_selection_open=True)
    assert len(result.proposals) == 1 and result.proposals[0].decision_nano == ENTRY_NANO
    assert set(result.candidate_checks) == {result.proposals[0].proposal_id}
    assert sum(1 for d in result.path_decisions if d.action == "MEASURE_ENTRY") == 1
    assert result.status == "COMPLETE" and not result.fills
    assert any(r.status == "EXPIRED" for r in result.execution_results)


def test_incomplete_path_preserves_real_state(monkeypatch):
    result, holder, bundle, _plan = _run(
        monkeypatch, _p1_bars(until=135),
        plan_overrides=dict(window_end_nano=102 * NS, training_end_nano=102 * NS))
    assert result.status == "INCOMPLETE"
    assert "PATH_NOT_RELEASED_BEFORE_TRAINING_END" in result.exclusions
    assert result.checkpoints[-1].account.lots
    assert result.checkpoints[-1].pending_intents
    # The label itself matures later; the unreleased path is the blocker.
    entry, request, label = _label_for(result, bundle, training_end=200 * NS)
    assert label.status == "MATURE"
    with pytest.raises(ValueError, match="EPISODE_NOT_CLOSED") as error:
        observation_from_replay(entry.proposal, entry.context, request, label, result,
                                result.plan.training_end_nano,
                                str(entry.context.features.trading_day),
                                rule_originals=result.rule_originals)
    assert _m5_washes(error.value)


# ─── ordinary-gate / router non-bypass and mutual rejection ────────────────

def test_measured_candidate_still_rejected_by_ordinary_gate_and_router(monkeypatch):
    result, holder, bundle, _plan = _run(monkeypatch, _p1_bars(stop_bar=102))
    entry = _entry_of(result)
    gate = evaluate_gate(entry.proposal, entry.context, research_policy(),
                         checked_at_nano=entry.observed_at_nano)
    assert not gate.eligible and "INSUFFICIENT_SAMPLES" in gate.reasons
    risked = enforce_account_risk((entry.proposal,), (gate,), entry.context.account,
                                  (instrument(),), (), (), (rule(),), {"rb": 3},
                                  research_policy(), valuations=())
    decision = choose_proposal((entry.proposal,), risked, entry.context.account, "opp",
                               entry.observed_at_nano, entry.context.run_policy, ())
    assert decision.selected_proposal_id is None
    assert decision.weights[entry.proposal.proposal_id] == 0.0
    assert decision.weights["CASH"] == 1.0

    # No GateDecision / DecisionRecord ever exists inside the measurement pack.
    def scan(value):
        if isinstance(value, (tuple, list)):
            return any(scan(item) for item in value)
        if isinstance(value, dict):
            return any(scan(item) for item in value.values())
        if hasattr(value, "__dict__"):
            if value.__class__.__name__ in ("GateDecision", "DecisionRecord"):
                return True
            return any(scan(item) for item in vars(value).values())
        return False
    assert not scan(result)


def test_measurement_and_normal_domains_reject_each_other(monkeypatch):
    from ak_trading.v2.schemas import SchemaError
    result, holder, bundle, _plan = _run(monkeypatch, _p1_bars(stop_bar=102))
    entry = _entry_of(result)
    _e, request, label = _label_for(result, bundle)
    with pytest.raises(SchemaError):
        decode_record(LedgerCheckpoint, encode_record(result.checkpoints[0]))
    with pytest.raises(ValueError):
        ContinuousLedger.restore(result.checkpoints[0], (instrument(),), (rule(),),
                                 result.checkpoints[0].run_policy, HASH, HASH,
                                 dict(result.checkpoints[0].model_hashes), {},
                                 exit_registry_sha256=HASH)
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model
    with pytest.raises(AttributeError):
        replay_payloads(result, fit_path_payoff_model((), 3 * NS, "research-policy-v2.1.0"))
    with pytest.raises(TypeError):
        observation_from_replay(entry.proposal, entry.context, request, label, result,
                                result.plan.training_end_nano, str(DAY))


def test_entry_point_has_no_payoff_label_or_force_parameters():
    import inspect
    parameters = inspect.signature(replay_payoff_training_window).parameters
    assert not {"payoff_model", "label", "force", "training", "skip_payoff_gate"} & set(parameters)
    assert set(parameters) == {
        "bundle", "forecast_models", "spec", "initial_account", "run_policy", "policy",
        "seasonality_model", "regime_reference", "contract_lock_sha256", "plan",
        "exit_specs", "exit_binding"}


# ─── source determinism and the eleven new suffixes ─────────────────────────

def test_source_determinism_and_recompute_same_hash(monkeypatch):
    result, holder, bundle, _plan = _run(monkeypatch, _p1_bars(stop_bar=102))
    entry, request, label = _label_for(result, bundle)
    block = str(entry.context.features.trading_day)
    first = _obs(result, bundle)
    second = observation_from_replay(entry.proposal, entry.context, request, label,
                                     result, result.plan.training_end_nano, block,
                                     rule_originals=result.rule_originals)
    assert first == second and first.source_sha256 == second.source_sha256
    source_bytes = payoff_observation_source(entry.proposal, entry.context, request,
                                             label, result, result.plan.training_end_nano,
                                             block, rule_originals=result.rule_originals)
    assert sha256_hex(source_bytes) == first.source_sha256
    assert first.observation_id == stable_id("payoff_observation", first.source_sha256)
    # A changed label moves the source hash; the condition key is unchanged.
    other = observation_from_replay(
        entry.proposal, entry.context, request, replace(label, path_outcome="UP_FIRST"),
        result, result.plan.training_end_nano, block, rule_originals=result.rule_originals)
    assert other.source_sha256 != first.source_sha256
    assert other.condition_key == first.condition_key
    import json
    projection = json.loads(source_bytes)
    assert projection["producer_version"] == "observation-from-replay-p1-v2"
    assert projection["source_kind"] == "P1_ACTION_CONDITIONAL_REPLAY"
    assert projection["plan"]["strategy_id"] == "trend_m1_v2"


def _complete(monkeypatch):
    """A genuinely multi-rule complete path: originals sorted [rule-later, rule-rb]."""
    rule_first, rule_later = _two_rules()
    result, holder, bundle, _plan = _run(
        monkeypatch, _p1_bars(stop_bar=102), rules=(rule_first, rule_later))
    entry, request, label = _label_for(result, bundle)
    return result, entry, request, label


def test_new_integrity_suffixes_fire_on_tampering(monkeypatch):
    result, entry, request, label = _complete(monkeypatch)
    te = result.plan.training_end_nano
    block = str(entry.context.features.trading_day)
    originals = result.rule_originals

    def call(replay, **kwargs):
        training_end = kwargs.pop("training_end_nano", te)
        return observation_from_replay(entry.proposal, entry.context, request, label,
                                       replay, training_end, block, **kwargs)

    with pytest.raises(ValueError, match="SOURCE_KIND_MISMATCH"):
        call(_tampered(result, source_kind="NORMAL_REPLAY"), rule_originals=originals)
    with pytest.raises(ValueError, match="SAMPLING_PLAN_MISMATCH"):
        call(result, training_end_nano=te + NS, rule_originals=originals)
    forged_entry = copy.copy(entry)
    object.__setattr__(forged_entry, "proposal", replace(entry.proposal, reasons=("X",)))
    with pytest.raises(ValueError, match="CANDIDATE_SOURCE_MISMATCH"):
        call(_tampered(result, path_decisions=(forged_entry,)), rule_originals=originals)
    with pytest.raises(ValueError, match="PATH_DECISION_CHAIN_MISSING"):
        call(_tampered(result, path_decisions=()), rule_originals=originals)
    with pytest.raises(ValueError, match="PATH_ORDER_CHAIN_MISSING"):
        call(_tampered(result, orders=()), rule_originals=originals)
    with pytest.raises(ValueError, match="RECEIPT_COMMIT_MISMATCH"):
        call(_tampered(result, receipt_commits=()), rule_originals=originals)
    wrong_account = copy.copy(entry)
    object.__setattr__(wrong_account, "account_before_sha256", "9" * 64)
    swapped = tuple(wrong_account if d is entry else d for d in result.path_decisions)
    with pytest.raises(ValueError, match="INITIAL_ACCOUNT_MISMATCH"):
        call(_tampered(result, path_decisions=swapped), rule_originals=originals)
    bad = replace(originals[-1], slippage_ticks=D(1))
    with pytest.raises(ValueError, match="RULE_ORIGINAL_CONFLICT"):
        call(result, rule_originals=(*originals[:-1], bad))
    with pytest.raises(ValueError, match="RULE_CONSUMPTION_MISMATCH"):
        call(_tampered(result, rule_originals=()), rule_originals=())
    # rule-later (originals[0], NOT the entry rule) is made future-visible in
    # both result and keyword: only the close fill's PIT check can fire.
    future = (replace(originals[0], available_at_nano=200 * NS), originals[1])
    with pytest.raises(ValueError, match="RULE_PIT_MISMATCH"):
        call(_tampered(result, rule_originals=future), rule_originals=future)
    # SOURCE_BYTES_MISMATCH stays registered for the M5 source-recompute guard.
    assert "SOURCE_BYTES_MISMATCH" in PAYOFF_PATH_INTEGRITY_SUFFIXES
    assert len(PAYOFF_PATH_INTEGRITY_SUFFIXES) == 11
    assert len(set(PAYOFF_PATH_INTEGRITY_SUFFIXES)) == 11


def test_rule_originals_extra_or_future_rejected_on_path(monkeypatch):
    result, entry, request, label = _complete(monkeypatch)
    te = result.plan.training_end_nano
    block = str(entry.context.features.trading_day)
    extra = replace(rule(), rule_id="rule-extra", effective_from_nano=0,
                    effective_until_nano=10000 * NS)
    # An extra never-consumed original posed as a consumed source (result and
    # keyword kept consistent so only the coverage check can fire).
    enlarged = (*result.rule_originals, extra)
    with pytest.raises(ValueError, match="RULE_CONSUMPTION_MISMATCH"):
        observation_from_replay(entry.proposal, entry.context, request, label,
                                _tampered(result, rule_originals=enlarged), te, block,
                                rule_originals=enlarged)
    # Tampering the frozen plan rejects (never silently re-hashes).
    other_plan = replace(result.plan, outer_split_id="outer-r2")
    with pytest.raises(ValueError, match="SAMPLING_PLAN_MISMATCH"):
        observation_from_replay(entry.proposal, entry.context, request, label,
                                _tampered(result, plan=other_plan), te, block,
                                rule_originals=result.rule_originals)


def test_multi_rule_path_future_original_rejected_at_fill_pit(monkeypatch):
    rule_first, rule_later = _two_rules()
    result, holder, bundle, _plan = _run(
        monkeypatch, _p1_bars(stop_bar=102), rules=(rule_first, rule_later))
    entry, request, label = _label_for(result, bundle)
    te = result.plan.training_end_nano
    block = str(entry.context.features.trading_day)
    # The later rule becomes visible only AFTER the close fill executed: with
    # result and keyword kept consistent, only the fill PIT check can fire.
    future = (replace(result.rule_originals[0], available_at_nano=200 * NS),
              result.rule_originals[1])
    with pytest.raises(ValueError, match="RULE_PIT_MISMATCH"):
        observation_from_replay(entry.proposal, entry.context, request, label,
                                _tampered(result, rule_originals=future), te, block,
                                rule_originals=future)


# ─── gate refactor invariance (counter-example with the frozen original) ────

def _original_evaluate_gate(proposal, context, policy, *, checked_at_nano):
    """VERBATIM copy of the pre-E-P1 evaluate_gate (frozen behavior oracle)."""
    from ak_trading.v2.contracts import GateDecision
    from ak_trading.v2.policy import confidence_policy, stress_policy
    from ak_trading.v2.schemas import calibration_geometry_key, probability_in_bucket
    from ak_trading.v2.decision.execution import simulate_stress_paths
    from ak_trading.v2.decision.payoffs import payoff_event, proposal_forecast
    if type(checked_at_nano) is not int or checked_at_nano < context.now_nano:
        raise ValueError("gate_clock_invalid")
    reasons = []
    evidence_ids = []
    now = context.now_nano
    spec = context.spec
    features = context.features
    position = context.position
    if proposal.strategy_id != spec.strategy_id or proposal.contract_id != features.contract_id \
            or proposal.decision_nano != now:
        reasons.append("EVIDENCE_IDENTITY_MISMATCH")
    if proposal.action in ("REDUCE", "EXIT"):
        if position is None or position.strategy_id != proposal.strategy_id \
                or position.episode_id != proposal.episode_id \
                or abs(proposal.target_lots) >= abs(position.lots) \
                or proposal.target_lots * position.lots < 0:
            reasons.append("POSITION_OWNERSHIP_MISMATCH")
        return GateDecision(proposal.proposal_id, not reasons, tuple(sorted(set(reasons))),
                            (), checked_at_nano)
    if proposal.action not in ("OPEN", "ADD"):
        return GateDecision(proposal.proposal_id, False,
                            tuple(sorted(set(reasons + list(proposal.reasons)
                                           + ["TRIGGER_NOT_MET"]))), (), checked_at_nano)
    cp = confidence_policy(spec.family)
    if context.confidence_policy != cp or context.stress_policy != stress_policy():
        reasons.append("EVIDENCE_IDENTITY_MISMATCH")
    if context.run_policy.research_policy_version != policy["policy_version"]:
        reasons.append("EVIDENCE_IDENTITY_MISMATCH")
    if features.quality.status != "READY" or context.account.quality.status != "READY":
        reasons.append("DATA_STALE" if "STALE" in (features.quality.status,
                                                   context.account.quality.status)
                       else "DATA_INVALID")
    if features.available_at_nano > now or features.decision_nano != now:
        reasons.append("DATA_INVALID")
    if not context.sessions or any(s.evidence_status == "UNKNOWN" for s in context.sessions):
        reasons.append("CLOCK_UNKNOWN")
    if spec.mode == "MINUTE" and any(b.duration_seconds == 86400
                                      and b.trading_day == features.trading_day
                                      for b in context.bars):
        reasons.append("CURRENT_DAY_DAILY_FORBIDDEN")
    if any(b.end_nano > now or b.available_at_nano is None or b.available_at_nano > now
           or not b.complete for b in context.bars):
        reasons.append("DATA_INVALID")
    factors = {v.factor_key: v for v in features.values}
    if len(factors) != len(features.values) or any(
            k not in factors or factors[k].value is None
            or factors[k].quality.status != "READY"
            or factors[k].available_at_nano is None
            or factors[k].available_at_nano > now for k in spec.required_factor_keys):
        reasons.append("DATA_INVALID")
    if spec.mode == "MINUTE" and context.bars:
        last = max(b.end_nano for b in context.bars if b.duration_seconds == 60)
        active_age = sum(max(0, min(checked_at_nano, s.end_nano) - max(last, s.start_nano))
                         for s in context.sessions)
        if active_age > int(policy["minute_freshness_seconds"]) * 1_000_000_000:
            reasons.append("DATA_STALE")
    if context.regime.base == "UNKNOWN":
        reasons.append("REGIME_UNKNOWN")
    if context.regime.base not in spec.allowed_regimes \
            or not set(context.regime.flags) <= set(spec.allowed_flags):
        reasons.append("REGIME_INELIGIBLE")
    if context.regime.cross_timeframe in ("CONFLICT", "COUNTERTREND"):
        reasons.append("UNVALIDATED_TIMEFRAME_CONFLICT")
    if spec.family in ("BREAKOUT", "PULLBACK") and spec.family not in context.regime.flags:
        reasons.append("REGIME_INELIGIBLE")
    if context.run_policy.macro_mode == "WITH_MACRO":
        if context.macro.mode != "WITH_MACRO" or context.macro.quality.status != "READY":
            reasons.append("MACRO_REQUIRED_MISSING")
        if context.macro.risk_tags:
            reasons.append("MACRO_EVENT_RISK")
    if "MACRO_COVERAGE_UNKNOWN" in context.macro.quality.reasons:
        reasons.append("MACRO_COVERAGE_UNKNOWN")
    if context.macro.decision_nano != now \
            or context.macro.policy_version != context.run_policy.macro_policy_version:
        reasons.append("EVIDENCE_IDENTITY_MISMATCH")
    if position is not None:
        if position.pending_exit:
            reasons.append("EXIT_PENDING")
        if (position.strategy_id != proposal.strategy_id
                or position.episode_id != proposal.episode_id
                or proposal.action == "OPEN" or proposal.target_lots * position.lots <= 0):
            reasons.append("POSITION_OWNERSHIP_MISMATCH")
    elif proposal.action == "ADD":
        reasons.append("POSITION_OWNERSHIP_MISMATCH")
    if proposal.budget is not None:
        if proposal.layer_index is None or proposal.layer_index >= len(proposal.budget.layers):
            reasons.append("EVIDENCE_IDENTITY_MISMATCH")
        else:
            signed_layer = proposal.budget.side * proposal.budget.layers[
                proposal.layer_index].lots
            if proposal.action == "OPEN" and (proposal.target_lots == 0
                                              or proposal.target_lots != signed_layer):
                reasons.append("EVIDENCE_IDENTITY_MISMATCH")
            if proposal.action == "ADD" and position is not None:
                if (proposal.target_lots != position.lots + signed_layer
                        or abs(proposal.target_lots) <= abs(position.lots)
                        or proposal.target_lots * proposal.budget.side <= 0
                        or position.lots * proposal.budget.side <= 0):
                    reasons.append("POSITION_OWNERSHIP_MISMATCH")
    if proposal.next_state.confirmation_count < cp.confirmation_observations:
        reasons.append("TRIGGER_NOT_MET")
    if proposal.next_state.pending_intent_ids:
        reasons.append("EXIT_PENDING")
    forecast = proposal_forecast(proposal, context)
    if forecast is None:
        reasons.append("EVIDENCE_IDENTITY_MISMATCH")
    else:
        expected_range = (proposal.budget.frozen_range if proposal.budget
                          and proposal.budget.frozen_range
                          else proposal.next_state.setup.frozen_range
                          if proposal.next_state.setup else context.regime.frozen_range)
        if (forecast.contract_id != features.contract_id
                or forecast.snapshot_id != features.snapshot_id
                or forecast.product != features.product
                or forecast.session_kind != features.session_kind
                or forecast.session_id != features.session_id
                or forecast.trading_day != features.trading_day
                or forecast.duration_seconds != features.duration_seconds
                or forecast.assessment_id != context.regime.assessment_id
                or forecast.decision_nano != now or forecast.base_regime != context.regime.base
                or forecast.reference_price != features.reference_price
                or forecast.atr_points != features.atr_points
                or forecast.frozen_range != expected_range
                or proposal.assessment_id != forecast.assessment_id):
            reasons.append("EVIDENCE_IDENTITY_MISMATCH")
        if (forecast.barrier_key != "ATR_1" or forecast.upper_distance != features.atr_points
                or forecast.lower_distance != features.atr_points):
            reasons.append("BARRIER_MISMATCH")
        if forecast.quality.status != "READY":
            reasons.append("DATA_INVALID")
        if forecast.published_at_nano >= forecast.window_start_nano \
                or "FORECAST_LATE" in forecast.quality.reasons:
            reasons.append("FORECAST_LATE")
        if forecast.published_at_nano > checked_at_nano:
            reasons.append("EVIDENCE_IDENTITY_MISMATCH")
        if checked_at_nano >= forecast.expires_at_nano:
            reasons.append("MODEL_EXPIRED")
        if forecast.out_of_distribution:
            reasons.append("OOD")
        if forecast.path_probabilities["AMBIGUOUS"] > cp.maximum_ambiguity:
            reasons.append("AMBIGUITY_ABOVE_CAP")
        if forecast.window_end_nano > context.holding_deadline_nano \
                or checked_at_nano >= context.holding_deadline_nano \
                or checked_at_nano >= proposal.valid_until_nano \
                or proposal.earliest_execution_nano < forecast.window_start_nano \
                or proposal.valid_until_nano > forecast.window_end_nano:
            reasons.append("HOLDING_WINDOW_TOO_SHORT")
        if proposal.action == "OPEN" and forecast.horizon != spec.max_holding:
            reasons.append("EVIDENCE_IDENTITY_MISMATCH")
        if proposal.action == "ADD" and (proposal.next_state.active_exit_nano is None
                                         or forecast.window_end_nano
                                         > proposal.next_state.active_exit_nano):
            reasons.append("HOLDING_WINDOW_TOO_SHORT")
        side = proposal.budget.side if proposal.budget else proposal.next_state.direction
        persist = ("ORDERED_RANGE" if spec.family in ("REVERSION", "GRID", "MARTINGALE")
                   else ("UP" if side == 1 else "DOWN")
                   + ("_BREAKOUT" if spec.family == "BREAKOUT" else "_TREND"))
        probability = forecast.persistence_probabilities.get(persist)
        if probability is None or probability < cp.minimum_persistence:
            reasons.append("PERSISTENCE_BELOW_THRESHOLD")
        events = [("PATH", None), ("PERSIST:" + persist, probability)]
        if spec.family in ("REVERSION", "GRID", "MARTINGALE"):
            events.append(("RETURN_BEFORE_BREAK", forecast.return_before_break_probability))
            terminals = [t for t in forecast.time_cdf
                         if t.event == "RETURN_FIRST" and t.step == forecast.horizon.value]
            if not terminals or any(t.unknown_probability > cp.maximum_ambiguity
                                    for t in terminals):
                reasons.append("AMBIGUITY_ABOVE_CAP")
        if spec.family == "MARTINGALE":
            stop = forecast.stop_touch_probabilities.get("LONG" if side == 1 else "SHORT")
            if stop is None or stop > cp.maximum_stop_touch:
                reasons.append("STOP_TOUCH_ABOVE_CAP")
            events.append(("STOP_TOUCH:" + ("LONG" if side == 1 else "SHORT"), stop))
        for event, value in events:
            geom_event = event.removeprefix("PERSIST:")
            if event.startswith("STOP_TOUCH:"):
                geom_event = "STOP_TOUCH"
            geometry = calibration_geometry_key(features, forecast.frozen_range, geom_event)
            matches = []
            for e in forecast.evidence:
                if e.event != event:
                    continue
                identity = (e.model_id == forecast.model_id and e.policy_id == cp.policy_id
                            and e.product == features.product
                            and e.session_kind == features.session_kind
                            and e.duration_seconds == features.duration_seconds
                            and e.base_regime == context.regime.base
                            and e.horizon == forecast.horizon
                            and e.label_version == forecast.label_version
                            and e.barrier_key == ("ATR_1" if event == "PATH" else "COMMON")
                            and geometry is not None and e.geometry_key == geometry
                            and (e.bucket == "ALL" if event == "PATH" else
                                 (value is not None and e.bucket != "ALL"
                                  and probability_in_bucket(value, e.bucket))))
                if identity:
                    matches.append(e)
            if not matches:
                reasons.append("EVIDENCE_IDENTITY_MISMATCH")
            for e in matches:
                evidence_ids.append(e.evidence_id)
                if e.training_end_nano >= e.calibration_end_nano \
                        or e.calibration_end_nano >= now:
                    reasons.append("EVIDENCE_IDENTITY_MISMATCH")
                if checked_at_nano >= e.valid_until_nano or e.status == "EXPIRED":
                    reasons.append("MODEL_EXPIRED")
                if e.status != "VALID" or e.calibration_error is None \
                        or e.calibration_error > cp.maximum_calibration_error:
                    reasons.append("CALIBRATION_FAILED")
                if e.sample_count < cp.minimum_samples:
                    reasons.append("INSUFFICIENT_SAMPLES")
                if e.effective_blocks < cp.minimum_blocks:
                    reasons.append("INSUFFICIENT_BLOCKS")
    rule = context.rule
    if rule is None or rule.evidence == "UNKNOWN" or any(x is None for x in (
            rule.open_fee, rule.close_fee, rule.close_today_fee,
            rule.long_margin_rate, rule.short_margin_rate)):
        reasons.append("COST_UNKNOWN")
    elif not (rule.available_at_nano <= checked_at_nano
              and rule.effective_from_nano <= checked_at_nano < rule.effective_until_nano):
        reasons.append("COST_UNKNOWN")
    budget = proposal.budget
    if budget is None:
        reasons.append("ROUND_BUDGET_EXCEEDED")
    else:
        if proposal.stop_price != budget.stop_price or proposal.target_price != budget.target_price \
                or abs(proposal.target_lots) > budget.maximum_lots:
            reasons.append("ROUND_BUDGET_EXCEEDED")
        if budget.planned_loss_cny > budget.equity_at_start_cny * D(
                str(policy["maximum_round_loss_ratio"])):
            reasons.append("ROUND_BUDGET_EXCEEDED")
        if budget.stress_loss_cny > budget.equity_at_start_cny * D(
                str(policy["maximum_stress_loss_to_equity"])):
            reasons.append("TAIL_LOSS_LIMIT")
        if rule is not None:
            try:
                paths = simulate_stress_paths(budget, rule, ())
                if max(p.loss_cny for p in paths) > budget.stress_loss_cny:
                    reasons.append("ROUND_BUDGET_EXCEEDED")
                if any(p.margin_shortfall_cny > 0 for p in paths):
                    reasons.append("MARGIN_LIMIT")
            except ValueError:
                reasons.append("COST_UNKNOWN")
    payoff = proposal.payoff
    if payoff is None:
        reasons.append("INSUFFICIENT_SAMPLES")
    else:
        bound_payoff = context.model_hashes["payoff"] if "payoff" in context.model_hashes else None
        expected_geometry = calibration_geometry_key(
            features, forecast.frozen_range if forecast else None,
            payoff_event(proposal, context))
        if (payoff.proposal_id != proposal.proposal_id or forecast is None
                or payoff.horizon != forecast.horizon
                or payoff.barrier_key != "ATR_1" or payoff.geometry_key != expected_geometry
                or payoff.training_end_nano >= now
                or bound_payoff is None or payoff.model_id != "payoff:" + bound_payoff):
            reasons.append("EVIDENCE_IDENTITY_MISMATCH")
        if payoff.missing_probability_mass > 0:
            reasons.append("UNSUPPORTED_PATH_MASS")
        if payoff.sample_count < cp.minimum_samples or payoff.status != "READY":
            reasons.append("INSUFFICIENT_SAMPLES")
        if payoff.effective_blocks < cp.minimum_blocks:
            reasons.append("INSUFFICIENT_BLOCKS")
        if payoff.conservative_ev_cny is None \
                or payoff.conservative_ev_cny <= cp.minimum_conservative_ev_cny:
            reasons.append("NON_POSITIVE_EV")
        if payoff.loss_cvar95_cny is None or budget is None \
                or payoff.loss_cvar95_cny > budget.planned_loss_cny:
            reasons.append("TAIL_LOSS_LIMIT")
    if context.run_policy.purpose == "SHADOW_OPERATIONAL":
        q = context.qualification
        if q is None or q.statistics != "PASS":
            reasons.append("STATISTICS_NOT_PASSED")
        if q is not None:
            if not q.issued_at_nano <= checked_at_nano < q.valid_until_nano:
                reasons.append("QUALIFICATION_EXPIRED")
            if (q.strategy_id != spec.strategy_id or q.strategy_version != spec.version
                    or q.parameter_version != spec.parameter_version
                    or q.code_sha256 != spec.code_sha256
                    or q.product != features.product
                    or proposal.contract_id not in q.contract_ids
                    or q.mode != spec.mode or forecast is None
                    or forecast.horizon not in q.horizons
                    or q.macro_mode != context.run_policy.macro_mode
                    or q.research_policy_version != context.run_policy.research_policy_version
                    or q.macro_policy_version != context.run_policy.macro_policy_version
                    or context.run_policy.cost_scenario not in q.cost_scenarios
                    or dict(q.model_hashes) != dict(context.model_hashes)):
                reasons.append("QUALIFICATION_MISMATCH")
    return GateDecision(proposal.proposal_id, not reasons, tuple(sorted(set(reasons))),
                        tuple(sorted(set(evidence_ids))), checked_at_nano)


def test_gate_refactor_keeps_outputs_byte_identical():
    from ak_trading.v2.contracts import PositionSnapshot, StrategyQualification
    from test_v2_m4_ledger import MODELS
    policy = research_policy()
    p, c = example()
    proposals = [p, replace(p, payoff=None),
                 replace(p, payoff=replace(p.payoff, status="INSUFFICIENT")),
                 replace(p, payoff=replace(p.payoff, conservative_ev_cny=D(0))),
                 replace(p, payoff=replace(p.payoff, missing_probability_mass=.1)),
                 replace(p, payoff=replace(p.payoff, sample_count=3)),
                 replace(p, payoff=replace(p.payoff, effective_blocks=2)),
                 replace(p, payoff=replace(p.payoff, model_id="payoff:" + "b" * 64)),
                 replace(p, payoff=replace(p.payoff, loss_cvar95_cny=D(9999))),
                 replace(p, payoff=replace(p.payoff, horizon=Horizon("TRADING_MINUTE", 5))),
                 replace(p, action="WAIT", target_lots=0, budget=None, payoff=None),
                 replace(p, action="HOLD", target_lots=1, payoff=None),
                 replace(p, action="EXIT", target_lots=0, payoff=None),
                 replace(p, action="REDUCE", target_lots=0, payoff=None),
                 replace(p, action="OPEN", target_lots=0),
                 replace(p, decision_nano=c.now_nano - NS),
                 replace(p, earliest_execution_nano=99 * NS),
                 replace(p, next_state=replace(p.next_state, confirmation_count=1))]
    contexts = [c,
                replace(c, rule=None),
                replace(c, rule=replace(c.rule, effective_until_nano=c.now_nano + 1)),
                replace(c, regime=replace(c.regime, base="UNKNOWN")),
                replace(c, regime=replace(c.regime, cross_timeframe="CONFLICT")),
                replace(c, features=replace(c.features, quality=quality("STALE"))),
                replace(c, account=replace(c.account, quality=quality("INVALID"))),
                replace(c, bars=c.bars + (bar(t=200 * NS),)),
                replace(c, bars=(), sessions=()),
                replace(c, forecasts=(replace(c.forecasts[0], published_at_nano=c.now_nano + 1),)),
                replace(c, forecasts=(replace(c.forecasts[0], out_of_distribution=True),)),
                replace(c, forecasts=(replace(
                    c.forecasts[0], path_probabilities={
                        "UP_FIRST": .5, "DOWN_FIRST": .4, "NEITHER": .05, "AMBIGUOUS": .05}),)),
                replace(c, forecasts=(replace(
                    c.forecasts[0], persistence_probabilities={"UP_TREND": .5}),)),
                replace(c, forecasts=(replace(
                    c.forecasts[0],
                    evidence=tuple(replace(e, sample_count=3) for e in c.forecasts[0].evidence)),)),
                replace(c, forecasts=(replace(
                    c.forecasts[0],
                    evidence=tuple(replace(e, status="FAILED") for e in c.forecasts[0].evidence)),)),
                replace(c, forecasts=()),
                replace(c, position=PositionSnapshot("rb", p.strategy_id, p.episode_id, 1,
                                                     D(100), True)),
                replace(c, holding_deadline_nano=c.now_nano),
                replace(c, model_hashes={**MODELS, "payoff": "b" * 64}),
                replace(c, run_policy=replace(c.run_policy, macro_mode="WITH_MACRO"))]
    qualification = StrategyQualification(
        "qualification", c.spec.strategy_id, c.spec.version, c.spec.parameter_version,
        c.spec.code_sha256, "d" * 64, c.exit_binding.binding_sha256,
        c.exit_binding.exit_spec_sha256s, "SHFE.RB", ("rb",), "MINUTE",
        (c.spec.max_holding,), "PRICE_ONLY", "research-policy-v2.1.0", "macro-risk-v2.1.0",
        ("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5", "GAP", "NO_STOP"),
        dict(MODELS), HASH, "PASS", HASH, HASH, NS, 500 * NS, ())
    contexts.append(replace(c, run_policy=replace(c.run_policy,
                                                  purpose="SHADOW_OPERATIONAL"),
                            qualification=qualification))
    compared = 0
    for proposal in proposals:
        for context in contexts:
            for checked in (c.now_nano, c.now_nano + 2):
                compared += 1
                assert evaluate_gate(proposal, context, policy,
                                     checked_at_nano=checked) == \
                    _original_evaluate_gate(proposal, context, policy,
                                            checked_at_nano=checked)
    assert compared > 500


def test_gate_clock_and_program_errors_unchanged():
    p, c = example()
    with pytest.raises(ValueError, match="gate_clock_invalid"):
        evaluate_gate(p, c, research_policy(), checked_at_nano=c.now_nano - 1)
    with pytest.raises(ValueError, match="gate_clock_invalid"):
        evaluate_gate(p, c, research_policy(), checked_at_nano=True)


# ─── legacy explicit multi-rule branch ──────────────────────────────────────

def test_legacy_replay_explicit_multi_rule_branch(monkeypatch):
    bundle, models, reference, holder, spec = _p1_fixture(
        monkeypatch, _p1_bars(stop_bar=102))
    replay = _p1_run(bundle, models, _p1_model(), reference, spec)
    opened, request, label = _p1_evidence(replay, holder["context"], 200 * NS, bundle)
    single = observation_from_replay(opened, holder["context"], request, label, replay,
                                     200 * NS, str(DAY))
    # Explicit originals on the legacy replay: same economics, producer v2 and
    # a distinct (superset) source projection.
    multi = observation_from_replay(opened, holder["context"], request, label, replay,
                                    200 * NS, str(DAY), rule_originals=(rule(),))
    assert (multi.traded, multi.net_pnl_cny, multi.peak_margin_cny,
            multi.maximum_loss_cny) == (True, D(-62), D(100), D(62))
    assert multi.source_sha256 != single.source_sha256
    source = payoff_observation_source(opened, holder["context"], request, label, replay,
                                       200 * NS, str(DAY), rule_originals=(rule(),))
    import json
    assert json.loads(source)["producer_version"] == "observation-from-replay-p1-v2"
    assert sha256_hex(source) == multi.source_sha256
    # Missing original for the consumed rule stays the frozen excludable gap.
    other = replace(rule(), rule_id="rule-other", effective_from_nano=0,
                    effective_until_nano=10000 * NS)
    with pytest.raises(ValueError, match="RULE_ORIGINAL_MISSING") as missing:
        observation_from_replay(opened, holder["context"], request, label, replay,
                                200 * NS, str(DAY), rule_originals=(other,))
    assert _m5_washes(missing.value)
    # An extra never-consumed original posed as consumed is a conflict.
    with pytest.raises(ValueError, match="RULE_ORIGINAL_CONFLICT"):
        observation_from_replay(opened, holder["context"], request, label, replay,
                                200 * NS, str(DAY), rule_originals=(rule(), other))
    # A duplicate rule_id is a conflict.
    with pytest.raises(ValueError, match="RULE_ORIGINAL_CONFLICT"):
        observation_from_replay(opened, holder["context"], request, label, replay,
                                200 * NS, str(DAY), rule_originals=(rule(), rule()))
    # A content-tampered original conflicts with the checkpoint record before
    # any timing judgment (the per-fill PIT semantics are exercised on the
    # measurement branch below with genuinely multi-rule paths).
    tampered_rule = replace(rule(), available_at_nano=200 * NS)
    with pytest.raises(ValueError, match="RULE_ORIGINAL_CONFLICT"):
        observation_from_replay(opened, holder["context"], request, label, replay,
                                200 * NS, str(DAY), rule_originals=(tampered_rule,))


def test_path_determinism_same_inputs_same_identity(monkeypatch):
    first, _h1, _b1, _p1 = _run(monkeypatch, _p1_bars(stop_bar=102))
    second, _h2, _b2, _p2 = _run(monkeypatch, _p1_bars(stop_bar=102))
    assert first == second
    assert first.path_id == second.path_id
    changed, _h3, _b3, _p3 = _run(monkeypatch, _p1_bars(stop_bar=102),
                                  plan_overrides=dict(initial_cash_cny=D("999999")))
    assert changed.path_id != first.path_id
