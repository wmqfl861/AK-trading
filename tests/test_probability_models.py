from __future__ import annotations

from dataclasses import replace
from decimal import Context, Decimal, Inexact, ROUND_UP, Rounded, localcontext
import json
import math

import pytest

from ak_trading.probability.contracts import (
    OUTCOMES, REGIMES, ActionOutcome, FactorRow, PathLabel, ProbabilityPrediction,
)
from ak_trading.probability.models import (
    fit_probability_model, predict_probability, probability_model_dict,
    predict_unconditional_probability,
    fit_payoff_model, estimate_payoff, payoff_model_dict, evaluate_predictions,
)

SCENARIO = "h5-u1-d1"
ACTION = "breakout:h5:t1:s1:p0.5:g0.5:n2:x3"


def pair(i, outcome="up_first", *, product="SHFE.RB", context="near_high",
         regime="uptrend", future_regime="range", scenario=SCENARIO):
    event = FactorRow(
        f"{product}:{i}", f"{product}:contract", f"{product}2601", product,
        i, i * 10, {"atr": 2.0}, "ready", "", context, regime,
    )
    label = PathLabel(
        event.event_id, scenario, event.contract_id, i, 5, i * 10 + 5,
        "mature", "", outcome, 100.0, 102.0, 98.0,
        1 if outcome in ("up_first", "down_first", "ambiguous") else None,
        0.02, 2.0, 3.0, -1.0, future_regime,
    )
    return event, label


def test_empirical_four_outcomes_laplace_and_future_regime_are_train_only():
    training = [pair(1), pair(2), pair(3, "down_first", future_regime="downtrend"),
                pair(4, "neither"), pair(5, "ambiguous")]
    model = fit_probability_model(training, [], min_samples=1)
    event, _ = pair(100, future_regime="uptrend")
    prediction = predict_probability(model, event, SCENARIO)
    assert prediction.probabilities == pytest.approx({
        "up_first": 3 / 9, "down_first": 2 / 9, "neither": 2 / 9, "ambiguous": 2 / 9,
    })
    assert prediction.regime_probabilities == pytest.approx({
        "uptrend": 1 / 8, "downtrend": 2 / 8, "range": 5 / 8,
    })
    assert prediction.train_count == 5
    assert prediction.model_status == "uncalibrated"
    assert prediction.bucket == "SHFE.RB|near_high|uptrend"
    assert prediction.diagnostics["fallback_level"] == "conditional"
    assert prediction.diagnostics["calibration_status"] == "insufficient_calibration"
    assert predict_probability(model, replace(event, values={"atr": 1e20}), SCENARIO) == prediction
    document = probability_model_dict(model)
    assert document["fit_counts"]["training"] == 5
    assert document["timestamps"]["training_terminal_max"] == 55
    assert len(document["model_hash"]) == 64
    json.dumps(document, allow_nan=False)


def test_calibration_selects_temperature_without_refitting_training_counts():
    training = [pair(i, future_regime="uptrend") for i in range(1, 13)]
    calibration = [pair(100 + i, OUTCOMES[i % 4], future_regime=REGIMES[i % 3]) for i in range(36)]
    frozen = fit_probability_model(training, [], min_samples=8)
    model = fit_probability_model(training, calibration, min_samples=8)
    prediction = predict_probability(model, pair(200)[0], SCENARIO)
    assert prediction.temperature == 4.0
    assert prediction.diagnostics["regime_temperature"] == 4.0
    assert prediction.model_status == "calibrated"
    assert prediction.train_count == 12
    assert prediction.calibration_count == 36
    assert prediction.probabilities["up_first"] == pytest.approx(13 ** 0.25 / (13 ** 0.25 + 3))
    before, after = probability_model_dict(frozen), probability_model_dict(model)
    assert before["training_hash"] == after["training_hash"]
    assert before["scenarios"][SCENARIO]["unconditional"] == after["scenarios"][SCENARIO]["unconditional"]
    assert after["calibration_method"] == "separate_outcome_and_regime_multiclass_logloss"
    assert after["timestamps"]["calibration_event_min"] == 1000


def test_insufficient_calibration_uses_temperature_one_and_uniform_ties_prefer_one():
    training = [pair(i + 1, outcome) for i, outcome in enumerate(OUTCOMES)]
    model = fit_probability_model(training, [pair(20)], min_samples=4)
    prediction = predict_probability(model, pair(100)[0], SCENARIO)
    assert prediction.temperature == 1.0
    assert prediction.model_status == "uncalibrated"
    assert prediction.calibration_count == 1
    assert prediction.diagnostics["calibration_status"] == "insufficient_calibration"
    model = fit_probability_model(training, [pair(20 + i) for i in range(4)], min_samples=4)
    assert predict_probability(model, pair(100)[0], SCENARIO).temperature == 1.0


def payoff_pair(i, outcome, net, *, action_id=ACTION, traded=True, **kwargs):
    event, label = pair(i, outcome, **kwargs)
    value = Decimal(str(net))
    action = ActionOutcome(
        event.event_id, action_id, event.contract_id, event.index, label.terminal_nano,
        "complete", "", traded, 1 if traded else 0, value, Decimal(0), Decimal(0),
        value, Decimal(100) if traded else Decimal(0), 0,
    )
    return event, label, action


def forecast(event, probabilities=None, **kwargs):
    return ProbabilityPrediction(
        event.event_id, SCENARIO, probabilities or dict(zip(OUTCOMES, (0.4, 0.3, 0.2, 0.1))),
        dict(zip(REGIMES, (0.2, 0.3, 0.5))), 100, 40, "calibrated", "fixture", 1.0,
        **kwargs,
    )


def test_conditional_payoff_mixes_four_outcome_distributions_with_real_win_rate():
    # State means: 20, -10, 0, -20. Their unbiased variances: 200, 50, 0, 0.
    training = [
        payoff_pair(1, "up_first", 10), payoff_pair(2, "up_first", 30),
        payoff_pair(3, "down_first", -5), payoff_pair(4, "down_first", -15),
        payoff_pair(5, "neither", 0, traded=False), payoff_pair(6, "neither", 0, traded=False),
        payoff_pair(7, "ambiguous", -20), payoff_pair(8, "ambiguous", -20),
    ]
    model = fit_payoff_model(training, min_samples=8, penalty=0.5)
    event = pair(100)[0]
    estimate = estimate_payoff(model, event, ACTION, forecast(event))
    assert estimate.status == "ready"
    assert estimate.count == 8
    assert estimate.mean_net == pytest.approx(3.0)
    assert estimate.standard_error == pytest.approx(math.sqrt(18.25))
    assert estimate.conservative_ev == pytest.approx(3.0 - 0.5 * math.sqrt(18.25))
    assert estimate.win_probability == pytest.approx(0.4)
    assert estimate.loss_q10 == -20.0
    assert estimate.diagnostics["missing_probability_mass"] == 0.0
    assert estimate.diagnostics["loss_probability"] == pytest.approx(0.4)
    assert estimate.diagnostics["zero_probability"] == pytest.approx(0.2)
    assert estimate.diagnostics["quantile_method"] == "weighted_empirical_inverse_cdf"
    assert "heuristic" in estimate.diagnostics["standard_error_method"]
    assert payoff_model_dict(model)["fit_counts"]["training"] == 8
    json.dumps(payoff_model_dict(model), allow_nan=False)


def test_evaluation_joins_by_identity_and_scores_ambiguous_without_dropping_it():
    event1, label1 = pair(10, "up_first")
    event2, label2 = pair(20, "ambiguous")
    one = forecast(event1, dict(zip(OUTCOMES, (0.7, 0.1, 0.1, 0.1))))
    two = forecast(event2, dict(zip(OUTCOMES, (0.1, 0.1, 0.2, 0.6))))
    result = evaluate_predictions([one, two], [label2, label1])
    assert result["count"] == 2
    assert result["brier"] == pytest.approx(0.17)
    assert result["log_loss"] == pytest.approx(-math.log(0.42) / 2)
    assert result["ece"] == pytest.approx(0.175)
    assert result["class_counts"]["ambiguous"] == 1
    assert len(result["reliability"]["ambiguous"]) == 10
    assert result["label_exclusions"] == {}
    assert result["regime"]["count"] == 2


def test_probability_fallback_is_product_then_scenario_and_sparse_is_explicit():
    training = [pair(1), pair(2), pair(3, "down_first", context="middle")]
    training += [pair(4, "ambiguous", product="DCE.M")]
    model = fit_probability_model(training, [], min_samples=2)
    local = predict_probability(model, pair(100)[0], SCENARIO)
    product = predict_probability(model, pair(100, context="middle")[0], SCENARIO)
    global_ = predict_probability(model, pair(100, product="SHFE.CU")[0], SCENARIO)
    assert local.train_count == 2 and local.diagnostics["fallback_level"] == "conditional"
    assert product.train_count == 3 and product.diagnostics["fallback_level"] == "product_global"
    assert product.probabilities["ambiguous"] == pytest.approx(1 / 7)
    assert global_.train_count == 4 and global_.diagnostics["fallback_level"] == "scenario_global"
    assert global_.probabilities["ambiguous"] == pytest.approx(2 / 8)
    assert predict_probability(model, pair(100)[0], "h20-u1-d1").model_status == "insufficient_training"
    sparse = fit_probability_model(training, [])
    assert predict_probability(sparse, pair(100)[0], SCENARIO).model_status == "insufficient_training"


def test_fit_eligibility_excludes_future_and_invalid_labels_keeps_ambiguous():
    ready = pair(1, "ambiguous")
    event2, label2 = pair(2)
    event3, label3 = pair(3)
    event4, label4 = pair(4)
    rows = [ready, (event2, replace(label2, status="unmatured", outcome=None, terminal_nano=None)),
            (event3, replace(label3, terminal_return=float("nan"))),
            (replace(event4, status="insufficient_history"), label4)]
    model = fit_probability_model(rows, [], min_samples=1)
    result = probability_model_dict(model)
    assert result["fit_counts"]["training"] == 1
    assert result["exclusions"]["training"] == {
        "label_status:unmatured": 1, "nonfinite:terminal_return": 1,
        "event_status:insufficient_history": 1,
    }
    assert result["timestamps"]["training_terminal_max"] == 15
    assert predict_probability(model, pair(100)[0], SCENARIO).probabilities["ambiguous"] == pytest.approx(0.4)


def test_fit_rejects_duplicate_mismatched_and_overlapping_event_identities():
    event, label = pair(1)
    with pytest.raises(ValueError, match="duplicate"):
        fit_probability_model([(event, label)] * 2, [])
    with pytest.raises(ValueError, match="identity"):
        fit_probability_model([(event, replace(label, contract_id="wrong"))], [])
    with pytest.raises(ValueError, match="overlap"):
        fit_probability_model([(event, label)], [(event, replace(label, scenario_id="h20-u1-d1"))])
    alias = replace(event, event_id="alias")
    with pytest.raises(ValueError, match="overlap"):
        fit_probability_model([(event, label)], [(alias, replace(label, event_id="alias"))])
    with pytest.raises(ValueError, match="identity"):
        fit_probability_model([(event, label), (replace(event, context="different"), replace(label, scenario_id="h20-u1-d1"))], [])


@pytest.mark.parametrize("terminal", [1000, 1001])
def test_training_terminal_must_precede_first_calibration_event_globally(terminal):
    event, label = pair(1)
    first_calibration = pair(100, product="DCE.M", scenario="h20-u1-d1")
    with pytest.raises(ValueError, match="strictly before"):
        fit_probability_model([(event, replace(label, terminal_nano=terminal))], [first_calibration])
    immature = (first_calibration[0], replace(first_calibration[1], status="unmatured"))
    with pytest.raises(ValueError, match="strictly before"):
        fit_probability_model([(event, replace(label, terminal_nano=terminal))], [immature])


def test_models_reject_prediction_at_or_before_any_training_terminal():
    event, label = pair(1)
    model = fit_probability_model([(event, label)], [], min_samples=1)
    with pytest.raises(ValueError, match="training terminal"):
        predict_probability(model, replace(pair(100)[0], timestamp_nano=15), SCENARIO)
    row = payoff_pair(1, "up_first", 10)
    payoff = fit_payoff_model([(*row[:2], replace(row[2], terminal_nano=100))], min_samples=1)
    decision = replace(pair(100)[0], timestamp_nano=100)
    with pytest.raises(ValueError, match="training label/action terminals"):
        estimate_payoff(payoff, decision, ACTION, forecast(decision))


def test_scenarios_and_time_magnitude_summaries_are_separate_and_train_only():
    rows = []
    for i, (outcome, change) in enumerate(zip(OUTCOMES, (-0.2, 0.1, 0.3, 0.5)), 1):
        event, label = pair(i, outcome)
        rows.append((event, replace(label, terminal_return=change, terminal_points=change * 100)))
        rows.append((event, replace(label, scenario_id="h20-u1-d1", horizon=20, outcome="down_first")))
    calibration_event, calibration_label = pair(100, "ambiguous")
    cal = [(calibration_event, replace(calibration_label, terminal_return=99.0, terminal_points=9900.0))]
    model = fit_probability_model(rows, cal, min_samples=1)
    decision = pair(200)[0]
    prediction = predict_probability(model, decision, SCENARIO)
    stats = prediction.diagnostics["path_statistics"]
    assert prediction.train_count == 4
    assert stats["terminal_return"]["q10"] == -0.2
    assert stats["terminal_return"]["q50"] == 0.1
    assert stats["terminal_return"]["q90"] == 0.5
    assert stats["first_hit_step_distribution"]["counts"] == {
        "1": 2, "no_hit": 1, "ambiguous": 1, "unknown": 0,
    }
    assert stats["first_hit_step_distribution"]["denominator"] == 4
    assert "not_calibrated" in stats["interpretation"]
    assert predict_probability(model, decision, "h20-u1-d1").probabilities["down_first"] == pytest.approx(5 / 8)
    global_ = predict_probability(model, replace(decision, product="DCE.M"), SCENARIO)
    assert global_.diagnostics["path_statistics"]["terminal_points"]["status"] == "unavailable_cross_product_units"


def test_sparse_payoffs_never_renormalize_or_fill_missing_outcomes_and_never_mix_products():
    rows = [payoff_pair(i + 1, outcome, net) for i, (outcome, net) in
            enumerate(zip(OUTCOMES[:3], (100, -10, 0)))]
    model = fit_payoff_model(rows, min_samples=1)
    event = pair(100)[0]
    estimate = estimate_payoff(model, event, ACTION, forecast(event))
    assert estimate.status == "insufficient_evidence"
    assert estimate.mean_net is estimate.conservative_ev is estimate.win_probability is None
    assert estimate.diagnostics["missing_outcomes"] == ["ambiguous"]
    assert estimate.diagnostics["missing_probability_mass"] == pytest.approx(0.1)
    other = pair(100, product="DCE.M")[0]
    estimate = estimate_payoff(model, other, ACTION, forecast(other))
    assert estimate.count == 0
    assert estimate.diagnostics["missing_probability_mass"] == pytest.approx(1.0)


def test_payoffs_fall_back_per_state_to_same_product_action_distribution():
    rows = [payoff_pair(1, "up_first", 20), payoff_pair(2, "up_first", 40)]
    rows += [payoff_pair(i + 3, outcome, net, context="middle") for i, (outcome, net) in
             enumerate(zip(OUTCOMES, (1000, -10, 0, -20)))]
    model = fit_payoff_model(rows, min_samples=2, penalty=0)
    event = pair(100)[0]
    estimate = estimate_payoff(model, event, ACTION, forecast(event))
    assert estimate.status == "ready"
    assert estimate.mean_net == pytest.approx(7.0)
    assert estimate.diagnostics["fallback_by_outcome"] == {
        "up_first": "conditional", "down_first": "product_action_outcome",
        "neither": "product_action_outcome", "ambiguous": "product_action_outcome",
    }


def test_zero_no_trades_are_full_observations_and_high_win_rate_can_have_negative_ev():
    event = pair(100)[0]
    cash_rows = [payoff_pair(i + 1, outcome, 0, traded=False, action_id="cash") for i, outcome in enumerate(OUTCOMES)]
    cash = estimate_payoff(fit_payoff_model(cash_rows, min_samples=1), event, "cash", forecast(event))
    assert cash.status == "ready" and cash.count == 4
    assert (cash.mean_net, cash.standard_error, cash.conservative_ev, cash.win_probability, cash.loss_q10) == (0, 0, 0, 0, 0)
    assert cash.diagnostics["zero_probability"] == 1.0
    rows = [payoff_pair(i + 1, outcome, net) for i, (outcome, net) in
            enumerate(zip(OUTCOMES, (1, 1, 1, -100)))]
    estimate = estimate_payoff(fit_payoff_model(rows, min_samples=1), event, ACTION, forecast(event))
    assert estimate.win_probability == pytest.approx(0.9)
    assert estimate.mean_net == pytest.approx(-9.1)
    assert estimate.win_probability != forecast(event).probabilities["up_first"]


def test_payoff_fit_rejects_identity_duplicates_and_mixed_scenarios_and_excludes_incomplete():
    row = payoff_pair(1, "up_first", 10)
    with pytest.raises(ValueError, match="duplicate"):
        fit_payoff_model([row, row])
    with pytest.raises(ValueError, match="identity"):
        fit_payoff_model([(*row[:2], replace(row[2], event_id="wrong"))])
    with pytest.raises(ValueError, match="single route scenario"):
        fit_payoff_model([row, payoff_pair(2, "neither", 0, scenario="h20-u1-d1")])
    event = pair(100)[0]
    with pytest.raises(ValueError, match="scenario"):
        estimate_payoff(fit_payoff_model([row]), event, ACTION, replace(forecast(event), scenario_id="h20-u1-d1"))
    incomplete = payoff_pair(2, "neither", 0)
    model = fit_payoff_model([row, (*incomplete[:2], replace(incomplete[2], status="unmatured"))])
    assert payoff_model_dict(model)["exclusions"] == {"action_status:unmatured": 1}


def test_evaluation_reports_exclusions_duplicates_and_invalid_probabilities():
    event, label = pair(10, "ambiguous")
    invalid = replace(pair(20)[1], status="unmatured", terminal_nano=None, outcome=None)
    result = evaluate_predictions([forecast(event), forecast(pair(20)[0])], [label, invalid])
    assert result["count"] == 1
    assert result["eligible_label_count"] == 1
    assert result["label_exclusions"] == {"label_status:unmatured": 1}
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_predictions([forecast(event)] * 2, [label])
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_predictions([forecast(event)], [label] * 2)
    with pytest.raises(ValueError, match="identity"):
        evaluate_predictions([forecast(event, diagnostics={"contract_id": "wrong"})], [label])
    with pytest.raises(ValueError, match="exactly"):
        evaluate_predictions([replace(forecast(event), probabilities={"up_first": 1.0})], [label])
    zero = dict(zip(OUTCOMES, (1.0, 0.0, 0.0, 0.0)))
    assert math.isfinite(evaluate_predictions([forecast(event, zero)], [label])["log_loss"])
    empty = evaluate_predictions([], [])
    assert empty["brier"] is empty["log_loss"] is empty["ece"] is None
    json.dumps(empty, allow_nan=False)


def test_probability_and_payoff_serialization_and_predictions_are_order_deterministic():
    rows = [pair(i + 1, outcome) for i, outcome in enumerate(OUTCOMES)]
    calibration = [pair(100 + i, outcome) for i, outcome in enumerate(OUTCOMES)]
    first = fit_probability_model(rows, calibration, min_samples=1)
    second = fit_probability_model(rows[::-1], calibration[::-1], min_samples=1)
    assert probability_model_dict(first) == probability_model_dict(second)
    event = pair(200)[0]
    assert predict_probability(first, event, SCENARIO) == predict_probability(second, event, SCENARIO)
    payoffs = [payoff_pair(i + 1, outcome, net) for i, (outcome, net) in enumerate(zip(OUTCOMES, (10, -4, 0, -7)))]
    model1, model2 = fit_payoff_model(payoffs, min_samples=1), fit_payoff_model(payoffs[::-1], min_samples=1)
    assert payoff_model_dict(model1) == payoff_model_dict(model2)
    assert estimate_payoff(model1, event, ACTION, forecast(event)) == estimate_payoff(model2, event, ACTION, forecast(event))


def test_unconditional_baseline_uses_raw_training_counts_and_never_calibration_temperature():
    training = [pair(i, "up_first") for i in range(1, 5)]
    training += [pair(i, "down_first", context="middle") for i in range(5, 9)]
    calibration = [pair(100 + i, "down_first") for i in range(4)]
    model = fit_probability_model(training, calibration, min_samples=4)
    event = pair(200)[0]
    conditioned = predict_probability(model, event, SCENARIO)
    baseline = predict_unconditional_probability(model, event, SCENARIO)
    assert conditioned.temperature == 4.0
    assert baseline.temperature == 1.0
    assert baseline.model_status == "uncalibrated" and baseline.calibration_count == 0
    assert baseline.train_count == 8
    assert baseline.probabilities == pytest.approx(dict(zip(OUTCOMES, (5 / 12, 5 / 12, 1 / 12, 1 / 12))))
    assert baseline.diagnostics["baseline"] == "unconditional_training_laplace"
    assert baseline.diagnostics["path_statistics"]["terminal_points"]["status"] == "unavailable_cross_product_units"
    assert baseline.probabilities != conditioned.probabilities
    with pytest.raises(ValueError, match="training terminal"):
        predict_unconditional_probability(model, replace(event, timestamp_nano=85), SCENARIO)


def test_calibrated_prediction_cannot_use_a_future_calibration_terminal():
    model = fit_probability_model([pair(1)], [pair(100)], min_samples=1)
    with pytest.raises(ValueError, match="calibration terminal"):
        predict_probability(model, pair(99)[0], SCENARIO)
    with pytest.raises(ValueError, match="calibration terminal"):
        predict_probability(model, replace(pair(101)[0], timestamp_nano=1005), SCENARIO)
    assert predict_probability(model, pair(101)[0], SCENARIO).model_status == "calibrated"
    # This helper ignores all calibration outcomes and therefore is causal here.
    baseline = predict_unconditional_probability(model, pair(99)[0], SCENARIO)
    assert baseline.temperature == 1 and baseline.calibration_count == 0


def test_payoff_fit_excludes_nonfinite_or_inconsistent_accounting_and_nonzero_no_trade():
    rows = [payoff_pair(i + 1, outcome, 10) for i, outcome in enumerate(OUTCOMES)]
    rows[0] = (*rows[0][:2], replace(rows[0][2], net_pnl=Decimal("NaN")))
    rows[1] = (*rows[1][:2], replace(rows[1][2], net_pnl=Decimal("999")))
    rows[2] = (*rows[2][:2], replace(rows[2][2], traded=False))
    rows[3] = (rows[3][0], replace(rows[3][1], status="unmatured"), rows[3][2])
    model = fit_payoff_model(rows, min_samples=1)
    assert payoff_model_dict(model)["fit_counts"]["training"] == 0
    assert payoff_model_dict(model)["exclusions"] == {
        "nonfinite_action:net_pnl": 1, "inconsistent_action_accounting": 1,
        "nonzero_no_trade_action": 1, "label_status:unmatured": 1,
    }


def test_payoff_accounting_uses_episode_precision_independent_of_caller_context():
    fees = Decimal("0.123456789012345678901234567890123456789")
    net = Decimal("0.876543210987654321098765432109876543211")
    rows = [payoff_pair(i + 1, outcome, net) for i, outcome in enumerate(OUTCOMES)]
    rows = [(event, label, replace(action, gross_pnl=Decimal(1), fees=fees))
            for event, label, action in rows]
    decision = pair(100)[0]
    prediction = forecast(decision)
    with localcontext(Context(prec=128)):
        reference = fit_payoff_model(rows, min_samples=1)
        expected = estimate_payoff(reference, decision, ACTION, prediction)
    assert reference.fit_counts["training"] == 4
    assert expected.status == "ready"
    for precision in (28, 6):
        with localcontext(Context(prec=precision, rounding=ROUND_UP)) as caller:
            caller.traps[Inexact] = True
            caller.traps[Rounded] = True
            model = fit_payoff_model(rows, min_samples=1)
            estimate = estimate_payoff(model, decision, ACTION, prediction)
            assert model.fit_counts["training"] == 4
            assert model.exclusions == {}
            assert payoff_model_dict(model) == payoff_model_dict(reference)
            assert estimate == expected
            assert caller.prec == precision and caller.rounding == ROUND_UP
            assert not any(caller.flags.values())


def test_missing_future_regimes_are_reported_without_discarding_valid_path_outcomes():
    event, label = pair(1, "ambiguous", future_regime=None)
    model = fit_probability_model([(event, label)], [], min_samples=1)
    prediction = predict_probability(model, pair(100)[0], SCENARIO)
    assert prediction.train_count == 1
    assert prediction.model_status == "uncalibrated"
    assert prediction.diagnostics["regime_model_status"] == "insufficient_training"
    assert prediction.diagnostics["regime_train_count"] == 0
    assert probability_model_dict(model)["exclusions"]["training_regime"] == {"invalid_terminal_regime": 1}


@pytest.mark.parametrize("kwargs", [{"min_samples": 0}, {"min_samples": True}, {"smoothing": 0}, {"smoothing": float("nan")}])
def test_probability_invalid_configuration_rejected(kwargs):
    with pytest.raises(ValueError):
        fit_probability_model([], [], **kwargs)
