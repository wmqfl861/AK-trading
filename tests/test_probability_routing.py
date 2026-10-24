from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
import random

import pytest

from ak_trading.probability.contracts import ActionOutcome, FactorRow, PayoffEstimate, ProbabilityPrediction
from ak_trading.probability.routing import CASH_ACTION_ID, aggregate_selected, choose_strategy, select_nonoverlapping


def prediction(event_id: str = "e1", **changes: object) -> ProbabilityPrediction:
    baseline = ProbabilityPrediction(
        event_id=event_id,
        scenario_id="h5-u1-d1",
        probabilities={"up_first": 0.5, "down_first": 0.2, "neither": 0.2, "ambiguous": 0.1},
        regime_probabilities={"uptrend": 0.5, "downtrend": 0.2, "range": 0.3},
        train_count=100,
        calibration_count=50,
        model_status="calibrated",
        bucket="range",
        temperature=1.0,
    )
    return replace(baseline, **changes)


def estimate(action_id: str = "breakout", event_id: str = "e1", **changes: object) -> PayoffEstimate:
    baseline = PayoffEstimate(
        event_id=event_id,
        action_id=action_id,
        count=100,
        mean_net=12.0,
        standard_error=1.0,
        conservative_ev=10.0,
        win_probability=0.6,
        loss_q10=-20.0,
        status="ready",
        bucket="range",
    )
    return replace(baseline, **changes)


def test_best_ev_uses_positive_conservative_ev_and_stable_action_id_ties():
    estimates = [
        estimate("z-high", conservative_ev=20.0, win_probability=0.6),
        estimate("a-high", conservative_ev=20.0, win_probability=0.55),
        estimate("frequent-small-wins", mean_net=-10.0, conservative_ev=-12.0, win_probability=0.99),
        estimate("insufficient", count=29, conservative_ev=50.0),
    ]
    chosen = choose_strategy("e1", "best_ev", estimates, prediction())
    assert chosen.action_id == "a-high"
    assert chosen.predicted_ev == 20.0
    assert chosen.prediction_confidence == 0.55
    assert chosen.random_u is None
    assert chosen.weights == {
        "a-high": 1.0, "frequent-small-wins": 0.0, "insufficient": 0.0, "z-high": 0.0, "cash": 0.0,
    }
    assert choose_strategy("e1", "best_ev", estimates[::-1], prediction()) == chosen
    assert CASH_ACTION_ID == "cash"


def test_probability_sample_weights_include_win_probability_ev_and_cash():
    # Risk mass is 0.8 * 0.9 = 0.72. Scores are 10*0.8 = 8 and 20*0.6 = 12.
    estimates = [
        estimate("breakout", conservative_ev=11.0, win_probability=0.8),
        estimate("grid", conservative_ev=21.0, win_probability=0.6),
        estimate("negative", conservative_ev=-1.0, win_probability=0.99),
    ]
    chosen = choose_strategy("e1", "probability_sample", estimates, prediction(), min_ev=1.0)
    assert chosen.weights == pytest.approx({"breakout": 0.288, "grid": 0.432, "negative": 0.0, "cash": 0.28})
    assert sum(chosen.weights.values()) == pytest.approx(1.0)
    assert all(0.0 <= weight <= 1.0 for weight in chosen.weights.values())
    assert chosen.random_u is not None and 0.0 <= chosen.random_u < 1.0
    assert "heuristic" in chosen.reason


@pytest.mark.parametrize("mode", ["fixed", "cash"])
def test_benchmarks_bypass_payoff_and_prediction_gates(mode):
    estimates = [estimate(count=0, conservative_ev=-1000.0, win_probability=0.01, status="insufficient_evidence")]
    uncertain = prediction(model_status="insufficient_training", train_count=0, probabilities={})
    chosen = choose_strategy("e1", mode, estimates, uncertain, fixed_action_id="breakout")
    expected_id = "breakout" if mode == "fixed" else "cash"
    assert chosen.action_id == expected_id
    assert chosen.weights[expected_id] == 1.0
    assert chosen.random_u is None
    assert chosen.prediction_confidence is None
    if mode == "fixed":
        assert chosen.predicted_ev == -1000.0
    assert choose_strategy("e1", mode, [], uncertain, fixed_action_id="breakout").action_id == expected_id


@pytest.mark.parametrize("mode", ["best_ev", "probability_sample"])
@pytest.mark.parametrize(
    "estimates, prediction_changes, kwargs, reason",
    [
        ([estimate(count=29)], {}, {}, "insufficient_payoff_samples"),
        ([estimate(status="insufficient_evidence")], {}, {}, "payoff_not_ready"),
        ([estimate(win_probability=0.49)], {}, {}, "win_probability_below_threshold"),
        ([estimate(conservative_ev=0.0)], {}, {"min_ev": -5.0}, "non_positive_conservative_ev"),
        ([estimate(conservative_ev=10.0)], {}, {"min_ev": 10.0}, "conservative_ev_below_threshold"),
        ([estimate()], {"model_status": "uncalibrated"}, {}, "uncalibrated"),
        ([estimate()], {"model_status": "insufficient_training"}, {"allow_uncalibrated": True}, "insufficient_training"),
        ([estimate()], {"train_count": 29}, {}, "insufficient_prediction_samples"),
        ([estimate()], {"probabilities": {"up_first": 0.3, "down_first": 0.2, "neither": 0.2, "ambiguous": 0.3}}, {}, "ambiguous_probability_above_cap"),
        ([], {}, {}, "no_candidate_estimates"),
    ],
)
def test_probability_modes_abstain_for_ineligible_evidence(mode, estimates, prediction_changes, kwargs, reason):
    chosen = choose_strategy("e1", mode, estimates, prediction(**prediction_changes), **kwargs)
    assert chosen.action_id == "cash"
    assert chosen.weights["cash"] == 1.0
    assert chosen.predicted_ev == 0.0
    assert reason in chosen.reason
    assert (chosen.random_u is not None) == (mode == "probability_sample")


def test_uncalibrated_override_is_explicit_and_sparse_predictions_still_abstain():
    chosen = choose_strategy(
        "e1", "best_ev", [estimate()], prediction(model_status="uncalibrated"), allow_uncalibrated=True,
    )
    assert chosen.action_id == "breakout"
    assert "allow_uncalibrated=True" in chosen.reason
    sparse = choose_strategy(
        "e1", "best_ev", [estimate()], prediction(model_status="uncalibrated", train_count=1), allow_uncalibrated=True,
    )
    assert sparse.action_id == "cash"


def test_seeded_draws_are_event_local_order_independent_and_leave_global_rng_untouched():
    candidates = [estimate("breakout", win_probability=0.8), estimate("grid", conservative_ev=20.0)]
    state = random.getstate()
    first = choose_strategy("e1", "probability_sample", candidates, prediction(), seed=19)
    choose_strategy("future-event", "probability_sample", [], prediction("future-event"), seed=19)
    repeat = choose_strategy("e1", "probability_sample", candidates[::-1], prediction(), seed=19)
    assert repeat == first
    assert random.getstate() == state
    assert choose_strategy("e1", "probability_sample", candidates, prediction(), seed=20).random_u != first.random_u
    other = choose_strategy("e2", "probability_sample", [replace(e, event_id="e2") for e in candidates], prediction("e2"), seed=19)
    assert other.random_u != first.random_u


def test_sampling_follows_documented_distribution_without_claiming_better_ev():
    observed = {"breakout": 0, "grid": 0, "cash": 0}
    for index in range(2000):
        event_id = f"event-{index}"
        chosen = choose_strategy(
            event_id, "probability_sample",
            [estimate("breakout", event_id, conservative_ev=11.0, win_probability=0.8),
             estimate("grid", event_id, conservative_ev=21.0, win_probability=0.6)],
            prediction(event_id), min_ev=1.0,
        )
        observed[chosen.action_id] += 1
    assert observed["breakout"] / 2000 == pytest.approx(0.288, abs=0.04)
    assert observed["grid"] / 2000 == pytest.approx(0.432, abs=0.04)
    assert observed["cash"] / 2000 == pytest.approx(0.28, abs=0.04)
    # Hand-computed expected conservative EV = .288*11 + .432*21 = 12.24,
    # below the best-EV action's 21. Random draws are a comparison policy.
    one = choose_strategy("e1", "probability_sample", [estimate("breakout", conservative_ev=11.0, win_probability=0.8), estimate("grid", conservative_ev=21.0)], prediction(), min_ev=1.0)
    assert one.weights["breakout"] * 11 + one.weights["grid"] * 21 == pytest.approx(12.24)


@pytest.mark.parametrize("kwargs", [
    {"min_win_probability": float("nan")}, {"min_win_probability": 1.01},
    {"min_ev": float("inf")}, {"min_train_samples": 0}, {"min_train_samples": True},
    {"max_ambiguous_probability": -0.1}, {"seed": 1.0}, {"seed": True}, {"allow_uncalibrated": 1},
])
def test_invalid_routing_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        choose_strategy("e1", "best_ev", [estimate()], prediction(), **kwargs)


@pytest.mark.parametrize("changes", [
    {"conservative_ev": float("nan")}, {"mean_net": float("inf")},
    {"win_probability": 1.01}, {"win_probability": -0.01}, {"win_probability": True},
    {"count": -1}, {"count": 3.0}, {"standard_error": -1.0},
    {"loss_q10": float("nan")},
])
def test_invalid_payoff_numbers_are_rejected(changes):
    with pytest.raises(ValueError):
        choose_strategy("e1", "best_ev", [estimate(**changes)], prediction())


@pytest.mark.parametrize("changes", [
    {"probabilities": {"up_first": 0.5, "down_first": 0.2, "neither": 0.2, "ambiguous": 0.2}},
    {"probabilities": {"up_first": 0.5, "down_first": 0.2, "neither": 0.2, "ambiguous": float("nan")}},
    {"probabilities": {}}, {"temperature": 0.0}, {"train_count": True},
])
def test_invalid_prediction_numbers_are_rejected(changes):
    with pytest.raises(ValueError):
        choose_strategy("e1", "best_ev", [estimate()], prediction(**changes))


def test_duplicate_ids_wrong_events_unknown_modes_and_unset_fixed_id_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        choose_strategy("e1", "best_ev", [estimate(), estimate()], prediction())
    with pytest.raises(ValueError, match="event_id"):
        choose_strategy("e1", "best_ev", [estimate(event_id="e2")], prediction())
    with pytest.raises(ValueError, match="event_id"):
        choose_strategy("e1", "best_ev", [estimate()], prediction("e2"))
    with pytest.raises(ValueError, match="mode"):
        choose_strategy("e1", "unknown", [], prediction())
    with pytest.raises(ValueError, match="fixed_action_id"):
        choose_strategy("e1", "fixed", [], prediction())
    with pytest.raises(ValueError, match="cash"):
        choose_strategy("e1", "best_ev", [estimate("cash")], prediction())


def test_extreme_finite_ev_cannot_overflow_sampling_weights():
    chosen = choose_strategy(
        "e1", "probability_sample",
        [estimate("a", conservative_ev=1e308, win_probability=1.0),
         estimate("b", conservative_ev=1e308, win_probability=1.0)],
        prediction(), min_ev=-1e308,
    )
    assert chosen.weights == pytest.approx({"a": 0.45, "b": 0.45, "cash": 0.1})
    json.dumps(chosen.weights, allow_nan=False)


def test_choice_does_not_inspect_future_outcomes_or_prediction_diagnostics():
    class UnreadableFuture(dict):
        def __getitem__(self, key):
            raise AssertionError("future diagnostics accessed")

        def get(self, key, default=None):
            raise AssertionError("future diagnostics accessed")

        def __iter__(self):
            raise AssertionError("future diagnostics accessed")

    initial = choose_strategy("e1", "best_ev", [estimate()], prediction())
    with_future = choose_strategy("e1", "best_ev", [estimate()], prediction(diagnostics=UnreadableFuture(realized_pnl=-999)))
    assert with_future == initial


def event(index: int, contract_id: str = "SHFE.rb2610", **changes: object) -> FactorRow:
    baseline = FactorRow(
        event_id=f"{contract_id}-{index}", contract_id=contract_id, symbol=contract_id, product="rb",
        index=index, timestamp_nano=(index + 1) * 1000, values={}, status="ready", reason="", context="mid", regime="range",
    )
    return replace(baseline, **changes)


def test_schedule_is_shared_sorted_and_nonoverlapping_within_each_contract():
    # Decisions at 0 and 5 cover bars 1..5 and 6..10. Other contracts may overlap.
    events = [event(index, contract) for contract in ("SHFE.rb2610", "SHFE.rb2701") for index in (0, 1, 4, 5, 6, 10)]
    expected = (
        "SHFE.rb2610-0", "SHFE.rb2610-5", "SHFE.rb2610-10",
        "SHFE.rb2701-0", "SHFE.rb2701-5", "SHFE.rb2701-10",
    )
    assert select_nonoverlapping(events[::-1], 5) == expected
    assert select_nonoverlapping(events, 5) == expected
    assert select_nonoverlapping(events + [event(99, status="insufficient_history")], 5) == expected


def test_schedule_handles_sparse_indices_without_compressing_gaps_and_ignores_unready_rows():
    events = [event(0, status="insufficient_history"), event(2), event(4), event(9)]
    assert select_nonoverlapping(events, 5) == ("SHFE.rb2610-2", "SHFE.rb2610-9")
    assert select_nonoverlapping([], 5) == ()


@pytest.mark.parametrize("events", [
    [event(0), event(0)],
    [event(0), event(1, event_id="SHFE.rb2610-0")],
    [event(0), event(0, event_id="duplicate-index")],
    [event(-1)],
    [event(0), event(1, timestamp_nano=500)],
])
def test_schedule_rejects_duplicate_or_incoherent_event_identity(events):
    with pytest.raises(ValueError):
        select_nonoverlapping(events, 5)


@pytest.mark.parametrize("horizon", [0, -1, True, 1.5, 61])
def test_schedule_rejects_invalid_horizon(horizon):
    with pytest.raises(ValueError):
        select_nonoverlapping([event(0)], horizon)


def outcome(row: FactorRow, net: str, *, action_id: str = "breakout", traded: bool = True, **changes: object) -> ActionOutcome:
    baseline = ActionOutcome(
        event_id=row.event_id, action_id=action_id, contract_id=row.contract_id, index=row.index,
        terminal_nano=row.timestamp_nano + 5000, status="complete", reason="terminal_close" if traded else "no_trade",
        traded=traded, direction=1 if traded else 0,
        gross_pnl=Decimal(net) + (Decimal("3") if traded else Decimal("0")),
        fees=Decimal("1") if traded else Decimal("0"), slippage=Decimal("2") if traded else Decimal("0"),
        net_pnl=Decimal(net), max_margin=Decimal("100") if traded else Decimal("0"), ambiguous_bars=0,
        fills=({"lots": 1}, {"lots": 1}) if traded else (),
    )
    return replace(baseline, **changes)


def selection(row: FactorRow, mode: str = "fixed", action_id: str = "breakout"):
    return choose_strategy(
        row.event_id, mode, [estimate(action_id, row.event_id)], prediction(row.event_id), fixed_action_id=action_id,
    )


def test_comparison_keeps_cash_opportunities_and_reports_only_per_contract_currency():
    events = [event(index) for index in (0, 5, 10, 15)] + [event(0, "SHFE.rb2701")]
    results = [outcome(events[0], "10"), outcome(events[1], "-4"), outcome(events[2], "0", traded=False), outcome(events[3], "-3"), outcome(events[4], "100")]
    selections = [selection(row, mode) for row in events for mode in ("fixed", "cash")]
    comparison = aggregate_selected(selections[::-1], results[::-1], events[::-1], horizon=5)
    assert comparison["status"] == "complete"
    assert comparison["opportunity_count"] == 5
    assert comparison["incomplete_count"] == 0
    fixed = comparison["per_mode"]["fixed"]
    assert fixed["event_ids"] == comparison["scheduled_event_ids"]
    assert "total_net" not in fixed
    assert "cumulative_event_pnl" not in fixed
    contract = fixed["per_contract"]["SHFE.rb2610"]
    assert contract["total_net"] == "3"
    assert Decimal(contract["mean_net"]) == Decimal("0.75")
    assert Decimal(contract["mean_trade_net"]) == Decimal("1")
    assert contract["traded_count"] == 3
    assert contract["no_trade_count"] == 1
    assert contract["wins"] == 1 and contract["losses"] == 2
    assert contract["trade_win_rate"] == pytest.approx(1 / 3)
    assert contract["opportunity_win_rate"] == 0.25
    assert contract["participation_rate"] == 0.75
    assert contract["turnover_lots"] == 6
    assert contract["cumulative_event_pnl"] == ["10", "6", "6", "3"]
    assert contract["max_drawdown_cny"] == "7"
    cash = comparison["per_mode"]["cash"]["per_contract"]["SHFE.rb2610"]
    assert cash["total_net"] == "0"
    assert cash["cash_count"] == cash["no_trade_count"] == 4
    assert cash["trade_win_rate"] is None
    assert cash["mean_net"] == "0"
    assert cash["cumulative_event_pnl"] == ["0", "0", "0", "0"]
    json.dumps(comparison, allow_nan=False)


@pytest.mark.parametrize("bad", [
    None, {"status": "unmatured", "reason": "tail"}, {"status": "invalid"},
    {"net_pnl": Decimal("NaN")}, {"net_pnl": Decimal("Infinity")},
    {"contract_id": "wrong-instance"}, {"index": 99}, {"net_pnl": Decimal("20")},
    {"traded": False}, {"fees": Decimal("-1")}, {"terminal_nano": None},
])
def test_missing_or_invalid_selected_outcomes_make_comparison_incomplete_without_dropping_events(bad):
    events = [event(0), event(5)]
    selections = [selection(row, mode) for row in events for mode in ("fixed", "cash")]
    results = [outcome(events[0], "10")]
    if bad is not None:
        results.append(outcome(events[1], "10", **bad))
    comparison = aggregate_selected(selections, results, events, horizon=5)
    assert comparison["status"] == "incomplete"
    assert comparison["incomplete_count"] == 1
    fixed = comparison["per_mode"]["fixed"]
    assert fixed["opportunity_count"] == 2
    assert fixed["complete_count"] == 1 and fixed["incomplete_count"] == 1
    assert fixed["event_ids"] == comparison["per_mode"]["cash"]["event_ids"]
    contract = fixed["per_contract"][events[0].contract_id]
    assert contract["total_net"] is None and contract["mean_net"] is None
    assert contract["trade_win_rate"] is None
    assert contract["max_drawdown_cny"] is None
    assert contract["known_total_net"] == "10"
    assert contract["cumulative_event_pnl"] == ["10", None]
    assert contract["events"][1]["status"] == "incomplete"
    assert contract["events"][1]["reason"]
    assert comparison["per_mode"]["cash"]["status"] == "complete"
    json.dumps(comparison, allow_nan=False)


def test_comparison_rejects_overlapping_selections_instead_of_action_specific_drops():
    events = [event(0), event(1), event(5)]
    with pytest.raises(ValueError, match="schedule"):
        aggregate_selected([selection(row) for row in events], [], events, horizon=5)


def test_comparison_requires_identical_full_schedule_for_each_mode():
    events = [event(0), event(5)]
    with pytest.raises(ValueError, match="same.*schedule"):
        aggregate_selected([selection(events[0]), selection(events[1]), selection(events[0], "cash")], [], events, horizon=5)
    with pytest.raises(ValueError, match="same.*schedule"):
        aggregate_selected([selection(events[0])], [], events, horizon=5)


def test_comparison_rejects_duplicate_selections_and_duplicate_selected_outcomes():
    row = event(0)
    with pytest.raises(ValueError, match="duplicate.*selection"):
        aggregate_selected([selection(row), selection(row)], [], [row], horizon=5)
    with pytest.raises(ValueError, match="duplicate.*outcome"):
        aggregate_selected([selection(row)], [outcome(row, "10"), outcome(row, "20")], [row], horizon=5)


@pytest.mark.parametrize("weights", [
    {"breakout": 0.5, "cash": 0.6}, {"breakout": float("nan"), "cash": 0.0},
    {"breakout": -0.1, "cash": 1.1}, {"cash": 1.0}, {"breakout": 0.0, "cash": 1.0},
])
def test_comparison_rejects_invalid_selection_weights(weights):
    row = event(0)
    invalid = replace(selection(row), weights=weights)
    with pytest.raises(ValueError, match="weight"):
        aggregate_selected([invalid], [outcome(row, "1")], [row], horizon=5)


def test_comparison_filters_unrelated_events_and_unselected_action_outcomes():
    row, unrelated = event(0), event(10)
    base = aggregate_selected([selection(row)], [outcome(row, "10")], [row], horizon=5)
    extra = aggregate_selected(
        [selection(unrelated), selection(row)],
        [outcome(unrelated, "100"), outcome(row, "-999", action_id="grid", status="invalid"), outcome(row, "10")],
        [row], horizon=5,
    )
    assert extra == base


def test_fixed_aliases_and_probability_draws_can_be_aggregated_on_one_schedule():
    row = event(0)
    fixed = replace(selection(row), mode="fixed:breakout")
    sampled = selection(row, "probability_sample")
    comparison = aggregate_selected([fixed, sampled], [outcome(row, "10")], [row], horizon=5)
    assert comparison["status"] == "complete"
    assert set(comparison["per_mode"]) == {"fixed:breakout", "probability_sample"}
    wrong_draw = replace(sampled, action_id="cash" if sampled.action_id != "cash" else "breakout")
    with pytest.raises(ValueError, match="random_u.*disagree"):
        aggregate_selected([wrong_draw], [outcome(row, "10")], [row], horizon=5)


def test_known_outcomes_after_a_gap_do_not_restart_the_cumulative_pnl_curve():
    events = [event(index) for index in (0, 5, 10)]
    selections = [selection(row) for row in events]
    # Missing middle result must not fabricate a drawdown from the two known wins.
    comparison = aggregate_selected(selections, [outcome(events[0], "10"), outcome(events[2], "20")], events, horizon=5)
    metrics = comparison["per_mode"]["fixed"]["per_contract"][events[0].contract_id]
    assert metrics["known_total_net"] == "30"
    assert metrics["total_net"] is None
    assert metrics["cumulative_event_pnl"] == ["10", None, None]
    assert metrics["max_drawdown_cny"] is None


def test_empty_comparison_has_no_nan_or_invented_zero_ev():
    comparison = aggregate_selected([], [], [], horizon=5)
    assert comparison["status"] == "empty"
    assert comparison["scheduled_event_ids"] == []
    assert comparison["per_mode"] == {}
    assert comparison["opportunity_count"] == 0
    json.dumps(comparison, allow_nan=False)
