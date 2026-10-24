"""M1 payoff-path measurement contracts (E-P1 v2-ep1-training-path
-20260917-r1 RULING 2/3.4, EP1-M1 work order).

Contract-only coverage over the frozen offline fixed-candidate measurement
source family: PayoffTrainingPlan strict schema and timing chain, the
plan_sha256 derivation without self-reference, PayoffPathDecision causal
source/id derivation and the seven-domain checks, PayoffPathOrder /
PayoffPathCheckpoint / PayoffReceiptCommit structure, PayoffPathResult
cross-record membership rules, normal-vs-measurement mutual rejection at
type and schema level, the source producer version split
(observation-from-replay-p1-v1 vs p1-v2) and the eleven new blocking
PAYOFF_INTEGRITY suffixes registered disjointly from E1's frozen
9 + 23 + 1 reason sets. All fixtures are SYNTHETIC.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from ak_trading.v2 import artifacts as a
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
    import hashlib
    return c.DataQuality(status=status, reasons=(), temporal_basis="observed",
                         input_hash=hashlib.sha256(status.encode()).hexdigest())


def horizon(**overrides: Any) -> c.Horizon:
    values: dict[str, Any] = {"unit": "TRADING_MINUTE", "value": 5}
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


def calibration_evidence() -> c.CalibrationEvidence:
    return c.CalibrationEvidence(
        evidence_id="ev-1", model_id="model-1", policy_id="confidence-ordinary-v2.1.0",
        product="SYN.rb", session_kind="DAY", duration_seconds=60,
        base_regime="ORDERED_RANGE", barrier_key="COMMON",
        geometry_key="geometry-v2.1.1|DIRECTION|ANCHOR_ATR", event="DIRECTION",
        horizon=horizon(), label_version="label-v2.1.0", bucket="ALL", sample_count=250,
        effective_blocks=32, calibration_error=0.03, brier=0.19, training_end_nano=unano(6, 7),
        calibration_end_nano=unano(8, 7), valid_until_nano=unano(8, 7) + 30 * 24 * H,
        status="VALID")


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
        return_quantiles={"Q05": -2 / 103, "Q25": -1 / 103, "Q50": 0.0, "Q75": 1 / 103,
                          "Q95": 4 / 103},
        point_quantiles={"Q05": Decimal("-2"), "Q25": Decimal("-1"), "Q50": Decimal("0"),
                         "Q75": Decimal("1"), "Q95": Decimal("4")},
        atr_quantiles={"Q05": -1.0, "Q25": -0.5, "Q50": 0.0, "Q75": 0.5, "Q95": 2.0},
        path_probabilities={"UP_FIRST": 0.4, "DOWN_FIRST": 0.3, "NEITHER": 0.25,
                            "AMBIGUOUS": 0.05},
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


def account_snapshot(**overrides: Any) -> c.AccountSnapshot:
    values: dict[str, Any] = dict(
        account_id="acc-1", as_of_nano=unano(9, 1, 5), trading_day=date(2026, 1, 9),
        initial_cash_cny=Decimal("1000000"), cash_cny=Decimal("999990"),
        realized_gross_cny=Decimal("10"), fees_cny=Decimal("20"),
        slippage_cny=Decimal("5"), unrealized_cny=Decimal("-40"),
        equity_cny=Decimal("999950"), margin_cny=Decimal("1000"),
        reserved_margin_cny=Decimal("1000"), day_start_equity_cny=Decimal("1000000"),
        day_net_cny=Decimal("-50"), peak_equity_cny=Decimal("1000000"), lots=(), killed=False,
        quality=quality())
    values.update(overrides)
    return c.AccountSnapshot(**values)


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


def strategy_proposal(**overrides: Any) -> c.StrategyProposal:
    values: dict[str, Any] = dict(
        proposal_id="prop-1", strategy_id="strat-1", episode_id="ep-1",
        contract_id="SYN.rb2609", decision_nano=unano(9, 1, 5), action="OPEN",
        target_lots=1, layer_index=0, trigger_price=Decimal("100.5"),
        target_price=Decimal("106"), stop_price=Decimal("96"),
        earliest_execution_nano=unano(9, 1, 6), valid_until_nano=unano(9, 2),
        forecast_id="fc-1", assessment_id="reg-1", budget=round_budget(),
        payoff=None,
        next_state=strategy_state(episode_id="ep-1", phase="WAIT_TRIGGER", direction=1,
                                  budget=round_budget(), layer_filled_lots=(0,)),
        reasons=())
    values.update(overrides)
    return c.StrategyProposal(**values)


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


def order_intent(**overrides: Any) -> c.OrderIntent:
    values: dict[str, Any] = dict(
        intent_id="int-1", decision_id="dec-1", strategy_id="strat-1", episode_id="ep-1",
        contract_id="SYN.rb2609", created_at_nano=unano(9, 1, 5),
        not_before_nano=unano(9, 1, 6), expires_at_nano=unano(9, 2), delta_lots=1,
        offset="OPEN", purpose="ENTRY", layer_index=0, limit_price=Decimal("100.5"))
    values.update(overrides)
    return c.OrderIntent(**values)


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


def exit_spec_fixture() -> c.ExitSpec:
    return c.ExitSpec(
        exit_policy_id="exit_trailing_m1_v1", family="TRAILING_STOP", mode="MINUTE",
        version="1.0.0", parameter_version="exit_trailing_m1_v1:params-1",
        code_sha256=ZERO_HASH,
        parameters={"arm_profit_r": "0.5", "trail_atr_multiple": "1",
                    "atr_basis": "FROZEN_ATR", "peak_source": "COMPLETED_CLOSE"},
        required_market_keys=("BASE:close:v2.1.0:d60",),
        required_state_keys=("EXITSTATE:peak_favorable_price:v1.0.0:d60",),
        applicable_entry_families=("TREND",), observation_duration_seconds=60,
        execution_timing="NEXT_LEGAL_WINDOW", validation_stage="RESEARCH")


def exit_feature_snapshot() -> c.ExitFeatureSnapshot:
    value = c.ExitFeatureValue(
        feature_key="EXITSTATE:peak_favorable_price:v1.0.0:d60", value=Decimal("105"),
        observed_at_nano=unano(9, 1, 5), available_at_nano=unano(9, 1, 5),
        quality=quality(), source_hashes={"snap": ZERO_HASH})
    return c.ExitFeatureSnapshot(
        snapshot_id="exit-snap-1", account_id="payoff-path:" + "a" * 64,
        contract_id="SYN.rb2609", strategy_id="strat-1", episode_id="ep-1",
        exit_binding_sha256=exit_binding().binding_sha256, duration_seconds=60,
        observed_at_nano=unano(9, 1, 5), available_at_nano=unano(9, 1, 5),
        values=(value,), source_state_sha256=ZERO_HASH)


def forecast_request() -> c.ForecastRequest:
    return c.ForecastRequest(
        request_id="req-1", features=feature_snapshot(), horizon=horizon(),
        window_start_nano=unano(9, 1, 6), window_end_nano=unano(9, 1, 11),
        barrier_key="ATR_1", upper_distance=Decimal("2"), lower_distance=Decimal("2"),
        frozen_range=frozen_range())


# ─── EP1 fixtures ───────────────────────────────────────────────────────────

def plan(**overrides: Any) -> c.PayoffTrainingPlan:
    values: dict[str, Any] = dict(
        protocol_version="payoff-training-p1-v1",
        outer_split_id="outer-r1", inner_split_id="inner-r1",
        inner_training_end_nano=unano(6, 7), inner_calibration_end_nano=unano(7, 7),
        warmup_start_nano=unano(8, 1), window_start_nano=unano(9, 1),
        window_end_nano=unano(10, 1), training_end_nano=unano(11, 1),
        contract_ids=("SYN.rb2609",), strategy_id="strat-1",
        exit_binding_sha256=exit_binding().binding_sha256,
        initial_cash_cny=Decimal("1000000"), calendar_sha256="1" * 64,
        selection_rule="FIRST_FEASIBLE_P1_OPEN_PER_WINDOW")
    values.update(overrides)
    return c.PayoffTrainingPlan(**values)


def flat_initial_account(plan_obj: c.PayoffTrainingPlan,
                         **overrides: Any) -> c.AccountSnapshot:
    values: dict[str, Any] = dict(
        account_id="payoff-path:" + "a" * 64,
        as_of_nano=plan_obj.warmup_start_nano,
        initial_cash_cny=plan_obj.initial_cash_cny,
        cash_cny=plan_obj.initial_cash_cny, realized_gross_cny=Decimal("0"),
        fees_cny=Decimal("0"), slippage_cny=Decimal("0"), unrealized_cny=Decimal("0"),
        equity_cny=plan_obj.initial_cash_cny, margin_cny=Decimal("0"),
        reserved_margin_cny=Decimal("0"), day_start_equity_cny=plan_obj.initial_cash_cny,
        day_net_cny=Decimal("0"), peak_equity_cny=plan_obj.initial_cash_cny)
    values.update(overrides)
    return account_snapshot(**values)


def empty_checks() -> dict[str, tuple[str, ...]]:
    return {domain: () for domain in c.PAYOFF_PATH_CHECK_DOMAINS}


def path_decision(**overrides: Any) -> c.PayoffPathDecision:
    plan_obj = plan()
    values: dict[str, Any] = dict(
        plan_sha256=c.payoff_training_plan_sha256(plan_obj),
        observed_at_nano=unano(9, 1, 5),
        proposal=strategy_proposal(), context=strategy_context(),
        request=forecast_request(), account_before_sha256=ZERO_HASH,
        checks=empty_checks(), action="MEASURE_ENTRY")
    values.update(overrides)
    source = c.payoff_path_decision_source_sha256(
        values["plan_sha256"], values["proposal"], values["context"], values["request"],
        values["account_before_sha256"], values["checks"], values["action"])
    return c.PayoffPathDecision(
        path_decision_id=c.payoff_path_decision_id(source),
        source_sha256=source, **values)


def path_order(**overrides: Any) -> c.PayoffPathOrder:
    values: dict[str, Any] = dict(
        intent_id="int-1", path_decision_id=path_decision().path_decision_id,
        strategy_id="strat-1", episode_id="ep-1", contract_id="SYN.rb2609",
        created_at_nano=unano(9, 1, 5), not_before_nano=unano(9, 1, 6),
        expires_at_nano=unano(9, 2), delta_lots=1, offset="OPEN", purpose="ENTRY",
        layer_index=0, limit_price=Decimal("100.5"))
    values.update(overrides)
    return c.PayoffPathOrder(**values)


def path_id() -> str:
    return c.payoff_path_id(
        c.payoff_training_plan_sha256(plan()), "2" * 64, flat_initial_account(plan()),
        "3" * 64, "4" * 64, "5" * 64,
        {"forecast": "6" * 64, "payoff": "7" * 64, "regime_reference": "8" * 64})


def path_checkpoint(**overrides: Any) -> c.PayoffPathCheckpoint:
    values: dict[str, Any] = dict(
        sequence=0, contract_lock_sha256=ZERO_HASH, registry_sha256=ZERO_HASH,
        exit_registry_sha256=ZERO_HASH, run_policy=run_policy(),
        rule_hashes={"rule-1": ZERO_HASH},
        model_hashes={"forecast": ZERO_HASH, "payoff": ZERO_HASH,
                      "regime_reference": ZERO_HASH},
        qualification_hashes={}, account=flat_initial_account(plan()),
        valuation_marks=(), states=(), exit_bindings=(), exit_states=(),
        exit_execution_links=(), pending_intents=(), pending_remaining_lots={},
        processed_event_ids=(), processed_fill_hashes={}, capacity_used={},
        previous_hash=ZERO_HASH, checkpoint_hash="c" * 64, path_id=path_id())
    values.update(overrides)
    return c.PayoffPathCheckpoint(**values)


def execution_result() -> c.ExecutionResult:
    return c.ExecutionResult(intent_id="int-1", fills=(fill(),), remaining_lots=0,
                             status="FILLED", reasons=())


def receipt_commit(**overrides: Any) -> c.PayoffReceiptCommit:
    values: dict[str, Any] = dict(execution_index=0, observed_at_nano=unano(9, 1, 7),
                                  checkpoint_sha256="c" * 64)
    values.update(overrides)
    return c.PayoffReceiptCommit(**values)


def path_result(**overrides: Any) -> c.PayoffPathResult:
    plan_obj = plan()
    decision = path_decision()
    values: dict[str, Any] = dict(
        source_kind="P1_ACTION_CONDITIONAL_REPLAY", path_id=path_id(), plan=plan_obj,
        input_manifest_sha256="2" * 64, initial_account=flat_initial_account(plan_obj),
        features=(feature_snapshot(),), forecasts=(market_forecast(),),
        regimes=(regime_assessment(),), proposals=(strategy_proposal(),),
        candidate_checks={strategy_proposal().proposal_id: empty_checks()},
        exit_specs=(exit_spec_fixture(),), exit_binding=exit_binding(),
        exit_features=(), exit_proposals=(), path_decisions=(decision,),
        orders=(path_order(),), fills=(fill(),), equity=(flat_initial_account(plan_obj),),
        checkpoints=(path_checkpoint(),), execution_results=(execution_result(),),
        receipt_commits=(receipt_commit(),), rule_originals=(trading_rule(),),
        bar_originals=(market_bar(),), sessions=(),
        exclusions=(), reconciliation={"cash_delta_cny": Decimal("0"),
                                       "conserved": True, "status": "CONSISTENT"},
        status="COMPLETE")
    values.update(overrides)
    return c.PayoffPathResult(**values)


def no_candidate_result() -> c.PayoffPathResult:
    plan_obj = plan()
    return path_result(
        proposals=(), candidate_checks={}, path_decisions=(), orders=(), fills=(),
        equity=(), checkpoints=(), execution_results=(), receipt_commits=(),
        exclusions=("NO_TRIGGERED_BUDGETED_PATH",), status="NO_CANDIDATE",
        plan=plan_obj, initial_account=flat_initial_account(plan_obj))


# ─── tests ──────────────────────────────────────────────────────────────────

def test_payoff_training_plan_strict_schema_and_timing_chain() -> None:
    plan_obj = plan()
    field_names = [f.name for f in dataclasses.fields(c.PayoffTrainingPlan)]
    assert len(field_names) == 15
    # §2.2: no self-referential hash field is stored on the plan.
    assert "plan_sha256" not in field_names and "plan_hash" not in field_names
    assert c.payoff_training_plan_sha256(plan_obj) == \
        c.payoff_training_plan_sha256(plan())
    assert len(c.payoff_training_plan_sha256(plan_obj)) == 64
    # Strict chain: inner_training_end < inner_calibration_end < warmup_start
    #   <= window_start < window_end <= training_end
    with pytest.raises(ValueError, match="timing_chain"):
        plan(inner_calibration_end_nano=unano(6, 7))            # == training end
    with pytest.raises(ValueError, match="timing_chain"):
        plan(warmup_start_nano=unano(7, 7))                      # == calibration end
    with pytest.raises(ValueError, match="timing_chain"):
        plan(window_start_nano=unano(8, 1) - 1)                  # < warmup start
    with pytest.raises(ValueError, match="timing_chain"):
        plan(window_end_nano=unano(9, 1))                        # == window start
    with pytest.raises(ValueError, match="timing_chain"):
        plan(training_end_nano=unano(9, 1))                      # < window end
    # Weak boundaries that MUST pass: warmup == window start, window end == cutoff.
    assert plan(window_start_nano=unano(8, 1)).window_start_nano == plan().warmup_start_nano
    assert plan(training_end_nano=unano(10, 1)).window_end_nano == \
        plan(training_end_nano=unano(10, 1)).training_end_nano
    with pytest.raises(ValueError, match="protocol_version"):
        plan(protocol_version="payoff-training-p1-v2")
    with pytest.raises(ValueError, match="selection_rule"):
        plan(selection_rule="BEST_EV_FIRST")
    with pytest.raises(ValueError, match="contract_ids_must_be_non_empty"):
        plan(contract_ids=())
    with pytest.raises(ValueError, match="duplicates"):
        plan(contract_ids=("SYN.rb2609", "SYN.rb2609"))
    with pytest.raises(ValueError, match="sorted"):
        plan(contract_ids=("SYN.rb2609", "SYN.ag2606"))
    with pytest.raises(ValueError, match="must_be_positive"):
        plan(initial_cash_cny=Decimal("0"))
    with pytest.raises(ValueError, match="64_lowercase_hex"):
        plan(calendar_sha256="nothex")
    with pytest.raises(TypeError, match="must_be_int"):
        c.PayoffTrainingPlan(**{**{f.name: getattr(plan_obj, f.name)
                                   for f in dataclasses.fields(c.PayoffTrainingPlan)},
                                "training_end_nano": True})
    # Any content change flips the derived plan hash.
    assert c.payoff_training_plan_sha256(plan()) != \
        c.payoff_training_plan_sha256(plan(initial_cash_cny=Decimal("999999")))
    assert c.payoff_training_plan_sha256(plan()) != \
        c.payoff_training_plan_sha256(plan(window_end_nano=unano(10, 2)))
    # Codec roundtrip + strict decode rejects unknown/self-hash fields.
    decoded = s.decode_record(c.PayoffTrainingPlan, s.encode_record(plan_obj))
    assert decoded == plan_obj
    smuggled = dict(s.encode_record(plan_obj))
    smuggled["plan_sha256"] = c.payoff_training_plan_sha256(plan_obj)
    with pytest.raises(s.SchemaError, match=r"extra=\[\'plan_sha256\'\]"):
        s.decode_record(c.PayoffTrainingPlan, smuggled)
    missing = dict(s.encode_record(plan_obj))
    del missing["calendar_sha256"]
    with pytest.raises(s.SchemaError, match="missing"):
        s.decode_record(c.PayoffTrainingPlan, missing)


def test_payoff_path_records_roundtrip_and_strict_decode() -> None:
    records = [
        (c.PayoffTrainingPlan, plan()),
        (c.PayoffPathDecision, path_decision()),
        (c.PayoffPathOrder, path_order()),
        (c.PayoffPathCheckpoint, path_checkpoint()),
        (c.PayoffReceiptCommit, receipt_commit()),
        (c.PayoffPathResult, path_result()),
        (c.PayoffPathResult, no_candidate_result()),
    ]
    for record_type, record in records:
        decoded = s.decode_record(record_type, s.encode_record(record))
        assert decoded == record
        tampered = dict(s.encode_record(record))
        tampered["unknown_field"] = 1
        with pytest.raises(s.SchemaError, match=r"extra=\[\'unknown_field\'\]"):
            s.decode_record(record_type, tampered)
    # The path file envelopes are bound to their own schema ids.
    payload = s.encode_record_file("PayoffTrainingPlan",
                                   s.PAYOFF_TRAINING_PLAN_SCHEMA_ID, [plan()])
    assert s.decode_payoff_training_plan_file(payload) == (plan(),)
    path_payload = s.encode_record_file("PayoffPathResult", s.PAYOFF_PATH_SCHEMA_ID,
                                        [path_result()])
    assert s.decode_payoff_path_file(path_payload, c.PayoffPathResult) == (path_result(),)
    checkpoint_payload = s.encode_record_file(
        "PayoffPathCheckpoint", s.PAYOFF_PATH_SCHEMA_ID, [path_checkpoint()])
    assert s.decode_payoff_path_file(checkpoint_payload, c.PayoffPathCheckpoint) \
        == (path_checkpoint(),)


def test_normal_and_measurement_schemas_mutually_reject() -> None:
    normal_checkpoint = c.LedgerCheckpoint(
        sequence=1, contract_lock_sha256=ZERO_HASH, registry_sha256=ZERO_HASH,
        exit_registry_sha256=ZERO_HASH, run_policy=run_policy(),
        rule_hashes={"rule-1": ZERO_HASH},
        model_hashes={"forecast": ZERO_HASH, "payoff": ZERO_HASH,
                      "regime_reference": ZERO_HASH},
        qualification_hashes={}, account=account_snapshot(), valuation_marks=(),
        states=(), exit_bindings=(), exit_states=(), exit_execution_links=(),
        pending_intents=(), pending_remaining_lots={}, processed_event_ids=(),
        processed_fill_hashes={}, capacity_used={}, previous_hash=ZERO_HASH,
        checkpoint_hash=ZERO_HASH)
    path_checkpoint_obj = path_checkpoint()
    # Type-level: pending intents are strictly separate families (use CLOSE so
    # LedgerCheckpoint's OPEN/ADD binding rule cannot fire before the type rule).
    with pytest.raises(TypeError, match="pending_intents_must_be_tuple_of_OrderIntent"):
        dataclasses.replace(normal_checkpoint, pending_intents=(
            path_order(offset="CLOSE", purpose="REDUCE"),))
    with pytest.raises(TypeError,
                       match="pending_intents_must_be_tuple_of_PayoffPathOrder"):
        dataclasses.replace(path_checkpoint_obj, pending_intents=(
            order_intent(offset="CLOSE", purpose="REDUCE"),))
    assert not issubclass(c.PayoffPathCheckpoint, c.LedgerCheckpoint)
    assert not issubclass(c.PayoffPathOrder, c.OrderIntent)
    # Record-level strict field sets: path_id / path_decision_id misdelivery.
    with pytest.raises(s.SchemaError, match=r"extra=\[\'path_id\'\]"):
        s.decode_record(c.LedgerCheckpoint, s.encode_record(path_checkpoint_obj))
    with pytest.raises(s.SchemaError, match=r"missing=\[\'path_id\'\]"):
        s.decode_record(c.PayoffPathCheckpoint, s.encode_record(normal_checkpoint))
    with pytest.raises(s.SchemaError, match=r"extra=\[\'path_decision_id\'\]"):
        s.decode_record(c.OrderIntent, s.encode_record(path_order()))
    with pytest.raises(s.SchemaError, match=r"missing=\[\'path_decision_id\'\]"):
        s.decode_record(c.PayoffPathOrder, s.encode_record(order_intent()))
    # Envelope-level schema domains reject each other.
    normal_payload = s.encode_record_file(
        "LedgerCheckpoint", s.CHECKPOINT_SCHEMA_ID, [normal_checkpoint])
    path_payload = s.encode_record_file(
        "PayoffPathCheckpoint", s.PAYOFF_PATH_SCHEMA_ID, [path_checkpoint_obj])
    with pytest.raises(s.SchemaError, match="schema_version_mismatch"):
        s.decode_record_file(normal_payload, c.PayoffPathCheckpoint,
                             s.PAYOFF_PATH_SCHEMA_ID)
    with pytest.raises(s.SchemaError, match="schema_version_mismatch"):
        s.decode_payoff_path_file(normal_payload, c.PayoffPathCheckpoint)
    with pytest.raises(s.SchemaError, match="schema_version_mismatch"):
        s.decode_payoff_training_plan_file(path_payload)
    with pytest.raises(s.SchemaError, match="schema_version_mismatch"):
        s.decode_record_file(path_payload, c.LedgerCheckpoint, s.CHECKPOINT_SCHEMA_ID)
    # The payoff-path domain hook refuses normal record types outright.
    with pytest.raises(s.SchemaError, match="payoff_path_record_type_not_in_domain"):
        s.decode_payoff_path_file(path_payload, c.LedgerCheckpoint)
    # The normal checkpoint schema id stays separate from the path domain.
    assert s.CHECKPOINT_SCHEMA_ID == "ak_probability_checkpoints_v2.1.3"
    assert s.PAYOFF_PATH_SCHEMA_ID == "ak_probability_payoff_path_v1"
    assert s.PAYOFF_TRAINING_PLAN_SCHEMA_ID == "ak_probability_payoff_training_plan_v1"


def test_path_decision_causal_source_and_identity_derivation() -> None:
    decision = path_decision()
    field_names = [f.name for f in dataclasses.fields(c.PayoffPathDecision)]
    # No selected/eligible/weight/gate vocabulary exists on the measurement
    # decision and no GateDecision field can be embedded.
    for forbidden in ("selected", "eligible", "weight", "gate"):
        assert not any(forbidden in name for name in field_names)
    assert decision.path_decision_id == \
        c.payoff_path_decision_id(decision.source_sha256)
    assert decision.path_decision_id.startswith("payoff_path_decision:")
    # Deterministic causal projection: identical inputs, identical source.
    again = c.payoff_path_decision_source_sha256(
        decision.plan_sha256, decision.proposal, decision.context, decision.request,
        decision.account_before_sha256, decision.checks, decision.action)
    assert again == decision.source_sha256
    changed = c.payoff_path_decision_source_sha256(
        decision.plan_sha256, decision.proposal, decision.context, None,
        decision.account_before_sha256, decision.checks, decision.action)
    assert changed != decision.source_sha256
    with pytest.raises(ValueError, match="action"):
        c.payoff_path_decision_source_sha256(
            decision.plan_sha256, decision.proposal, decision.context,
            decision.request, decision.account_before_sha256, decision.checks,
            "OPEN_GATE")  # not in the frozen action enum
    # Tampering the recorded id or source breaks the frozen derivation check.
    fresh = path_decision()
    with pytest.raises(ValueError, match="path_decision_id_must_derive"):
        dataclasses.replace(fresh, path_decision_id="payoff_path_decision:" + "0" * 64)
    with pytest.raises(ValueError, match="source_sha256_must_equal"):
        dataclasses.replace(fresh, source_sha256="e" * 64)
    # MEASURE_ENTRY preconditions (§2.3): request required, native single-layer
    # OPEN with payoff=None, empty necessary-input/resource checks.
    with pytest.raises(ValueError, match="measure_entry_requires_request"):
        path_decision(request=None)
    with pytest.raises(ValueError, match="measure_entry_requires_payoff_none"):
        path_decision(proposal=strategy_proposal(payoff=payoff_estimate()))
    with pytest.raises(ValueError, match="measure_entry_requires_open_proposal"):
        path_decision(proposal=strategy_proposal(action="WAIT", target_lots=0,
                                                 layer_index=None, budget=None))
    checks_with_reason = empty_checks()
    checks_with_reason["COST_BUDGET"] = ("LOSS_BUDGET_EXCEEDED",)
    with pytest.raises(ValueError, match="measure_entry_requires_empty_check_reasons"):
        path_decision(checks=checks_with_reason)
    # Seven-domain exact key set (unknown/missing domain rejected).
    missing_domain = {key: value for key, value in empty_checks().items()
                      if key != "ACCOUNT_CAPACITY"}
    with pytest.raises(ValueError, match="seven_domains"):
        path_decision(checks=missing_domain)
    extra_domain = dict(empty_checks())
    extra_domain["PAYOFF_EV"] = ()
    with pytest.raises(ValueError, match="seven_domains"):
        path_decision(checks=extra_domain)
    # Non-entry actions keep the causal discipline without entry preconditions.
    manage = path_decision(
        action="MANAGE_EXIT", request=None,
        checks={**empty_checks(), "ACCOUNT_CAPACITY": ("NO_NEW_RISK",)})
    assert manage.action == "MANAGE_EXIT"


def test_payoff_path_result_cross_record_membership_rules() -> None:
    result = path_result()
    assert result.status == "COMPLETE"
    assert result.path_id.startswith("payoff_path:")
    assert no_candidate_result().status == "NO_CANDIDATE"

    wrong_plan = plan(initial_cash_cny=Decimal("999999"))
    with pytest.raises(ValueError, match="path_decision_plan_hash"):
        path_result(plan=wrong_plan, initial_account=flat_initial_account(wrong_plan))
    with pytest.raises(ValueError, match="at_most_one_measure_entry"):
        path_result(path_decisions=(path_decision(),
                                    path_decision(account_before_sha256="9" * 64)))
    with pytest.raises(ValueError, match="path_order_must_resolve_to_path_decision"):
        path_result(orders=(path_order(
            path_decision_id="payoff_path_decision:" + "f" * 64),))
    cancel = path_decision(action="CANCEL_ENTRY", request=None)
    with pytest.raises(ValueError, match="orders_require_exactly_one_measure_entry"):
        path_result(path_decisions=(cancel,),
                    orders=(path_order(path_decision_id=cancel.path_decision_id),))
    with pytest.raises(ValueError, match="path_fill_must_resolve_to_path_order"):
        path_result(fills=(c.Fill(
            fill_id="fill-x", intent_id="int-x", contract_id="SYN.rb2609",
            strategy_id="strat-1", episode_id="ep-1", execution_nano=unano(9, 1, 6),
            trading_day=date(2026, 1, 9), side=1, lots=1, offset="OPEN",
            reference_price=Decimal("100.5"), execution_price=Decimal("100.4"),
            fee_cny=Decimal("0.1"), slippage_cny=Decimal("0.1"), rule_id="rule-1",
            evidence="SCENARIO"),))
    with pytest.raises(ValueError, match="execution_result_must_resolve_to_path_order"):
        path_result(execution_results=(c.ExecutionResult(
            intent_id="int-x", fills=(), remaining_lots=0, status="CANCELLED",
            reasons=()),))
    with pytest.raises(ValueError, match="receipt_commit_execution_index"):
        path_result(receipt_commits=(receipt_commit(execution_index=3),))
    with pytest.raises(ValueError, match="receipt_commit_checkpoint"):
        path_result(receipt_commits=(receipt_commit(checkpoint_sha256="9" * 64),))
    with pytest.raises(ValueError, match="receipt_commit_must_not_precede_fill"):
        path_result(receipt_commits=(receipt_commit(observed_at_nano=unano(9, 1, 5)),))
    with pytest.raises(ValueError, match="checkpoint_path_id"):
        path_result(checkpoints=(path_checkpoint(path_id="payoff_path:" + "b" * 64),))
    # Valid chain passes; broken links, wrong genesis and bad order all fail.
    cp0 = path_checkpoint()
    cp1 = path_checkpoint(sequence=1, previous_hash="c" * 64, checkpoint_hash="d" * 64)
    chained = path_result(checkpoints=(cp0, cp1),
                          receipt_commits=(receipt_commit(),
                                           receipt_commit(checkpoint_sha256="d" * 64)))
    assert chained.checkpoints[1].sequence == 1
    with pytest.raises(ValueError, match="checkpoint_previous_hash_chain_broken"):
        path_result(checkpoints=(cp0, path_checkpoint(sequence=1, previous_hash="e" * 64,
                                                      checkpoint_hash="d" * 64)))
    with pytest.raises(ValueError, match="checkpoint_previous_hash_chain_broken"):
        path_result(checkpoints=(path_checkpoint(sequence=1, previous_hash="c" * 64,
                                                 checkpoint_hash="d" * 64),))
    with pytest.raises(ValueError, match="checkpoints_must_be_sorted_unique_by_sequence"):
        path_result(checkpoints=(cp1, cp0))
    # A normal exit link inside a path checkpoint must resolve inside the pack.
    link = c.ExitExecutionLink(
        link_id="exit-link-1", exit_proposal_id=None, strategy_proposal_id="prop-1",
        decision_id="dec-normal", intent_id="int-normal",
        exit_binding_sha256=exit_binding().binding_sha256, action="REDUCE",
        target_lots=1, requested_lots=1, execution_kind="NEXT_WINDOW_CLOSE")
    with pytest.raises(ValueError, match="checkpoint_exit_link.*path_decision"):
        path_result(checkpoints=(path_checkpoint(
            exit_bindings=(exit_binding(),), exit_execution_links=(link,)),))
    # candidate_checks: exact proposal coverage with seven domains each.
    with pytest.raises(ValueError, match="candidate_checks_must_cover_exactly"):
        path_result(candidate_checks={})  # proposals exist but no checks
    with pytest.raises(ValueError, match="candidate_checks_must_cover_exactly"):
        path_result(candidate_checks={**{strategy_proposal().proposal_id: empty_checks()},
                                      "prop-other": empty_checks()})
    bad_domains = {strategy_proposal().proposal_id:
                   {key: value for key, value in empty_checks().items()
                    if key != "DATA_CLOCK"}}
    with pytest.raises(ValueError, match="candidate_checks_keys_must_be_exactly"):
        path_result(candidate_checks=bad_domains)
    with pytest.raises(ValueError, match="no_candidate_path"):
        path_result(status="NO_CANDIDATE")
    # Isolated initial account: namespace, plan-equal cash, flat, clock.
    with pytest.raises(ValueError, match="initial_account_id_must_use_payoff_path"):
        path_result(initial_account=flat_initial_account(plan(), account_id="acc-1"))
    with pytest.raises(ValueError, match="initial_account_cash_must_equal_plan"):
        path_result(initial_account=flat_initial_account(
            plan(), initial_cash_cny=Decimal("999"), cash_cny=Decimal("999"),
            equity_cny=Decimal("999"), day_start_equity_cny=Decimal("999"),
            peak_equity_cny=Decimal("999")))
    with pytest.raises(ValueError, match="initial_account_must_be_flat"):
        path_result(initial_account=flat_initial_account(
            plan(), reserved_margin_cny=Decimal("1000")))
    with pytest.raises(ValueError, match="initial_account_clock_must_not_precede"):
        path_result(initial_account=flat_initial_account(
            plan(), as_of_nano=plan().warmup_start_nano - 1))
    with pytest.raises(ValueError, match="equity_snapshots_must_share"):
        path_result(equity=(flat_initial_account(plan(), account_id="payoff-path:" + "e" * 64),))
    # Plan/binding identity binding.
    with pytest.raises(ValueError, match="plan_strategy_must_match_exit_binding"):
        path_result(plan=plan(strategy_id="strat-other"),
                    initial_account=flat_initial_account(plan(strategy_id="strat-other")))
    with pytest.raises(ValueError, match="plan_exit_binding_hash_must_match"):
        path_result(plan=plan(exit_binding_sha256="5" * 64),
                    initial_account=flat_initial_account(plan(exit_binding_sha256="5" * 64)))
    # Exit originals only empty when no such facts exist.
    with pytest.raises(ValueError, match="exit_facts_require_exit_specs"):
        path_result(exit_specs=(), exit_features=(exit_feature_snapshot(),))
    with pytest.raises(ValueError, match="not_in_allowed_enum"):
        path_result(source_kind="NORMAL_REPLAY")


def test_payoff_path_id_derivation() -> None:
    plan_obj = plan()
    account = flat_initial_account(plan_obj)
    base = c.payoff_path_id(c.payoff_training_plan_sha256(plan_obj), "2" * 64,
                            account, "3" * 64, "4" * 64, "5" * 64,
                            {"forecast": "6" * 64, "payoff": "7" * 64})
    assert base.startswith("payoff_path:") and len(base) == len("payoff_path:") + 64
    # model_hashes key order never matters (canonical mapping sort).
    assert base == c.payoff_path_id(c.payoff_training_plan_sha256(plan_obj), "2" * 64,
                                    account, "3" * 64, "4" * 64, "5" * 64,
                                    {"payoff": "7" * 64, "forecast": "6" * 64})
    # Any dependency change flips the identity.
    assert base != c.payoff_path_id(c.payoff_training_plan_sha256(
        plan(window_end_nano=unano(10, 2))), "2" * 64, account,
        "3" * 64, "4" * 64, "5" * 64, {"forecast": "6" * 64, "payoff": "7" * 64})
    assert base != c.payoff_path_id(c.payoff_training_plan_sha256(plan_obj),
                                    "9" * 64, account, "3" * 64, "4" * 64,
                                    "5" * 64, {"forecast": "6" * 64, "payoff": "7" * 64})
    with pytest.raises(ValueError, match="64_lowercase_hex"):
        c.payoff_path_id("nothex", "2" * 64, account, "3" * 64, "4" * 64, "5" * 64, {})
    with pytest.raises(TypeError, match="initial_account_must_be"):
        c.payoff_path_id("1" * 64, "2" * 64, "not-an-account", "3" * 64, "4" * 64,
                         "5" * 64, {})


def test_source_producer_version_split_and_suffix_registry() -> None:
    from ak_trading.v2.decision import payoffs

    # §2.4: v2 identifies the PayoffPathResult branch and the explicit
    # multi-rule branch; the legacy seven-argument single-rule branch keeps v1.
    assert c.OBSERVATION_FROM_REPLAY_P1_V2 == "observation-from-replay-p1-v2"
    assert payoffs.OBSERVATION_FROM_REPLAY_VERSION == "observation-from-replay-p1-v1"
    assert c.OBSERVATION_FROM_REPLAY_P1_V2 != payoffs.OBSERVATION_FROM_REPLAY_VERSION
    # Protocol constants (§2.2).
    assert c.PAYOFF_TRAINING_PLAN_PROTOCOL_VERSION == "payoff-training-p1-v1"
    assert c.PAYOFF_TRAINING_PLAN_SELECTION_RULE == "FIRST_FEASIBLE_P1_OPEN_PER_WINDOW"
    assert c.PAYOFF_PATH_ACCOUNT_ID_PREFIX == "payoff-path:"
    # Seven fixed check domains (§3.1.3).
    assert c.PAYOFF_PATH_CHECK_DOMAINS == (
        "IDENTITY_TIME", "DATA_CLOCK", "REGIME_MACRO", "FORECAST_CONFIDENCE",
        "TRIGGER_DOMAIN", "COST_BUDGET", "ACCOUNT_CAPACITY")
    # RULING 3.4: eleven new blocking suffixes, all PAYOFF_INTEGRITY:, all
    # disjoint from E1's frozen 9 + 23 + 1 sets.
    assert c.PAYOFF_PATH_INTEGRITY_SUFFIXES == (
        "SOURCE_KIND_MISMATCH", "SAMPLING_PLAN_MISMATCH", "CANDIDATE_SOURCE_MISMATCH",
        "PATH_DECISION_CHAIN_MISSING", "PATH_ORDER_CHAIN_MISSING",
        "RECEIPT_COMMIT_MISMATCH", "INITIAL_ACCOUNT_MISMATCH", "RULE_ORIGINAL_CONFLICT",
        "RULE_CONSUMPTION_MISMATCH", "RULE_PIT_MISMATCH", "SOURCE_BYTES_MISMATCH")
    assert len(set(c.PAYOFF_PATH_INTEGRITY_SUFFIXES)) == 11
    assert len(payoffs._REASONS_UNAVAILABLE) == 9
    assert len(payoffs._REASONS_INTEGRITY) == 23
    assert len(payoffs._REASONS_INTEGRITY_PROGRAM) == 1
    assert not (set(c.PAYOFF_PATH_INTEGRITY_SUFFIXES)
                & set(payoffs._REASONS_UNAVAILABLE))
    assert not (set(c.PAYOFF_PATH_INTEGRITY_SUFFIXES)
                & set(payoffs._REASONS_INTEGRITY))
    assert not (set(c.PAYOFF_PATH_INTEGRITY_SUFFIXES)
                & set(payoffs._REASONS_INTEGRITY_PROGRAM))
    # Similar-looking suffixes stay distinct (no prefix-shadowing).
    assert "RULE_PIT_MISMATCH" in c.PAYOFF_PATH_INTEGRITY_SUFFIXES
    assert "FILL_RULE_PIT_MISMATCH" in payoffs._REASONS_INTEGRITY


def test_frozen_declarations_allocation_and_lock_registration() -> None:
    signatures = a._public_signature_map()
    for declaration_map in (a.EP1_PAYOFF_PATH_DECLARATIONS,
                            a.EP1_PLAN_REGISTRATION_DECLARATIONS):
        for key, text in declaration_map.items():
            assert signatures[key] == text
    # §2.1/§2.4 declaration texts are frozen verbatim.
    assert a.EP1_PAYOFF_PATH_DECLARATIONS[
        "ak_trading.v2.decision.payoff_paths.replay_payoff_training_window"] == (
        "replay_payoff_training_window(bundle: FrozenInput, "
        "forecast_models: ForecastModelSet, spec: StrategySpec, "
        "initial_account: AccountSnapshot, run_policy: RunPolicy, "
        "policy: Mapping[str, JsonValue], "
        "seasonality_model: SeasonalityModel | None, "
        "regime_reference: RegimeReference, contract_lock_sha256: str, "
        "plan: PayoffTrainingPlan, *, exit_specs: tuple[ExitSpec, ...], "
        "exit_binding: ExitPolicyBinding) -> PayoffPathResult")
    source_declaration = a.EP1_PAYOFF_PATH_DECLARATIONS[
        "ak_trading.v2.decision.payoffs.observation_from_replay"]
    assert source_declaration.startswith("observation_from_replay(opened: StrategyProposal")
    assert "replay: ReplayResult | PayoffPathResult" in source_declaration
    assert "rule_originals: tuple[TradingRule, ...] | None = None" in source_declaration
    assert "-> PayoffObservation" in source_declaration
    bytes_declaration = a.EP1_PAYOFF_PATH_DECLARATIONS[
        "ak_trading.v2.decision.payoffs.payoff_observation_source"]
    assert bytes_declaration.endswith("-> bytes")
    assert "replay: ReplayResult | PayoffPathResult" in bytes_declaration
    # Plan registration declarations name the allocated target schemas.
    assert "ak_probability_experiment_v2.1.3" in \
        a.EP1_PLAN_REGISTRATION_DECLARATIONS[
            "ak_trading.v2.validation.experiments.ExperimentSpec.payoff_training_plan_sha256"]
    assert "ak_probability_research_config_v2.1.3" in \
        a.EP1_PLAN_REGISTRATION_DECLARATIONS[
            "ak_trading.v2.validation.experiments.research_config.payoff_training_plan_sha256"]
    # Ownership: producers belong to M4, plan registration to M5.
    for key in a.EP1_PAYOFF_PATH_DECLARATIONS:
        assert key in a.DECLARATION_OWNERSHIP["M4"]
    for key in a.EP1_PLAN_REGISTRATION_DECLARATIONS:
        assert key in a.DECLARATION_OWNERSHIP["M5"]
    # Written allocation record: unique unoccupied target, C1 untouched.
    allocation = dict(a.EP1_CONTRACT_TARGET_ALLOCATION)
    assert allocation["contract_chain"] == "data/research_runs/v2/contracts-2.1.5"
    assert allocation["contract_version_target"] == \
        "ak_probability_strategy_contract_v2.1.5"
    assert allocation["lock_path"] == \
        "data/research_runs/v2/contracts-2.1.5/contract-lock.ep1-r1.json"
    assert allocation["reviewed_revision_target"] == "ep1-r1"
    assert allocation["experiment_schema_target"] == "ak_probability_experiment_v2.1.3"
    assert allocation["research_config_schema_target"] == \
        "ak_probability_research_config_v2.1.3"
    assert allocation["predecessor_published_lock"] == \
        "data/research_runs/v2/contracts-2.1.3/contract-lock.p3-r1.json"
    assert "contracts-2.1.4" in allocation["reserved_revisions"]
    assert "v2.1.2" in allocation["reserved_revisions"]
    assert "C1" in allocation["reserved_revisions"]
    assert allocation["experiment_schema_target"] != "ak_probability_experiment_v2.1.2"
    # New schemas and this test file enter every future lock build.
    schema_hashes = a._schema_hash_map()
    assert s.PAYOFF_TRAINING_PLAN_SCHEMA_ID in schema_hashes
    assert s.PAYOFF_PATH_SCHEMA_ID in schema_hashes
    assert "tests/test_v2_m1_payoff_path_contracts.py" in a.PUBLIC_TEST_FILES
    # The public M1 layer never reverse-imports business modules.
    import inspect
    for module in (c, s, a):
        text = inspect.getsource(module)
        assert "from ak_trading.v2.decision" not in text
        assert "from ak_trading.v2.validation" not in text
        assert "import ak_trading.v2.decision" not in text
        assert "import ak_trading.v2.validation" not in text


def test_t7_acceptance_lock_registration_wiring() -> None:
    # T7 form arbitration §5.3 (v2-ep1-t7-form-20260917-r1): the lock-side
    # registration for the acceptance input manifest F and the payoff
    # measurement tests, without any new lock root field for F/E/R.
    # Item 1: the three named test files join required_tests alongside every
    # existing entry — nothing is dropped and nothing is duplicated.
    for name in ("tests/test_v2_m4_payoff_paths.py",
                 "tests/test_v2_m5_payoff_integration.py",
                 "tests/test_v2_m5_t7_evidence.py"):
        assert name in a.DOWNSTREAM_TEST_FILES
    assert len(set(a.DOWNSTREAM_TEST_FILES)) == len(a.DOWNSTREAM_TEST_FILES)
    # Item 2: F joins FIXTURE_FILES so fixture_files[F] = H_F.
    assert a.ACCEPTANCE_INPUT_MANIFEST_PATH in a.FIXTURE_FILES
    assert a.ACCEPTANCE_INPUT_MANIFEST_PATH == \
        "tests/fixtures/v2/ep1_t7/acceptance-input-manifest.json"
    assert a.ACCEPTANCE_INPUT_MANIFEST_SCHEMA_ID == \
        "ak_probability_acceptance_input_manifest_v1"
    # §5.3 closing: the strict lock schema gains no private F/E/R root field.
    assert set(a._LOCK_KEYS) == {
        "schema_version", "contract_version", "document_version", "public_files",
        "public_signatures", "schemas", "fixture_files", "ownership",
        "required_tests", "required_check_ids", "policy", "reviewed_revision",
        "reviewer_report_sha256"}
    # §5.1 non-circularity: the forbidden-field set actually names the lock,
    # report, evidence and readiness identities F must never carry.
    for forbidden in ("contract_lock_sha256", "reviewer_report_sha256",
                      "evidence_manifest_sha256", "readiness"):
        assert forbidden in a._ACCEPTANCE_FORBIDDEN_KEY_NAMES
    assert a._ACCEPTANCE_FORBIDDEN_ATTACHMENT_PREFIXES == ("data/research_runs/",)
    # Item 3: the behavioral-source floor constants match the code_hashes()
    # scope plus the offline runner (validation/experiments.py code_hashes).
    assert a.ACCEPTANCE_SOURCE_ROOT == "src/ak_trading/v2"
    assert a.ACCEPTANCE_RUNNER_PATH == "scripts/research_probability_v2.py"
    assert a.ACCEPTANCE_OFFLINE_RUNNER_PATH == "scripts/test_offline.py"
    # §5.3 item 5: the quick-check wiring entry points exist on M1's surface;
    # verify_contract_lock itself keeps every original check (lock/document
    # versions, public files, signatures, schemas, ownership, tests, policy).
    for function in (a.verify_acceptance_input_manifest,
                     a.acceptance_input_manifest_sha256,
                     a.verify_reviewer_report_binding,
                     a.verify_evidence_manifest_binding,
                     a.verify_contract_lock_with_report):
        assert callable(function)
