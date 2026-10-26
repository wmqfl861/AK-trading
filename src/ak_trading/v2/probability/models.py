"""Forecast model families and the frozen fit pipeline (M3A §5.1).

Reference trainers are bounded, deterministic, standard-library
implementations: Laplace/unconditional and conditional frequency baselines,
L2-regularized softmax/logistic regression, depth-2 gradient-boosted
classification trees with vector leaves, conditional empirical and linear
quantile regression, conditional empirical CDFs and discrete-time competing
risk hazards. Family selection uses date-equal-weighted inner-development
losses only; the outer calibration segment only selects temperatures. Any
non-finite or non-converging fit is recorded FAILED and never published as a
seemingly valid probability.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from ak_trading.v2.artifacts import sha256_hex, stable_id
from ak_trading.v2.contracts import (
    CalibrationEvidence,
    DataQuality,
    FactorValue,
    FeatureSnapshot,
    ForecastRequest,
    Horizon,
    JsonValue,
    MarketForecast,
    PathLabel,
    PERSISTENCE_EVENTS,
    RegimeAssessment,
    TimeProbability,
)
from ak_trading.v2.policy import confidence_policy
from ak_trading.v2.seasonality import SeasonalityModel
from ak_trading.v2.schemas import canonical_json_bytes
from ak_trading.v2._fold_records import RecordView
from ak_trading.v2.probability.calibration import (
    LABEL_VERSION,
    _build_calibration_evidence_from_view,
    build_calibration_evidence,
    canonical_evidence_id,
    evidence_projection_without_back_references,
    horizon_code,
)
from ak_trading.v2.probability.regimes import RegimeReference

FIT_VERSION = "forecast-fit-v2.1.0"
PENDING_MODEL_SET_ID = "forecast_model_set:PENDING"
ALGORITHM_PRIORITY = (
    "UNCONDITIONAL_FREQUENCY",
    "CONDITIONAL_FREQUENCY",
    "L2_SOFTMAX",
    "L2_LOGISTIC",
    "GRADIENT_BOOSTED_TREE",
    "EMPIRICAL_QUANTILE",
    "LINEAR_QUANTILE",
    "EMPIRICAL_CDF",
    "DISCRETE_COMPETING_RISK",
)
_MULTICLASS_ALGORITHMS = ("UNCONDITIONAL_FREQUENCY", "CONDITIONAL_FREQUENCY",
                          "L2_SOFTMAX", "GRADIENT_BOOSTED_TREE")
_BINARY_ALGORITHMS = ("CONDITIONAL_FREQUENCY", "L2_LOGISTIC", "GRADIENT_BOOSTED_TREE")
_QUANTILE_ALGORITHMS = ("EMPIRICAL_QUANTILE", "LINEAR_QUANTILE")
_TIME_ALGORITHMS = ("EMPIRICAL_CDF", "DISCRETE_COMPETING_RISK")

DIRECTION_CLASSES = ("UP", "DOWN", "NEUTRAL")
PATH_CLASSES = ("UP_FIRST", "DOWN_FIRST", "NEITHER", "AMBIGUOUS")
BINARY_CLASSES = ("NEG", "POS")
QUANTILE_CLASSES = ("Q05", "Q25", "Q50", "Q75", "Q95")
CANONICAL_QUANTILE_TAUS = (0.05, 0.25, 0.5, 0.75, 0.95)
TOUCH_HAZARD_CLASSES = ("SURVIVE", "TOUCH")
COMPETING_HAZARD_CLASSES = ("SURVIVE", "RETURN", "BREAK")

BARRIER_ORDER = ("ATR_0.5", "ATR_1", "ATR_2")
COMMON_BARRIER = "COMMON"
CANONICAL_BARRIER = "ATR_1"
MINUTE_EXPIRY_NANO = 120 * 1_000_000_000
WINSOR_BOUND = 5.0
TREE_CANDIDATE_LIMIT = 8
TIE_TOLERANCE = 1e-12
_EPS = 1e-12

TASK_DIRECTION = "DIRECTION"
TASK_PATH = "PATH"
TASK_QUANTILE = "QUANTILE"
TASK_TIME_UP = "TIME:UP_TOUCH"
TASK_TIME_DOWN = "TIME:DOWN_TOUCH"
TASK_TIME_RETURN = "TIME:RETURN_FIRST"
TASK_TIME_BREAK = "TIME:BREAK_FIRST"

_FIT_CONFIG_KEYS = (
    "version", "seed", "feature_keys", "l2", "learning_rate", "max_iterations",
    "tree_count", "tree_depth", "tree_learning_rate", "minimum_leaf_blocks",
    "temperature_candidates", "quantiles", "training_end_nano", "calibration_end_nano",
    "label_version", "inner_split_fraction", "inner_minimum_train_days",
    "inner_minimum_validation_days", "inner_embargo_minutes", "inner_embargo_days",
    "model_valid_until_nano",
)
_FIT_CONFIG_INITIAL: Mapping[str, JsonValue] = MappingProxyType({
    "version": FIT_VERSION,
    "seed": 7,
    "feature_keys": (),
    "l2": 1.0,
    "learning_rate": 0.01,
    "max_iterations": 500,
    "tree_count": 50,
    "tree_depth": 2,
    "tree_learning_rate": 0.05,
    "minimum_leaf_blocks": 10,
    "temperature_candidates": [0.5, 0.75, 1.0, 1.5, 2.0],
    "quantiles": [0.05, 0.25, 0.5, 0.75, 0.95],
    "training_end_nano": None,
    "calibration_end_nano": None,
    "label_version": LABEL_VERSION,
    "inner_split_fraction": 0.70,
    "inner_minimum_train_days": 120,
    "inner_minimum_validation_days": 30,
    "inner_embargo_minutes": 60,
    "inner_embargo_days": 20,
    "model_valid_until_nano": None,
})


class FitError(ValueError):
    """Fixed-reason fit configuration or training-input violation."""


def default_fit_config(feature_keys: tuple[str, ...], training_end_nano: int,
                       calibration_end_nano: int, model_valid_until_nano: int,
                       **overrides: JsonValue) -> dict[str, JsonValue]:
    """Full fit config with the registered initial values (§5.1)."""
    config: dict[str, JsonValue] = {
        "version": FIT_VERSION,
        "seed": 7,
        "feature_keys": list(feature_keys),
        "l2": 1.0,
        "learning_rate": 0.01,
        "max_iterations": 500,
        "tree_count": 50,
        "tree_depth": 2,
        "tree_learning_rate": 0.05,
        "minimum_leaf_blocks": 10,
        "temperature_candidates": [0.5, 0.75, 1.0, 1.5, 2.0],
        "quantiles": [0.05, 0.25, 0.5, 0.75, 0.95],
        "training_end_nano": training_end_nano,
        "calibration_end_nano": calibration_end_nano,
        "label_version": LABEL_VERSION,
        "inner_split_fraction": 0.70,
        "inner_minimum_train_days": 120,
        "inner_minimum_validation_days": 30,
        "inner_embargo_minutes": 60,
        "inner_embargo_days": 20,
        "model_valid_until_nano": model_valid_until_nano,
    }
    config.update(overrides)
    return config


def _validate_fit_config(fit_config: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    if not isinstance(fit_config, Mapping):
        raise TypeError("fit_config_must_be_mapping")
    merged: dict[str, JsonValue] = dict(_FIT_CONFIG_INITIAL)
    merged.update(dict(fit_config))
    extra = sorted(set(fit_config) - set(_FIT_CONFIG_KEYS))
    if extra:
        raise FitError(f"fit_config_unknown_keys:{extra}")
    if merged["version"] != FIT_VERSION:
        raise FitError("fit_config_version_mismatch")
    if merged["label_version"] != LABEL_VERSION:
        raise FitError("fit_config_label_version_mismatch")
    if isinstance(merged["feature_keys"], list):
        merged["feature_keys"] = tuple(merged["feature_keys"])
    if not isinstance(merged["feature_keys"], tuple) or not merged["feature_keys"] \
            or any(not isinstance(key, str) or not key for key in merged["feature_keys"]):
        raise FitError("fit_config_feature_keys_must_be_non_empty_str_tuple")
    if len(set(merged["feature_keys"])) != len(merged["feature_keys"]):
        raise FitError("fit_config_feature_keys_duplicate")
    for name in ("l2", "learning_rate", "tree_learning_rate", "inner_split_fraction"):
        value = merged[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(float(value)) or float(value) <= 0:
            raise FitError(f"fit_config_{name}_must_be_positive_finite")
        merged[name] = float(value)
    if not 0.0 < float(merged["inner_split_fraction"]) < 1.0:
        raise FitError("fit_config_inner_split_fraction_must_be_in_open_0_1")
    for name in ("max_iterations", "tree_count", "tree_depth", "minimum_leaf_blocks",
                 "seed", "inner_minimum_train_days", "inner_minimum_validation_days",
                 "inner_embargo_minutes", "inner_embargo_days"):
        value = merged[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise FitError(f"fit_config_{name}_must_be_positive_int")
    temperatures = merged["temperature_candidates"]
    if not isinstance(temperatures, list) or not temperatures or any(
            isinstance(t, bool) or not isinstance(t, (int, float))
            or not math.isfinite(float(t)) or float(t) <= 0 for t in temperatures):
        raise FitError("fit_config_temperature_candidates_must_be_positive_floats")
    quantiles = merged["quantiles"]
    if not isinstance(quantiles, list) or len(quantiles) != 5 or any(
            isinstance(q, bool) or not isinstance(q, (int, float))
            or not 0 < float(q) < 1 for q in quantiles) \
            or any(float(quantiles[i]) >= float(quantiles[i + 1]) for i in range(4)):
        raise FitError("fit_config_quantiles_must_be_5_strictly_increasing_in_0_1")
    for name in ("training_end_nano", "calibration_end_nano", "model_valid_until_nano"):
        if isinstance(merged[name], bool) or not isinstance(merged[name], int):
            raise FitError(f"fit_config_{name}_must_be_int")
    if not merged["training_end_nano"] < merged["calibration_end_nano"] \
            < merged["model_valid_until_nano"]:
        raise FitError("fit_config_nanos_must_satisfy_training<calibration<valid_until")
    frozen = dict(merged)
    frozen["feature_keys"] = list(merged["feature_keys"])
    return frozen


@dataclass(frozen=True)
class ForecastModelSet:
    """Immutable fitted model bundle; other nodes may only pass it back here."""

    model_set_id: str
    regime_reference_id: str
    feature_keys: tuple[str, ...]
    training_end_nano: int
    calibration_end_nano: int
    valid_until_nano: int
    fit_config: Mapping[str, JsonValue]
    models: Mapping[str, JsonValue]
    evidence: tuple[CalibrationEvidence, ...]
    source_hashes: Mapping[str, str]

    def __post_init__(self) -> None:
        for name in ("model_set_id", "regime_reference_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise TypeError(f"{name}_must_be_str")
        for name in ("feature_keys",):
            value = getattr(self, name)
            if not isinstance(value, tuple) or any(not isinstance(k, str) for k in value):
                raise TypeError(f"{name}_must_be_tuple_of_str")
        for name in ("training_end_nano", "calibration_end_nano", "valid_until_nano"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name}_must_be_int")
        if not self.training_end_nano < self.calibration_end_nano <= self.valid_until_nano:
            raise FitError("model_set_nanos_must_be_ordered")
        for name in ("fit_config", "models", "source_hashes"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise TypeError(f"{name}_must_be_mapping")
            object.__setattr__(self, name, MappingProxyType(dict(value)))
        if not isinstance(self.evidence, tuple) or any(
                not isinstance(e, CalibrationEvidence) for e in self.evidence):
            raise TypeError("evidence_must_be_tuple_of_CalibrationEvidence")

    def content_hash(self) -> str:
        """Canonical content hash of the final fitted set (03-M3A §5.2).

        Covers the selected post-temperature model parameters, preprocessing
        (fit_config + per-model center/scale), the frozen segment cut-offs,
        the legal input hashes and the complete evidence statistics. Per
        变更申请-004 §3 the projection removes exactly two back references:
        this set's own ``model_set_id`` is not a hashed field, and each
        embedded evidence row drops only ``model_id`` and ``evidence_id`` —
        every other evidence identity/statistic is preserved. Evidence rows
        are ordered by their de-referenced canonical bytes and duplicate
        identities are rejected.
        """
        projected_evidence = [
            evidence_projection_without_back_references(row) for row in self.evidence]
        projected_evidence.sort(
            key=lambda projection: canonical_json_bytes(projection))
        fingerprints = [canonical_json_bytes(projection)
                        for projection in projected_evidence]
        if len(set(fingerprints)) != len(fingerprints):
            raise FitError("duplicate_evidence_identity_in_projection")
        payload = {
            "regime_reference_id": self.regime_reference_id,
            "feature_keys": list(self.feature_keys),
            "training_end_nano": self.training_end_nano,
            "calibration_end_nano": self.calibration_end_nano,
            "valid_until_nano": self.valid_until_nano,
            "fit_config": dict(self.fit_config),
            "models": {key: dict(value) for key, value in sorted(self.models.items())},
            "source_hashes": dict(sorted(self.source_hashes.items())),
            "evidence": projected_evidence,
        }
        return sha256_hex(canonical_json_bytes(payload))


# ---------------------------------------------------------------------------
# feature extraction and standardization
# ---------------------------------------------------------------------------

def feature_vector(features: FeatureSnapshot, feature_keys: tuple[str, ...]) -> tuple[float, ...] | None:
    """Numeric vector in ``feature_keys`` order; None when any key is missing."""
    return _feature_vector_values(features.values, feature_keys)


def _feature_vector_values(values: tuple[FactorValue, ...],
                           feature_keys: tuple[str, ...]) -> tuple[float, ...] | None:
    """Shared first-occurrence-wins walk over factor values (identical
    semantics for tuple and compact view paths)."""
    seen: dict[str, Decimal | float | None] = {}
    for factor in values:
        if factor.factor_key not in seen:
            seen[factor.factor_key] = factor.value
    vector: list[float] = []
    for key in feature_keys:
        value = seen.get(key)
        if value is None:
            return None
        vector.append(float(value))
    return tuple(vector)


def _standardize_stats(vectors: list[tuple[float, ...]]) -> tuple[list[float], list[float]]:
    if not vectors:
        return [], []
    dimension = len(vectors[0])
    center = [sum(vector[i] for vector in vectors) / len(vectors) for i in range(dimension)]
    scale = []
    for i in range(dimension):
        variance = sum((vector[i] - center[i]) ** 2 for vector in vectors) / len(vectors)
        std = math.sqrt(variance)
        scale.append(std if std > _EPS else 1.0)
    return center, scale


def _standardized(vector: tuple[float, ...], center: list[float],
                  scale: list[float]) -> tuple[float, ...]:
    return tuple(
        max(-WINSOR_BOUND, min(WINSOR_BOUND, (value - center[i]) / scale[i]))
        for i, value in enumerate(vector))


def model_key(product: str, mode: str, horizon: Horizon, barrier: str,
              task: str, algorithm: str) -> str:
    """``<product>|<mode>|<horizon>|<barrier>|<task>|<algorithm>`` (§6)."""
    return f"{product}|{mode}|{horizon_code(horizon)}|{barrier}|{task}|{algorithm}"


# ---------------------------------------------------------------------------
# classification primitives
# ---------------------------------------------------------------------------

def _finite_iterable(values) -> bool:
    return all(math.isfinite(value) for value in values)


def _laplace(counts: list[int]) -> list[float]:
    total = sum(counts) + len(counts)
    if total <= 0:
        return [1.0 / len(counts)] * len(counts)
    return [(count + 1) / total for count in counts]


def _softmax(scores: list[float]) -> list[float]:
    top = max(scores)
    exps = [math.exp(score - top) for score in scores]
    total = sum(exps)
    return [value / total for value in exps]


def _softmax_gd(X: list[tuple[float, ...]], y: list[int], classes_count: int,
                l2: float, learning_rate: float, iterations: int,
                weights: list[list[float]] | None = None,
                intercepts: list[float] | None = None) -> tuple[list[list[float]], list[float]] | None:
    """Full-batch L2 gradient descent on softmax cross entropy; None on blow-up."""
    dimension = len(X[0]) if X else 0
    if weights is None:
        weights = [[0.0] * dimension for _ in range(classes_count)]
        intercepts = [0.0] * classes_count
    assert intercepts is not None
    n = len(X)
    for _ in range(iterations):
        grad_w = [[0.0] * dimension for _ in range(classes_count)]
        grad_b = [0.0] * classes_count
        for vector, target in zip(X, y):
            scores = [intercepts[k] + sum(w * x for w, x in zip(weights[k], vector))
                      for k in range(classes_count)]
            probs = _softmax(scores)
            for k in range(classes_count):
                coefficient = probs[k] - (1.0 if k == target else 0.0)
                grad_b[k] += coefficient
                row = grad_w[k]
                for i, x in enumerate(vector):
                    row[i] += coefficient * x
        for k in range(classes_count):
            intercepts[k] -= learning_rate * grad_b[k] / n
            for i in range(dimension):
                weights[k][i] -= learning_rate * (grad_w[k][i] / n + l2 * weights[k][i])
        if not _finite_iterable(intercepts) or any(
                not _finite_iterable(row) for row in weights):
            return None
    return weights, intercepts


def _apply_softmax(weights: list[list[float]], intercepts: list[float],
                   vector: tuple[float, ...]) -> list[float]:
    scores = [intercepts[k] + sum(w * x for w, x in zip(weights[k], vector))
              for k in range(len(intercepts))]
    return _softmax(scores)


def _constant_classification_model(algorithm: str, feature_keys: list[str],
                                   classes: tuple[str, ...], counts: list[int],
                                   center: list[float], scale: list[float]) -> dict[str, JsonValue]:
    return {
        "algorithm": algorithm,
        "feature_keys": list(feature_keys),
        "center": list(center),
        "scale": list(scale),
        "classes": list(classes),
        "coefficients": [],
        "intercepts": [],
        "trees": [],
        "counts": {"product": list(counts)},
        "samples": {},
        "temperature": 1.0,
        "fit_status": "CONVERGED",
    }


def _failed_model(algorithm: str, feature_keys: list[str], reason: str
                  ) -> dict[str, JsonValue]:
    placeholder = _constant_classification_model(
        algorithm, feature_keys, ("UP", "DOWN", "NEUTRAL"), [0, 0, 0], [], [])
    placeholder["fit_status"] = reason
    return placeholder


def _fit_unconditional(samples_y: list[str], classes: tuple[str, ...],
                       feature_keys: list[str], center: list[float],
                       scale: list[float]) -> dict[str, JsonValue]:
    counts = [sum(1 for outcome in samples_y if outcome == class_key) for class_key in classes]
    return _constant_classification_model(
        "UNCONDITIONAL_FREQUENCY", feature_keys, classes, counts, center, scale)


def _condition_levels(product: str, session_kind: str, base_regime: str) -> tuple[str, str, str]:
    return (
        f"{product}|{session_kind}|{base_regime}",
        f"{product}|{session_kind}",
        product,
    )


def _fit_conditional(X: list[tuple[float, ...]], y: list[str],
                     levels: list[tuple[str, str, str]], classes: tuple[str, ...],
                     feature_keys: list[str], center: list[float],
                     scale: list[float]) -> dict[str, JsonValue]:
    counts: dict[str, list[int]] = {}
    for level in levels:
        for key in level:
            counts.setdefault(key, [0] * len(classes))
    for index, outcome in enumerate(y):
        for key in levels[index]:
            counts[key][classes.index(outcome)] += 1
    return {
        "algorithm": "CONDITIONAL_FREQUENCY",
        "feature_keys": list(feature_keys),
        "center": list(center),
        "scale": list(scale),
        "classes": list(classes),
        "coefficients": [],
        "intercepts": [],
        "trees": [],
        "counts": {key: list(value) for key, value in sorted(counts.items())},
        "samples": {},
        "temperature": 1.0,
        "fit_status": "CONVERGED",
    }


def _apply_conditional(model: dict[str, JsonValue], levels: tuple[str, str, str],
                       classes: tuple[str, ...]) -> list[float] | None:
    counts = model["counts"]
    if not isinstance(counts, Mapping):
        return None
    for level in levels:
        entry = counts.get(level)
        if isinstance(entry, list) and sum(entry) >= 1:
            return _laplace([int(value) for value in entry])
    return None


def _apply_conditional_min_blocks(model: dict[str, JsonValue], levels: tuple[str, str, str],
                                  minimum_leaf_blocks: int,
                                  classes: tuple[str, ...]) -> list[float]:
    counts = model["counts"]
    assert isinstance(counts, Mapping)
    for level in levels:
        entry = counts.get(level)
        if isinstance(entry, list) and sum(int(v) for v in entry) >= minimum_leaf_blocks:
            return _laplace([int(value) for value in entry])
    last = counts.get(levels[-1])
    if isinstance(last, list):
        return _laplace([int(value) for value in last])
    # constant-target shortcut models keep their counts under "product"
    constant = counts.get("product")
    if isinstance(constant, list):
        return _laplace([int(value) for value in constant])
    return [1.0 / len(classes)] * len(classes)


# ---------------------------------------------------------------------------
# gradient boosted depth-2 vector-leaf trees
# ---------------------------------------------------------------------------

def _candidate_thresholds(values: list[float]) -> list[float]:
    uniques = sorted(set(values))
    if not uniques:
        return []
    if len(uniques) > TREE_CANDIDATE_LIMIT + 1:
        picks = [uniques[round(index * (len(uniques) - 1) / TREE_CANDIDATE_LIMIT)]
                 for index in range(TREE_CANDIDATE_LIMIT + 1)]
        picks = sorted(set(picks))
    else:
        picks = uniques
    return [(picks[i] + picks[i + 1]) / 2 for i in range(len(picks) - 1)]


def _sse(targets: list[list[float]], indexes: list[int]) -> float:
    if not indexes:
        return 0.0
    dimension = len(targets[0])
    total = 0.0
    for k in range(dimension):
        mean = sum(targets[index][k] for index in indexes) / len(indexes)
        total += sum((targets[index][k] - mean) ** 2 for index in indexes)
    return total


def _build_tree(X: list[tuple[float, ...]], targets: list[list[float]], depth: int,
                min_leaf: int) -> list[dict[str, JsonValue]]:
    """Greedy MSE splits; node refs index this tree's own array, leaves -1."""
    nodes: list[dict[str, JsonValue]] = []

    def build(indexes: list[int], remaining_depth: int) -> int:
        best: tuple[float, int, float, list[int], list[int]] | None = None
        if remaining_depth > 0 and len(indexes) >= 2 * min_leaf:
            parent_sse = _sse(targets, indexes)
            for feature_index in range(len(X[0])):
                thresholds = _candidate_thresholds([X[i][feature_index] for i in indexes])
                for threshold in thresholds:
                    left = [i for i in indexes if X[i][feature_index] <= threshold]
                    right = [i for i in indexes if X[i][feature_index] > threshold]
                    if len(left) < min_leaf or len(right) < min_leaf:
                        continue
                    gain = parent_sse - _sse(targets, left) - _sse(targets, right)
                    if best is None or gain > best[0] + _EPS:
                        best = (gain, feature_index, threshold, left, right)
        if best is None:
            dimension = len(targets[0])
            value = [sum(targets[i][k] for i in indexes) / len(indexes) if indexes else 0.0
                     for k in range(dimension)]
            nodes.append({"feature_index": -1, "threshold": 0.0, "left": -1, "right": -1,
                          "value": value})
            return len(nodes) - 1
        _, feature_index, threshold, left, right = best
        node: dict[str, JsonValue] = {"feature_index": feature_index, "threshold": threshold,
                                      "left": -1, "right": -1, "value": []}
        nodes.append(node)
        index = len(nodes) - 1
        left_index = build(left, remaining_depth - 1)
        right_index = build(right, remaining_depth - 1)
        node["left"] = left_index
        node["right"] = right_index
        return index

    build(list(range(len(X))), depth)
    return nodes


def _tree_predict(nodes: list[dict[str, JsonValue]], vector: tuple[float, ...]) -> list[float]:
    index = 0
    while nodes[index]["feature_index"] != -1:
        feature_index = nodes[index]["feature_index"]
        threshold = nodes[index]["threshold"]
        index = nodes[index]["left"] if vector[feature_index] <= threshold \
            else nodes[index]["right"]
        if index < 0 or index >= len(nodes):
            return [0.0]
    return list(nodes[index]["value"])


def _fit_gbm(X: list[tuple[float, ...]], y: list[int], classes: tuple[str, ...],
             config: dict[str, JsonValue], center: list[float], scale: list[float],
             feature_keys: list[str]) -> dict[str, JsonValue] | None:
    n = len(X)
    class_count = len(classes)
    counts = [0] * class_count
    for target in y:
        counts[target] += 1
    prior = _laplace(counts)
    init = [math.log(p) for p in prior]
    logits = [list(init) for _ in range(n)]
    trees: list[list[dict[str, JsonValue]]] = []
    tree_lr = float(config["tree_learning_rate"])
    min_leaf = int(config["minimum_leaf_blocks"])
    tree_depth = int(config["tree_depth"])
    for _ in range(int(config["tree_count"])):
        probs = [_softmax(sample_logits) for sample_logits in logits]
        targets = [[(1.0 if k == target else 0.0) - prob[k] for k in range(class_count)]
                   for target, prob in zip(y, probs)]
        nodes = _build_tree(X, targets, tree_depth, min_leaf)
        trees.append(nodes)
        for index, vector in enumerate(X):
            update = _tree_predict(nodes, vector)
            for k in range(class_count):
                logits[index][k] += tree_lr * update[k] if k < len(update) else 0.0
        if any(not _finite_iterable(sample_logits) for sample_logits in logits):
            return None
    return {
        "algorithm": "GRADIENT_BOOSTED_TREE",
        "feature_keys": list(feature_keys),
        "center": list(center),
        "scale": list(scale),
        "classes": list(classes),
        "coefficients": [],
        "intercepts": list(init),
        "trees": trees,
        "counts": {"product": list(counts)},
        "samples": {},
        "temperature": 1.0,
        "fit_status": "CONVERGED",
    }


def _apply_gbm(model: dict[str, JsonValue], vector: tuple[float, ...]) -> list[float] | None:
    trees = model["trees"]
    intercepts = model["intercepts"]
    if not isinstance(trees, list) or not isinstance(intercepts, list):
        return None
    logits = [float(value) for value in intercepts]
    for tree in trees:
        if not isinstance(tree, list):
            return None
        update = _tree_predict(tree, vector)
        for k in range(len(logits)):
            if k < len(update):
                logits[k] += float(update[k])
    if not _finite_iterable(logits):
        return None
    return _softmax(logits)


# ---------------------------------------------------------------------------
# quantile models
# ---------------------------------------------------------------------------

def _empirical_quantile(sorted_values: list[float], quantile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * quantile
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return sorted_values[int(position)]
    fraction = position - low
    return sorted_values[low] * (1 - fraction) + sorted_values[high] * fraction


def _fit_empirical_quantile(values: list[float], levels: list[tuple[str, str, str]],
                            quantiles: list[float], feature_keys: list[str],
                            center: list[float], scale: list[float],
                            minimum_leaf_blocks: int) -> dict[str, JsonValue]:
    samples: dict[str, list[float]] = {}
    for index, value in enumerate(values):
        for level in levels[index]:
            samples.setdefault(level, []).append(value)
    return {
        "algorithm": "EMPIRICAL_QUANTILE",
        "feature_keys": list(feature_keys),
        "center": list(center),
        "scale": list(scale),
        "classes": list(QUANTILE_CLASSES),
        "coefficients": [],
        "intercepts": [],
        "trees": [],
        "counts": {"minimum_leaf_blocks": [minimum_leaf_blocks]},
        "samples": {key: sorted(set(sample)) for key, sample in sorted(samples.items())},
        "temperature": 1.0,
        "fit_status": "CONVERGED",
    }


def _apply_empirical_quantile(model: dict[str, JsonValue], levels: tuple[str, str, str],
                              quantiles: list[float]) -> list[float] | None:
    samples = model["samples"]
    counts = model["counts"]
    if not isinstance(samples, Mapping) or not isinstance(counts, Mapping):
        return None
    minimum = counts.get("minimum_leaf_blocks")
    minimum_leaf_blocks = int(minimum[0]) if isinstance(minimum, list) and minimum else 10
    chosen = None
    for level in levels:
        entry = samples.get(level)
        if isinstance(entry, list) and len(entry) >= minimum_leaf_blocks:
            chosen = entry
            break
    if chosen is None:
        chosen = samples.get(levels[-1])
    if not isinstance(chosen, list) or not chosen:
        return None
    return [_empirical_quantile([float(v) for v in chosen], q) for q in quantiles]


def _pinball(quantile: float, actual: float, prediction: float) -> float:
    error = actual - prediction
    return quantile * error if error >= 0 else (quantile - 1.0) * error


def _fit_linear_quantile(X: list[tuple[float, ...]], values: list[float],
                         quantiles: list[float], config: dict[str, JsonValue],
                         feature_keys: list[str], center: list[float],
                         scale: list[float]) -> dict[str, JsonValue] | None:
    dimension = len(X[0]) if X else 0
    l2 = float(config["l2"])
    learning_rate = float(config["learning_rate"])
    iterations = int(config["max_iterations"])
    n = len(X)
    coefficients: list[list[float]] = []
    intercepts: list[float] = []
    for quantile in quantiles:
        weights = [0.0] * dimension
        intercept = 0.0
        for _ in range(iterations):
            grad_w = [0.0] * dimension
            grad_b = 0.0
            for vector, actual in zip(X, values):
                prediction = intercept + sum(w * x for w, x in zip(weights, vector))
                indicator = 1.0 if actual < prediction else 0.0
                coefficient = -(quantile - indicator)
                grad_b += coefficient
                for i, x in enumerate(vector):
                    grad_w[i] += coefficient * x
            intercept -= learning_rate * grad_b / n
            for i in range(dimension):
                weights[i] -= learning_rate * (grad_w[i] / n + l2 * weights[i])
            if not math.isfinite(intercept) or not _finite_iterable(weights):
                return None
        coefficients.append(weights)
        intercepts.append(intercept)
    for vector, actual in zip(X, values):
        predictions = [intercepts[k] + sum(w * x for w, x in zip(coefficients[k], vector))
                       for k in range(len(quantiles))]
        if any(predictions[k] > predictions[k + 1] for k in range(len(quantiles) - 1)):
            return None  # ties legal; a real crossing fails the candidate
    return {
        "algorithm": "LINEAR_QUANTILE",
        "feature_keys": list(feature_keys),
        "center": list(center),
        "scale": list(scale),
        "classes": list(QUANTILE_CLASSES),
        "coefficients": coefficients,
        "intercepts": intercepts,
        "trees": [],
        "counts": {},
        "samples": {},
        "temperature": 1.0,
        "fit_status": "CONVERGED",
    }


def _apply_linear_quantile(model: dict[str, JsonValue],
                           vector: tuple[float, ...]) -> list[float] | None:
    coefficients = model["coefficients"]
    intercepts = model["intercepts"]
    if not isinstance(coefficients, list) or not isinstance(intercepts, list):
        return None
    predictions = []
    for weights, intercept in zip(coefficients, intercepts):
        if not isinstance(weights, list):
            return None
        predictions.append(float(intercept) + sum(
            float(w) * x for w, x in zip(weights, vector)))
    if not _finite_iterable(predictions) or any(
            predictions[k] > predictions[k + 1] for k in range(len(predictions) - 1)):
        return None  # ties legal; a real crossing is invalid
    return predictions


# ---------------------------------------------------------------------------
# time-to-event models
# ---------------------------------------------------------------------------

def _fit_empirical_cdf(first_steps: list[int], levels: list[tuple[str, str, str]],
                       feature_keys: list[str], center: list[float],
                       scale: list[float]) -> dict[str, JsonValue]:
    """Sample encoding in ``samples``: step number, 0 = never, -1 = ambiguous."""
    samples: dict[str, list[float]] = {}
    for index, step in enumerate(first_steps):
        for level in levels[index]:
            samples.setdefault(level, []).append(float(step))
    return {
        "algorithm": "EMPIRICAL_CDF",
        "feature_keys": list(feature_keys),
        "center": list(center),
        "scale": list(scale),
        "classes": ["STEP"],
        "coefficients": [],
        "intercepts": [],
        "trees": [],
        "counts": {},
        "samples": {key: sorted(value) for key, value in sorted(samples.items())},
        "temperature": 1.0,
        "fit_status": "CONVERGED",
    }


def _apply_empirical_cdf(model: dict[str, JsonValue], levels: tuple[str, str, str],
                         horizon_value: int) -> list[tuple[float, float]] | None:
    samples = model["samples"]
    if not isinstance(samples, Mapping):
        return None
    chosen = None
    total = 0
    for level in levels:
        entry = samples.get(level)
        if isinstance(entry, list) and entry:
            chosen = [float(v) for v in entry]
            total = len(chosen)
            break
    if chosen is None or total == 0:
        return None
    ambiguous = sum(1 for value in chosen if value < 0)
    unknown = ambiguous / total
    cumulative = 0.0
    entries: list[tuple[float, float]] = []
    for step in range(1, horizon_value + 1):
        cumulative = sum(1 for value in chosen if 0 < value <= step) / total
        entries.append((cumulative, unknown))
    return entries


def _fit_competing_risk(X: list[tuple[float, ...]], event_steps: list[tuple[str, int | None]],
                        hazard_classes: tuple[str, ...], horizon_value: int,
                        config: dict[str, JsonValue], feature_keys: list[str],
                        center: list[float], scale: list[float]) -> dict[str, JsonValue] | None:
    """Per-step softmax hazards; cumulative products form the CIF (§5.1)."""
    class_count = len(hazard_classes)
    dimension = len(X[0]) if X else 0
    coefficients: list[list[list[float]]] = []
    intercepts: list[list[float]] = []
    for step in range(1, horizon_value + 1):
        risk_vectors: list[tuple[float, ...]] = []
        risk_targets: list[int] = []
        for index, (event, event_step) in enumerate(event_steps):
            if event_step is not None and event_step < step:
                continue
            if event_step == step:
                risk_targets.append(hazard_classes.index(event))
            else:
                risk_targets.append(0)
            risk_vectors.append(X[index])
        if not risk_vectors:
            coefficients.append([[0.0] * dimension for _ in range(class_count)])
            intercepts.append([0.0] * class_count)
            continue
        distinct = set(risk_targets)
        if len(distinct) == 1:
            counts = [sum(1 for t in risk_targets if t == k) for k in range(class_count)]
            intercepts.append([math.log(count + 1) for count in counts])
            coefficients.append([[0.0] * dimension for _ in range(class_count)])
            continue
        fitted = _softmax_gd(risk_vectors, risk_targets, class_count,
                             float(config["l2"]), float(config["learning_rate"]),
                             int(config["max_iterations"]))
        if fitted is None:
            return None
        weights, bias = fitted
        coefficients.append(weights)
        intercepts.append(bias)
    return {
        "algorithm": "DISCRETE_COMPETING_RISK",
        "feature_keys": list(feature_keys),
        "center": list(center),
        "scale": list(scale),
        "classes": list(hazard_classes),
        "coefficients": coefficients,
        "intercepts": [value for step_bias in intercepts for value in step_bias],
        "trees": [],
        "counts": {},
        "samples": {"step_intercept_stride": [class_count]},
        "temperature": 1.0,
        "fit_status": "CONVERGED",
    }


def cumulative_incidence(hazards: Mapping[str, list[float]],
                         steps: int) -> tuple[dict[str, list[float]], list[float]]:
    """Discrete-time competing-risk CIF from non-negative conditional hazards.

    ``cif_e(s) = cif_e(s-1) + S(s-1) * h_e(s)`` with ``S`` the product of
    ``1 - sum(h)`` over event (non-survive) hazards — the §8.5 hand example
    ``h_return=0.2, h_break=0.1`` gives CIF 0.34/0.17 and survival 0.49 at
    step 2.
    """
    events = sorted(hazards)
    cifs: dict[str, list[float]] = {event: [] for event in events}
    survival_curve: list[float] = []
    survival = 1.0
    for step in range(steps):
        step_hazards = {
            event: hazards[event][step] if step < len(hazards[event]) else 0.0
            for event in events}
        for event in events:
            previous = cifs[event][-1] if cifs[event] else 0.0
            cifs[event].append(previous + survival * step_hazards[event])
        survival *= (1.0 - sum(step_hazards.values()))
        survival_curve.append(survival)
    return cifs, survival_curve


def _apply_competing_risk(model: dict[str, JsonValue], vector: tuple[float, ...],
                          horizon_value: int, event_class: str) -> list[tuple[float, float]] | None:
    hazard_classes = list(model["classes"])
    stride_entry = model["samples"].get("step_intercept_stride")
    if not isinstance(stride_entry, list) or not stride_entry:
        return None
    stride = int(stride_entry[0])
    intercepts = model["intercepts"]
    coefficients = model["coefficients"]
    if not isinstance(intercepts, list) or not isinstance(coefficients, list):
        return None
    event_hazards: dict[str, list[float]] = {}
    for name in hazard_classes[1:]:
        event_hazards[name] = []
    for step in range(1, horizon_value + 1):
        if step > len(coefficients):
            for name in event_hazards:
                event_hazards[name].append(0.0)
            continue
        step_bias = intercepts[(step - 1) * stride: step * stride]
        step_weights = coefficients[step - 1]
        scores = [step_bias[k] + sum(
            float(w) * x for w, x in zip(step_weights[k], vector))
            for k in range(stride)]
        hazards = _softmax(scores)
        for index, name in enumerate(hazard_classes[1:], start=1):
            event_hazards[name].append(hazards[index])
    if not all(math.isfinite(value) for values in event_hazards.values() for value in values):
        return None
    cifs, _ = cumulative_incidence(event_hazards, horizon_value)
    cif = cifs.get(event_class)
    if cif is None:
        return None
    return [(value, 0.0) for value in cif]


# ---------------------------------------------------------------------------
# temperature scaling on the calibration segment
# ---------------------------------------------------------------------------

def _scaled(probabilities: list[float], temperature: float) -> list[float]:
    if temperature == 1.0:
        return probabilities
    powered = [max(_EPS, value) ** (1.0 / temperature) for value in probabilities]
    total = sum(powered)
    if total <= 0:
        return [1.0 / len(probabilities)] * len(probabilities)
    return [value / total for value in powered]


def _choose_temperature(model: dict[str, JsonValue], targets: list[int],
                        predictions: list[list[float]],
                        candidates: list[float]) -> float:
    if not targets:
        return 1.0
    best_temperature = 1.0
    best_loss = None
    for temperature in candidates:
        loss = 0.0
        for target, probabilities in zip(targets, predictions):
            adjusted = _scaled(probabilities, temperature)
            loss += -math.log(max(_EPS, adjusted[target]))
        if not math.isfinite(loss):
            continue
        if best_loss is None or loss < best_loss - _EPS:
            best_loss = loss
            best_temperature = temperature
    return best_temperature


def apply_temperature(probabilities: list[float], temperature: float) -> list[float]:
    return _scaled(probabilities, temperature)


# ---------------------------------------------------------------------------
# training samples
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Sample:
    request_id: str
    snapshot_id: str
    contract_id: str
    product: str
    session_kind: str
    duration_seconds: int
    trading_day: date
    decision_nano: int
    base_regime: str
    barrier: str
    horizon: Horizon
    window_start: int
    window_end: int
    label_available_at: int
    label: PathLabel
    vector: tuple[float, ...]

    @property
    def mode(self) -> str:
        return "MINUTE" if self.horizon.unit == "TRADING_MINUTE" else "DAILY"

    @property
    def levels(self) -> tuple[str, str, str]:
        return _condition_levels(self.product, self.session_kind, self.base_regime)


class _RequestFields:
    """Join-relevant request fields (value-identical on both paths)."""

    __slots__ = ("request_id", "horizon", "window_start", "window_end",
                 "barrier", "frozen_range", "features_handle",
                 "features_snapshot_id")

    def __init__(self, request_id, horizon, window_start, window_end, barrier,
                 frozen_range, features_handle, features_snapshot_id) -> None:
        self.request_id = request_id
        self.horizon = horizon
        self.window_start = window_start
        self.window_end = window_end
        self.barrier = barrier
        self.frozen_range = frozen_range
        self.features_handle = features_handle
        self.features_snapshot_id = features_snapshot_id


class _TupleSegmentAccess:
    """Segment access over the public tuple inputs (objects resident)."""

    def __init__(self, features, requests) -> None:
        self._features = features
        self._requests = requests

    def iter_snapshot_handles(self):
        return iter(self._features)

    def snapshot_id(self, handle) -> str:
        return handle.snapshot_id

    def snapshot_scalars(self, handle):
        return handle  # the dataclass already carries every join scalar

    def factor_values(self, handle) -> tuple[FactorValue, ...]:
        return handle.values

    def features_equal(self, left, right) -> bool:
        return left != right

    def iter_request_handles(self):
        return iter(self._requests)

    def request_id(self, handle) -> str:
        return handle.request_id

    def request_fields(self, handle) -> _RequestFields:
        return _RequestFields(
            request_id=handle.request_id,
            horizon=handle.horizon,
            window_start=handle.window_start_nano,
            window_end=handle.window_end_nano,
            barrier=handle.barrier_key,
            frozen_range=handle.frozen_range,
            features_handle=handle.features,
            features_snapshot_id=handle.features.snapshot_id)

    def seasonal_column_exists(self) -> bool:
        return any(factor.factor_key.startswith("SEASONAL:")
                   for snapshot in self._features for factor in snapshot.values)

    def snapshot_id_and_decision(self, handle):
        return handle.snapshot_id, handle.decision_nano

    def decision_nanos(self):
        return [snapshot.decision_nano for snapshot in self._features]

    def request_getter(self, requests_by_id=None):
        table = {request.request_id: request for request in self._requests} \
            if requests_by_id is None else requests_by_id
        return table.get

    def evidence_features(self, referenced):
        return tuple(snapshot for snapshot in self._features
                     if snapshot.snapshot_id in referenced)


class _ViewSegmentAccess:
    """Segment access over store views (compact records, bounded
    materialization; join value equality via the store's same_snapshot)."""

    def __init__(self, features_view, requests_view) -> None:
        if not isinstance(features_view, RecordView) \
                or features_view.record_kind != "FeatureSnapshot":
            raise TypeError("features_view_must_be_snapshot_RecordView")
        if not isinstance(requests_view, RecordView) \
                or requests_view.record_kind != "ForecastRequest":
            raise TypeError("requests_view_must_be_request_RecordView")
        if features_view.store is not requests_view.store:
            raise TypeError("segment_views_must_share_one_store")
        self._features_view = features_view
        self._requests_view = requests_view
        self._store = features_view.store

    def _snapshot_compact(self, handle):
        return self._store._resolve_snapshot_ref(handle)

    def iter_snapshot_handles(self):
        for index in range(len(self._features_view)):
            yield self._features_view.meta(index).handle

    def snapshot_id(self, handle) -> str:
        return self._snapshot_compact(handle).snapshot_id

    def snapshot_scalars(self, handle):
        return self._snapshot_compact(handle)

    def factor_values(self, handle) -> tuple[FactorValue, ...]:
        return self._snapshot_compact(handle).values

    def features_equal(self, left, right) -> bool:
        return not self._store.same_snapshot(left, right)

    def iter_request_handles(self):
        for index in range(len(self._requests_view)):
            yield self._requests_view.meta(index).handle

    def request_id(self, handle) -> str:
        return self._store._request_compact(handle).request_id

    def request_fields(self, handle) -> _RequestFields:
        meta = self._store._meta("ForecastRequest", handle.slot)
        return _RequestFields(
            request_id=meta.request_id,
            horizon=meta.horizon,
            window_start=meta.window_start_nano,
            window_end=meta.window_end_nano,
            barrier=meta.barrier_key,
            frozen_range=meta.frozen_range,
            features_handle=meta.features_ref,
            features_snapshot_id=meta.features_snapshot_id)

    def seasonal_column_exists(self) -> bool:
        return any(factor.factor_key.startswith("SEASONAL:")
                   for handle in self.iter_snapshot_handles()
                   for factor in self.factor_values(handle))

    def decision_nanos(self):
        return [self.snapshot_id_and_decision(handle)[1]
                for handle in self.iter_snapshot_handles()]

    def snapshot_id_and_decision(self, handle):
        compact = self._snapshot_compact(handle)
        return compact.snapshot_id, compact.decision_nano

    def request_getter(self, requests_by_id=None):
        def getter(request_id: str):
            handle = self._store._request_handle_by_id(request_id)
            if handle is None:
                return None
            return self._store._materialize("ForecastRequest", handle.slot)
        return getter

    def evidence_features(self, referenced):
        selected = [index for index in range(len(self._features_view))
                    if self._features_view.meta(index).snapshot_id in referenced]
        return self._features_view.select(selected)


def _validate_join_access(snapshot_access, request_access,
                          regimes: tuple[RegimeAssessment, ...],
                          labels: tuple[PathLabel, ...],
                          feature_keys: tuple[str, ...], label_version: str,
                          label_deadline: int, deadline_reason: str) -> list[_Sample]:
    """Shared join validation core (public tuple path and internal view path
    run the identical checks, in the identical order, with the identical
    messages; only the record access differs)."""
    snapshots_by_id = {}
    for handle in snapshot_access.iter_snapshot_handles():
        snapshot_id = snapshot_access.snapshot_id(handle)
        if snapshot_id in snapshots_by_id:
            raise FitError("duplicate_snapshot_id")
        snapshots_by_id[snapshot_id] = handle
    assessments: dict[str, RegimeAssessment] = {}
    for assessment in regimes:
        if assessment.snapshot_id in assessments:
            raise FitError("duplicate_regime_snapshot")
        assessments[assessment.snapshot_id] = assessment
    request_map = {}
    for handle in request_access.iter_request_handles():
        request_id = request_access.request_id(handle)
        if request_id in request_map:
            raise FitError("duplicate_request_id")
        request_map[request_id] = handle
    label_map: dict[str, PathLabel] = {}
    for label in labels:
        if label.request_id in label_map:
            raise FitError("duplicate_label_request_id")
        if label.label_version != label_version:
            raise FitError("label_version_mismatch")
        label_map[label.request_id] = label

    samples: list[_Sample] = []
    vector_by_snapshot: dict[str, tuple[float, ...]] = {}
    for handle in request_access.iter_request_handles():
        fields = request_access.request_fields(handle)
        label = label_map.get(fields.request_id)
        if label is None:
            raise FitError("label_missing_for_request")
        if label.status != "MATURE" or label.available_at_nano is None:
            raise FitError("label_not_mature")
        if label.available_at_nano > label_deadline:
            raise FitError(deadline_reason)
        if label.horizon != fields.horizon:
            raise FitError("label_horizon_mismatch")
        if label.window_start_nano != fields.window_start \
                or label.window_end_nano != fields.window_end:
            raise FitError("label_window_mismatch")
        snapshot_handle = snapshots_by_id.get(fields.features_snapshot_id)
        if snapshot_handle is None or snapshot_access.features_equal(
                snapshot_handle, fields.features_handle):
            raise FitError("request_feature_snapshot_mismatch")
        scalars = snapshot_access.snapshot_scalars(snapshot_handle)
        assessment = assessments.get(scalars.snapshot_id)
        if assessment is None:
            raise FitError("regime_snapshot_mismatch")
        if assessment.decision_nano != scalars.decision_nano:
            raise FitError("regime_decision_mismatch")
        vector = vector_by_snapshot.get(scalars.snapshot_id)
        if vector is None:
            vector = _feature_vector_values(
                snapshot_access.factor_values(snapshot_handle), feature_keys)
            if vector is None:
                continue
            vector_by_snapshot[scalars.snapshot_id] = vector
        label_range = label.range_id
        request_range = fields.frozen_range.range_id if fields.frozen_range is not None else None
        if label_range != request_range:
            raise FitError("label_range_mismatch")
        samples.append(_Sample(
            request_id=fields.request_id,
            snapshot_id=scalars.snapshot_id,
            contract_id=scalars.contract_id,
            product=scalars.product,
            session_kind=scalars.session_kind,
            duration_seconds=scalars.duration_seconds,
            trading_day=scalars.trading_day,
            decision_nano=scalars.decision_nano,
            base_regime=assessment.base,
            barrier=fields.barrier,
            horizon=fields.horizon,
            window_start=fields.window_start,
            window_end=fields.window_end,
            label_available_at=label.available_at_nano,
            label=label,
            vector=vector,
        ))
    return samples


def _validate_join(features: tuple[FeatureSnapshot, ...],
                   regimes: tuple[RegimeAssessment, ...],
                   requests: tuple[ForecastRequest, ...],
                   labels: tuple[PathLabel, ...],
                   feature_keys: tuple[str, ...], label_version: str,
                   label_deadline: int, deadline_reason: str) -> list[_Sample]:
    return _validate_join_access(_TupleSegmentAccess(features, requests),
                                 _TupleSegmentAccess(features, requests),
                                 regimes, labels, feature_keys, label_version,
                                 label_deadline, deadline_reason)


def _cell_key(sample: _Sample) -> tuple[str, str, str, int]:
    return (sample.product, sample.mode, sample.horizon.unit, sample.horizon.value)


def _common_samples(cell_samples: list[_Sample]) -> list[_Sample]:
    """One barrier per snapshot for barrier-independent tasks (§4.1)."""
    by_snapshot: dict[str, list[_Sample]] = {}
    for sample in cell_samples:
        by_snapshot.setdefault(sample.snapshot_id, []).append(sample)
    chosen: list[_Sample] = []
    for snapshot_id in sorted(by_snapshot):
        members = by_snapshot[snapshot_id]
        pick = next((s for s in members if s.barrier == CANONICAL_BARRIER), members[0])
        chosen.append(pick)
    return chosen


# ---------------------------------------------------------------------------
# losses (date-equal-weighted per §5.1)
# ---------------------------------------------------------------------------

def _daily_mean(pairs: list[tuple[date, float]]) -> float | None:
    if not pairs:
        return None
    by_day: dict[date, list[float]] = {}
    for day, value in pairs:
        by_day.setdefault(day, []).append(value)
    return sum(sum(values) / len(values) for values in by_day.values()) / len(by_day)


def _multiclass_brier_loss(samples: list[_Sample], predictions: list[list[float]],
                           classes: tuple[str, ...]) -> float | None:
    pairs: list[tuple[date, float]] = []
    for sample, probabilities in zip(samples, predictions):
        outcome = _multiclass_target(sample, classes)
        pairs.append((sample.trading_day, sum(
            (probabilities[k] - (1.0 if k == outcome else 0.0)) ** 2
            for k in range(len(classes)))))
    return _daily_mean(pairs)


def _binary_brier_loss(samples: list[_Sample], probabilities: list[float],
                       task: str) -> float | None:
    pairs = [(sample.trading_day, (probabilities[index] - _binary_target(sample, task)) ** 2)
             for index, sample in enumerate(samples)]
    return _daily_mean(pairs)


def _multiclass_target(sample: _Sample, classes: tuple[str, ...]) -> int:
    if classes == DIRECTION_CLASSES:
        return classes.index(sample.label.direction)
    return classes.index(sample.label.path_outcome)


def _binary_target(sample: _Sample, task: str) -> float:
    if task.startswith("PERSIST:"):
        value = sample.label.persistence.get(task.split(":", 1)[1])
        return 1.0 if value else 0.0
    if task == "STOP_TOUCH:LONG":
        return 1.0 if sample.label.stop_touched.get("LONG") else 0.0
    if task == "STOP_TOUCH:SHORT":
        return 1.0 if sample.label.stop_touched.get("SHORT") else 0.0
    return 1.0 if sample.label.competing_outcome == "RETURN_FIRST" else 0.0


def _pinball_loss(samples: list[_Sample], predictions: list[list[float]],
                  quantiles: list[float]) -> float | None:
    pairs: list[tuple[date, float]] = []
    for sample, quantile_values in zip(samples, predictions):
        actual = sample.label.terminal_atr
        pairs.append((sample.trading_day, sum(
            _pinball(quantiles[k], actual, quantile_values[k]) for k in range(len(quantiles)))
            / len(quantiles)))
    return _daily_mean(pairs)


def _time_targets(sample: _Sample, task: str) -> tuple[int | None, bool] | None:
    """(first step or None, ambiguous) for a time task; None when unusable.

    Only ``AMBIGUOUS`` outcomes are unknown mass for the competing tasks: a
    ``NEITHER`` sample is a KNOWN "never before the competing event" outcome
    (CDF indicator 0 at every step) and stays scoreable, as is the losing
    side of a resolved ``RETURN_FIRST``/``BREAK_FIRST`` pair.
    """
    if task == TASK_TIME_UP:
        return sample.label.first_up_step, False
    if task == TASK_TIME_DOWN:
        return sample.label.first_down_step, False
    if task == TASK_TIME_RETURN:
        outcome = sample.label.competing_outcome
        if outcome is None:
            return None
        return sample.label.return_step, outcome == "AMBIGUOUS"
    if task == TASK_TIME_BREAK:
        outcome = sample.label.competing_outcome
        if outcome is None:
            return None
        return sample.label.break_step, outcome == "AMBIGUOUS"
    return None


def _time_brier_loss(samples: list[_Sample], predictions: list[list[tuple[float, float]]],
                     task: str, horizon_value: int) -> float | None:
    pairs: list[tuple[date, float]] = []
    for sample, entries in zip(samples, predictions):
        target = _time_targets(sample, task)
        if target is None or target[1]:
            continue
        first_step = target[0]
        step_losses = []
        for index in range(horizon_value):
            cumulative = entries[index][0]
            indicator = 1.0 if first_step is not None and first_step <= index + 1 else 0.0
            step_losses.append((cumulative - indicator) ** 2)
        pairs.append((sample.trading_day, sum(step_losses) / len(step_losses)))
    return _daily_mean(pairs)


# ---------------------------------------------------------------------------
# candidate fitting per task
# ---------------------------------------------------------------------------

def _fit_multiclass_candidate(algorithm: str, X: list[tuple[float, ...]],
                              y: list[int], classes: tuple[str, ...],
                              samples: list[_Sample], config: dict[str, JsonValue],
                              center: list[float], scale: list[float]) -> dict[str, JsonValue] | None:
    feature_keys = config["feature_keys"]
    if algorithm == "UNCONDITIONAL_FREQUENCY":
        outcomes = [classes[index] for index in y]
        return _fit_unconditional(outcomes, classes, feature_keys, center, scale)
    if algorithm == "CONDITIONAL_FREQUENCY":
        outcomes = [classes[index] for index in y]
        levels = [sample.levels for sample in samples]
        return _fit_conditional(X, outcomes, levels, classes, feature_keys, center, scale)
    counts = [sum(1 for index in y if index == k) for k in range(len(classes))]
    if max(counts) == len(y):
        return _constant_classification_model(
            algorithm, feature_keys, classes, counts, center, scale)
    if algorithm == "L2_SOFTMAX":
        fitted = _softmax_gd(X, y, len(classes), float(config["l2"]),
                             float(config["learning_rate"]), int(config["max_iterations"]))
        if fitted is None:
            return None
        weights, bias = fitted
        return {
            "algorithm": "L2_SOFTMAX", "feature_keys": list(feature_keys),
            "center": list(center), "scale": list(scale), "classes": list(classes),
            "coefficients": weights, "intercepts": bias, "trees": [], "counts": {},
            "samples": {}, "temperature": 1.0, "fit_status": "CONVERGED",
        }
    if algorithm == "GRADIENT_BOOSTED_TREE":
        return _fit_gbm(X, y, classes, config, center, scale, feature_keys)
    return None


def _apply_multiclass_candidate(model: dict[str, JsonValue], vector: tuple[float, ...],
                                levels: tuple[str, str, str],
                                minimum_leaf_blocks: int) -> list[float] | None:
    algorithm = model["algorithm"]
    classes = tuple(model["classes"])
    if algorithm == "UNCONDITIONAL_FREQUENCY":
        entry = model["counts"].get("product")
        if not isinstance(entry, list):
            return None
        return _laplace([int(v) for v in entry])
    if algorithm == "CONDITIONAL_FREQUENCY":
        return _apply_conditional_min_blocks(model, levels, minimum_leaf_blocks, classes)
    if not model["intercepts"] and not model["coefficients"] and not model["trees"]:
        # constant-target shortcut model (single class / degenerate sample set)
        entry = model["counts"].get("product")
        if isinstance(entry, list):
            return _laplace([int(v) for v in entry])
        return None
    if algorithm in ("L2_SOFTMAX", "L2_LOGISTIC"):
        probabilities = _apply_softmax(model["coefficients"], model["intercepts"], vector)
    elif algorithm == "GRADIENT_BOOSTED_TREE":
        probabilities = _apply_gbm(model, vector)
    else:
        return None
    if probabilities is None or not _finite_iterable(probabilities):
        return None
    total = sum(probabilities)
    if total <= 0:
        return None
    return [value / total for value in probabilities]


def _fit_binary_candidate(algorithm: str, X: list[tuple[float, ...]], y: list[int],
                          samples: list[_Sample], config: dict[str, JsonValue],
                          center: list[float], scale: list[float]) -> dict[str, JsonValue] | None:
    feature_keys = config["feature_keys"]
    counts = [sum(1 for index in y if index == 0), sum(1 for index in y if index == 1)]
    if min(counts) == 0:
        return _constant_classification_model(
            algorithm, feature_keys, BINARY_CLASSES, counts, center, scale)
    if algorithm == "CONDITIONAL_FREQUENCY":
        outcomes = [BINARY_CLASSES[index] for index in y]
        levels = [sample.levels for sample in samples]
        return _fit_conditional(X, outcomes, levels, BINARY_CLASSES, feature_keys, center, scale)
    if algorithm == "L2_LOGISTIC":
        fitted = _softmax_gd(X, y, 2, float(config["l2"]),
                             float(config["learning_rate"]), int(config["max_iterations"]))
        if fitted is None:
            return None
        weights, bias = fitted
        return {
            "algorithm": "L2_LOGISTIC", "feature_keys": list(feature_keys),
            "center": list(center), "scale": list(scale), "classes": list(BINARY_CLASSES),
            "coefficients": weights, "intercepts": bias, "trees": [], "counts": {},
            "samples": {}, "temperature": 1.0, "fit_status": "CONVERGED",
        }
    if algorithm == "GRADIENT_BOOSTED_TREE":
        return _fit_gbm(X, y, BINARY_CLASSES, config, center, scale, feature_keys)
    return None


def _apply_binary_candidate(model: dict[str, JsonValue], vector: tuple[float, ...],
                            levels: tuple[str, str, str],
                            minimum_leaf_blocks: int) -> float | None:
    probabilities = _apply_multiclass_candidate(model, vector, levels, minimum_leaf_blocks)
    if probabilities is None or len(probabilities) != 2:
        return None
    return probabilities[1]


# ---------------------------------------------------------------------------
# public fit entry point
# ---------------------------------------------------------------------------

def fit_forecast_models(training_features: tuple[FeatureSnapshot, ...],
                        training_regimes: tuple[RegimeAssessment, ...],
                        training_requests: tuple[ForecastRequest, ...],
                        training_labels: tuple[PathLabel, ...],
                        calibration_features: tuple[FeatureSnapshot, ...],
                        calibration_regimes: tuple[RegimeAssessment, ...],
                        calibration_requests: tuple[ForecastRequest, ...],
                        calibration_labels: tuple[PathLabel, ...],
                        fit_config: Mapping[str, JsonValue],
                        reference: RegimeReference, *,
                        seasonality: SeasonalityModel | None) -> ForecastModelSet:
    """Fit, select, refit and calibrate the first-round model families.

    Stage order is fixed (§4): features are already PIT; the regime reference
    is fitted before probability fitting; labels mature before their segment
    deadline; family selection happens on the inner development split only;
    the final refit uses all legal outer training samples; the outer
    calibration segment only chooses temperatures; evidence is generated for
    both ordinary and martingale policies and references the new model id.
    """
    training_access = _TupleSegmentAccess(training_features, training_requests)
    calibration_access = _TupleSegmentAccess(calibration_features, calibration_requests)
    return _fit_forecast_models_core(
        training_access, training_regimes, training_labels,
        calibration_access, calibration_regimes, calibration_labels,
        fit_config, reference, seasonality=seasonality)


def _fit_forecast_models_from_views(training_features,
                                    training_regimes: tuple[RegimeAssessment, ...],
                                    training_requests,
                                    training_labels: tuple[PathLabel, ...],
                                    calibration_features,
                                    calibration_regimes: tuple[RegimeAssessment, ...],
                                    calibration_requests,
                                    calibration_labels: tuple[PathLabel, ...],
                                    fit_config: Mapping[str, JsonValue],
                                    reference: RegimeReference, *,
                                    seasonality: SeasonalityModel | None) -> ForecastModelSet:
    """Internal view entry (MEMORY-DISPOSITION §3): identical validation,
    error order and numeric implementation as ``fit_forecast_models``; the
    snapshot/request collection positions are bound store views."""
    training_access = _ViewSegmentAccess(training_features, training_requests)
    calibration_access = _ViewSegmentAccess(calibration_features, calibration_requests)
    return _fit_forecast_models_core(
        training_access, training_regimes, training_labels,
        calibration_access, calibration_regimes, calibration_labels,
        fit_config, reference, seasonality=seasonality)


def _fit_forecast_models_core(training_access,
                              training_regimes: tuple[RegimeAssessment, ...],
                              training_labels: tuple[PathLabel, ...],
                              calibration_access,
                              calibration_regimes: tuple[RegimeAssessment, ...],
                              calibration_labels: tuple[PathLabel, ...],
                              fit_config: Mapping[str, JsonValue],
                              reference: RegimeReference, *,
                              seasonality: SeasonalityModel | None) -> ForecastModelSet:
    config = _validate_fit_config(fit_config)
    feature_keys = tuple(config["feature_keys"])
    training_end = int(config["training_end_nano"])
    calibration_end = int(config["calibration_end_nano"])
    valid_until = int(config["model_valid_until_nano"])
    if not isinstance(reference, RegimeReference):
        raise TypeError("reference_must_be_RegimeReference")
    if reference.available_at_nano > training_end:
        raise FitError("regime_reference_available_after_training_end")
    seasonal_bound_hash = _bind_seasonality(
        training_access, calibration_access, feature_keys, seasonality,
        training_end)

    training_samples = _validate_join_access(
        training_access, training_access, training_regimes, training_labels,
        feature_keys, str(config["label_version"]), training_end, "label_late_for_training")
    calibration_samples = _validate_join_access(
        calibration_access, calibration_access, calibration_regimes, calibration_labels,
        feature_keys, str(config["label_version"]), calibration_end, "label_late_for_calibration")

    training_ids = {sample.request_id for sample in training_samples}
    calibration_ids = {sample.request_id for sample in calibration_samples}
    if training_ids & calibration_ids:
        raise FitError("training_calibration_overlap")
    if any(sample.decision_nano > training_end for sample in training_samples):
        raise FitError("training_decision_after_training_end")
    if any(sample.decision_nano <= training_end for sample in calibration_samples):
        raise FitError("calibration_decision_not_after_training_end")
    for assessment in calibration_regimes:
        if assessment.reference_id != reference.reference_id:
            raise FitError("calibration_regime_reference_mismatch")
    for assessment in training_regimes:
        if assessment.reference_id == reference.reference_id \
                and assessment.decision_nano < reference.available_at_nano:
            raise FitError("training_regime_reference_not_yet_available")
    latest_label = max([sample.label_available_at for sample in training_samples]
                       + [sample.label_available_at for sample in calibration_samples]
                       + [sample.decision_nano for sample in calibration_samples])
    if valid_until <= latest_label:
        raise FitError("model_valid_until_not_after_calibration_inputs")

    all_days = sorted({sample.trading_day for sample in training_samples})
    split_index = math.floor(float(config["inner_split_fraction"]) * len(all_days))
    fit_days = set(all_days[:split_index])
    dev_days = [day for day in all_days[split_index:]]
    fit_samples = [s for s in training_samples if s.trading_day in fit_days]
    dev_samples = [s for s in training_samples if s.trading_day not in fit_days]

    # Purge by actual label influence intervals, then embargo from dev start.
    if fit_samples:
        boundary = min(s.decision_nano for s in dev_samples) if dev_samples else None
        if boundary is not None:
            fit_samples = [s for s in fit_samples if s.label.window_end_nano < boundary]
    if fit_samples and dev_samples:
        max_fit_label_end = max(s.label.window_end_nano for s in fit_samples)
        dev_samples = [s for s in dev_samples if s.window_start > max_fit_label_end]
    embargo_days = set(dev_days[:int(config["inner_embargo_days"])])
    dev_samples = [s for s in dev_samples if s.trading_day not in embargo_days]

    fit_day_count = len({s.trading_day for s in fit_samples})
    dev_day_count = len({s.trading_day for s in dev_samples})
    insufficient = (fit_day_count < int(config["inner_minimum_train_days"])
                    or dev_day_count < int(config["inner_minimum_validation_days"]))

    cells: dict[tuple[str, str, str, int], list[_Sample]] = {}
    for sample in training_samples:
        cells.setdefault(_cell_key(sample), []).append(sample)

    fit_ids = {sample.request_id for sample in fit_samples}
    dev_ids = {sample.request_id for sample in dev_samples}
    models: dict[str, dict[str, JsonValue]] = {}

    for cell in sorted(cells):
        cell_samples = sorted(cells[cell], key=lambda s: (s.decision_nano, s.request_id))
        product, mode, _, horizon_value = cell
        horizon = cell_samples[0].horizon
        common = _common_samples(cell_samples)

        def register(task: str, barrier: str, algorithm: str,
                     model: dict[str, JsonValue], score: float | None,
                     selected: bool, reason: str) -> None:
            model = dict(model)
            model["samples"] = dict(model.get("samples", {}))
            model["samples"]["development_score"] = [score] if score is not None else []
            if selected:
                model["fit_status"] = f"SELECTED:{reason}"
            elif reason.startswith("FAILED") or reason.startswith("EXCLUDED"):
                model["fit_status"] = reason
            models[model_key(product, mode, horizon, barrier, task, algorithm)] = model

        task_specs: list[tuple[str, str, list[_Sample]]] = [
            (TASK_DIRECTION, COMMON_BARRIER, common),
            (TASK_PATH, "ATR_0.5", [s for s in cell_samples if s.barrier == "ATR_0.5"]),
            (TASK_PATH, "ATR_1", [s for s in cell_samples if s.barrier == "ATR_1"]),
            (TASK_PATH, "ATR_2", [s for s in cell_samples if s.barrier == "ATR_2"]),
        ]
        for event in PERSISTENCE_EVENTS:
            task_specs.append((f"PERSIST:{event}", COMMON_BARRIER, common))
        task_specs.extend([
            ("STOP_TOUCH:LONG", COMMON_BARRIER, common),
            ("STOP_TOUCH:SHORT", COMMON_BARRIER, common),
            ("RETURN_BEFORE_BREAK", COMMON_BARRIER, common),
            (TASK_QUANTILE, COMMON_BARRIER, common),
            (TASK_TIME_UP, "ATR_0.5", [s for s in cell_samples if s.barrier == "ATR_0.5"]),
            (TASK_TIME_UP, "ATR_1", [s for s in cell_samples if s.barrier == "ATR_1"]),
            (TASK_TIME_UP, "ATR_2", [s for s in cell_samples if s.barrier == "ATR_2"]),
            (TASK_TIME_DOWN, "ATR_0.5", [s for s in cell_samples if s.barrier == "ATR_0.5"]),
            (TASK_TIME_DOWN, "ATR_1", [s for s in cell_samples if s.barrier == "ATR_1"]),
            (TASK_TIME_DOWN, "ATR_2", [s for s in cell_samples if s.barrier == "ATR_2"]),
            (TASK_TIME_RETURN, COMMON_BARRIER, common),
            (TASK_TIME_BREAK, COMMON_BARRIER, common),
        ])

        for task, barrier, task_samples in task_specs:
            if insufficient:
                register(task, barrier, "UNCONDITIONAL_FREQUENCY",
                         _failed_model("UNCONDITIONAL_FREQUENCY", list(feature_keys),
                                       "FAILED:insufficient_inner_split"),
                         None, False, "FAILED:insufficient_inner_split")
                continue
            usable = _usable_samples(task, task_samples)
            if not usable:
                register(task, barrier, "UNCONDITIONAL_FREQUENCY",
                         _failed_model("UNCONDITIONAL_FREQUENCY", list(feature_keys),
                                       "FAILED:no_samples"),
                         None, False, "FAILED:no_samples")
                continue
            _run_task_selection(
                task, barrier, usable, fit_ids, dev_ids, cell, config, register,
                horizon_value)

    if calibration_samples and not insufficient:
        _apply_calibration_temperatures(models, config, calibration_samples)

    source_hashes = {
        "training_snapshots": sha256_hex("\n".join(sorted(
            {s.snapshot_id for s in training_samples})).encode("utf-8")),
        "training_labels": sha256_hex("\n".join(sorted(
            {f"{s.request_id}@{s.label_available_at}" for s in training_samples})).encode("utf-8")),
        "calibration_requests": sha256_hex("\n".join(sorted(
            {s.request_id for s in calibration_samples})).encode("utf-8")),
        "regime_reference": reference.content_hash(),
        "fit_config": sha256_hex(canonical_json_bytes(dict(config))),
    }
    if seasonal_bound_hash is not None:
        source_hashes["seasonality"] = seasonal_bound_hash

    # 变更申请-004 §3 generation order: final post-temperature parameters and
    # back-reference-free evidence content → set content hash / set id →
    # back-fill every evidence.model_id → canonical evidence ids → assemble.
    pending_evidence: list[CalibrationEvidence] = []
    if calibration_samples and not insufficient:
        request_getter = calibration_access.request_getter()
        predictions = _calibration_predictions(
            models, PENDING_MODEL_SET_ID, config, calibration_samples,
            calibration_regimes, reference, request_getter)
        block_ids = {sample.request_id: sample.trading_day.isoformat()
                     for sample in calibration_samples}
        referenced_snapshots = {prediction.snapshot_id for prediction in predictions}
        evidence_features = calibration_access.evidence_features(referenced_snapshots)
        for policy in (confidence_policy("TREND"), confidence_policy("MARTINGALE")):
            if isinstance(evidence_features, tuple):
                pending_evidence.extend(build_calibration_evidence(
                    tuple(predictions), tuple(s.label for s in calibration_samples),
                    block_ids, policy, calibration_end, valid_until,
                    features=evidence_features))
            else:
                pending_evidence.extend(_build_calibration_evidence_from_view(
                    tuple(predictions), tuple(s.label for s in calibration_samples),
                    block_ids, policy, calibration_end, valid_until,
                    features=evidence_features))

    projected_evidence = sorted(
        (evidence_projection_without_back_references(row) for row in pending_evidence),
        key=lambda projection: canonical_json_bytes(projection))
    identity_payload = {
        "regime_reference_id": reference.reference_id,
        "feature_keys": list(feature_keys),
        "training_end_nano": training_end,
        "calibration_end_nano": calibration_end,
        "valid_until_nano": valid_until,
        "fit_config": dict(config),
        "models": {key: dict(value) for key, value in sorted(models.items())},
        "source_hashes": dict(sorted(source_hashes.items())),
        "evidence": projected_evidence,
    }
    content_hash = sha256_hex(canonical_json_bytes(identity_payload))
    model_set_id = f"forecast_model_set:{content_hash}"

    evidence: tuple[CalibrationEvidence, ...] = ()
    if pending_evidence:
        filled = [dataclasses.replace(row, model_id=model_set_id)
                  for row in pending_evidence]
        evidence = tuple(
            dataclasses.replace(row, evidence_id=canonical_evidence_id(row))
            for row in filled)
        evidence = tuple(sorted(
            evidence,
            key=lambda row: canonical_json_bytes(
                evidence_projection_without_back_references(row))))

    return ForecastModelSet(
        model_set_id=model_set_id,
        regime_reference_id=reference.reference_id,
        feature_keys=feature_keys,
        training_end_nano=training_end,
        calibration_end_nano=calibration_end,
        valid_until_nano=valid_until,
        fit_config=MappingProxyType(dict(config)),
        models=MappingProxyType(dict(models)),
        evidence=evidence,
        source_hashes=MappingProxyType(dict(source_hashes)),
    )


# ---------------------------------------------------------------------------
# per-task candidate selection (§5.1)
# ---------------------------------------------------------------------------

def _usable_samples(task: str, samples: list[_Sample]) -> list[_Sample]:
    label_check = {
        TASK_DIRECTION: lambda label: label.direction is not None,
        TASK_PATH: lambda label: label.path_outcome is not None,
        TASK_QUANTILE: lambda label: label.terminal_atr is not None,
        TASK_TIME_UP: lambda label: label.path_outcome is not None,
        TASK_TIME_DOWN: lambda label: label.path_outcome is not None,
        TASK_TIME_RETURN: lambda label: label.competing_outcome is not None,
        TASK_TIME_BREAK: lambda label: label.competing_outcome is not None,
        "RETURN_BEFORE_BREAK": lambda label: label.competing_outcome in
            ("RETURN_FIRST", "BREAK_FIRST", "NEITHER"),
    }
    for event in PERSISTENCE_EVENTS:
        label_check[f"PERSIST:{event}"] = (
            lambda label, e=event: label.persistence.get(e) is not None)
    for side in ("LONG", "SHORT"):
        label_check[f"STOP_TOUCH:{side}"] = (
            lambda label, s=side: label.stop_touched.get(s) is not None)
    check = label_check[task]
    return [sample for sample in samples if check(sample.label)]


def _task_kind(task: str) -> str:
    if task in (TASK_DIRECTION, TASK_PATH):
        return "multiclass"
    if task in (TASK_QUANTILE,):
        return "quantile"
    if task in (TASK_TIME_UP, TASK_TIME_DOWN, TASK_TIME_RETURN, TASK_TIME_BREAK):
        return "time"
    return "binary"


def _task_algorithms(task: str) -> tuple[str, ...]:
    kind = _task_kind(task)
    if kind == "multiclass":
        return _MULTICLASS_ALGORITHMS
    if kind == "binary":
        return _BINARY_ALGORITHMS
    if kind == "quantile":
        return _QUANTILE_ALGORITHMS
    return _TIME_ALGORITHMS


def _fit_candidate(task: str, algorithm: str, samples: list[_Sample],
                   config: dict[str, JsonValue], center: list[float],
                   scale: list[float]) -> dict[str, JsonValue] | None:
    X = [_standardized(sample.vector, center, scale) for sample in samples]
    kind = _task_kind(task)
    if kind == "multiclass":
        classes = DIRECTION_CLASSES if task == TASK_DIRECTION else PATH_CLASSES
        y = [_multiclass_target(sample, classes) for sample in samples]
        return _fit_multiclass_candidate(algorithm, X, y, classes, samples, config, center, scale)
    if kind == "binary":
        y = [1 if _binary_target(sample, task) >= 1.0 else 0 for sample in samples]
        return _fit_binary_candidate(algorithm, X, y, samples, config, center, scale)
    if kind == "quantile":
        values = [sample.label.terminal_atr for sample in samples]
        if algorithm == "EMPIRICAL_QUANTILE":
            levels = [sample.levels for sample in samples]
            return _fit_empirical_quantile(values, levels, list(config["quantiles"]),
                                           list(config["feature_keys"]), center, scale,
                                           int(config["minimum_leaf_blocks"]))
        return _fit_linear_quantile(X, values, list(config["quantiles"]), config,
                                    list(config["feature_keys"]), center, scale)
    # time tasks
    hazard_classes = COMPETING_HAZARD_CLASSES if task in (TASK_TIME_RETURN, TASK_TIME_BREAK) \
        else TOUCH_HAZARD_CLASSES
    if algorithm == "EMPIRICAL_CDF":
        first_steps = [_time_cdf_sample(sample, task) for sample in samples]
        levels = [sample.levels for sample in samples]
        return _fit_empirical_cdf(first_steps, levels, list(config["feature_keys"]),
                                  center, scale)
    event_steps = [_time_event_pair(sample, task) for sample in samples]
    horizon_value = samples[0].horizon.value
    return _fit_competing_risk(X, event_steps, hazard_classes, horizon_value, config,
                               list(config["feature_keys"]), center, scale)


def _time_cdf_sample(sample: _Sample, task: str) -> int:
    target = _time_targets(sample, task)
    if target is None:
        return -1
    first_step, ambiguous = target
    if ambiguous:
        return -1
    return first_step if first_step is not None else 0


def _time_event_pair(sample: _Sample, task: str) -> tuple[str, int | None]:
    target = _time_targets(sample, task)
    if target is None or target[1]:
        return ("NEVER", None)
    first_step = target[0]
    if first_step is None:
        return ("NEVER", None)
    if task == TASK_TIME_UP or task == TASK_TIME_DOWN:
        return ("TOUCH", first_step)
    if task == TASK_TIME_RETURN:
        return ("RETURN", first_step)
    return ("BREAK", first_step)


def _apply_candidate(task: str, model: dict[str, JsonValue], sample: _Sample,
                     center: list[float], scale: list[float],
                     minimum_leaf_blocks: int, horizon_value: int):
    vector = _standardized(sample.vector, center, scale)
    algorithm = model["algorithm"]
    kind = _task_kind(task)
    if kind == "multiclass":
        return _apply_multiclass_candidate(model, vector, sample.levels, minimum_leaf_blocks)
    if kind == "binary":
        return _apply_binary_candidate(model, vector, sample.levels, minimum_leaf_blocks)
    if kind == "quantile":
        if algorithm == "EMPIRICAL_QUANTILE":
            return _apply_empirical_quantile(model, sample.levels, list(CANONICAL_QUANTILE_TAUS))
        return _apply_linear_quantile(model, vector)
    if algorithm == "EMPIRICAL_CDF":
        return _apply_empirical_cdf(model, sample.levels, horizon_value)
    hazard_classes = tuple(model["classes"])
    event_class = "TOUCH" if task in (TASK_TIME_UP, TASK_TIME_DOWN) else (
        "RETURN" if task == TASK_TIME_RETURN else "BREAK")
    if event_class not in hazard_classes:
        return None
    return _apply_competing_risk(model, vector, horizon_value, event_class)


def _candidate_consistent(task: str, prediction) -> bool:
    if prediction is None:
        return False
    kind = _task_kind(task)
    if kind == "multiclass":
        if not _finite_iterable(prediction):
            return False
        total = sum(prediction)
        return abs(total - 1.0) <= 1e-9 and all(value >= 0.0 for value in prediction)
    if kind == "binary":
        return math.isfinite(prediction) and 0.0 <= prediction <= 1.0
    if kind == "quantile":
        return (_finite_iterable(prediction)
                and all(prediction[k] <= prediction[k + 1]
                        for k in range(len(prediction) - 1)))
    previous = -1.0
    for cumulative, unknown in prediction:
        if not math.isfinite(cumulative) or not math.isfinite(unknown):
            return False
        if cumulative < previous - 1e-12 or cumulative > 1.0 + 1e-12:
            return False
        if cumulative + unknown > 1.0 + 1e-9:
            return False
        previous = cumulative
    return True


def _candidate_dev_loss(task: str, samples: list[_Sample], predictions: list,
                        quantiles: list[float], horizon_value: int) -> float | None:
    kind = _task_kind(task)
    if kind == "multiclass":
        classes = DIRECTION_CLASSES if task == TASK_DIRECTION else PATH_CLASSES
        return _multiclass_brier_loss(samples, predictions, classes)
    if kind == "binary":
        return _binary_brier_loss(samples, predictions, task)
    if kind == "quantile":
        return _pinball_loss(samples, predictions, quantiles)
    return _time_brier_loss(samples, predictions, task, horizon_value)


def _bind_seasonality(training_access, calibration_access,
                      feature_keys: tuple[str, ...],
                      seasonality: SeasonalityModel | None,
                      training_end: int) -> str | None:
    """变更申请-006 Q1 seasonality binding matrix; returns the bound content
    hash (bare lowercase 64-hex) or None when no seasonal columns exist.

    S is the existence of any ``SEASONAL:`` factor column across the actual
    union of training+calibration snapshot values — rows whose value is
    None/MISSING/WARMUP/INVALID still count as existing columns. Shared by
    the public tuple entry and the internal view entry.
    """
    if seasonality is not None and not isinstance(seasonality, SeasonalityModel):
        raise TypeError("seasonality_must_be_SeasonalityModel_or_None")
    seasonal_columns = (training_access.seasonal_column_exists()
                        or calibration_access.seasonal_column_exists())
    declared = any(key.startswith("SEASONAL:") for key in feature_keys)
    if declared and not seasonal_columns:
        raise FitError("seasonal_feature_keys_declared_without_columns")
    if not seasonal_columns:
        if seasonality is not None:
            raise FitError("seasonality_provided_without_seasonal_columns")
        return None
    if seasonality is None:
        raise FitError("seasonality_required_for_seasonal_columns")
    if seasonality.model_id != f"seasonality:{seasonality.content_hash()}":
        raise FitError("seasonality_model_id_mismatch")
    if seasonality.fit_end_nano > training_end:
        raise FitError("seasonality_fit_end_after_training_end")
    for handle in calibration_access.iter_snapshot_handles():
        if seasonality.fit_end_nano > calibration_access.snapshot_id_and_decision(handle)[1]:
            raise FitError("seasonality_fit_end_after_calibration_decision")
    return seasonality.content_hash()


def _rank_candidates(ranked: list[tuple[float, int, str, dict[str, JsonValue]]]
                     ) -> list[tuple[float, int, str, dict[str, JsonValue]]]:
    """Order candidates by minimum dev loss with the §5.1 tie rule.

    Repeatedly take the smallest remaining loss; among candidates within
    TIE_TOLERANCE (≤1e-12) of it, take the one earliest in the fixed
    ALGORITHM_PRIORITY order.
    """
    pool = sorted(ranked, key=lambda item: item[0])
    ordered: list[tuple[float, int, str, dict[str, JsonValue]]] = []
    while pool:
        head_loss = pool[0][0]
        tied = [entry for entry in pool if entry[0] <= head_loss + TIE_TOLERANCE]
        pick = min(tied, key=lambda entry: entry[1])
        pool.remove(pick)
        ordered.append(pick)
    return ordered


def _run_task_selection(task: str, barrier: str, task_samples: list[_Sample],
                        fit_ids: set[str], dev_ids: set[str],
                        cell: tuple[str, str, str, int], config: dict[str, JsonValue],
                        register, horizon_value: int) -> None:
    product, mode, _, _ = cell
    horizon = task_samples[0].horizon
    quantiles = [float(q) for q in config["quantiles"]]
    minimum_leaf_blocks = int(config["minimum_leaf_blocks"])
    fit_samples = [s for s in task_samples if s.request_id in fit_ids]
    dev_samples = [s for s in task_samples if s.request_id in dev_ids]

    if not fit_samples or not dev_samples:
        register(task, barrier, "UNCONDITIONAL_FREQUENCY",
                 _failed_model("UNCONDITIONAL_FREQUENCY", list(config["feature_keys"]),
                               "FAILED:inner_split_empty"),
                 None, False, "FAILED:inner_split_empty")
        return

    inner_center, inner_scale = _standardize_stats([s.vector for s in fit_samples])
    final_center, final_scale = _standardize_stats([s.vector for s in task_samples])

    ranked: list[tuple[float, int, str, dict[str, JsonValue]]] = []
    for priority, algorithm in enumerate(_task_algorithms(task)):
        model = _fit_candidate(task, algorithm, fit_samples, config, inner_center, inner_scale)
        if model is None:
            register(task, barrier, algorithm,
                     _failed_model(algorithm, list(config["feature_keys"]),
                                   "FAILED:fit_failed"),
                     None, False, "FAILED:fit_failed")
            continue
        predictions = []
        consistent = True
        for sample in dev_samples:
            prediction = _apply_candidate(
                task, model, sample, inner_center, inner_scale, minimum_leaf_blocks,
                horizon_value)
            if not _candidate_consistent(task, prediction):
                consistent = False
                break
            predictions.append(prediction)
        if not consistent:
            register(task, barrier, algorithm, model, None, False,
                     "EXCLUDED:development_consistency")
            continue
        loss = _candidate_dev_loss(task, dev_samples, predictions, quantiles, horizon_value)
        if loss is None or not math.isfinite(loss):
            register(task, barrier, algorithm, model, None, False, "EXCLUDED:loss_not_finite")
            continue
        register(task, barrier, algorithm, model, loss, False, "CONVERGED")
        ranked.append((loss, priority, algorithm, model))

    # §5.1 ranking: minimum development loss; losses within TIE_TOLERANCE
    # (≤1e-12) fall back to the fixed priority order. Selection and the final
    # refit fallback walk the SAME order — never priority alone.
    ordered = _rank_candidates(ranked)

    selected_entry = None
    fallback_reason = ""
    for loss, priority, algorithm, _ in ordered:
        model = _fit_candidate(task, algorithm, task_samples, config, final_center, final_scale)
        if model is None:
            fallback_reason = f"SELECTED:{algorithm}:final_refit_failed"
            continue
        consistent = True
        for sample in task_samples[:1] + task_samples[-1:]:
            prediction = _apply_candidate(
                task, model, sample, final_center, final_scale, minimum_leaf_blocks,
                horizon_value)
            if not _candidate_consistent(task, prediction):
                consistent = False
                break
        if not consistent:
            fallback_reason = f"SELECTED:{algorithm}:final_consistency_failed"
            continue
        selected_entry = (loss, priority, algorithm, model)
        break
    if selected_entry is None:
        if ordered:
            register(task, barrier, ordered[0][2],
                     _failed_model(ordered[0][2], list(config["feature_keys"]),
                                   fallback_reason or "FAILED:no_qualified_candidate"),
                     ordered[0][0], False, fallback_reason or "FAILED:no_qualified_candidate")
        else:
            register(task, barrier, "UNCONDITIONAL_FREQUENCY",
                     _failed_model("UNCONDITIONAL_FREQUENCY", list(config["feature_keys"]),
                                   "FAILED:no_qualified_candidate"),
                     None, False, "FAILED:no_qualified_candidate")
        return
    loss, _, algorithm, model = selected_entry
    register(task, barrier, algorithm, model, loss, True, algorithm)


# ---------------------------------------------------------------------------
# model application shared with forecast.py
# ---------------------------------------------------------------------------

def _selected_model(models: Mapping[str, dict[str, JsonValue]], product: str, mode: str,
                    horizon: Horizon, barrier: str, task: str) -> dict[str, JsonValue] | None:
    for algorithm in _task_algorithms(task):
        key = model_key(product, mode, horizon, barrier, task, algorithm)
        model = models.get(key)
        if isinstance(model, dict) and str(model.get("fit_status", "")).startswith("SELECTED"):
            return model
    return None


def _raw_out_of_distribution(vector: tuple[float, ...], center: list[float],
                             scale: list[float], bound: float = 10.0) -> bool:
    return any(abs((value - center[i]) / scale[i]) > bound
               for i, value in enumerate(vector) if i < len(center))


def apply_model_set(models: Mapping[str, dict[str, JsonValue]],
                    feature_keys: tuple[str, ...], minimum_leaf_blocks: int,
                    request: ForecastRequest, base_regime: str) -> dict[str, object]:
    """Apply the selected models of one model set to a single request.

    Returns component values used by ``predict_market`` and by the internal
    calibration-prediction assembly; missing tasks stay None and are reported
    so callers can degrade quality instead of guessing.
    """
    features = request.features
    product = features.product
    mode = "MINUTE" if request.horizon.unit == "TRADING_MINUTE" else "DAILY"
    horizon = request.horizon
    vector = feature_vector(features, feature_keys)
    missing: list[str] = []
    if vector is None:
        return {
            "direction": None, "path": None, "atr_quantiles": None,
            "persistence": {}, "stop": {}, "return_before_break": None,
            "time_up": None, "time_down": None, "time_return": None, "time_break": None,
            "missing": [f"features_missing:{key}" for key in feature_keys],
            "out_of_distribution": False,
        }
    levels = _condition_levels(product, features.session_kind, base_regime)

    def standard(model: dict[str, JsonValue]) -> tuple[float, ...]:
        return _standardized(vector, model["center"], model["scale"])

    components: dict[str, object] = {
        "persistence": {}, "stop": {}, "missing": missing, "out_of_distribution": False}

    direction_model = _selected_model(models, product, mode, horizon, COMMON_BARRIER, TASK_DIRECTION)
    if direction_model is None:
        missing.append("task_missing:DIRECTION")
        components["direction"] = None
    else:
        probabilities = _apply_multiclass_candidate(
            direction_model, standard(direction_model), levels, minimum_leaf_blocks)
        if probabilities is None:
            missing.append("task_missing:DIRECTION")
            components["direction"] = None
        else:
            components["direction"] = {
                class_key: probabilities[index] for index, class_key in enumerate(DIRECTION_CLASSES)}
            if _raw_out_of_distribution(vector, direction_model["center"], direction_model["scale"]):
                components["out_of_distribution"] = True

    path_model = _selected_model(models, product, mode, horizon, request.barrier_key, TASK_PATH)
    if path_model is None:
        missing.append("task_missing:PATH")
        components["path"] = None
    else:
        probabilities = _apply_multiclass_candidate(
            path_model, standard(path_model), levels, minimum_leaf_blocks)
        if probabilities is None:
            missing.append("task_missing:PATH")
            components["path"] = None
        else:
            temperature = float(path_model.get("temperature", 1.0))
            scaled = apply_temperature(probabilities, temperature)
            components["path"] = {
                class_key: scaled[index] for index, class_key in enumerate(PATH_CLASSES)}

    if direction_model is not None and components["direction"] is not None:
        temperature = float(direction_model.get("temperature", 1.0))
        scaled = apply_temperature(
            [components["direction"][key] for key in DIRECTION_CLASSES], temperature)
        components["direction"] = {
            key: scaled[index] for index, key in enumerate(DIRECTION_CLASSES)}

    quantile_model = _selected_model(models, product, mode, horizon, COMMON_BARRIER, TASK_QUANTILE)
    if quantile_model is None:
        missing.append("task_missing:QUANTILE")
        components["atr_quantiles"] = None
    else:
        if quantile_model["algorithm"] == "EMPIRICAL_QUANTILE":
            values = _apply_empirical_quantile(
                quantile_model, levels, list(CANONICAL_QUANTILE_TAUS))
        else:
            values = _apply_linear_quantile(quantile_model, standard(quantile_model))
        if values is None:
            missing.append("task_missing:QUANTILE")
            components["atr_quantiles"] = None
        else:
            components["atr_quantiles"] = values

    persistence: dict[str, float] = {}
    for event in PERSISTENCE_EVENTS:
        task = f"PERSIST:{event}"
        model = _selected_model(models, product, mode, horizon, COMMON_BARRIER, task)
        if model is None:
            continue
        probability = _apply_binary_candidate(
            model, standard(model), levels, minimum_leaf_blocks)
        if probability is None:
            continue
        temperature = float(model.get("temperature", 1.0))
        scaled = apply_temperature([1.0 - probability, probability], temperature)
        persistence[event] = scaled[1]
    components["persistence"] = persistence

    stop: dict[str, float] = {}
    for side in ("LONG", "SHORT"):
        task = f"STOP_TOUCH:{side}"
        model = _selected_model(models, product, mode, horizon, COMMON_BARRIER, task)
        if model is None:
            continue
        probability = _apply_binary_candidate(
            model, standard(model), levels, minimum_leaf_blocks)
        if probability is None:
            continue
        temperature = float(model.get("temperature", 1.0))
        scaled = apply_temperature([1.0 - probability, probability], temperature)
        stop[side] = scaled[1]
    components["stop"] = stop

    return_model = _selected_model(
        models, product, mode, horizon, COMMON_BARRIER, "RETURN_BEFORE_BREAK")
    if return_model is None:
        components["return_before_break"] = None
    else:
        probability = _apply_binary_candidate(
            return_model, standard(return_model), levels, minimum_leaf_blocks)
        temperature = float(return_model.get("temperature", 1.0))
        if probability is None:
            components["return_before_break"] = None
        else:
            scaled = apply_temperature([1.0 - probability, probability], temperature)
            components["return_before_break"] = scaled[1]

    time_specs = (
        ("time_up", TASK_TIME_UP, request.barrier_key),
        ("time_down", TASK_TIME_DOWN, request.barrier_key),
        ("time_return", TASK_TIME_RETURN, COMMON_BARRIER),
        ("time_break", TASK_TIME_BREAK, COMMON_BARRIER),
    )
    for name, task, barrier in time_specs:
        model = _selected_model(models, product, mode, horizon, barrier, task)
        if model is None:
            components[name] = None
            continue
        if model["algorithm"] == "EMPIRICAL_CDF":
            entries = _apply_empirical_cdf(model, levels, horizon.value)
        else:
            hazard_classes = tuple(model["classes"])
            event_class = "TOUCH" if task in (TASK_TIME_UP, TASK_TIME_DOWN) else (
                "RETURN" if task == TASK_TIME_RETURN else "BREAK")
            entries = None
            if event_class in hazard_classes:
                entries = _apply_competing_risk(
                    model, standard(model), horizon.value, event_class)
        if entries is None or not _candidate_consistent(task, entries):
            components[name] = None
            missing.append(f"task_missing:{task}")
        else:
            components[name] = entries

    return components


class QuantileCrossingError(ValueError):
    """Real quantile crossing (a later quantile below an earlier one).

    Ties are legal (discrete/degenerate distributions may have equal
    quantiles); only a genuine crossing is invalid. No jitter, sorting or
    cumulative-maximum patching is ever applied (变更申请-006 Q3).
    """


def quantile_triple(atr_quantiles: list[float], reference_price: Decimal,
                    atr_points: Decimal) -> tuple[dict[str, float], dict[str, Decimal], dict[str, float]]:
    """Return/point/ATR quantile dictionaries that reconcile across units.

    Non-decreasing input (ties allowed, all-equal allowed) is passed through
    unchanged; a real crossing raises ``QuantileCrossingError``.
    """
    atr_values = [float(v) for v in atr_quantiles]
    if any(atr_values[i] > atr_values[i + 1] for i in range(len(atr_values) - 1)):
        raise QuantileCrossingError("atr_quantiles_crossing")
    point: dict[str, Decimal] = {}
    returns: dict[str, float] = {}
    for key, atr_value in zip(QUANTILE_CLASSES, atr_values):
        # Frozen M1/03 convention: point quantiles are DISPLACEMENT points
        # relative to C (C_end − C), return = displacement/C, atr =
        # displacement/ATR — never absolute price levels (M1 rejects those).
        point_value = Decimal(str(atr_value)) * atr_points
        point[key] = point_value
        returns[key] = float(point_value / reference_price)
    atr_map = {key: value for key, value in zip(QUANTILE_CLASSES, atr_values)}
    return returns, point, atr_map


def _calibration_predictions(models: dict[str, dict[str, JsonValue]], model_set_id: str,
                             config: dict[str, JsonValue], samples: list[_Sample],
                             calibration_regimes: tuple[RegimeAssessment, ...],
                             reference: RegimeReference,
                             request_getter) -> list[MarketForecast]:
    feature_keys = tuple(config["feature_keys"])
    minimum_leaf_blocks = int(config["minimum_leaf_blocks"])
    assessments = {assessment.snapshot_id: assessment for assessment in calibration_regimes}
    predictions: list[MarketForecast] = []
    for sample in samples:
        request = request_getter(sample.request_id)
        assessment = assessments.get(sample.snapshot_id)
        if request is None or assessment is None:
            continue
        components = apply_model_set(
            models, feature_keys, minimum_leaf_blocks, request, assessment.base)
        published = request.window_start_nano - 1
        forecast = assemble_forecast(
            components, request, assessment, model_set_id, published,
            reference.reference_id, evidence=())
        predictions.append(forecast)
    return predictions


def _apply_calibration_temperatures(models: dict[str, dict[str, JsonValue]],
                                    config: dict[str, JsonValue],
                                    calibration_samples: list[_Sample]) -> None:
    """Choose temperatures for selected classification models (§5.2)."""
    temperatures = [float(t) for t in config["temperature_candidates"]]
    minimum_leaf_blocks = int(config["minimum_leaf_blocks"])
    by_cell: dict[tuple[str, str, str, int], list[_Sample]] = {}
    for sample in calibration_samples:
        by_cell.setdefault(_cell_key(sample), []).append(sample)
    for cell in sorted(by_cell):
        product, mode, _, horizon_value = cell
        cell_samples = sorted(by_cell[cell], key=lambda s: s.request_id)
        horizon = cell_samples[0].horizon
        common = _common_samples(cell_samples)

        def tune(task: str, barrier: str, samples: list[_Sample]) -> None:
            model = _selected_model(models, product, mode, horizon, barrier, task)
            if model is None or _task_kind(task) not in ("multiclass", "binary"):
                return
            center, scale = model["center"], model["scale"]
            usable = _usable_samples(task, samples)
            if not usable:
                return
            if _task_kind(task) == "multiclass":
                classes = DIRECTION_CLASSES if task == TASK_DIRECTION else PATH_CLASSES
                targets = [classes.index(
                    sample.label.direction if task == TASK_DIRECTION
                    else sample.label.path_outcome) for sample in usable]
                predictions = [
                    _apply_multiclass_candidate(
                        model, _standardized(sample.vector, center, scale),
                        sample.levels, minimum_leaf_blocks) or []
                    for sample in usable]
                for probabilities in predictions:
                    if not _finite_iterable(probabilities) or abs(sum(probabilities) - 1.0) > 1e-9:
                        return
            else:
                targets = [1 if _binary_target(sample, task) >= 1.0 else 0 for sample in usable]
                predictions = [[1.0 - (_apply_binary_candidate(
                    model, _standardized(sample.vector, center, scale),
                    sample.levels, minimum_leaf_blocks) or 0.0),
                    _apply_binary_candidate(
                        model, _standardized(sample.vector, center, scale),
                        sample.levels, minimum_leaf_blocks) or 0.0] for sample in usable]
            model["temperature"] = _choose_temperature(model, targets, predictions, temperatures)

        tune(TASK_DIRECTION, COMMON_BARRIER, common)
        for barrier in BARRIER_ORDER:
            tune(TASK_PATH, barrier, [s for s in cell_samples if s.barrier == barrier])
        for event in PERSISTENCE_EVENTS:
            tune(f"PERSIST:{event}", COMMON_BARRIER, common)
        for side in ("LONG", "SHORT"):
            tune(f"STOP_TOUCH:{side}", COMMON_BARRIER, common)
        tune("RETURN_BEFORE_BREAK", COMMON_BARRIER, common)


def assemble_forecast(components: dict[str, object], request: ForecastRequest,
                      assessment: RegimeAssessment, model_id: str, published_at_nano: int,
                      regime_reference_id: str,
                      evidence: tuple[CalibrationEvidence, ...],
                      force_late: bool = False) -> MarketForecast:
    """Build the public MarketForecast record from model components.

    ``force_late`` marks a late publication (published >= window_start): the
    record is constructed directly with quality=INVALID, FORECAST_LATE in the
    reasons and expiry pinned to the original window_end (变更申请-002 §4).
    """
    features = request.features
    reference_price = features.reference_price
    atr_points = features.atr_points
    assert reference_price is not None and atr_points is not None

    missing = list(components.get("missing", []))
    direction = components.get("direction")
    if direction is None:
        direction = {"UP": 1.0 / 3.0, "DOWN": 1.0 / 3.0, "NEUTRAL": 1.0 / 3.0}
        if "task_missing:DIRECTION" not in missing:
            missing.append("task_missing:DIRECTION")
    path = components.get("path")
    if path is None:
        path = {"UP_FIRST": 0.25, "DOWN_FIRST": 0.25, "NEITHER": 0.25, "AMBIGUOUS": 0.25}
        if "task_missing:PATH" not in missing:
            missing.append("task_missing:PATH")

    atr_quantiles = components.get("atr_quantiles")
    if atr_quantiles is None:
        atr_quantiles = [-1.0, -0.5, 0.0, 0.5, 1.0]
        missing.append("task_missing:QUANTILE")
    return_quantiles, point_quantiles, atr_map = quantile_triple(
        atr_quantiles, reference_price, atr_points)

    time_cdf: list[TimeProbability] = []
    for name, event in (("time_up", "UP_TOUCH"), ("time_down", "DOWN_TOUCH"),
                        ("time_return", "RETURN_FIRST"), ("time_break", "BREAK_FIRST")):
        entries = components.get(name)
        if not entries:
            continue
        for index, (cumulative, unknown) in enumerate(entries):
            time_cdf.append(TimeProbability(
                event=event, step=index + 1,
                probability=min(1.0, max(0.0, cumulative)),
                unknown_probability=min(1.0, max(0.0, unknown))))

    if force_late:
        expires = request.window_end_nano
        missing.append("FORECAST_LATE")
        status = "INVALID"
    elif request.horizon.unit == "TRADING_MINUTE":
        expires = min(published_at_nano + MINUTE_EXPIRY_NANO, request.window_end_nano)
        status = "DEGRADED" if missing else "READY"
    else:
        expires = request.window_end_nano
        status = "DEGRADED" if missing else "READY"
    quality = DataQuality(
        status=status,
        reasons=tuple(sorted(set(missing))),
        temporal_basis=features.quality.temporal_basis,
        input_hash=sha256_hex(
            f"{request.request_id}|{model_id}|{published_at_nano}|{request.barrier_key}".encode()),
    )
    identity = "|".join((
        request.request_id, model_id, str(published_at_nano), request.barrier_key,
        assessment.assessment_id))
    return MarketForecast(
        forecast_id=stable_id("market_forecast", identity),
        request_id=request.request_id,
        snapshot_id=features.snapshot_id,
        contract_id=features.contract_id,
        product=features.product,
        trading_day=features.trading_day,
        session_id=features.session_id,
        session_kind=features.session_kind,
        duration_seconds=features.duration_seconds,
        assessment_id=assessment.assessment_id,
        base_regime=assessment.base,
        regime_reference_id=regime_reference_id,
        decision_nano=features.decision_nano,
        published_at_nano=published_at_nano,
        expires_at_nano=expires,
        horizon=request.horizon,
        window_start_nano=request.window_start_nano,
        window_end_nano=request.window_end_nano,
        barrier_key=request.barrier_key,
        reference_price=reference_price,
        atr_points=atr_points,
        upper_distance=request.upper_distance,
        lower_distance=request.lower_distance,
        frozen_range=request.frozen_range,
        label_version=LABEL_VERSION,
        model_id=model_id,
        direction_probabilities=direction,
        return_quantiles=return_quantiles,
        point_quantiles=point_quantiles,
        atr_quantiles=atr_map,
        path_probabilities=path,
        time_cdf=tuple(time_cdf),
        persistence_probabilities=dict(components.get("persistence", {})),
        stop_touch_probabilities=dict(components.get("stop", {})),
        return_before_break_probability=components.get("return_before_break"),
        evidence=evidence,
        out_of_distribution=bool(components.get("out_of_distribution", False)),
        quality=quality,
    )
