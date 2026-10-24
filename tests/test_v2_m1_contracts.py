"""M1-01 contracts: records, mutual exclusions, immutability, codec roundtrips."""
from __future__ import annotations

import hashlib
import dataclasses
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import pytest

from ak_trading.v2 import CONTRACT_VERSION
from ak_trading.v2 import contracts as c
from ak_trading.v2 import schemas as s

H = 3_600_000_000_000
M = 60_000_000_000
ZERO_HASH = "0" * 64


def unano(day: int, hour: int, minute: int = 0) -> int:
    moment = datetime(2026, 1, day, hour, minute, tzinfo=timezone.utc) - timedelta(hours=8)
    delta = moment - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9


def quality(status: str = "READY") -> c.DataQuality:
    return c.DataQuality(status=status, reasons=(), temporal_basis="observed",
                         input_hash=hashlib.sha256(status.encode()).hexdigest())


def horizon(**overrides: Any) -> c.Horizon:
    values = {"unit": "TRADING_MINUTE", "value": 5}
    values.update(overrides)
    return c.Horizon(**values)


def frozen_range() -> c.FrozenRange:
    return c.FrozenRange(range_id="r1", frozen_at_nano=unano(9, 9),
                         lower=Decimal("100"), center=Decimal("103"), upper=Decimal("106"),
                         stop_lower=Decimal("97"), stop_upper=Decimal("109"),
                         source_snapshot_id="snap1")


def market_bar() -> c.MarketBar:
    return c.MarketBar(
        bar_id="bar-1", contract_id="SYN.rb2609", raw_symbol="SYN.rb2609", product="SYN.rb",
        duration_seconds=60, datetime_nano=unano(9, 1), start_nano=unano(9, 1),
        end_nano=unano(9, 1) + M, trading_day=date(2026, 1, 9), session_id="D09-0",
        available_at_nano=unano(9, 1) + 2 * M, imported_at_nano=unano(9, 1) + M,
        open=Decimal("100"), high=Decimal("101"), low=Decimal("99"), close=Decimal("100"),
        volume=Decimal("5"), open_oi=Decimal("20"), close_oi=Decimal("21"),
        complete=True, expected_minutes=1, actual_minutes=1, source="test", quality=quality())


def macro_item(**overrides: Any) -> c.MacroItem:
    values: dict[str, Any] = dict(
        item_id="mac-1", meeting_id=None, revision="initial", kind="FACT",
        derived_tag=None, derivation_version=None, published_at_nano=unano(9, 1),
        first_seen_nano=unano(9, 1, 4), retrieved_at_nano=unano(9, 1, 5),
        available_at_nano=unano(9, 1, 6), valid_from_nano=unano(9, 1),
        valid_until_nano=unano(9, 2), publication_state="published",
        degradation_state="normal", assets=(), products=(), unit=None,
        source_urls=("https://example.invalid/x",), text="美联储加息",
        parent_ids=(), raw_sha256=ZERO_HASH, quality=quality())
    values.update(overrides)
    return c.MacroItem(**values)


def fee_leg() -> c.FeeLeg:
    return c.FeeLeg(per_lot_cny=Decimal("0"), notional_rate=Decimal("0.0001"),
                    explicit_zero=False)


def trading_rule() -> c.TradingRule:
    return c.TradingRule(
        rule_id="rule-1", contract_id="SYN.rb2609", effective_from_nano=unano(5, 1),
        effective_until_nano=unano(16, 15), available_at_nano=unano(5, 1),
        multiplier=Decimal("10"), price_tick=Decimal("1"), lot_step=1, min_open_lots=1,
        open_fee=fee_leg(), close_fee=fee_leg(), close_today_fee=fee_leg(),
        long_margin_rate=Decimal("0.09"), short_margin_rate=Decimal("0.09"),
        limit_up=Decimal("4345"), limit_down=Decimal("3555"), slippage_ticks=Decimal("1"),
        impact_ticks=Decimal("0"), participation_rate=Decimal("0.1"),
        evidence="SCENARIO", source_sha256=ZERO_HASH)


def feature_snapshot() -> c.FeatureSnapshot:
    tick = c.FactorValue(factor_key="META:price_tick:v2.1.0", value=Decimal("1"),
                         available_at_nano=unano(9, 1, 5), quality=quality())
    return c.FeatureSnapshot(
        snapshot_id="snap-1", contract_id="SYN.rb2609", raw_symbol="SYN.rb2609",
        product="SYN.rb", trading_day=date(2026, 1, 9), session_id="D09-0", session_kind="DAY",
        decision_nano=unano(9, 1, 5), available_at_nano=unano(9, 1, 5), duration_seconds=60,
        source_bar_ids=("bar-1",), reference_price=Decimal("103"), atr_points=Decimal("2"),
        values=(tick,), daily_background_id=None, quality=quality())


def calibration_evidence(**overrides: Any) -> c.CalibrationEvidence:
    values: dict[str, Any] = dict(
        evidence_id="ev-1", model_id="model-1", policy_id="confidence-ordinary-v2.1.0",
        product="SYN.rb", session_kind="DAY", duration_seconds=60,
        base_regime="ORDERED_RANGE", barrier_key="COMMON",
        geometry_key="geometry-v2.1.1|DIRECTION|ANCHOR_ATR", event="DIRECTION",
        horizon=horizon(), label_version="label-v2.1.0", bucket="ALL", sample_count=250,
        effective_blocks=32, calibration_error=0.03, brier=0.19, training_end_nano=unano(6, 7),
        calibration_end_nano=unano(8, 7), valid_until_nano=unano(8, 7) + 30 * 24 * H,
        status="VALID")
    values.update(overrides)
    return c.CalibrationEvidence(**values)


def market_forecast() -> c.MarketForecast:
    return c.MarketForecast(
        forecast_id="fc-1", request_id="req-1", snapshot_id="snap-1",
        contract_id="SYN.rb2609", product="SYN.rb", trading_day=date(2026, 1, 9),
        session_id="D09-0", session_kind="DAY", duration_seconds=60,
        assessment_id="reg-1", base_regime="ORDERED_RANGE", regime_reference_id="rr-1",
        decision_nano=unano(9, 1, 5), published_at_nano=unano(9, 1, 5) + M // 2,
        expires_at_nano=unano(9, 1, 11), horizon=horizon(),
        window_start_nano=unano(9, 1, 6), window_end_nano=unano(9, 1, 11),
        barrier_key="ATR_1", reference_price=Decimal("103"), atr_points=Decimal("2"),
        upper_distance=Decimal("2"), lower_distance=Decimal("2"), frozen_range=frozen_range(),
        label_version="label-v2.1.0", model_id="model-1",
        direction_probabilities={"UP": 0.5, "DOWN": 0.3, "NEUTRAL": 0.2},
        return_quantiles={"Q05": -2/103, "Q25": -1/103, "Q50": 0.0, "Q75": 1/103, "Q95": 4/103},
        point_quantiles={"Q05": Decimal("-2"), "Q25": Decimal("-1"), "Q50": Decimal("0"),
                         "Q75": Decimal("1"), "Q95": Decimal("4")},
        atr_quantiles={"Q05": -1.0, "Q25": -0.5, "Q50": 0.0, "Q75": 0.5, "Q95": 2.0},
        path_probabilities={"UP_FIRST": 0.4, "DOWN_FIRST": 0.3, "NEITHER": 0.25, "AMBIGUOUS": 0.05},
        time_cdf=(c.TimeProbability(event="UP_TOUCH", step=1, probability=0.1,
                                    unknown_probability=0.0),),
        persistence_probabilities={"ORDERED_RANGE": 0.86},
        stop_touch_probabilities={"LONG": 0.08, "SHORT": 0.07},
        return_before_break_probability=0.5, evidence=(calibration_evidence(),),
        out_of_distribution=False, quality=quality())


def regime_assessment() -> c.RegimeAssessment:
    return c.RegimeAssessment(
        assessment_id="reg-1", snapshot_id="snap-1", reference_id="rr-1",
        decision_nano=unano(9, 1, 5), base="ORDERED_RANGE", flags=("PULLBACK",),
        frozen_range=frozen_range(), cross_timeframe="ALIGNED", confirmed_observations=3,
        reasons=(), quality=quality())


def strategy_spec() -> c.StrategySpec:
    return c.StrategySpec(
        strategy_id="strat-1", family="TREND", version="1.0.0", parameter_version="p1",
        code_sha256=ZERO_HASH, mode="MINUTE", parameters={"window": 20},
        required_factor_keys=(), allowed_regimes=("UP_TREND", "DOWN_TREND"),
        allowed_flags=("PULLBACK",), confidence_policy_id="confidence-ordinary-v2.1.0",
        path_barrier_key="ATR_1", max_holding=horizon(unit="TRADING_MINUTE", value=60),
        holding_policy=c.HoldingPolicy(True, True, False, 30, 5), max_layers=1,
        max_total_lots=1, validation_stage="RESEARCH")


def stress_policy() -> c.StressPolicy:
    return c.StressPolicy(policy_id="stress-v2.1.0", gap_atr_multiple=Decimal("3"),
                          locked_trading_days=3, adverse_atr_per_day=Decimal("1"),
                          release_extra_atr=Decimal("1"), fee_multiplier=Decimal("2"),
                          slippage_multiplier=Decimal("2"), margin_multiplier=Decimal("1.5"))


def run_policy() -> c.RunPolicy:
    return c.RunPolicy(purpose="RESEARCH_EVALUATION", routing_mode="BEST_EV", seed=7,
                       fixed_strategy_id=None, fixed_exit_binding_sha256=None,
                       research_policy_version="research-policy-v2.1.0",
                       macro_policy_version="macro-risk-v2.1.0", macro_mode="PRICE_ONLY",
                       cost_scenario="BASE")


def qualification(**overrides: Any) -> c.StrategyQualification:
    values: dict[str, Any] = dict(
        qualification_id="qual-1", strategy_id="strat-1", strategy_version="1.0.0",
        parameter_version="p1", code_sha256=ZERO_HASH,
        exit_registry_sha256=ZERO_HASH,
        exit_binding_sha256=exit_binding().binding_sha256,
        exit_spec_sha256s=(ZERO_HASH,), product="SYN.rb",
        contract_ids=("SYN.rb2609",), mode="MINUTE",
        horizons=(horizon(unit="TRADING_MINUTE", value=60),), macro_mode="PRICE_ONLY",
        research_policy_version="research-policy-v2.1.0",
        macro_policy_version="macro-risk-v2.1.0",
        cost_scenarios=("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5", "GAP", "NO_STOP"),
        model_hashes={"forecast": ZERO_HASH, "payoff": ZERO_HASH, "regime_reference": ZERO_HASH},
        experiment_sha256=ZERO_HASH, statistics="PASS", report_sha256=ZERO_HASH,
        review_sha256=ZERO_HASH, issued_at_nano=unano(9, 1),
        valid_until_nano=unano(9, 1) + 30 * 24 * H, reasons=())
    values.update(overrides)
    return c.StrategyQualification(**values)


def round_budget() -> c.RoundBudget:
    return c.RoundBudget(
        budget_id="bud-1", episode_id="ep-1", contract_id="SYN.rb2609",
        frozen_at_nano=unano(9, 1), range_id="r1", frozen_range=frozen_range(),
        stress_policy=stress_policy(), initial_atr_points=Decimal("2"), side=1,
        layers=(c.LayerSpec(index=0, trigger_price=Decimal("100.5"),
                            reference_entry_price=Decimal("100"), lots=1),),
        stop_price=Decimal("96"), target_price=Decimal("106"), latest_exit_nano=unano(9, 6),
        maximum_lots=1, planned_loss_cny=Decimal("50"), stress_loss_cny=Decimal("152.3744"),
        reserved_margin_cny=Decimal("1000"), day_loss_limit_cny=Decimal("20000"),
        equity_at_start_cny=Decimal("1000000"))


def account_snapshot() -> c.AccountSnapshot:
    return c.AccountSnapshot(
        account_id="acc-1", as_of_nano=unano(9, 1, 5), trading_day=date(2026, 1, 9),
        initial_cash_cny=Decimal("1000000"), cash_cny=Decimal("999990"),
        realized_gross_cny=Decimal("10"), fees_cny=Decimal("20"),
        slippage_cny=Decimal("5"), unrealized_cny=Decimal("-40"),
        equity_cny=Decimal("999950"), margin_cny=Decimal("1000"),
        reserved_margin_cny=Decimal("1000"), day_start_equity_cny=Decimal("1000000"),
        day_net_cny=Decimal("-50"), peak_equity_cny=Decimal("1000000"), lots=(), killed=False,
        quality=quality())


def strategy_state(**overrides: Any) -> c.StrategyState:
    values: dict[str, Any] = dict(
        strategy_id="strat-1", episode_id=None, contract_id="SYN.rb2609", phase="OBSERVE",
        setup=None, last_regime=None, direction=0, filled_layers=(), layer_filled_lots=(),
        pending_layer_index=None, active_exit_nano=None, pending_exit_nano=None,
        pending_intent_ids=(), budget=None, confirmation_count=0, cooldown_until_nano=None,
        last_event_id=None)
    values.update(overrides)
    return c.StrategyState(**values)


def strategy_context() -> c.StrategyContext:
    return c.StrategyContext(
        now_nano=unano(9, 1, 5), spec=strategy_spec(), exit_binding=exit_binding(),
        features=feature_snapshot(),
        forecasts=(market_forecast(),), regime=regime_assessment(),
        macro=c.MacroContext(context_id="ctx-1", decision_nano=unano(9, 1, 5), items=(),
                             policy_version="macro-risk-v2.1.0", risk_tags=(),
                             mode="PRICE_ONLY", quality=quality()),
        bars=(market_bar(),), sessions=(), account=account_snapshot(), position=None,
        rule=trading_rule(),
        confidence_policy=c.ConfidencePolicy(
            policy_id="confidence-ordinary-v2.1.0", minimum_persistence=0.70,
            maximum_stop_touch=None, minimum_samples=200, minimum_blocks=30,
            maximum_calibration_error=0.05, maximum_ambiguity=0.05,
            confirmation_observations=3, exit_persistence=0.65, cooldown_minutes=30,
            minimum_conservative_ev_cny=Decimal("0")),
        stress_policy=stress_policy(), run_policy=run_policy(), qualification=None,
        model_hashes={"forecast": ZERO_HASH}, holding_deadline_nano=unano(9, 7))


def payoff_estimate() -> c.PayoffEstimate:
    return c.PayoffEstimate(
        estimate_id="pay-1", proposal_id="prop-1", model_id="model-1", horizon=horizon(),
        barrier_key="ATR_1", geometry_key="geometry-v2.1.1|PATH|ANCHOR_ATR",
        opportunity_set_id="opp-1", exit_binding_sha256=exit_binding().binding_sha256,
        exit_state_condition="OPEN_INITIAL", training_end_nano=unano(8, 7), sample_count=200,
        effective_blocks=30, expected_net_cny=Decimal("120"),
        conservative_ev_cny=Decimal("10"), net_profit_probability=0.55,
        loss_cvar95_cny=Decimal("80"), peak_margin_cny=Decimal("1000"),
        return_before_stop_probability=0.5, missing_probability_mass=0.0,
        status="READY", reasons=())


def strategy_proposal() -> c.StrategyProposal:
    return c.StrategyProposal(
        proposal_id="prop-1", strategy_id="strat-1", episode_id="ep-1",
        contract_id="SYN.rb2609", decision_nano=unano(9, 1, 5), action="OPEN",
        target_lots=1, layer_index=0, trigger_price=Decimal("100.5"),
        target_price=Decimal("106"), stop_price=Decimal("96"),
        earliest_execution_nano=unano(9, 1, 6), valid_until_nano=unano(9, 2),
        forecast_id="fc-1", assessment_id="reg-1", budget=round_budget(),
        payoff=payoff_estimate(),
        next_state=strategy_state(episode_id="ep-1", phase="WAIT_TRIGGER", direction=1,
                                  budget=round_budget(), layer_filled_lots=(0,)),
        reasons=())


def order_intent() -> c.OrderIntent:
    return c.OrderIntent(
        intent_id="int-1", decision_id="dec-1", strategy_id="strat-1", episode_id="ep-1",
        contract_id="SYN.rb2609", created_at_nano=unano(9, 1, 5),
        not_before_nano=unano(9, 1, 6), expires_at_nano=unano(9, 2), delta_lots=1,
        offset="OPEN", purpose="ENTRY", layer_index=0, limit_price=Decimal("100.5"))


def fill() -> c.Fill:
    return c.Fill(
        fill_id="fill-1", intent_id="int-1", contract_id="SYN.rb2609", strategy_id="strat-1",
        episode_id="ep-1", execution_nano=unano(9, 1, 6), trading_day=date(2026, 1, 9),
        side=1, lots=1, offset="OPEN", reference_price=Decimal("100.5"),
        execution_price=Decimal("100.4"), fee_cny=Decimal("0.1"),
        slippage_cny=Decimal("0.1"), rule_id="rule-1", evidence="SCENARIO")


def exit_binding() -> c.ExitPolicyBinding:
    values = dict(
        strategy_id="strat-1", entry_spec_sha256=ZERO_HASH,
        exit_spec_sha256s=(ZERO_HASH,), sizing_spec_sha256=None,
        priority=("exit-legacy-m1",), legacy_rules_version="1.0.0")
    return c.ExitPolicyBinding(
        binding_sha256=c.exit_binding_content_hash(**values), **values)


def exit_state(**overrides: Any) -> c.ExitState:
    values: dict[str, Any] = dict(
        account_id="acc-1", contract_id="SYN.rb2609", strategy_id="strat-1",
        episode_id="ep-1", first_fill_nano=unano(9, 1, 6),
        exit_binding_sha256=exit_binding().binding_sha256,
        realized_gross_cny=Decimal("0"), fees_paid_cny=Decimal("0"),
        price_peak=None, price_peak_source_nano=None, price_peak_source_hash=None,
        net_peak_r=None, net_peak_source_nano=None, net_peak_source_hash=None,
        armed=False, protection_stop_price=None, protection_line_version=None,
        protection_effective_from_nano=None, confirmations=(), no_more_add=False,
        qbase=None, closed_lots=0, ladder_progress=(), phase="MANAGING",
        last_committed_event_id=None, state_prefix_hash=ZERO_HASH)
    values.update(overrides)
    return c.ExitState(**values)


def exit_spec_fixture(**overrides: Any) -> c.ExitSpec:
    values: dict[str, Any] = dict(
        exit_policy_id="exit_trailing_m1_v1", family="TRAILING_STOP", mode="MINUTE",
        version="1.0.0", parameter_version="exit_trailing_m1_v1:params-1",
        code_sha256=ZERO_HASH,
        parameters={"arm_profit_r": "0.5", "trail_atr_multiple": "1",
                    "atr_basis": "FROZEN_ATR", "peak_source": "COMPLETED_CLOSE"},
        required_market_keys=("BASE:close:v2.1.0:d60",),
        required_state_keys=("EXITSTATE:peak_favorable_price:v1.0.0:d60",),
        applicable_entry_families=("TREND",), observation_duration_seconds=60,
        execution_timing="NEXT_LEGAL_WINDOW", validation_stage="RESEARCH")
    values.update(overrides)
    return c.ExitSpec(**values)


def exit_feature_snapshot() -> c.ExitFeatureSnapshot:
    value = c.ExitFeatureValue(
        feature_key="EXITSTATE:peak_favorable_price:v1.0.0:d60", value=Decimal("105"),
        observed_at_nano=unano(9, 1, 5), available_at_nano=unano(9, 1, 5),
        quality=quality(), source_hashes={"snap": ZERO_HASH})
    return c.ExitFeatureSnapshot(
        snapshot_id="exit-snap-1", account_id="acc-1", contract_id="SYN.rb2609",
        strategy_id="strat-1", episode_id="ep-1",
        exit_binding_sha256=exit_binding().binding_sha256, duration_seconds=60,
        observed_at_nano=unano(9, 1, 5), available_at_nano=unano(9, 1, 5),
        values=(value,), source_state_sha256=ZERO_HASH)


def exit_context() -> c.ExitContext:
    return c.ExitContext(
        now_nano=unano(9, 1, 5), account_id="acc-1", contract_id="SYN.rb2609",
        strategy_id="strat-1", episode_id="ep-1", exit_binding=exit_binding(),
        exit_specs=(exit_spec_fixture(),), market_features=(exit_feature_snapshot(),),
        forecasts=(market_forecast(),), entry_spec=strategy_spec(),
        entry_state=strategy_state(episode_id="ep-1", phase="WAIT_TRIGGER",
                                   direction=1, budget=round_budget(),
                                   layer_filled_lots=(0,)),
        budget=round_budget(), account=account_snapshot(), lots=(),
        marks=(), episode_fills=(), rules=(), sessions=(),
        previous_exit_state=None, continuation=None)


def all_instances() -> dict[str, Any]:
    lot = c.PositionLot(lot_id="lot-1", contract_id="SYN.rb2609", strategy_id="strat-1",
                        episode_id="ep-1", side=1, lots=1, opened_at_nano=unano(9, 1, 6),
                        opened_trading_day=date(2026, 1, 9), entry_price=Decimal("100.4"),
                        opening_fee_cny=Decimal("0.1"))
    return {
        "Horizon": horizon(),
        "DataQuality": quality(),
        "Instrument": c.Instrument(
            contract_id="SYN.rb2609", raw_symbol="SYN.rb2609", product="SYN.rb",
            sector="synthetic", risk_groups=("SYN",), listing_nano=unano(5, 1),
            expiry_nano=None, last_trade_nano=None, delivery_cutoff_nano=None,
            quality=quality()),
        "SessionWindow": c.SessionWindow(
            session_id="D09-0", product="SYN.rb", trading_day=date(2026, 1, 9),
            start_nano=unano(9, 1), end_nano=unano(9, 1) + 75 * M,
            kind="DAY", calendar_version="synthetic-v1", evidence_status="SCENARIO"),
        "MarketBar": market_bar(),
        "MacroItem": macro_item(),
        "MacroContext": c.MacroContext(
            context_id="ctx-1", decision_nano=unano(9, 1, 5), items=(macro_item(),),
            policy_version="macro-risk-v2.1.0", risk_tags=(), mode="WITH_MACRO",
            quality=quality()),
        "FeeLeg": fee_leg(),
        "TradingRule": trading_rule(),
        "FrozenInput": c.FrozenInput(
            root=__import__("pathlib").Path("x"), manifest_sha256=ZERO_HASH,
            instruments=(), sessions=(), bars=(), macro_items=(), rules=()),
        "FactorSpec": c.FactorSpec(
            factor_key="MOM010:v2.1.0:d60:m1-n60", source_factor_id="MOM010",
            operator_version="op-v1", parameter_version="m1-n60", duration_seconds=60,
            lookback=60, parameters={"n": 60}, unit="ratio",
            required_fields=("close",), formula="sum(r,n)", source_sha256=ZERO_HASH),
        "FactorValue": c.FactorValue(factor_key="MOM010:v2.1.0:d60:m1-n60",
                                     value=Decimal("1.5"), available_at_nano=unano(9, 1, 5),
                                     quality=quality()),
        "FeatureSnapshot": feature_snapshot(),
        "FrozenRange": frozen_range(),
        "ForecastRequest": c.ForecastRequest(
            request_id="req-1", features=feature_snapshot(), horizon=horizon(),
            window_start_nano=unano(9, 1, 6), window_end_nano=unano(9, 1, 11),
            barrier_key="ATR_1", upper_distance=Decimal("2"), lower_distance=Decimal("2"),
            frozen_range=frozen_range()),
        "PathLabel": c.PathLabel(
            request_id="req-1", contract_id="SYN.rb2609", label_version="label-v2.1.0",
            horizon=horizon(), window_start_nano=unano(9, 1, 6),
            window_end_nano=unano(9, 1, 11), available_at_nano=unano(9, 1, 11),
            status="MATURE", reasons=(), direction="UP", terminal_return=0.01,
            terminal_points=Decimal("1"), terminal_atr=0.5, path_outcome="UP_FIRST",
            first_up_step=3, first_down_step=None, return_step=None, break_step=None,
            competing_outcome="RETURN_FIRST",
            persistence={event: True for event in c.PERSISTENCE_EVENTS},
            stop_touched={"LONG": False, "SHORT": False}, range_id="r1"),
        "CalibrationEvidence": calibration_evidence(),
        "TimeProbability": c.TimeProbability(event="UP_TOUCH", step=1, probability=0.1,
                                             unknown_probability=0.05),
        "MarketForecast": market_forecast(),
        "RegimeAssessment": regime_assessment(),
        "HoldingPolicy": c.HoldingPolicy(True, True, False, 30, 5),
        "ConfidencePolicy": c.ConfidencePolicy(
            policy_id="confidence-ordinary-v2.1.0", minimum_persistence=0.70,
            maximum_stop_touch=None, minimum_samples=200, minimum_blocks=30,
            maximum_calibration_error=0.05, maximum_ambiguity=0.05,
            confirmation_observations=3, exit_persistence=0.65, cooldown_minutes=30,
            minimum_conservative_ev_cny=Decimal("0")),
        "StressPolicy": stress_policy(),
        "RunPolicy": run_policy(),
        "StrategyQualification": qualification(),
        "StrategySpec": strategy_spec(),
        "LayerSpec": c.LayerSpec(index=0, trigger_price=Decimal("100.5"),
                                 reference_entry_price=Decimal("100"), lots=1),
        "RoundBudget": round_budget(),
        "PositionLot": lot,
        "PositionSnapshot": c.PositionSnapshot(
            contract_id="SYN.rb2609", strategy_id="strat-1", episode_id="ep-1", lots=1,
            average_entry_price=Decimal("100.4"), pending_exit=False),
        "AccountSnapshot": account_snapshot(),
        "StrategySetup": c.StrategySetup(
            setup_id="setup-1", strategy_id="strat-1", contract_id="SYN.rb2609", side=1,
            started_at_nano=unano(9, 1), expires_at_nano=unano(9, 7), observed_bars=30,
            frozen_range=frozen_range(), frozen_ma=Decimal("103"), frozen_atr=Decimal("2"),
            trigger_level=Decimal("105.5"), armed=False, consecutive_confirmations=1,
            previous_close=Decimal("103"), last_bar_id="bar-1"),
        "StrategyState": strategy_state(),
        "StrategyContext": strategy_context(),
        "PayoffEstimate": payoff_estimate(),
        "StrategyProposal": strategy_proposal(),
        "GateDecision": c.GateDecision(
            proposal_id="prop-1", eligible=True, reasons=(), evidence_ids=("ev-1",),
            checked_at_nano=unano(9, 1, 5)),
        "DecisionRecord": c.DecisionRecord(
            decision_id="dec-1", opportunity_set_id="opp-1", decision_nano=unano(9, 1, 5),
            policy_version="research-policy-v2.1.0", run_policy_sha256=ZERO_HASH,
            purpose="RESEARCH_EVALUATION", qualification_ids=("qual-1",), mode="BEST_EV",
            proposals=(strategy_proposal(),), gates=(), weights={"prop-1": 1.0}, seed=7,
            random_u=0.42, selected_proposal_id="prop-1", outcome="SELECTED", reasons=(),
            account_before_hash=ZERO_HASH),
        "OrderIntent": order_intent(),
        "Fill": fill(),
        "ExecutionResult": c.ExecutionResult(
            intent_id="int-1", fills=(fill(),), remaining_lots=0, status="FILLED", reasons=()),
        "LedgerCheckpoint": c.LedgerCheckpoint(
            sequence=1, contract_lock_sha256=ZERO_HASH, registry_sha256=ZERO_HASH,
            exit_registry_sha256=ZERO_HASH,
            run_policy=run_policy(), rule_hashes={"rule-1": ZERO_HASH},
            model_hashes={"forecast": ZERO_HASH, "payoff": ZERO_HASH,
                          "regime_reference": ZERO_HASH},
            qualification_hashes={"qual-1": ZERO_HASH},
            account=account_snapshot(), valuation_marks=(),
            states=(strategy_state(episode_id="ep-1", phase="WAIT_TRIGGER",
                                   direction=1, budget=round_budget(),
                                   layer_filled_lots=(0,),
                                   pending_intent_ids=("int-1",)),),
            exit_bindings=(exit_binding(),), exit_states=(), exit_execution_links=(),
            pending_intents=(order_intent(),), pending_remaining_lots={"int-1": 1},
            processed_event_ids=("evt-1",), processed_fill_hashes={},
            capacity_used={"SYN.rb2609": 1}, previous_hash=ZERO_HASH,
            checkpoint_hash=ZERO_HASH),
        "ValuationMark": c.ValuationMark(
            contract_id="SYN.rb2609", price=Decimal("103"), as_of_nano=unano(9, 1, 5),
            valued_at_nano=unano(9, 1, 5), source_bar_id="bar-1",
            source_bar_sha256=ZERO_HASH, rule_id="rule-1", status="CURRENT",
            reasons=()),
        "ContinuationEvidence": c.ContinuationEvidence(
            evidence_sha256=ZERO_HASH, version="1.0.0", kind="P4_RESERVED"),
        "ExitSpec": exit_spec_fixture(),
        "ExitFeatureSpec": c.ExitFeatureSpec(
            feature_key="EXITSTATE:peak_favorable_price:v1.0.0:d60",
            formula_version="v1.0.0", unit="price_points", window_kind="EPISODE_PREFIX",
            minimum_source_facts=1, source_scope="EPISODE", clock_unit="NONE",
            required_source_keys=("BASE:close:v2.1.0:d60",)),
        "ExitFeatureValue": c.ExitFeatureValue(
            feature_key="EXITSTATE:peak_favorable_price:v1.0.0:d60",
            value=Decimal("105"), observed_at_nano=unano(9, 1, 5),
            available_at_nano=unano(9, 1, 5), quality=quality(),
            source_hashes={"snap": ZERO_HASH}),
        "ExitFeatureSnapshot": exit_feature_snapshot(),
        "ExitPolicyBinding": exit_binding(),
        "ExitConfirmation": c.ExitConfirmation(
            exit_policy_id="exit_signal_reversal_m1_v1", rule_id="trigger", count=0,
            last_observation_id=None),
        "ExitProtectionRecord": c.ExitProtectionRecord(
            policy_id="exit_trailing_m1_v1", stop_price=Decimal("98"),
            line_version="line-1", effective_from_nano=unano(9, 1, 5)),
        "ExitLadderStep": c.ExitLadderStep(
            level_index=0, triggered=True, requested_lots=2, completed_lots=2),
        "ExitState": exit_state(),
        "ExitProposal": c.ExitProposal(
            proposal_id="exit-prop-1", strategy_id="strat-1", episode_id="ep-1",
            contract_id="SYN.rb2609", exit_binding_sha256=exit_binding().binding_sha256,
            action="KEEP", target_lots=1, triggering_policy_id="exit_trailing_m1_v1",
            input_sha256=ZERO_HASH, observed_at_nano=unano(9, 1, 5),
            earliest_execution_nano=unano(9, 1, 5), protection=None,
            next_state=exit_state(), reasons=()),
        "ExitContext": exit_context(),
        "ExitExecutionLink": c.ExitExecutionLink(
            link_id="exit-link-1", exit_proposal_id="exit-prop-1",
            strategy_proposal_id="prop-1", decision_id="dec-1", intent_id="int-1",
            exit_binding_sha256=exit_binding().binding_sha256, action="REDUCE",
            target_lots=1, requested_lots=1, execution_kind="NEXT_WINDOW_CLOSE"),
    }


def test_records_are_frozen_and_defensively_copied() -> None:
    record = macro_item()
    with pytest.raises(FrozenInstanceError):
        record.text = "mutated"  # type: ignore[misc]
    source = {"UP": 0.5, "DOWN": 0.3, "NEUTRAL": 0.2}
    forecast = market_forecast()
    assert isinstance(forecast.direction_probabilities, MappingProxyType)
    with pytest.raises(TypeError):
        forecast.direction_probabilities["UP"] = 0.9  # type: ignore[index]
    parameters = {"window": 20}
    spec = strategy_spec()
    parameters["window"] = 999
    assert spec.parameters["window"] == 20
    with pytest.raises(TypeError):
        spec.parameters["window"] = 5  # type: ignore[index]
    budget = round_budget()
    assert budget.frozen_range is not None
    with pytest.raises(FrozenInstanceError):
        budget.frozen_range.center = Decimal("1")  # type: ignore[misc]
    state = strategy_state(episode_id="ep-1", phase="WAIT_TRIGGER", direction=1,
                           budget=round_budget(), layer_filled_lots=(0,))
    with pytest.raises(FrozenInstanceError):
        state.budget.stop_price = Decimal("1")  # type: ignore[misc]


def test_encode_decode_roundtrip_all_registered_records() -> None:
    instances = all_instances()
    assert set(instances) == set(s.RECORD_TYPES), (
        set(s.RECORD_TYPES) ^ set(instances))
    for kind, record in instances.items():
        payload = s.encode_record(record)
        assert set(payload) == {
            field.name for field in __import__("dataclasses").fields(type(record))}
        decoded = s.decode_record(type(record), payload)
        assert decoded == record, kind
        s.validate_record(kind, payload)
        text = s.canonical_json_bytes(payload)
        reparsed = s.parse_json_bytes(text)
        assert s.decode_record(type(record), reparsed) == record, kind
    csv_bytes = s.encode_csv_bytes(c.MarketBar, [market_bar()])
    assert s.decode_csv_bytes(csv_bytes, c.MarketBar) == (market_bar(),)


def test_field_constraints_and_enum_rejections() -> None:
    with pytest.raises(ValueError):
        horizon(value=3)
    with pytest.raises(TypeError):
        horizon(value=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        horizon(unit="TRADING_DAY", value=15)
    with pytest.raises(ValueError):
        c.FrozenRange(range_id="r", frozen_at_nano=1, lower=Decimal("100"),
                      center=Decimal("99"), upper=Decimal("106"), stop_lower=Decimal("97"),
                      stop_upper=Decimal("109"), source_snapshot_id="s")
    forecast_kwargs = {
        "forecast_id": "fc", "request_id": "req", "snapshot_id": "snap",
        "contract_id": "C", "product": "P", "trading_day": date(2026, 1, 9),
        "session_id": "D09-0", "session_kind": "DAY", "duration_seconds": 60,
        "assessment_id": "a", "base_regime": "ORDERED_RANGE", "regime_reference_id": "rr",
        "decision_nano": 10, "published_at_nano": 20, "expires_at_nano": 50,
        "horizon": horizon(), "window_start_nano": 40, "window_end_nano": 50,
        "barrier_key": "ATR_1", "reference_price": Decimal("100"),
        "atr_points": Decimal("2"), "upper_distance": Decimal("2"),
        "lower_distance": Decimal("2"), "frozen_range": None,
        "label_version": "l", "model_id": "m",
        "direction_probabilities": {"UP": 0.5, "DOWN": 0.3, "NEUTRAL": 0.2},
        "return_quantiles": {"Q05": -0.02, "Q25": -0.01, "Q50": 0.0, "Q75": 0.01,
                             "Q95": 0.04},
        "point_quantiles": {"Q05": Decimal("-2"), "Q25": Decimal("-1"),
                            "Q50": Decimal("0"), "Q75": Decimal("1"),
                            "Q95": Decimal("4")},
        "atr_quantiles": {"Q05": -1.0, "Q25": -0.5, "Q50": 0.0, "Q75": 0.5, "Q95": 2.0},
        "path_probabilities": {"UP_FIRST": 0.4, "DOWN_FIRST": 0.3, "NEITHER": 0.25,
                               "AMBIGUOUS": 0.05},
        "time_cdf": (), "persistence_probabilities": {}, "stop_touch_probabilities": {},
        "return_before_break_probability": None, "evidence": (),
        "out_of_distribution": False, "quality": quality()}
    with pytest.raises(ValueError):
        bad = dict(forecast_kwargs)
        bad["direction_probabilities"] = {"UP": 0.5, "DOWN": 0.4, "NEUTRAL": 0.2}
        c.MarketForecast(**bad)
    with pytest.raises(ValueError):
        bad = dict(forecast_kwargs)
        bad["point_quantiles"] = {"Q05": Decimal("103"), "Q25": Decimal("102"),
                                  "Q50": Decimal("103"), "Q75": Decimal("104"),
                                  "Q95": Decimal("106")}
        c.MarketForecast(**bad)
    with pytest.raises(ValueError):
        c.TimeProbability(event="UP_TOUCH", step=1, probability=0.8,
                          unknown_probability=0.5)
    with pytest.raises(ValueError):
        c.TimeProbability(event="NOT_AN_EVENT", step=1, probability=0.1,
                          unknown_probability=0.0)
    with pytest.raises(ValueError):
        c.DataQuality(status="READY", reasons=(), temporal_basis="observed",
                      input_hash="NOT HEX")
    with pytest.raises(ValueError):
        c.FeeLeg(per_lot_cny=Decimal("0"), notional_rate=Decimal("0"), explicit_zero=False)
    with pytest.raises(ValueError):
        c.RunPolicy(purpose="RESEARCH_EVALUATION", routing_mode="FIXED", seed=1,
                    fixed_strategy_id=None, fixed_exit_binding_sha256=ZERO_HASH,
                    research_policy_version="r",
                    macro_policy_version="m", macro_mode="PRICE_ONLY",
                    cost_scenario="BASE")
    with pytest.raises(ValueError):
        spec_kwargs = dict(strategy_id="s", family="TREND", version="1",
                           parameter_version="p", code_sha256=ZERO_HASH, mode="MINUTE",
                           parameters={}, required_factor_keys=(),
                           allowed_regimes=(), allowed_flags=(),
                           confidence_policy_id="c", path_barrier_key="ATR_0.5",
                           max_holding=horizon(unit="TRADING_MINUTE", value=60),
                           holding_policy=c.HoldingPolicy(True, True, False, 30, 5),
                           max_layers=1, max_total_lots=1, validation_stage="RESEARCH")
        c.StrategySpec(**spec_kwargs)
    with pytest.raises(ValueError):
        qualification(cost_scenarios=("BASE", "GAP"))
    with pytest.raises(ValueError):
        qualification(valid_until_nano=unano(9, 1))
    with pytest.raises(ValueError):
        broken_account = dict(
            account_id="acc-1", as_of_nano=unano(9, 1, 5), trading_day=date(2026, 1, 9),
            initial_cash_cny=Decimal("1000000"), cash_cny=Decimal("1"),
            realized_gross_cny=Decimal("10"), fees_cny=Decimal("20"),
            slippage_cny=Decimal("5"), unrealized_cny=Decimal("-40"),
            equity_cny=Decimal("999950"), margin_cny=Decimal("1000"),
            reserved_margin_cny=Decimal("1000"), day_start_equity_cny=Decimal("1000000"),
            day_net_cny=Decimal("-50"), peak_equity_cny=Decimal("1000000"), lots=(),
            killed=False, quality=quality())
        c.AccountSnapshot(**broken_account)
    with pytest.raises(ValueError):
        c.PathLabel(request_id="r", contract_id="C", label_version="l", horizon=horizon(),
                    window_start_nano=1, window_end_nano=2, available_at_nano=None,
                    status="MATURE", reasons=(), direction=None, terminal_return=None,
                    terminal_points=None, terminal_atr=None, path_outcome=None,
                    first_up_step=None, first_down_step=None, return_step=None,
                    break_step=None, competing_outcome=None,
                    persistence={"UP_TREND": True}, stop_touched={}, range_id=None)
    with pytest.raises(ValueError):
        strategy_state(episode_id=None, budget=round_budget())
    with pytest.raises(ValueError):
        strategy_state(episode_id="ep-1", phase="WAIT_TRIGGER", direction=1,
                       budget=round_budget(), layer_filled_lots=(0,),
                       pending_layer_index=0, active_exit_nano=None)
    with pytest.raises(ValueError):
        c.LayerSpec(index=0, trigger_price=Decimal("1"),
                    reference_entry_price=Decimal("0"), lots=1)
    with pytest.raises(ValueError):
        c.StrategyContext(
            now_nano=unano(9, 1, 5), spec=strategy_spec(),
            exit_binding=exit_binding(), features=feature_snapshot(),
            forecasts=(), regime=regime_assessment(),
            macro=c.MacroContext(context_id="ctx", decision_nano=1, items=(),
                                 policy_version="m", risk_tags=(), mode="PRICE_ONLY",
                                 quality=quality()),
            bars=(), sessions=(), account=account_snapshot(), position=None, rule=None,
            confidence_policy=c.ConfidencePolicy(
                policy_id="confidence-martingale-v2.1.0", minimum_persistence=0.85,
                maximum_stop_touch=0.10, minimum_samples=500, minimum_blocks=60,
                maximum_calibration_error=0.03, maximum_ambiguity=0.02,
                confirmation_observations=3, exit_persistence=0.80, cooldown_minutes=30,
                minimum_conservative_ev_cny=Decimal("0")),
            stress_policy=stress_policy(), run_policy=run_policy(),
            qualification=qualification(), model_hashes={"forecast": ZERO_HASH},
            holding_deadline_nano=unano(9, 7))


def test_forecast_late_records_kept_as_invalid() -> None:
    """变更申请-002 §4: strict publication/expiry branches and ns boundaries."""
    late_quality = c.DataQuality(status="INVALID", reasons=("FORECAST_LATE",),
                                 temporal_basis="observed", input_hash=ZERO_HASH)
    base = market_forecast()

    def at(published: int, *, expires: int | None = None,
           quality_arg: c.DataQuality | None = None) -> c.MarketForecast:
        return dataclasses.replace(base, published_at_nano=published,
                                   expires_at_nano=base.window_end_nano
                                   if expires is None else expires,
                                   quality=quality_arg or base.quality)

    # On-time boundary: start-1ns keeps the normal branch.
    assert at(base.window_start_nano - 1).published_at_nano < base.window_start_nano
    # Late boundaries: start, end-1ns, end, end+1ns all constructible only as
    # INVALID/FORECAST_LATE with expires fixed to the original window_end.
    for published in (base.window_start_nano, base.window_end_nano - 1,
                      base.window_end_nano, base.window_end_nano + 1):
        late = at(published, quality_arg=late_quality)
        assert late.expires_at_nano == base.window_end_nano
        assert late.request_id == base.request_id and late.horizon == base.horizon
        assert late.frozen_range == base.frozen_range
        assert late.reference_price == base.reference_price
    # published == window_end: expires == published is legal (audit record);
    # published > window_end: expires < published is legal.
    assert at(base.window_end_nano, quality_arg=late_quality).expires_at_nano ==         base.window_end_nano
    beyond = at(base.window_end_nano + 1, quality_arg=late_quality)
    assert beyond.expires_at_nano < beyond.published_at_nano
    # JSON round-trip of a legal late record preserves the original expiry.
    encoded = s.encode_record(beyond)
    assert s.decode_record(c.MarketForecast, encoded) == beyond
    assert encoded["expires_at_nano"] == encoded["window_end_nano"]
    # Daily horizon covered as well.
    daily_late = dataclasses.replace(
        beyond, horizon=horizon(unit="TRADING_DAY", value=1))
    assert s.decode_record(c.MarketForecast, s.encode_record(daily_late)) == daily_late

    # On-time faking FORECAST_LATE, late without the marker/status, and a late
    # expiry that is not the original window_end are all rejected.
    with pytest.raises(ValueError):
        at(base.published_at_nano, quality_arg=late_quality)
    with pytest.raises(ValueError):
        at(base.window_start_nano)
    with pytest.raises(ValueError):
        at(base.window_start_nano, expires=base.window_end_nano - 1,
           quality_arg=late_quality)
    with pytest.raises(ValueError):
        at(base.window_start_nano, expires=base.window_end_nano + 1,
           quality_arg=late_quality)
    # Plain INVALID gets no expiry exemption on the on-time branch.
    plain_invalid = c.DataQuality(status="INVALID", reasons=("OTHER",),
                                  temporal_basis="observed", input_hash=ZERO_HASH)
    with pytest.raises(ValueError):
        at(base.published_at_nano, expires=base.published_at_nano,
           quality_arg=plain_invalid)
    # On-time expiry window: expires must satisfy published < expires <= end.
    with pytest.raises(ValueError):
        at(base.published_at_nano, expires=base.window_end_nano + 1)
    with pytest.raises(ValueError):
        at(base.published_at_nano, expires=base.published_at_nano)
    # decision must strictly precede window_start.
    with pytest.raises(ValueError):
        dataclasses.replace(base, decision_nano=base.window_start_nano)
    # Illegal probabilities/prices stay rejected regardless of branch.
    with pytest.raises(ValueError):
        dataclasses.replace(
            beyond,
            direction_probabilities={"UP": 0.6, "DOWN": 0.3, "NEUTRAL": 0.2})
    with pytest.raises(ValueError):
        dataclasses.replace(beyond, reference_price=Decimal("0"))


def test_wait_family_layer_and_lot_boundaries() -> None:
    def spec_for(family: str, layers: int, lots: int) -> c.StrategySpec:
        return c.StrategySpec(
            strategy_id="s", family=family, version="1", parameter_version="p",
            code_sha256=ZERO_HASH, mode="MINUTE", parameters={}, required_factor_keys=(),
            allowed_regimes=("UP_TREND",), allowed_flags=(), confidence_policy_id="c",
            path_barrier_key="ATR_1", max_holding=horizon(unit="TRADING_MINUTE", value=60),
            holding_policy=c.HoldingPolicy(True, True, False, 30, 5), max_layers=layers,
            max_total_lots=lots, validation_stage="RESEARCH")
    # WAIT is exactly 0/0: accepts zero, rejects any nonzero placeholder.
    assert spec_for("WAIT", 0, 0).max_total_lots == 0
    with pytest.raises(ValueError):
        spec_for("WAIT", 1, 0)
    with pytest.raises(ValueError):
        spec_for("WAIT", 0, 1)
    with pytest.raises(ValueError):
        spec_for("WAIT", 1, 1)
    # Trading families keep positive budgets: 0/0 is rejected, 1/7 accepted.
    with pytest.raises(ValueError):
        spec_for("TREND", 0, 0)
    assert spec_for("TREND", 1, 7).max_layers == 1
    assert spec_for("GRID", 3, 6).max_total_lots == 6
    assert spec_for("MARTINGALE", 3, 7).max_layers == 3


def test_signed_lot_fields_accept_negative_without_abs() -> None:
    # 01 §4.1 / 05 §7.1: target_lots is the signed absolute target net lots and
    # remaining_lots keeps the signed unfinished quantity; PositionSnapshot has
    # no side field, so its net lots sign is the only direction carrier.
    long_exit = c.PositionSnapshot(contract_id="C", strategy_id="S", episode_id="E",
                                   lots=-4, average_entry_price=Decimal("100.4"),
                                   pending_exit=True)
    assert long_exit.lots == -4
    assert c.PositionSnapshot(contract_id="C", strategy_id="S", episode_id="E",
                              lots=0, average_entry_price=None,
                              pending_exit=False).lots == 0
    with pytest.raises(TypeError):
        c.PositionSnapshot(contract_id="C", strategy_id="S", episode_id="E",
                           lots=True, average_entry_price=None, pending_exit=False)
    with pytest.raises(TypeError):
        c.PositionSnapshot(contract_id="C", strategy_id="S", episode_id="E",
                           lots=3.0, average_entry_price=None, pending_exit=False)
    short_proposal = dataclasses.replace(
        strategy_proposal(), action="HOLD", target_lots=-7)
    assert short_proposal.target_lots == -7
    zero_proposal = dataclasses.replace(strategy_proposal(), action="WAIT", target_lots=0)
    assert zero_proposal.target_lots == 0
    with pytest.raises(TypeError):
        dataclasses.replace(strategy_proposal(), target_lots=True)
    with pytest.raises(TypeError):
        dataclasses.replace(strategy_proposal(), target_lots=-1.5)
    short_partial = c.ExecutionResult(
        intent_id="i", fills=(), remaining_lots=-3, status="PARTIAL", reasons=())
    assert short_partial.remaining_lots == -3
    assert c.ExecutionResult(intent_id="i", fills=(), remaining_lots=0,
                             status="FILLED", reasons=()).remaining_lots == 0
    with pytest.raises(TypeError):
        c.ExecutionResult(intent_id="i", fills=(), remaining_lots=False,
                          status="PARTIAL", reasons=())
    with pytest.raises(TypeError):
        c.ExecutionResult(intent_id="i", fills=(), remaining_lots=-2.0,
                          status="PARTIAL", reasons=())
    # Per-lot and fill quantities remain strictly positive integers.
    with pytest.raises(ValueError):
        c.PositionLot(lot_id="l", contract_id="C", strategy_id="S", episode_id="E",
                      side=-1, lots=-1, opened_at_nano=1,
                      opened_trading_day=date(2026, 1, 9), entry_price=Decimal("1"),
                      opening_fee_cny=Decimal("0"))
    with pytest.raises(ValueError):
        fill_kwargs = dict(fill_id="f", intent_id="i", contract_id="C", strategy_id="S",
                           episode_id="E", execution_nano=1,
                           trading_day=date(2026, 1, 9), side=1, lots=-2,
                           offset="OPEN", reference_price=Decimal("1"),
                           execution_price=Decimal("1"), fee_cny=Decimal("0"),
                           slippage_cny=Decimal("0"), rule_id="r", evidence="SCENARIO")
        c.Fill(**fill_kwargs)


def test_exit_context_constructible_with_mismatched_qualification() -> None:
    # Qualification consistency is a gate concern (QUALIFICATION_MISMATCH /
    # QUALIFICATION_EXPIRED with OPEN/ADD weight 0), not a constructor error:
    # M4 must be able to build the context that manages an existing position.
    mismatched_versions = qualification(strategy_id="other-strategy",
                                        strategy_version="9.9",
                                        code_sha256="e" * 64)
    context = c.StrategyContext(
        now_nano=unano(9, 1, 5), spec=strategy_spec(), exit_binding=exit_binding(),
        features=feature_snapshot(),
        forecasts=(), regime=regime_assessment(),
        macro=c.MacroContext(context_id="ctx", decision_nano=unano(9, 1, 5), items=(),
                             policy_version="macro-risk-v2.1.0", risk_tags=(),
                             mode="PRICE_ONLY", quality=quality()),
        bars=(), sessions=(), account=account_snapshot(),
        position=c.PositionSnapshot(contract_id="SYN.rb2609", strategy_id="strat-1",
                                    episode_id="ep-1", lots=-2,
                                    average_entry_price=Decimal("100.4"),
                                    pending_exit=True),
        rule=trading_rule(),
        confidence_policy=c.ConfidencePolicy(
            policy_id="confidence-ordinary-v2.1.0", minimum_persistence=0.70,
            maximum_stop_touch=None, minimum_samples=200, minimum_blocks=30,
            maximum_calibration_error=0.05, maximum_ambiguity=0.05,
            confirmation_observations=3, exit_persistence=0.65, cooldown_minutes=30,
            minimum_conservative_ev_cny=Decimal("0")),
        stress_policy=stress_policy(), run_policy=run_policy(),
        qualification=mismatched_versions,
        model_hashes={"forecast": "9" * 64}, holding_deadline_nano=unano(9, 7))
    assert context.qualification is not None
    assert context.qualification.strategy_id == "other-strategy"
    assert context.position.lots == -2
    # Structural typing still rejects non-qualification payloads.
    with pytest.raises(TypeError):
        c.StrategyContext(
            now_nano=unano(9, 1, 5), spec=strategy_spec(),
            exit_binding=exit_binding(), features=feature_snapshot(),
            forecasts=(), regime=regime_assessment(),
            macro=c.MacroContext(context_id="ctx", decision_nano=1, items=(),
                                 policy_version="m", risk_tags=(), mode="PRICE_ONLY",
                                 quality=quality()),
            bars=(), sessions=(), account=account_snapshot(), position=None, rule=None,
            confidence_policy=c.ConfidencePolicy(
                policy_id="confidence-ordinary-v2.1.0", minimum_persistence=0.70,
                maximum_stop_touch=None, minimum_samples=200, minimum_blocks=30,
                maximum_calibration_error=0.05, maximum_ambiguity=0.05,
                confirmation_observations=3, exit_persistence=0.65, cooldown_minutes=30,
                minimum_conservative_ev_cny=Decimal("0")),
            stress_policy=stress_policy(), run_policy=run_policy(),
            qualification="not-a-qualification", model_hashes={},
            holding_deadline_nano=unano(9, 7))


def test_geometry_key_002_hand_calcs_and_boundaries() -> None:
    """geometry-v2.1.1 (变更申请-002 §3): C = features.reference_price."""
    q2 = c.DataQuality(status="READY", reasons=(), temporal_basis="observed",
                       input_hash=ZERO_HASH)

    def snapshot(reference: str, tick: str = "0.1", *, tick_available: int | None = 999,
                 values_extra: tuple[c.FactorValue, ...] = (),
                 decision: int = 1000) -> c.FeatureSnapshot:
        tick_value = c.FactorValue(factor_key="META:price_tick:v2.1.0",
                                   value=Decimal(tick), available_at_nano=tick_available,
                                   quality=q2)
        return c.FeatureSnapshot(
            snapshot_id="snap-geo", contract_id="C", raw_symbol="R", product="P",
            trading_day=date(2026, 1, 9), session_id="s", session_kind="DAY",
            decision_nano=decision, available_at_nano=decision, duration_seconds=60,
            source_bar_ids=(), reference_price=Decimal(reference),
            atr_points=Decimal("2"), values=(tick_value, *values_extra),
            daily_background_id=None, quality=q2)

    frozen = c.FrozenRange(range_id="r", frozen_at_nano=999, lower=Decimal("98"),
                           center=Decimal("100"), upper=Decimal("102"),
                           stop_lower=Decimal("96"), stop_upper=Decimal("104"),
                           source_snapshot_id="OLD-SNAPSHOT")
    # Doc hand-calc: C=99 -> 1-2-2-3-2; C=100 -> 1-3-3-3-2. The mapped event
    # name (STOP_TOUCH) is used in the key; the full LONG/SHORT event name
    # stays on CalibrationEvidence.
    assert s.calibration_geometry_key(snapshot("99"), frozen, "STOP_TOUCH:LONG") ==         "geometry-v2.1.1|STOP_TOUCH|1-2-2-3-2"
    assert s.calibration_geometry_key(snapshot("100"), frozen, "STOP_TOUCH:SHORT") ==         "geometry-v2.1.1|STOP_TOUCH|1-3-3-3-2"
    assert s.calibration_geometry_key(snapshot("99"), frozen, "PERSIST:UP_TREND") ==         "geometry-v2.1.1|UP_TREND|ANCHOR_ATR"
    # C may sit outside L/U without clamping or redrawing (old range reuse:
    # source_snapshot_id differs from the current snapshot, frozen before the
    # decision).
    assert frozen.source_snapshot_id != "snap-geo"
    outside = s.calibration_geometry_key(snapshot("105"), frozen, "RETURN_BEFORE_BREAK")
    assert outside is not None and outside.startswith("geometry-v2.1.1|")
    # Missing tick, future tick, invalid tick and a range frozen after the
    # decision return None instead of guessing; duplicate metadata errors.
    assert s.calibration_geometry_key(
        snapshot("99", tick_available=1001), frozen, "STOP_TOUCH:LONG") is None
    assert s.calibration_geometry_key(
        snapshot("99", tick_available=None), frozen, "STOP_TOUCH:LONG") is None
    assert s.calibration_geometry_key(
        snapshot("99", tick="0"), frozen, "STOP_TOUCH:LONG") is None
    later = c.FrozenRange(range_id="r2", frozen_at_nano=1001, lower=Decimal("98"),
                          center=Decimal("100"), upper=Decimal("102"),
                          stop_lower=Decimal("96"), stop_upper=Decimal("104"),
                          source_snapshot_id="OLD-SNAPSHOT")
    assert s.calibration_geometry_key(snapshot("99"), later, "STOP_TOUCH:LONG") is None
    duplicate = c.FactorValue(factor_key="META:price_tick:v2.1.0",
                              value=Decimal("0.1"), available_at_nano=999, quality=q2)
    with pytest.raises(s.SchemaError):
        s.calibration_geometry_key(
            snapshot("99", values_extra=(duplicate,)), frozen, "STOP_TOUCH:LONG")
    assert s.calibration_geometry_key(snapshot("99"), None, "STOP_TOUCH:LONG") is None
    # The function never emits the old geometry prefix.
    for event in ("DIRECTION", "PATH", "STOP_TOUCH:LONG", "PERSIST:ORDERED_RANGE",
                  "RETURN_BEFORE_BREAK", "UP_BREAKOUT"):
        key = s.calibration_geometry_key(snapshot("99"), frozen, event)
        assert key is None or not key.startswith("geometry-v2.1.0")


def test_valuation_marks_and_checkpoint_recovery_fields() -> None:
    """变更申请-003: ValuationMark combinations and checkpoint recovery facts."""
    import hashlib as _hashlib
    import dataclasses as _dc

    lot = c.PositionLot(lot_id="lot-9", contract_id="SYN.rb2609", strategy_id="strat-1",
                        episode_id="ep-1", side=1, lots=2, opened_at_nano=unano(9, 1, 6),
                        opened_trading_day=date(2026, 1, 9), entry_price=Decimal("100.4"),
                        opening_fee_cny=Decimal("0.1"))
    holding_account = _dc.replace(account_snapshot(), lots=(lot,))
    current = c.ValuationMark(
        contract_id="SYN.rb2609", price=Decimal("103"), as_of_nano=unano(9, 1, 5),
        valued_at_nano=unano(9, 1, 5), source_bar_id="bar-1",
        source_bar_sha256=ZERO_HASH, rule_id="rule-1", status="CURRENT", reasons=())
    stale = c.ValuationMark(
        contract_id="SYN.rb2609", price=Decimal("102"), as_of_nano=unano(9, 1, 5),
        valued_at_nano=unano(9, 1, 5), source_bar_id="bar-1",
        source_bar_sha256=ZERO_HASH, rule_id="rule-1", status="STALE",
        reasons=("PRICE_MISSING",))
    unvalued = c.ValuationMark(
        contract_id="SYN.rb2609", price=None, as_of_nano=None, valued_at_nano=None,
        source_bar_id=None, source_bar_sha256=None, rule_id=None, status="UNVALUED",
        reasons=("PRICE_MISSING", "RULE_MISSING"))
    for mark in (current, stale, unvalued):
        assert s.decode_record(c.ValuationMark, s.encode_record(mark)) == mark

    owning_state = strategy_state(episode_id="ep-1", phase="WAIT_TRIGGER",
                                  direction=1, budget=round_budget(),
                                  layer_filled_lots=(0,),
                                  pending_intent_ids=("int-1",))

    def checkpoint(**overrides: Any) -> c.LedgerCheckpoint:
        values: dict[str, Any] = dict(
            sequence=1, contract_lock_sha256=ZERO_HASH, registry_sha256=ZERO_HASH,
            exit_registry_sha256=ZERO_HASH,
            run_policy=run_policy(), rule_hashes={"rule-1": ZERO_HASH},
            model_hashes={"forecast": ZERO_HASH, "payoff": ZERO_HASH,
                          "regime_reference": ZERO_HASH},
            qualification_hashes={"qual-1": ZERO_HASH}, account=holding_account,
            valuation_marks=(current,), states=(owning_state,),
            exit_bindings=(exit_binding(),), exit_states=(exit_state(),),
            exit_execution_links=(),
            pending_intents=(order_intent(),), pending_remaining_lots={"int-1": 1},
            processed_event_ids=("evt-1",), processed_fill_hashes={},
            capacity_used={"SYN.rb2609": 1}, previous_hash=ZERO_HASH,
            checkpoint_hash=ZERO_HASH)
        values.update(overrides)
        return c.LedgerCheckpoint(**values)

    assert checkpoint().valuation_marks == (current,)
    assert checkpoint(valuation_marks=(stale,)).valuation_marks[0].status == "STALE"
    assert checkpoint(valuation_marks=(unvalued,)).valuation_marks[0].price is None

    # Fill hash binding: the complete canonical encode_record content hash.
    one_fill = fill()
    fill_hash = _hashlib.sha256(s.canonical_json_bytes(
        s.encode_record(one_fill))).hexdigest()
    assert checkpoint(processed_fill_hashes={one_fill.fill_id: fill_hash})         .processed_fill_hashes[one_fill.fill_id] == fill_hash

    # Strict negatives.
    with pytest.raises(ValueError):
        c.ValuationMark(contract_id="C", price=None, as_of_nano=1, valued_at_nano=2,
                        source_bar_id="b", source_bar_sha256=ZERO_HASH, rule_id="r",
                        status="CURRENT", reasons=())
    with pytest.raises(ValueError):
        _dc.replace(current, reasons=("PRICE_STALE",))  # CURRENT must be empty
    with pytest.raises(ValueError):
        _dc.replace(stale, reasons=())
    with pytest.raises(ValueError):
        _dc.replace(stale, reasons=("NOT_A_REASON",))
    with pytest.raises(ValueError):
        _dc.replace(stale, reasons=("PRICE_STALE", "PRICE_MISSING"))  # unsorted
    with pytest.raises(ValueError):
        _dc.replace(unvalued, price=Decimal("1"))
    with pytest.raises(ValueError):
        _dc.replace(current, price=Decimal("0"))
    with pytest.raises(ValueError):
        _dc.replace(current, as_of_nano=unano(9, 1, 6),
                    valued_at_nano=unano(9, 1, 5))  # as_of > valued_at
    # Checkpoint cross rules.
    with pytest.raises(ValueError):
        checkpoint(valuation_marks=(
            _dc.replace(current, valued_at_nano=holding_account.as_of_nano + 1),))
    with pytest.raises(ValueError):
        checkpoint(valuation_marks=(current, current))  # duplicates / unsorted
    with pytest.raises(ValueError):
        checkpoint(valuation_marks=())  # held contract without a mark
    empty_account = _dc.replace(account_snapshot(), lots=())
    with pytest.raises(ValueError):
        checkpoint(account=empty_account)  # mark without a held contract
    with pytest.raises(ValueError):
        checkpoint(pending_remaining_lots={})  # keyset mismatch
    with pytest.raises(ValueError):
        checkpoint(pending_remaining_lots={"int-1": 0})
    with pytest.raises(TypeError):
        checkpoint(pending_remaining_lots={"int-1": True})
    with pytest.raises(ValueError):
        checkpoint(pending_remaining_lots={"int-1": -1})  # sign mismatch
    owning = strategy_state(episode_id="ep-1", phase="WAIT_TRIGGER", direction=1,
                            budget=round_budget(), layer_filled_lots=(0,),
                            pending_intent_ids=("int-1",))
    with pytest.raises(ValueError):
        checkpoint(states=(
            strategy_state(episode_id="OTHER", phase="WAIT_TRIGGER", direction=1,
                           budget=round_budget(), layer_filled_lots=(0,),
                           pending_intent_ids=("int-1",)),))  # ownership mismatch
    with pytest.raises(ValueError):
        checkpoint(pending_remaining_lots={"int-1": 2})  # exceeds delta +1
    with pytest.raises(ValueError):
        checkpoint(processed_fill_hashes={one_fill.fill_id: "nothex"})
    assert checkpoint(states=(owning,)).pending_remaining_lots["int-1"] == 1


def test_quantiles_nondecreasing_allows_equality() -> None:
    """Q3 arbitration (006): quantiles are non-decreasing; equal adjacent
    values are legal for degenerate/discrete distributions, crossings are
    not, and no epsilon/jitter is ever applied."""
    base = market_forecast()
    # All-equal quantiles are legal.
    flat = dataclasses.replace(base, return_quantiles={
        "Q05": 0.0, "Q25": 0.0, "Q50": 0.0, "Q75": 0.0, "Q95": 0.0},
        point_quantiles={"Q05": Decimal("0"), "Q25": Decimal("0"),
                         "Q50": Decimal("0"), "Q75": Decimal("0"),
                         "Q95": Decimal("0")},
        atr_quantiles={"Q05": 0.0, "Q25": 0.0, "Q50": 0.0, "Q75": 0.0, "Q95": 0.0})
    assert flat.return_quantiles["Q05"] == flat.return_quantiles["Q95"]
    # Plateaus with rises are legal.
    plateau = dataclasses.replace(base, return_quantiles={
        "Q05": -2.0, "Q25": -2.0, "Q50": 0.0, "Q75": 0.0, "Q95": 4.0},
        point_quantiles={"Q05": Decimal("-206"), "Q25": Decimal("-206"),
                         "Q50": Decimal("0"), "Q75": Decimal("0"),
                         "Q95": Decimal("412")},
        atr_quantiles={"Q05": -103.0, "Q25": -103.0, "Q50": 0.0,
                       "Q75": 0.0, "Q95": 206.0})
    assert plateau.return_quantiles["Q25"] == plateau.return_quantiles["Q05"]
    # Decimal point quantiles follow the same rule.
    flat_points = dataclasses.replace(base, point_quantiles={
        "Q05": Decimal("0"), "Q25": Decimal("0"), "Q50": Decimal("0"),
        "Q75": Decimal("0"), "Q95": Decimal("0")},
        return_quantiles={"Q05": 0.0, "Q25": 0.0, "Q50": 0.0, "Q75": 0.0, "Q95": 0.0},
        atr_quantiles={"Q05": 0.0, "Q25": 0.0, "Q50": 0.0, "Q75": 0.0, "Q95": 0.0})
    assert flat_points.point_quantiles["Q50"] == Decimal("0")
    with pytest.raises(ValueError):
        dataclasses.replace(base, return_quantiles={
            "Q05": -2.0, "Q25": 0.0, "Q50": -1.0, "Q75": 0.0, "Q95": 4.0})
    with pytest.raises(ValueError):
        dataclasses.replace(base, point_quantiles={
            "Q05": Decimal("1"), "Q25": Decimal("-1"), "Q50": Decimal("0"),
            "Q75": Decimal("1"), "Q95": Decimal("4")})
    # Type/finite/key-set constraints are unchanged.
    with pytest.raises(ValueError):
        dataclasses.replace(base, return_quantiles={
            "Q05": float("nan"), "Q25": 0.0, "Q50": 0.0, "Q75": 0.0, "Q95": 0.0})
    with pytest.raises(ValueError):
        dataclasses.replace(base, return_quantiles={
            "Q05": float("inf"), "Q25": 0.0, "Q50": 0.0, "Q75": 0.0, "Q95": 0.0})
    with pytest.raises(ValueError):
        dataclasses.replace(base, return_quantiles={
            "Q05": 0.0, "Q25": 0.0, "Q50": 0.0, "Q75": 0.0})  # missing Q95


def test_probability_bucket_membership() -> None:
    assert s.probability_in_bucket(0.85, "[0.85,0.90)")
    assert not s.probability_in_bucket(0.85, "[0.80,0.85)")
    assert s.probability_in_bucket(0.10, "[0.05,0.10]")
    assert not s.probability_in_bucket(0.10, "(0.10,0.15]")
    assert s.probability_in_bucket(0.42, "ALL")
    with pytest.raises(s.SchemaError):
        s.probability_in_bucket(0.42, "nonsense")


def test_hash_format_and_stable_ids() -> None:
    from ak_trading.v2.artifacts import stable_id
    identifier = stable_id("bar", "identity")
    assert identifier.startswith("bar:")
    assert len(identifier) == 4 + 64
    with pytest.raises(ValueError):
        c.DataQuality(status="READY", reasons=(), temporal_basis="observed",
                      input_hash="NO HEX")
