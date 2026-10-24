"""Pure context assembly with an explicit producer for every published forecast."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Callable, Mapping
from types import MappingProxyType

from ak_trading.v2.artifacts import canonical_json_bytes, sha256_hex, stable_id
from ak_trading.v2.clock import ClockError, holding_deadline, horizon_window
from ak_trading.v2.contracts import (
    AccountSnapshot, DataQuality, ExitExecutionLink, ExitPolicyBinding, ExitProposal,
    ExitSpec, ForecastRequest, FrozenInput, Horizon, MacroContext, MarketForecast,
    OrderIntent, RegimeAssessment, RunPolicy, StrategyContext, StrategyQualification,
    StrategySpec, StrategyState, StrategyProposal,
)
from ak_trading.v2.data import visible_bars, visible_rule
from ak_trading.v2.factor_specs import factor_registry_v2, baseline_registry_v2
from ak_trading.v2.factors import _deduplicate_visible, build_feature_snapshot
from ak_trading.v2.macro import MacroCache
from ak_trading.v2.policy import confidence_policy, research_policy, stress_policy
from ak_trading.v2.probability.models import ForecastModelSet
from ak_trading.v2.probability.regimes import RegimeReference, assess_regime, freeze_range
from ak_trading.v2.probability.forecast import build_forecast_requests, predict_market
from ak_trading.v2.probability.labels import LABEL_VERSION
from ak_trading.v2.schemas import calibration_geometry_key, encode_record
from ak_trading.v2.seasonality import SeasonalityModel
from ak_trading.v2.decision.execution import fee_amount, position_for
from ak_trading.v2.decision.payoffs import PathPayoffModel


# --- M3C P1 exit consumption (08 §4.2 / 007 §5.4) -------------------------
from decimal import Decimal


# 007 §7.4 (audit-corrected): a NEXT_WINDOW_CLOSE exit — partial or full —
# always emits purpose REDUCE; only frozen protective/roll tokens
# (STOP_TOUCHED / TARGET_REACHED / ROLL_EXIT) keep their special purposes.
_NEXT_WINDOW_PURPOSE = {"REDUCE": "REDUCE", "EXIT": "REDUCE"}
_PROTECTIVE_PURPOSE = {"REDUCE": "REDUCE", "EXIT": "STOP"}
_ROLL_TOKENS = frozenset({"STOP_TOUCHED", "TARGET_REACHED", "ROLL_EXIT"})


def execution_kind_for(exit_specs: tuple[ExitSpec, ...],
                       triggering_policy_id: str | None) -> str:
    """Resolve the execution kind for a triggering exit policy.

    P1 soft exits are NEXT_LEGAL_WINDOW (NEXT_WINDOW_CLOSE); only policies
    whose frozen spec declares LEGACY_PROTECTIVE keep protective matching.
    Hard-risk exits (no triggering policy) stay legacy-protective.
    """
    if triggering_policy_id is None:
        return "LEGACY_PROTECTIVE"
    for spec in exit_specs:
        if spec.exit_policy_id == triggering_policy_id:
            return "LEGACY_PROTECTIVE" if spec.execution_timing == "LEGACY_PROTECTIVE" \
                else "NEXT_WINDOW_CLOSE"
    return "NEXT_WINDOW_CLOSE"


def evaluate_exit_proposal(exit_proposal: ExitProposal, account: AccountSnapshot,
                           exit_specs: tuple[ExitSpec, ...],
                           exit_binding: ExitPolicyBinding,
                           entry_proposal: StrategyProposal) -> tuple[StrategyProposal, tuple[OrderIntent, ...]]:
    """Admit one selected exit candidate through the frozen M4 routing.

    Returns the risk-reduction StrategyProposal (action REDUCE/EXIT with a
    signed strictly-reducing target) and its CLOSE/CLOSE_TODAY legs, split
    from the real FIFO today/yesterday batches of the owned episode. The
    candidate binding must equal the context binding; KEEP never reaches here.
    """
    if exit_proposal.exit_binding_sha256 != exit_binding.binding_sha256:
        raise ValueError("exit_proposal_binding_mismatch")
    if exit_proposal.strategy_id != exit_binding.strategy_id:
        raise ValueError("exit_proposal_strategy_mismatch")
    if exit_proposal.observed_at_nano > entry_proposal.decision_nano:
        raise ValueError("exit_proposal_observed_after_decision")
    position = position_for(account, exit_proposal.contract_id)
    if position is None or position.strategy_id != exit_proposal.strategy_id \
            or position.episode_id != exit_proposal.episode_id:
        raise ValueError("exit_proposal_requires_owned_position")
    current = position.lots
    target = exit_proposal.target_lots
    if target == current:
        raise ValueError("exit_target_must_strictly_reduce")
    if target * current < 0 or abs(target) > abs(current):
        raise ValueError("exit_target_cannot_reverse_or_increase")

    kind = execution_kind_for(exit_specs, exit_proposal.triggering_policy_id)
    purposes = _PROTECTIVE_PURPOSE if kind == "LEGACY_PROTECTIVE" else _NEXT_WINDOW_PURPOSE
    purpose = purposes[exit_proposal.action]
    # Frozen protective/roll tokens override even a soft kind (audit P1:
    # only these tokens may carry STOP/TARGET/ROLL semantics).
    if _ROLL_TOKENS & set(exit_proposal.reasons):
        token = next(t for t in ("ROLL_EXIT", "STOP_TOUCHED", "TARGET_REACHED")
                     if t in exit_proposal.reasons)
        purpose = {"ROLL_EXIT": "ROLL_EXIT", "STOP_TOUCHED": "STOP",
                   "TARGET_REACHED": "TARGET"}[token]
    valid_until = max(exit_proposal.earliest_execution_nano + 1, entry_proposal.valid_until_nano)

    routed = replace(entry_proposal,
        proposal_id=stable_id("exit_routed", canonical_json_bytes(
            [exit_proposal.proposal_id, exit_proposal.input_sha256, target])),
        decision_nano=exit_proposal.observed_at_nano,
        action=exit_proposal.action, target_lots=target, layer_index=None,
        trigger_price=None, target_price=None, stop_price=None,
        earliest_execution_nano=exit_proposal.earliest_execution_nano,
        valid_until_nano=valid_until, forecast_id=None, budget=entry_proposal.budget,
        payoff=None, reasons=tuple(sorted(set(exit_proposal.reasons) | {f"EXIT_KIND:{kind}"})))

    delta = target - current
    today = sum(lot.lots for lot in account.lots
                if lot.contract_id == exit_proposal.contract_id
                and lot.episode_id == exit_proposal.episode_id
                and lot.opened_trading_day == account.trading_day)
    old = abs(current) - today
    quantity = abs(delta)
    sign = 1 if delta > 0 else -1
    old_close = min(quantity, old)
    pieces = tuple((offset, sign * q) for offset, q in
                   (("CLOSE", old_close), ("CLOSE_TODAY", quantity - old_close)) if q)
    intents = tuple(OrderIntent(
        intent_id=stable_id("intent", canonical_json_bytes(
            [exit_proposal.proposal_id, exit_proposal.input_sha256, offset, d])),
        decision_id=stable_id("exit_decision", canonical_json_bytes(
            [exit_proposal.proposal_id, exit_proposal.input_sha256])),
        strategy_id=exit_proposal.strategy_id, episode_id=exit_proposal.episode_id,
        contract_id=exit_proposal.contract_id, created_at_nano=exit_proposal.observed_at_nano,
        not_before_nano=exit_proposal.earliest_execution_nano,
        expires_at_nano=valid_until, delta_lots=d, offset=offset,
        purpose=purpose, layer_index=None, limit_price=None)
        for offset, d in pieces)
    return routed, intents


def exit_execution_links(exit_proposal: ExitProposal, routed: StrategyProposal,
                         intents: tuple[OrderIntent, ...],
                         exit_specs: tuple[ExitSpec, ...]) -> tuple[ExitExecutionLink, ...]:
    """Register the pre-commit foreign keys for every emitted exit intent (007)."""
    kind = execution_kind_for(exit_specs, exit_proposal.triggering_policy_id)
    decision_id = intents[0].decision_id if intents else stable_id(
        "exit_decision", canonical_json_bytes(
            [exit_proposal.proposal_id, exit_proposal.input_sha256]))
    return tuple(ExitExecutionLink(
        link_id=stable_id("exit_link", canonical_json_bytes(
            [intent.intent_id, exit_proposal.proposal_id, kind])),
        exit_proposal_id=exit_proposal.proposal_id,
        strategy_proposal_id=routed.proposal_id, decision_id=decision_id,
        intent_id=intent.intent_id, exit_binding_sha256=exit_proposal.exit_binding_sha256,
        action=exit_proposal.action, target_lots=exit_proposal.target_lots,
        requested_lots=abs(intent.delta_lots), execution_kind=kind)
        for intent in intents)


def registry_hash_for(specs: tuple[StrategySpec, ...]) -> str:
    if len({s.strategy_id for s in specs})!=len(specs):
        raise ValueError("duplicate_strategy_specs")
    return sha256_hex(canonical_json_bytes([encode_record(s) for s in sorted(specs,key=lambda s:s.strategy_id)]))


def exit_registry_hash_for(exit_specs: tuple[ExitSpec, ...]) -> str:
    """007 §5.4: independent hash over the sorted full ExitSpec array."""
    if len({s.exit_policy_id for s in exit_specs})!=len(exit_specs):
        raise ValueError("duplicate_exit_specs")
    return sha256_hex(canonical_json_bytes([encode_record(s) for s in
                                            sorted(exit_specs,key=lambda s:s.exit_policy_id)]))


def legacy_exit_spec(mode: str) -> ExitSpec:
    """Thin forward to the sole LEGACY ExitSpec producer, M3C exits.registry.

    Arbitration v2-arbitration-legacy-binding-convergence-20260917-r1
    (RULING 1.2): this entry point no longer constructs its own spec. It
    forwards without re-encoding, field substitution, re-hashing, priority
    changes or implicit legacy branches; 08 §5.4 "parameters exactly empty"
    and the explicit no-hidden-default rule (007 §5.4) now hold through
    the M3C producer itself.
    """
    from ak_trading.v2.exits.registry import legacy_exit_spec as m3c_legacy_exit_spec
    return m3c_legacy_exit_spec(mode)


def legacy_exit_binding(spec: StrategySpec) -> ExitPolicyBinding:
    """Thin forward to the sole legacy binding producer, M3B select_exit_binding.

    Arbitration RULING 1.1/1.2: the orthodox legacy binding is
    strategies.registry.select_exit_binding(spec, profile="legacy") with
    entry_spec_sha256 = spec.code_sha256. This wrapper forwards unchanged --
    no transformation before or after -- so existing M4 call sites keep
    working while construction authority stays with the producers.
    """
    from ak_trading.v2.strategies.registry import select_exit_binding
    return select_exit_binding(spec, profile="legacy")


_HEX = frozenset("0123456789abcdef")


def _is_content_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _HEX for char in value)


def model_hashes_for(forecast_models: ForecastModelSet, payoff_model: PathPayoffModel,
                     regime_reference: RegimeReference,
                     seasonality_model: SeasonalityModel | None) -> Mapping[str, str]:
    """Public dependency binding (004 §3 + Q1 arbitration).

    B = the forecast set itself declares a bound seasonality source;
    K = its feature_keys select SEASONAL columns. K without B is rejected;
    B requires a legal handle whose content hash equals the recorded source;
    B without K stays bound (transform existed, predictor skipped the column);
    no B requires an explicit None. Missing keys are never merged with illegal
    null/empty values via dict.get.
    """
    if not payoff_model.identity_matches():
        raise ValueError("payoff_model_identity_mismatch")
    sources = forecast_models.source_hashes
    bound = "seasonality" in sources
    uses_columns = any(key.startswith("SEASONAL:") for key in forecast_models.feature_keys)
    if uses_columns and not bound:
        raise ValueError("seasonality_source_missing_for_selected_columns")
    if bound:
        if seasonality_model is None:
            raise ValueError("bound_seasonality_requires_handle")
        recorded = sources["seasonality"]
        content = seasonality_model.content_hash()
        if not _is_content_hash(recorded) or recorded != content:
            raise ValueError("seasonality_source_hash_mismatch")
    elif seasonality_model is not None:
        raise ValueError("unbound_seasonality_handle_rejected")
    if "regime_reference" in sources and sources["regime_reference"] != regime_reference.content_hash():
        raise ValueError("regime_reference_source_hash_mismatch")
    if forecast_models.regime_reference_id != regime_reference.reference_id:
        raise ValueError("regime_reference_model_mismatch")
    hashes = {"forecast": forecast_models.content_hash(), "payoff": payoff_model.content_hash(),
              "regime_reference": regime_reference.content_hash()}
    if bound:
        hashes["seasonality"] = seasonality_model.content_hash()
    return MappingProxyType(hashes)


def _features(bundle, contract, now, duration, seasonal, background, sessions, *, canonical_daily=False):
    bars=visible_bars(bundle,contract,duration,now,duration!=86400)
    if canonical_daily:
        # DAILY ruling003: validate the full as-of source before publication
        # and consume M2's canonical tuple in both snapshot and context.
        # MINUTE and its DAY1 background keep the default path.
        bars=_deduplicate_visible(list(bars))
    rule=visible_rule(bundle,contract,now)
    specs=factor_registry_v2(duration)+baseline_registry_v2(duration)
    snapshot=build_feature_snapshot(contract,bars,now,specs,background,seasonal,rule,sessions)
    return snapshot,bars,rule


def _provided_forecast(provider, models, request, regime):
    forecast=provider(models,request,regime)
    f=request.features
    if (not isinstance(forecast,MarketForecast) or forecast.request_id!=request.request_id
            or forecast.snapshot_id!=f.snapshot_id or forecast.contract_id!=f.contract_id
            or forecast.decision_nano!=f.decision_nano or forecast.reference_price!=f.reference_price
            or forecast.atr_points!=f.atr_points or forecast.horizon!=request.horizon
            or forecast.window_start_nano!=request.window_start_nano or forecast.window_end_nano!=request.window_end_nano
            or forecast.frozen_range!=request.frozen_range or forecast.upper_distance!=request.upper_distance
            or forecast.lower_distance!=request.lower_distance or forecast.barrier_key!=request.barrier_key
            or forecast.assessment_id!=regime.assessment_id):
        raise ValueError("forecast_provider_request_identity_mismatch")
    return forecast


def _published_or_none(provider, models, request, regime):
    """Publish through the provider; an unpublishable model is a recorded
    missing publication for the original request (004 §4), never a fabricated
    forecast and never a crash of the whole decision host. Other provider or
    join failures still raise."""
    from ak_trading.v2.probability.forecast import UnpublishableForecastError
    try:
        return _provided_forecast(provider, models, request, regime)
    except UnpublishableForecastError:
        return None


def _daily_background_evidence(forecast, models, request, regime, family, now):
    """Use the existing confidence policy for the independent DAY20 cell.

    READY publication alone does not prove calibration support. Direction
    and ATR_1 path evidence must each have one frozen, mature, valid row;
    duplicate rows cannot be resolved by selecting the better statistic.
    """
    features=request.features
    policy=confidence_policy(family)
    if (forecast.model_id!=models.model_set_id
            or forecast.regime_reference_id!=models.regime_reference_id
            or forecast.product!=features.product or forecast.trading_day!=features.trading_day
            or forecast.session_id!=features.session_id or forecast.session_kind!="DAILY"
            or forecast.duration_seconds!=86400 or forecast.base_regime!=regime.base
            or forecast.label_version!=LABEL_VERSION):
        return False
    for event,barrier in (("DIRECTION","COMMON"),("PATH","ATR_1")):
        geometry=calibration_geometry_key(features,request.frozen_range,event)
        rows=[row for row in forecast.evidence if row.event==event
              and row.model_id==forecast.model_id and row.policy_id==policy.policy_id
              and row.product==features.product and row.session_kind=="DAILY"
              and row.duration_seconds==86400 and row.base_regime==regime.base
              and row.horizon==request.horizon and row.barrier_key==barrier
              and row.label_version==LABEL_VERSION]
        if len(rows)!=1:
            return False
        row=rows[0]
        if (row not in models.evidence or geometry is None or row.geometry_key!=geometry
                or row.bucket!="ALL" or row.status!="VALID"
                or row.sample_count<policy.minimum_samples or row.effective_blocks<policy.minimum_blocks
                or row.calibration_error is None or row.calibration_error>policy.maximum_calibration_error
                or not row.training_end_nano<row.calibration_end_nano<features.decision_nano
                or now>=row.valid_until_nano):
            return False
    return True


def _daily_background(bundle, contract, current, models, reference, family, sessions, provider, macro_version):
    """Original prior-day source, product trading-day age, then DAY20/ATR_1.

    A source that passes the age gate remains snapshot provenance even if
    its original forecast cannot be published. It never supplies direction
    through a DAY1/DAY5 fallback or through the current daily bar.
    """
    prior=[bar for bar in visible_bars(bundle,contract,86400,current.decision_nano,False)
           if bar.product==current.product and bar.trading_day is not None
           and bar.trading_day<current.trading_day]
    if not prior:
        return None,None
    day=max(bar.trading_day for bar in prior)
    sources=[bar for bar in prior if bar.trading_day==day]
    if len(sources)!=1:
        return None,None
    days=sorted({session.trading_day for session in sessions if session.product==current.product})
    if day not in days or current.trading_day not in days:
        return None,None
    age=days.index(current.trading_day)-days.index(day)
    if not 0<age<=research_policy()["daily_background_max_trading_days"]:
        return None,None
    source=sources[0]
    at=max(source.end_nano,source.available_at_nano)
    background,_,rule=_features(bundle,contract,at,86400,None,None,sessions,canonical_daily=True)
    if (background.contract_id!=current.contract_id or background.product!=current.product
            or background.trading_day!=day or background.available_at_nano>at):
        return None,None
    if (background.quality.status!="READY" or background.reference_price is None
            or background.atr_points is None or rule is None
            or not models.calibration_end_nano<at<models.valid_until_nano
            or at<reference.available_at_nano):
        return background,None
    macro=MacroContext(stable_id("price_only",str(at)),at,(),macro_version,(),
        "PRICE_ONLY",DataQuality("READY",(),"reconstructed",sha256_hex(b"PRICE_ONLY")))
    frozen=freeze_range(background)
    regime=assess_regime(background,macro,None,frozen,Decimal(0),reference,None)
    try:
        requests=build_forecast_requests(background,(Horizon("TRADING_DAY",20),),frozen,sessions)
        request=next(row for row in requests if row.barrier_key=="ATR_1")
        published=_published_or_none(provider,models,request,regime)
    except ClockError:
        return background,None
    except ValueError as exc:
        # This named provider join failure is missing background only in the
        # DAILY seam. Other provider failures and MINUTE joins still raise.
        if str(exc)!="forecast_provider_request_identity_mismatch":
            raise
        return background,None
    if (published is None or published.quality.status!="READY" or published.out_of_distribution
            or not published.published_at_nano<=current.decision_nano<published.expires_at_nano
            or published.published_at_nano>=published.window_start_nano
            or set(published.quality.reasons)&{"FORECAST_LATE","OOD","STALE","model_expired","out_of_distribution"}
            or not _daily_background_evidence(published,models,request,regime,family,current.decision_nano)):
        return background,None
    return background,published


def build_strategy_context(bundle: FrozenInput, contract_id: str, decision_nano: int,
                           account: AccountSnapshot, state: StrategyState | None,
                           forecast_models: ForecastModelSet, payoff_model: PathPayoffModel,
                           spec: StrategySpec, seasonality_model: SeasonalityModel | None,
                           regime_reference: RegimeReference, run_policy: RunPolicy,
                           qualifications: tuple[StrategyQualification, ...], *,
                           exit_binding: ExitPolicyBinding,
                           forecast_provider: Callable[[ForecastModelSet, ForecastRequest,
                                                        RegimeAssessment], MarketForecast]) -> StrategyContext:
    matches=[i for i in bundle.instruments if i.contract_id==contract_id]
    if len(matches)!=1:
        raise ValueError("contract_identity_missing_or_duplicate")
    instrument=matches[0]
    sessions=tuple(s for s in bundle.sessions if s.product==instrument.product)
    hashes=model_hashes_for(forecast_models,payoff_model,regime_reference,seasonality_model)
    if (forecast_models.calibration_end_nano>=decision_nano or payoff_model.training_end_nano>=decision_nano
            or regime_reference.available_at_nano>decision_nano):
        raise ValueError("model_not_available_at_decision")
    background=None
    background_forecast=None
    if spec.mode=="MINUTE":
        daily=visible_bars(bundle,contract_id,86400,decision_nano,True)
        if daily:
            # Publish background at its original complete/available boundary,
            # never synthesize a current-day final daily forecast.
            last=daily[-1]
            at=max(last.end_nano,last.available_at_nano)
            if at>forecast_models.calibration_end_nano and at>=regime_reference.available_at_nano:
                background,_,daily_rule=_features(bundle,contract_id,at,86400,None,None,sessions)
                bg_macro=MacroContext(stable_id("price_only",str(at)),at,(),run_policy.macro_policy_version,(),
                    "PRICE_ONLY",DataQuality("READY",(),"reconstructed",sha256_hex(b"PRICE_ONLY")))
                bg_range=freeze_range(background)
                bg_regime=assess_regime(background,bg_macro,None,bg_range,Decimal(0),regime_reference,None)
                if background.reference_price is not None and background.atr_points is not None and daily_rule is not None:
                    try:
                        requests=build_forecast_requests(background,(Horizon("TRADING_DAY",1),),bg_range,sessions)
                        published=_published_or_none(forecast_provider,forecast_models,next(r for r in requests if r.barrier_key=="ATR_1"),bg_regime)
                        # An unpublishable background is a recorded missing
                        # publication for that original request (004 §4); the
                        # host continues toward NO_TRADE and never crashes.
                        if published is not None and published.quality.status=="READY" \
                                and published.published_at_nano<=decision_nano<published.expires_at_nano:
                            background_forecast=published
                    except ClockError:
                        background_forecast=None
    elif spec.mode=="DAILY":
        current,_,_=_features(bundle,contract_id,decision_nano,86400,None,None,sessions,canonical_daily=True)
        background,background_forecast=_daily_background(bundle,contract_id,current,forecast_models,
            regime_reference,spec.family,sessions,forecast_provider,run_policy.macro_policy_version)
    # 005/Q1: daily snapshots never produce seasonal columns; a collection
    # bound to seasonality keeps that collection-level source for its daily
    # requests (validated above), so minute handles are passed only for minute.
    features,bars,rule=_features(bundle,contract_id,decision_nano,60 if spec.mode=="MINUTE" else 86400,
                                 seasonality_model if spec.mode=="MINUTE" else None,background,sessions,
                                 canonical_daily=spec.mode=="DAILY")
    if run_policy.macro_mode=="PRICE_ONLY":
        macro=MacroContext(stable_id("price_only",str(decision_nano)),decision_nano,(),run_policy.macro_policy_version,(),
            "PRICE_ONLY",DataQuality("READY",(),"reconstructed",sha256_hex(b"PRICE_ONLY")))
    else:
        macro=MacroCache(bundle.macro_items).snapshot(decision_nano,instrument.product,True,False)
    frozen=(state.budget.frozen_range if state is not None and state.budget and state.budget.frozen_range
            else state.setup.frozen_range if state is not None and state.setup and state.setup.frozen_range
            else freeze_range(features))
    costs_known=rule is not None and all(x is not None for x in (rule.open_fee,rule.close_fee,rule.close_today_fee)) \
        and features.reference_price is not None
    cost=Decimal(0)
    if costs_known:
        price=features.reference_price
        cost=(fee_amount(rule.open_fee,price,1,rule.multiplier)
              +max(fee_amount(leg,price,1,rule.multiplier) for leg in (rule.close_fee,rule.close_today_fee)))/rule.multiplier \
              +2*rule.price_tick*(rule.slippage_ticks+rule.impact_ticks)
    regime=assess_regime(features,macro,background_forecast,frozen,cost,regime_reference,
                         None if state is None else state.last_regime)
    if not costs_known:
        regime=replace(regime,flags=tuple(sorted(set(regime.flags)|{"ILLIQUID"})),
            reasons=tuple(sorted(set(regime.reasons)|{"COST_UNKNOWN"})))
    horizon=spec.max_holding
    if state is not None and state.episode_id is not None and state.active_exit_nano is not None:
        horizon=None
        values=(60,30,15,5,1) if spec.mode=="MINUTE" else (20,5,1)
        for value in values:
            candidate=Horizon(spec.max_holding.unit,value)
            try:
                _,end=horizon_window(decision_nano,instrument.product,candidate,sessions)
            except ClockError:
                continue
            if end<=state.active_exit_nano:
                horizon=candidate
                break
    forecasts=()
    if horizon is not None and features.reference_price is not None and features.atr_points is not None and rule is not None:
        try:
            requests=build_forecast_requests(features,(horizon,),frozen,sessions)
            forecasts=tuple(f for f in (_published_or_none(forecast_provider,forecast_models,r,regime)
                                         for r in requests) if f is not None)
        except ClockError:
            forecasts=()
    deadline=holding_deadline(decision_nano,instrument.product,spec.holding_policy,sessions)
    if spec.mode=="MINUTE" and spec.holding_policy.flatten_before_minutes:
        slots=sorted(t for s in sessions for t in range(s.start_nano,s.end_nano,60_000_000_000) if t<deadline)
        count=spec.holding_policy.flatten_before_minutes
        if len(slots)>=count:
            deadline=slots[-count]
    for cutoff in (instrument.delivery_cutoff_nano,instrument.last_trade_nano,instrument.expiry_nano):
        if cutoff is not None:
            deadline=min(deadline,cutoff)
    matching=[q for q in qualifications if q.strategy_id==spec.strategy_id and q.product==instrument.product]
    if len(matching)>1:
        raise ValueError("ambiguous_strategy_qualification")
    position=position_for(account,contract_id)
    if position is not None and state is not None and state.phase=="EXITING":
        position=replace(position,pending_exit=True)
    return StrategyContext(decision_nano,spec,exit_binding,features,forecasts,regime,macro,bars,sessions,account,
        position,rule,confidence_policy(spec.family),stress_policy(),run_policy,
        matching[0] if matching else None,hashes,deadline)
