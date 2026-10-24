"""Pure empirical probability and conditional net-payoff research models.

Only product/context/current-regime categories enter the fit. Future labels are
training targets, never prediction inputs. Sample counts are observation counts;
overlapping windows are dependent and are not independent effective sample sizes.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from decimal import Context, localcontext
import hashlib
import json
import math
from typing import Mapping, Sequence

from .contracts import (
    OUTCOMES, REGIMES, ActionOutcome, FactorRow, PathLabel, PayoffEstimate,
    ProbabilityPrediction,
)

TEMPERATURES = (0.5, 1.0, 2.0, 4.0)
LOGLOSS_EPSILON = 1e-15
DEPENDENCE_CAVEAT = (
    "Overlapping observations can be correlated; counts are not effective sample "
    "sizes. Calibration and descriptive quantiles are not confidence intervals."
)


@dataclass(frozen=True)
class EmpiricalBucket:
    count: int = 0
    outcome_counts: tuple[int, ...] = (0, 0, 0, 0)
    regime_counts: tuple[int, ...] = (0, 0, 0)
    first_hit_counts: tuple[tuple[str, int], ...] = ()
    terminal_returns: tuple[float, ...] = ()
    terminal_points: tuple[float, ...] = ()


@dataclass(frozen=True)
class ScenarioModel:
    conditional: Mapping[str, EmpiricalBucket]
    products: Mapping[str, EmpiricalBucket]
    unconditional: EmpiricalBucket
    temperature: float = 1.0
    regime_temperature: float = 1.0
    calibration_count: int = 0
    regime_calibration_count: int = 0
    calibration_status: str = "insufficient_calibration"
    regime_calibration_status: str = "insufficient_calibration"
    calibration_scores: Mapping[str, float] = field(default_factory=dict)
    regime_calibration_scores: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbabilityModel:
    scenarios: Mapping[str, ScenarioModel]
    min_samples: int
    smoothing: float
    fit_counts: Mapping[str, int]
    exclusions: Mapping[str, Mapping[str, int]]
    timestamps: Mapping[str, int | None]
    training_hash: str
    calibration_hash: str


def _positive_integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _finite(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return False


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _bucket_key(event: FactorRow) -> str:
    return "|".join((event.product, event.context, event.regime))


def _validate_event(event: FactorRow) -> None:
    for name in ("event_id", "contract_id", "symbol", "product", "context", "regime"):
        value = getattr(event, name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"event {name} must be a nonempty string")
    if any("|" in value for value in (event.product, event.context, event.regime)):
        raise ValueError("product/context/regime cannot contain the bucket separator '|'")
    if isinstance(event.index, bool) or not isinstance(event.index, int) or event.index < 0:
        raise ValueError("event index must be a nonnegative integer")
    if isinstance(event.timestamp_nano, bool) or not isinstance(event.timestamp_nano, int):
        raise ValueError("event timestamp_nano must be an integer")


def _validate_join(event: FactorRow, label: PathLabel) -> None:
    _validate_event(event)
    if (event.event_id, event.contract_id, event.index) != (
        label.event_id, label.contract_id, label.index,
    ):
        raise ValueError("event/label identity mismatch (event_id, contract_id, index)")
    if not isinstance(label.scenario_id, str) or not label.scenario_id:
        raise ValueError("scenario_id must be a nonempty string")


def _label_exclusion(label: PathLabel) -> str | None:
    if label.status != "mature":
        return f"label_status:{label.status}"
    if label.outcome not in OUTCOMES:
        return "invalid_outcome"
    if isinstance(label.terminal_nano, bool) or not isinstance(label.terminal_nano, int):
        return "invalid_terminal_nano"
    if isinstance(label.horizon, bool) or not isinstance(label.horizon, int) or not 1 <= label.horizon <= 60:
        return "invalid_horizon"
    for name in ("entry_price", "upper_price", "lower_price", "terminal_return",
                 "terminal_points", "mfe_points", "mae_points"):
        value = getattr(label, name)
        if value is not None and not _finite(value):
            return f"nonfinite:{name}"
    if label.first_hit_step is not None and (
        isinstance(label.first_hit_step, bool) or not isinstance(label.first_hit_step, int)
        or not 1 <= label.first_hit_step <= label.horizon
    ):
        return "invalid_first_hit_step"
    return None


def _pair_exclusion(event: FactorRow, label: PathLabel) -> str | None:
    if event.status != "ready":
        return f"event_status:{event.status}"
    reason = _label_exclusion(label)
    if reason:
        return reason
    if label.terminal_nano <= event.timestamp_nano:
        return "terminal_not_after_event"
    return None


def _event_signature(event: FactorRow) -> tuple:
    return (event.contract_id, event.index, event.timestamp_nano, event.product,
            event.symbol, event.context, event.regime, event.status)


def _register_event(event: FactorRow, by_id: dict, by_location: dict) -> None:
    signature = _event_signature(event)
    if event.event_id in by_id and by_id[event.event_id] != signature:
        raise ValueError("conflicting event identity across records")
    location = (event.contract_id, event.index)
    if location in by_location and by_location[location] != event.event_id:
        raise ValueError("conflicting event identity at contract/index")
    by_id[event.event_id] = signature
    by_location[location] = event.event_id


def _prepare_pairs(rows: Sequence[tuple[FactorRow, PathLabel]]) -> tuple[list, dict, dict, dict]:
    seen: set[tuple[str, str]] = set()
    by_id: dict = {}
    by_location: dict = {}
    eligible: list[tuple[FactorRow, PathLabel]] = []
    excluded: Counter = Counter()
    for event, label in rows:
        _validate_join(event, label)
        _register_event(event, by_id, by_location)
        key = (event.event_id, label.scenario_id)
        if key in seen:
            raise ValueError("duplicate event/scenario")
        seen.add(key)
        reason = _pair_exclusion(event, label)
        if reason:
            excluded[reason] += 1
        else:
            eligible.append((event, label))
    eligible.sort(key=lambda row: (row[0].timestamp_nano, row[0].event_id, row[1].scenario_id))
    return eligible, dict(sorted(excluded.items())), by_id, by_location


def _pair_fingerprint(rows: Sequence[tuple[FactorRow, PathLabel]]) -> list:
    return [
        [event.event_id, *_event_signature(event), asdict(label)]
        for event, label in rows
    ]


def _bounds(training: Sequence[tuple[FactorRow, PathLabel]],
            calibration: Sequence[tuple[FactorRow, PathLabel]]) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for name, rows in (("training", training), ("calibration", calibration)):
        events = [event.timestamp_nano for event, _ in rows]
        terminals = [label.terminal_nano for _, label in rows]
        result[f"{name}_event_min"] = min(events, default=None)
        result[f"{name}_event_max"] = max(events, default=None)
        result[f"{name}_terminal_max"] = max(terminals, default=None)
    return result


def _make_bucket(labels: Sequence[PathLabel]) -> EmpiricalBucket:
    outcomes = Counter(label.outcome for label in labels)
    regimes = Counter(label.terminal_regime for label in labels)
    hit_counts: Counter = Counter()
    for label in labels:
        if label.outcome == "ambiguous":
            hit_counts["ambiguous"] += 1
        elif label.outcome == "neither":
            hit_counts["no_hit"] += 1
        elif label.first_hit_step is None:
            hit_counts["unknown"] += 1
        else:
            hit_counts[str(label.first_hit_step)] += 1
    return EmpiricalBucket(
        len(labels), tuple(outcomes[value] for value in OUTCOMES),
        tuple(regimes[value] for value in REGIMES), tuple(sorted(hit_counts.items())),
        tuple(sorted(float(label.terminal_return) for label in labels if label.terminal_return is not None)),
        tuple(sorted(float(label.terminal_points) for label in labels if label.terminal_points is not None)),
    )


def _select_bucket(scenario: ScenarioModel | None, event: FactorRow,
                   min_samples: int) -> tuple[EmpiricalBucket, str, str]:
    if scenario is None:
        return EmpiricalBucket(), "*", "scenario_global"
    candidates = (
        (scenario.conditional.get(_bucket_key(event), EmpiricalBucket()), _bucket_key(event), "conditional"),
        (scenario.products.get(event.product, EmpiricalBucket()), event.product, "product_global"),
        (scenario.unconditional, "*", "scenario_global"),
    )
    for candidate in candidates:
        if candidate[0].count >= min_samples:
            return candidate
    return candidates[-1]


def _probabilities(counts: tuple[int, ...], classes: tuple[str, ...],
                   smoothing: float, temperature: float = 1.0) -> dict[str, float]:
    # Log space also handles finite but very large alpha without sum overflow.
    logs = [math.log(count + smoothing) / temperature for count in counts]
    maximum = max(logs)
    weights = [max(math.exp(value - maximum), 1e-300) for value in logs]
    total = math.fsum(weights)
    return dict(zip(classes, (weight / total for weight in weights)))


def _quantiles(values: tuple[float, ...]) -> dict[str, object]:
    # Empirical inverse CDF (nearest rank), with no interpolation or normality.
    return {
        "count": len(values), "status": "ready" if values else "unavailable",
        **{name: values[max(0, math.ceil(q * len(values)) - 1)] if values else None
           for name, q in (("q10", 0.1), ("q50", 0.5), ("q90", 0.9))},
    }


def _path_statistics(bucket: EmpiricalBucket, level: str) -> dict[str, object]:
    hits = {"no_hit": 0, "ambiguous": 0, "unknown": 0, **dict(bucket.first_hit_counts)}
    points = (_quantiles(bucket.terminal_points) if level != "scenario_global" else
              {"count": 0, "status": "unavailable_cross_product_units",
               "q10": None, "q50": None, "q90": None})
    return {
        "count": bucket.count, "status": "ready" if bucket.count else "insufficient_training",
        "source": "training_only", "interpretation": "descriptive_not_calibrated_prediction_intervals",
        "quantile_method": "empirical_inverse_cdf",
        "first_hit_step_distribution": {
            "denominator": bucket.count, "counts": hits,
            "probabilities": {key: count / bucket.count if bucket.count else None for key, count in hits.items()},
        },
        "terminal_return": {**_quantiles(bucket.terminal_returns), "unit": "fraction"},
        "terminal_points": {**points, "unit": "product_price_points"},
    }


def _choose_temperature(samples: Sequence[tuple[tuple[int, ...], str]],
                        classes: tuple[str, ...], min_samples: int,
                        smoothing: float) -> tuple[float, str, dict[str, float]]:
    if len(samples) < min_samples:
        return 1.0, "insufficient_calibration", {}
    scores = {
        str(temperature): math.fsum(
            -math.log(max(LOGLOSS_EPSILON, _probabilities(counts, classes, smoothing, temperature)[target]))
            for counts, target in samples
        ) / len(samples)
        for temperature in TEMPERATURES
    }
    best = min(scores.values())
    # Flat likelihoods must not arbitrarily sharpen a uniform distribution.
    temperature = min(
        (value for value in TEMPERATURES if math.isclose(scores[str(value)], best, rel_tol=0.0, abs_tol=1e-12)),
        key=lambda value: (abs(math.log(value)), value),
    )
    return temperature, "calibrated", scores


def _calibrate(scenario: ScenarioModel, rows: Sequence[tuple[FactorRow, PathLabel]],
               min_samples: int, smoothing: float) -> ScenarioModel:
    outcomes: list = []
    regimes: list = []
    for event, label in rows:
        bucket, _, _ = _select_bucket(scenario, event, min_samples)
        if bucket.count >= min_samples:
            outcomes.append((bucket.outcome_counts, label.outcome))
        if sum(bucket.regime_counts) >= min_samples and label.terminal_regime in REGIMES:
            regimes.append((bucket.regime_counts, label.terminal_regime))
    temperature, status, scores = _choose_temperature(outcomes, OUTCOMES, min_samples, smoothing)
    regime_temperature, regime_status, regime_scores = _choose_temperature(regimes, REGIMES, min_samples, smoothing)
    return replace(
        scenario, temperature=temperature, regime_temperature=regime_temperature,
        calibration_count=len(outcomes), regime_calibration_count=len(regimes),
        calibration_status=status, regime_calibration_status=regime_status,
        calibration_scores=scores, regime_calibration_scores=regime_scores,
    )


def fit_probability_model(
    training: Sequence[tuple[FactorRow, PathLabel]],
    calibration: Sequence[tuple[FactorRow, PathLabel]], *,
    min_samples: int = 30, smoothing: float = 1.0,
) -> ProbabilityModel:
    """Fit categorical counts, with globally later disjoint calibration events."""
    _positive_integer(min_samples, "min_samples")
    if not _finite(smoothing) or smoothing <= 0:
        raise ValueError("smoothing must be finite and positive")
    train, train_excluded, train_ids, train_locations = _prepare_pairs(training)
    cal, cal_excluded, cal_ids, cal_locations = _prepare_pairs(calibration)
    if train_ids.keys() & cal_ids.keys() or train_locations.keys() & cal_locations.keys():
        raise ValueError("training/calibration event identity overlap")
    first_cal = min((event.timestamp_nano for event, _ in calibration), default=None)
    last_train = max((label.terminal_nano for _, label in train), default=None)
    if first_cal is not None and last_train is not None and last_train >= first_cal:
        raise ValueError("training terminal must be strictly before first calibration event globally")
    grouped: dict[str, list] = defaultdict(list)
    for event, label in train:
        grouped[label.scenario_id].append((event, label))
    scenarios = {}
    for scenario_id in sorted({label.scenario_id for _, label in (*training, *calibration)}):
        conditional: dict[str, list] = defaultdict(list)
        products: dict[str, list] = defaultdict(list)
        labels = []
        for event, label in grouped[scenario_id]:
            conditional[_bucket_key(event)].append(label)
            products[event.product].append(label)
            labels.append(label)
        scenarios[scenario_id] = _calibrate(
            ScenarioModel(
                {key: _make_bucket(value) for key, value in sorted(conditional.items())},
                {key: _make_bucket(value) for key, value in sorted(products.items())},
                _make_bucket(labels),
            ),
            [(event, label) for event, label in cal if label.scenario_id == scenario_id],
            min_samples, float(smoothing),
        )
    return ProbabilityModel(
        scenarios, min_samples, float(smoothing),
        {"training_input": len(training), "training": len(train),
         "calibration_input": len(calibration), "calibration": len(cal)},
        {"training": train_excluded, "calibration": cal_excluded,
         "training_regime": dict(Counter("invalid_terminal_regime" for _, label in train if label.terminal_regime not in REGIMES)),
         "calibration_regime": dict(Counter("invalid_terminal_regime" for _, label in cal if label.terminal_regime not in REGIMES))},
        _bounds(train, cal), _hash(_pair_fingerprint(train)), _hash(_pair_fingerprint(cal)),
    )


def predict_probability(model: ProbabilityModel, event: FactorRow,
                        scenario_id: str) -> ProbabilityPrediction:
    """Predict using frozen training categories, never a future path label."""
    _validate_event(event)
    latest = model.timestamps["training_terminal_max"]
    if latest is not None and event.timestamp_nano <= latest:
        raise ValueError("prediction event must be strictly after training terminals")
    calibration_latest = model.timestamps["calibration_terminal_max"]
    if calibration_latest is not None and event.timestamp_nano <= calibration_latest:
        raise ValueError("prediction event must be strictly after calibration terminals")
    scenario = model.scenarios.get(scenario_id)
    bucket, key, level = _select_bucket(scenario, event, model.min_samples)
    temperature = scenario.temperature if scenario else 1.0
    regime_temperature = scenario.regime_temperature if scenario else 1.0
    calibration_status = scenario.calibration_status if scenario else "insufficient_calibration"
    status = "calibrated" if calibration_status == "calibrated" else "uncalibrated"
    if bucket.count < model.min_samples or event.status != "ready":
        status = "insufficient_training"
    regime_status = ("calibrated" if scenario and scenario.regime_calibration_status == "calibrated" else "uncalibrated")
    if sum(bucket.regime_counts) < model.min_samples or event.status != "ready":
        regime_status = "insufficient_training"
    return ProbabilityPrediction(
        event.event_id, scenario_id,
        _probabilities(bucket.outcome_counts, OUTCOMES, model.smoothing, temperature),
        _probabilities(bucket.regime_counts, REGIMES, model.smoothing, regime_temperature),
        bucket.count, scenario.calibration_count if scenario else 0, status, key, temperature,
        {
            "fallback_level": level, "calibration_status": calibration_status,
            "regime_temperature": regime_temperature, "regime_model_status": regime_status,
            "regime_calibration_status": scenario.regime_calibration_status if scenario else "insufficient_calibration",
            "regime_train_count": sum(bucket.regime_counts),
            "regime_calibration_count": scenario.regime_calibration_count if scenario else 0,
            "outcome_counts": dict(zip(OUTCOMES, bucket.outcome_counts)),
            "regime_counts": dict(zip(REGIMES, bucket.regime_counts)),
            "contract_id": event.contract_id, "index": event.index,
            "timestamp_nano": event.timestamp_nano, "product": event.product,
            "training_terminal_max": latest, "calibration_terminal_max": calibration_latest,
            "path_statistics": _path_statistics(bucket, level),
            "caveat": DEPENDENCE_CAVEAT,
        },
    )


def predict_unconditional_probability(model: ProbabilityModel, event: FactorRow,
                                      scenario_id: str) -> ProbabilityPrediction:
    """Raw training scenario-global Laplace baseline; no calibration is applied."""
    scenario = model.scenarios.get(scenario_id)
    if scenario is not None:
        scenario = replace(
            scenario, conditional={}, products={}, temperature=1.0, regime_temperature=1.0,
            calibration_count=0, regime_calibration_count=0,
            calibration_status="not_applied_unconditional_baseline",
            regime_calibration_status="not_applied_unconditional_baseline",
            calibration_scores={}, regime_calibration_scores={},
        )
    baseline_model = replace(
        model, scenarios={scenario_id: scenario} if scenario else {},
        timestamps={**model.timestamps, "calibration_event_min": None,
                    "calibration_event_max": None, "calibration_terminal_max": None},
    )
    prediction = predict_probability(baseline_model, event, scenario_id)
    return replace(prediction, diagnostics={**prediction.diagnostics, "baseline": "unconditional_training_laplace"})


def probability_model_dict(model: ProbabilityModel) -> dict:
    """Return deterministic JSON-safe counts, calibration decisions and provenance."""
    result = {
        "version": "conditional_empirical_v1", **asdict(model),
        "outcomes": list(OUTCOMES), "regimes": list(REGIMES),
        "temperature_candidates": list(TEMPERATURES),
        "calibration_method": "separate_outcome_and_regime_multiclass_logloss",
        "count_source": "training_only_no_calibration_refit",
        "bucket_order": ["product|context|regime", "product", "scenario_global"],
        "caveat": DEPENDENCE_CAVEAT,
    }
    result["model_hash"] = _hash(result)
    return result


def _validate_distribution(probabilities: Mapping[str, float], classes: tuple[str, ...]) -> None:
    if set(probabilities) != set(classes):
        raise ValueError("probability distribution must contain exactly the declared classes")
    if any(not _finite(value) or not 0 <= value <= 1 for value in probabilities.values()):
        raise ValueError("probabilities must be finite and in [0, 1]")
    if not math.isclose(math.fsum(probabilities.values()), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("probabilities must sum to one; they are never silently renormalized")


def _classification_metrics(rows: Sequence[tuple[Mapping[str, float], str]],
                            classes: tuple[str, ...]) -> dict:
    count = len(rows)
    brier = math.fsum(math.fsum((probabilities[name] - (name == target)) ** 2 for name in classes)
                      for probabilities, target in rows) / count if count else None
    log_loss = math.fsum(-math.log(max(LOGLOSS_EPSILON, min(1.0, probabilities[target])))
                        for probabilities, target in rows) / count if count else None
    reliability: dict = {}
    ece_per_class = {}
    for name in classes:
        bins: list[list] = [[] for _ in range(10)]
        for probabilities, target in rows:
            p = probabilities[name]
            bins[min(9, int(p * 10))].append((p, int(target == name)))
        summaries = []
        weighted_errors = []
        for i, samples in enumerate(bins):
            n = len(samples)
            mean_prediction = math.fsum(p for p, _ in samples) / n if n else None
            observed_frequency = sum(y for _, y in samples) / n if n else None
            summaries.append({
                "lower": i / 10, "upper": (i + 1) / 10, "count": n,
                "mean_prediction": mean_prediction, "observed_frequency": observed_frequency,
            })
            if n:
                weighted_errors.append(n / count * abs(mean_prediction - observed_frequency))
        reliability[name] = summaries
        ece_per_class[name] = math.fsum(weighted_errors) if count else None
    return {
        "count": count, "brier": brier, "log_loss": log_loss,
        "ece": math.fsum(ece_per_class.values()) / len(classes) if count else None,
        "ece_per_class": ece_per_class, "reliability": reliability,
        "class_counts": {name: sum(target == name for _, target in rows) for name in classes},
        "brier_definition": "mean_sum_over_classes_squared_error",
        "ece_definition": "macro_mean_one_vs_class_10_equal_width_bins",
        "bin_convention": "left_closed_right_open_last_includes_one",
        "log_loss_clip": LOGLOSS_EPSILON,
    }


def evaluate_predictions(predictions: Sequence[ProbabilityPrediction],
                         labels: Sequence[PathLabel]) -> dict:
    """Score exact event/scenario joins; unpaired rows are reported, never zipped.

    Identity collisions are errors. Immature/invalid labels are excluded with
    reason counts; ambiguous is a fully scored fourth class. The caller supplies
    an unconditional baseline separately for an apples-to-apples comparison.
    """
    label_by_key: dict = {}
    exclusions: Counter = Counter()
    for label in labels:
        key = (label.event_id, label.scenario_id)
        if key in label_by_key:
            raise ValueError("duplicate label event/scenario")
        label_by_key[key] = label
        reason = _label_exclusion(label)
        if reason:
            exclusions[reason] += 1
    seen = set()
    outcomes: list = []
    regimes: list = []
    per_scenario: dict[str, list] = defaultdict(list)
    prediction_exclusions: Counter = Counter()
    regime_exclusions: Counter = Counter()
    statuses: Counter = Counter()
    for prediction in predictions:
        key = (prediction.event_id, prediction.scenario_id)
        if key in seen:
            raise ValueError("duplicate prediction event/scenario")
        seen.add(key)
        _validate_distribution(prediction.probabilities, OUTCOMES)
        _validate_distribution(prediction.regime_probabilities, REGIMES)
        label = label_by_key.get(key)
        if label is None:
            prediction_exclusions["missing_label"] += 1
            continue
        for name in ("contract_id", "index"):
            if name in prediction.diagnostics and prediction.diagnostics[name] != getattr(label, name):
                raise ValueError(f"prediction/label identity mismatch: {name}")
        if _label_exclusion(label):
            prediction_exclusions["ineligible_label"] += 1
            continue
        timestamp = prediction.diagnostics.get("timestamp_nano")
        if timestamp is not None and label.terminal_nano <= timestamp:
            prediction_exclusions["terminal_not_after_event"] += 1
            continue
        row = (prediction.probabilities, label.outcome)
        outcomes.append(row)
        per_scenario[label.scenario_id].append(row)
        statuses[prediction.model_status] += 1
        if label.terminal_regime in REGIMES:
            regimes.append((prediction.regime_probabilities, label.terminal_regime))
        else:
            regime_exclusions["invalid_terminal_regime"] += 1
    return {
        **_classification_metrics(outcomes, OUTCOMES),
        "regime": {**_classification_metrics(regimes, REGIMES), "exclusions": dict(regime_exclusions)},
        "by_scenario": {name: _classification_metrics(rows, OUTCOMES) for name, rows in sorted(per_scenario.items())},
        "prediction_count": len(predictions), "label_count": len(labels),
        "eligible_label_count": len(labels) - sum(exclusions.values()),
        "label_exclusions": dict(sorted(exclusions.items())),
        "prediction_exclusions": dict(sorted(prediction_exclusions.items())),
        "unmatched_label_count": len(label_by_key.keys() - seen),
        "model_status_counts": dict(sorted(statuses.items())),
        "caveat": DEPENDENCE_CAVEAT,
    }


@dataclass(frozen=True)
class PayoffDistribution:
    count: int
    mean: float
    variance: float
    win_count: int
    loss_count: int
    zero_count: int
    q10: float
    samples: tuple[float, ...]


@dataclass(frozen=True)
class PayoffModel:
    scenario_id: str | None
    conditional: Mapping[str, Mapping[str, Mapping[str, PayoffDistribution]]]
    products: Mapping[str, Mapping[str, Mapping[str, PayoffDistribution]]]
    min_samples: int
    penalty: float
    fit_counts: Mapping[str, int]
    exclusions: Mapping[str, int]
    timestamps: Mapping[str, int | None]
    training_hash: str


PAYOFF_CAVEAT = (
    "Standard error sqrt(sum(p_state^2 * sample_variance_state / n_state)) is an "
    "independent-observation heuristic. Overlapping labels may be correlated; "
    "it ignores probability/calibration uncertainty and is not a normal confidence "
    "bound. A singleton has zero empirical variance, not proven certainty."
)


def _payoff_distribution(samples: Sequence[float]) -> PayoffDistribution:
    values = tuple(sorted(samples))
    count = len(values)
    mean = math.fsum(value / count for value in values)
    variance = math.fsum((value - mean) ** 2 / (count - 1) for value in values) if count > 1 else 0.0
    if not _finite(mean) or not _finite(variance):
        raise ValueError("payoff distribution overflows finite arithmetic")
    return PayoffDistribution(
        count, mean, variance, sum(value > 0 for value in values),
        sum(value < 0 for value in values), sum(value == 0 for value in values),
        values[max(0, math.ceil(0.1 * count) - 1)], values,
    )


def _action_exclusion(event: FactorRow, action: ActionOutcome) -> str | None:
    if action.status != "complete":
        return f"action_status:{action.status}"
    if isinstance(action.terminal_nano, bool) or not isinstance(action.terminal_nano, int):
        return "invalid_action_terminal_nano"
    if action.terminal_nano <= event.timestamp_nano:
        return "action_terminal_not_after_event"
    for name in ("gross_pnl", "fees", "slippage", "net_pnl", "max_margin"):
        if not _finite(getattr(action, name)):
            return f"nonfinite_action:{name}"
    if not action.traded and any(getattr(action, name) != 0 for name in (
        "gross_pnl", "fees", "slippage", "net_pnl", "max_margin",
    )):
        return "nonzero_no_trade_action"
    if action.fees < 0 or action.slippage < 0 or action.max_margin < 0:
        return "negative_action_cost_or_margin"
    # Match episode accounting without inheriting caller precision or traps.
    with localcontext(Context(prec=128)):
        if action.net_pnl != action.gross_pnl - action.fees - action.slippage:
            return "inconsistent_action_accounting"
    return None


def _freeze_payoffs(groups: Mapping) -> dict:
    return {
        bucket: {
            action: {outcome: _payoff_distribution(samples)
                     for outcome, samples in sorted(states.items())}
            for action, states in sorted(actions.items())
        }
        for bucket, actions in sorted(groups.items())
    }


def fit_payoff_model(
    training: Sequence[tuple[FactorRow, PathLabel, ActionOutcome]], *,
    min_samples: int = 30, penalty: float = 1.0,
) -> PayoffModel:
    """Fit one route scenario's net payoff distributions using training only.

    min_samples gates total observations per action bucket, not each state;
    each positive-probability state additionally needs at least one observation.
    Values retain the action simulator's net currency per configured contract
    policy (including grid inventory and costs). Product buckets never mix;
    no price-point/margin conversion or cross-product currency pooling is made.
    Completed no-trade observations contribute zeros to the full distribution.
    """
    _positive_integer(min_samples, "min_samples")
    if not _finite(penalty) or penalty < 0:
        raise ValueError("penalty must be finite and nonnegative")
    scenario_ids = {label.scenario_id for _, label, _ in training}
    if len(scenario_ids) > 1:
        raise ValueError("payoff model requires a single route scenario")
    eligible: list = []
    exclusions: Counter = Counter()
    seen = set()
    events: dict = {}
    locations: dict = {}
    labels: dict = {}
    for event, label, action in training:
        _validate_join(event, label)
        _register_event(event, events, locations)
        if (action.event_id, action.contract_id, action.index) != (event.event_id, event.contract_id, event.index):
            raise ValueError("event/action identity mismatch (event_id, contract_id, index)")
        if not isinstance(action.action_id, str) or not action.action_id:
            raise ValueError("action_id must be a nonempty string")
        key = (event.event_id, label.scenario_id, action.action_id)
        if key in seen:
            raise ValueError("duplicate event/scenario/action")
        seen.add(key)
        if event.event_id in labels and labels[event.event_id] != label:
            raise ValueError("conflicting path label across action records")
        labels[event.event_id] = label
        reason = _pair_exclusion(event, label) or _action_exclusion(event, action)
        if reason:
            exclusions[reason] += 1
        else:
            eligible.append((event, label, action))
    eligible.sort(key=lambda row: (row[0].timestamp_nano, row[0].event_id, row[2].action_id))
    conditional = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    products = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for event, label, action in eligible:
        net = float(action.net_pnl)
        conditional[_bucket_key(event)][action.action_id][label.outcome].append(net)
        products[event.product][action.action_id][label.outcome].append(net)
    timestamps = {
        "training_event_min": min((event.timestamp_nano for event, _, _ in eligible), default=None),
        "training_event_max": max((event.timestamp_nano for event, _, _ in eligible), default=None),
        "training_terminal_max": max((max(label.terminal_nano, action.terminal_nano)
                                      for _, label, action in eligible), default=None),
    }
    fingerprints = [
        [event.event_id, *_event_signature(event), asdict(label), action.action_id,
         action.terminal_nano, action.status, action.traded, action.direction,
         *[str(getattr(action, name)) for name in ("gross_pnl", "fees", "slippage", "net_pnl", "max_margin")],
         action.ambiguous_bars]
        for event, label, action in eligible
    ]
    return PayoffModel(
        next(iter(scenario_ids), None), _freeze_payoffs(conditional), _freeze_payoffs(products),
        min_samples, float(penalty),
        {"training_input": len(training), "training": len(eligible),
         "training_events": len({event.event_id for event, _, _ in eligible})},
        dict(sorted(exclusions.items())), timestamps, _hash(fingerprints),
    )


def estimate_payoff(model: PayoffModel, event: FactorRow, action_id: str,
                    prediction: ProbabilityPrediction) -> PayoffEstimate:
    """Mix supported state net distributions at the supplied four-class probabilities.

    Unsupported positive mass yields insufficient_evidence and None estimates,
    never zero filling or renormalization. win_probability is P(action net > 0),
    including zero-return no-trade events in its denominator, not P(up_first).
    """
    _validate_event(event)
    if prediction.event_id != event.event_id:
        raise ValueError("prediction/event identity mismatch")
    for name in ("contract_id", "index", "product", "timestamp_nano"):
        if name in prediction.diagnostics and prediction.diagnostics[name] != getattr(event, name):
            raise ValueError(f"prediction/event identity mismatch: {name}")
    if model.scenario_id is not None and prediction.scenario_id != model.scenario_id:
        raise ValueError("prediction scenario must match payoff route scenario")
    _validate_distribution(prediction.probabilities, OUTCOMES)
    latest = model.timestamps["training_terminal_max"]
    if latest is not None and event.timestamp_nano <= latest:
        raise ValueError("payoff event must be strictly after training label/action terminals")
    conditional = model.conditional.get(_bucket_key(event), {}).get(action_id, {})
    product = model.products.get(event.product, {}).get(action_id, {})
    local_count = sum(state.count for state in conditional.values())
    product_count = sum(state.count for state in product.values())
    distributions: dict[str, PayoffDistribution] = {}
    levels = {}
    for outcome in OUTCOMES:
        if local_count >= model.min_samples and outcome in conditional:
            distributions[outcome] = conditional[outcome]
            levels[outcome] = "conditional"
        elif outcome in product:
            distributions[outcome] = product[outcome]
            levels[outcome] = "product_action_outcome"
        else:
            levels[outcome] = "unsupported"
    missing = [outcome for outcome in OUTCOMES if outcome not in distributions and prediction.probabilities[outcome] > 0]
    missing_mass = math.fsum(prediction.probabilities[outcome] for outcome in missing)
    support = {outcome: distributions[outcome].count if outcome in distributions else 0 for outcome in OUTCOMES}
    diagnostics = {
        "scenario_id": prediction.scenario_id, "product": event.product,
        "unit": "net_currency_per_configured_contract_action",
        "support": support, "fallback_by_outcome": levels,
        "conditional_action_count": local_count, "product_action_count": product_count,
        "missing_outcomes": missing, "missing_probability_mass": missing_mass,
        "minimum_count_rule": "total_action_bucket_min_samples_and_each_positive_mass_state_at_least_one",
        "standard_error_method": "independent_observation_heuristic",
        "quantile_method": "weighted_empirical_inverse_cdf",
        "probability_model_status": prediction.model_status,
        "caveat": PAYOFF_CAVEAT,
    }
    count = sum(support.values())
    bucket = f"{_bucket_key(event) if local_count >= model.min_samples else event.product}|{action_id}"
    if missing or product_count < model.min_samples or event.status != "ready" or prediction.model_status == "insufficient_training":
        return PayoffEstimate(event.event_id, action_id, count, None, None, None, None, None,
                              "insufficient_evidence", bucket, diagnostics)
    weights = prediction.probabilities
    mean = math.fsum(weights[outcome] * state.mean for outcome, state in distributions.items())
    se = math.sqrt(math.fsum(weights[outcome] ** 2 * state.variance / state.count
                             for outcome, state in distributions.items()))
    win_probability = math.fsum(weights[outcome] * state.win_count / state.count
                               for outcome, state in distributions.items())
    loss_probability = math.fsum(weights[outcome] * state.loss_count / state.count
                                for outcome, state in distributions.items())
    zero_probability = math.fsum(weights[outcome] * state.zero_count / state.count
                                for outcome, state in distributions.items())
    atoms: dict[float, list[float]] = defaultdict(list)
    for outcome, state in distributions.items():
        if weights[outcome] > 0:
            for value in state.samples:
                atoms[value].append(weights[outcome] / state.count)
    cumulative = 0.0
    loss_q10 = None
    for value, atom_weights in sorted(atoms.items()):
        cumulative = math.fsum((cumulative, math.fsum(atom_weights)))
        if cumulative >= 0.1 or math.isclose(cumulative, 0.1, rel_tol=0.0, abs_tol=1e-14):
            loss_q10 = value
            break
    conservative = mean - model.penalty * se
    if any(not _finite(value) for value in (mean, se, conservative, win_probability, loss_q10)):
        raise ValueError("payoff mixture overflows finite arithmetic")
    diagnostics.update({"loss_probability": loss_probability, "zero_probability": zero_probability,
                        "penalty": model.penalty})
    return PayoffEstimate(event.event_id, action_id, count, mean, se, conservative,
                          win_probability, loss_q10, "ready", bucket, diagnostics)


def payoff_model_dict(model: PayoffModel) -> dict:
    """Return the auditable training distributions without any direct data I/O."""
    result = {
        "version": "conditional_empirical_payoff_v1", **asdict(model),
        "unit": "net_currency_per_configured_contract_action",
        "product_pooling": "separate_products_no_cross_product_fallback",
        "minimum_count_rule": "total_action_bucket_min_samples_and_each_positive_mass_state_at_least_one",
        "missing_state_policy": "insufficient_evidence_no_renormalization",
        "quantile_method": "weighted_empirical_inverse_cdf",
        "sample_variance_ddof": 1, "singleton_variance": 0.0,
        "caveat": PAYOFF_CAVEAT,
    }
    result["model_hash"] = _hash(result)
    return result
