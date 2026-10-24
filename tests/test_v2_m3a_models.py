"""M3A model tests: families, frozen parameters, selection, identity (03-M3A §5.1, §8.4-8.7, §8.13).

The heavy fit fixture is built once per module (~180 synthetic training days
+ 30 calibration days, minute and daily modes); everything is deterministic
(seed 7) and offline.
"""
from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ak_trading.v2.contracts import (
    CalibrationEvidence,
    DataQuality,
    FactorValue,
    FeatureSnapshot,
    ForecastRequest,
    Horizon,
    PathLabel,
    RegimeAssessment,
    SessionWindow,
)
from ak_trading.v2.clock import horizon_window
from ak_trading.v2.artifacts import stable_id
from ak_trading.v2.probability.models import (
    ALGORITHM_PRIORITY,
    ForecastModelSet,
    _apply_linear_quantile,
    _build_tree,
    _fit_unconditional,
    _softmax_gd,
    _standardize_stats,
    _standardized,
    cumulative_incidence,
    default_fit_config,
    fit_forecast_models,
    model_key,
)
from ak_trading.v2.probability.models import (
    _fit_empirical_cdf,
    _apply_empirical_cdf,
)
from ak_trading.v2.probability.regimes import fit_regime_reference

NANO = 1_000_000_000
MINUTE = 60 * NANO
DAYN = 24 * 3600 * NANO
HASH = hashlib.sha256(b"m3a-models").hexdigest()
PRODUCT = "SHFE.RB"
CONTRACT = "SHFE.RB2605"
BASE = 1_770_000_000 * NANO
FEATURE_KEYS = ("TESTF:momentum", "TESTF:bias")
PERSISTENCE_KEYS = ("UP_TREND", "DOWN_TREND", "ORDERED_RANGE", "WIDE_RANGE",
                    "COMPRESSION", "UP_BREAKOUT", "DOWN_BREAKOUT")


def quality() -> DataQuality:
    return DataQuality(status="READY", reasons=(), temporal_basis="observed", input_hash=HASH)


def trading_days(count: int) -> list[date]:
    days: list[date] = []
    cursor = date(2026, 1, 12)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


DAYS = trading_days(214)
DAY_INDEX = {day: index for index, day in enumerate(DAYS)}
SESSIONS = tuple(
    SessionWindow(
        session_id=f"{PRODUCT}-{day.isoformat()}", product=PRODUCT, trading_day=day,
        start_nano=BASE + index * DAYN, end_nano=BASE + index * DAYN + 60 * MINUTE,
        kind="DAY", calendar_version="cal-v2.1.0", evidence_status="SCENARIO")
    for index, day in enumerate(DAYS))

_window_cache: dict[tuple[int, str, int], tuple[int, int]] = {}


def build_segment(segment_days: list[date], prefix: str, rng: random.Random):
    features: list[FeatureSnapshot] = []
    requests: list[ForecastRequest] = []
    labels: list[PathLabel] = []
    for day in segment_days:
        for mode, horizon in (("minute", Horizon("TRADING_MINUTE", 1)),
                              ("daily", Horizon("TRADING_DAY", 1))):
            momentum = rng.uniform(-1.5, 1.5)
            bias = rng.uniform(-0.3, 0.3)
            decision = BASE + DAY_INDEX[day] * DAYN - 1
            cache_key = (decision, horizon.unit, horizon.value)
            if cache_key not in _window_cache:
                _window_cache[cache_key] = horizon_window(decision, PRODUCT, horizon, SESSIONS)
            window_start, window_end = _window_cache[cache_key]
            snapshot_id = f"snap-{prefix}-{mode}-{len(features)}"
            values = (
                FactorValue("META:price_tick:v2.1.0", Decimal("1"), decision, quality()),
                FactorValue(FEATURE_KEYS[0], momentum, decision, quality()),
                FactorValue(FEATURE_KEYS[1], bias, decision, quality()),
            )
            snapshot = FeatureSnapshot(
                snapshot_id=snapshot_id, contract_id=CONTRACT, raw_symbol="RB2605",
                product=PRODUCT, trading_day=day, session_id=f"sess-{snapshot_id}",
                session_kind="DAILY" if mode == "daily" else "DAY",
                decision_nano=decision, available_at_nano=decision,
                duration_seconds=86400 if mode == "daily" else 60,
                source_bar_ids=(), reference_price=Decimal("100"),
                atr_points=Decimal("2"), values=values, daily_background_id=None,
                quality=quality())
            features.append(snapshot)
            up = momentum > 0.3
            down = momentum < -0.3
            terminal_atr = momentum * 0.5 + rng.uniform(-0.2, 0.2)
            compression = momentum > 0.8  # one varied binary task
            from ak_trading.v2.contracts import FrozenRange
            frozen = FrozenRange(f"range-{snapshot_id}", decision, Decimal("98"),
                                 Decimal("100"), Decimal("102"), Decimal("96"),
                                 Decimal("104"), snapshot_id)
            competing = ("RETURN_FIRST" if momentum > 0.8 else
                         "BREAK_FIRST" if momentum < -0.8 else "NEITHER")
            for barrier, distance in (("ATR_0.5", "1"), ("ATR_1", "2"), ("ATR_2", "4")):
                request = ForecastRequest(
                    request_id=stable_id(
                        "forecast_request",
                        f"{snapshot_id}|{horizon.unit}{horizon.value}|{barrier}"),
                    features=snapshot, horizon=horizon, window_start_nano=window_start,
                    window_end_nano=window_end, barrier_key=barrier,
                    upper_distance=Decimal(distance), lower_distance=Decimal(distance),
                    frozen_range=frozen)
                requests.append(request)
                labels.append(PathLabel(
                    request_id=request.request_id, contract_id=CONTRACT,
                    label_version="path-label-v2.1.0", horizon=horizon,
                    window_start_nano=window_start, window_end_nano=window_end,
                    available_at_nano=window_end + 3600 * NANO, status="MATURE",
                    reasons=(),
                    direction="UP" if up else ("DOWN" if down else "NEUTRAL"),
                    terminal_return=terminal_atr * 2 / 100,
                    terminal_points=Decimal(str(terminal_atr * 2)),
                    terminal_atr=terminal_atr,
                    path_outcome="UP_FIRST" if up else ("DOWN_FIRST" if down else "NEITHER"),
                    first_up_step=1 if up else None,
                    first_down_step=1 if down else None,
                    return_step=1 if competing == "RETURN_FIRST" else None,
                    break_step=1 if competing == "BREAK_FIRST" else None,
                    competing_outcome=competing,
                    persistence={"UP_TREND": False, "DOWN_TREND": False,
                                 "ORDERED_RANGE": abs(momentum) < 0.5,
                                 "WIDE_RANGE": abs(momentum) < 0.5,
                                 "COMPRESSION": compression,
                                 "UP_BREAKOUT": None, "DOWN_BREAKOUT": None},
                    stop_touched={"LONG": momentum < -1.2, "SHORT": momentum > 1.2},
                    range_id=frozen.range_id))
    return tuple(features), tuple(requests), tuple(labels)


def regimes_for(features, reference_id):
    return tuple(
        RegimeAssessment(
            assessment_id=f"assess-{snapshot.snapshot_id}",
            snapshot_id=snapshot.snapshot_id, reference_id=reference_id,
            decision_nano=snapshot.decision_nano, base="UNKNOWN", flags=(),
            frozen_range=None, cross_timeframe="UNKNOWN", confirmed_observations=1,
            reasons=("synthetic",), quality=quality())
        for snapshot in features)


@pytest.fixture(scope="module")
def fitted():
    rng = random.Random(7)
    training = build_segment(DAYS[:180], "tr", rng)
    calibration = build_segment(DAYS[182:212], "cal", rng)
    training_end = max(label.available_at_nano for label in training[2])
    calibration_end = max(label.available_at_nano for label in calibration[2])
    reference = fit_regime_reference(training[0], training_end, training_end)
    training_regimes = regimes_for(training[0], "regime_reference:inner-train")
    calibration_regimes = regimes_for(calibration[0], reference.reference_id)
    config = default_fit_config(
        FEATURE_KEYS, training_end, calibration_end, calibration_end + 30 * DAYN)
    model_set = fit_forecast_models(
        training[0], training_regimes, training[1], training[2],
        calibration[0], calibration_regimes, calibration[1], calibration[2],
        config, reference, seasonality=None)
    return {
        "model_set": model_set,
        "training": training,
        "calibration": calibration,
        "training_regimes": training_regimes,
        "calibration_regimes": calibration_regimes,
        "reference": reference,
        "config": config,
    }


# ---------------------------------------------------------------------------
# §8.4 hand-computed frequency baseline
# ---------------------------------------------------------------------------

def test_frequency_laplace_hand_computed():
    """Counts 6/2/1/1 with Laplace 1 → 7/14, 3/14, 2/14, 2/14; sums to 1."""
    model = _fit_unconditional(
        ["UP_FIRST", "DOWN_FIRST", "NEITHER", "AMBIGUOUS"] * 0 + [
            "UP_FIRST"] * 6 + ["DOWN_FIRST"] * 2 + ["NEITHER"] + ["AMBIGUOUS"],
        ("UP_FIRST", "DOWN_FIRST", "NEITHER", "AMBIGUOUS"), list(FEATURE_KEYS), [], [])
    counts = model["counts"]["product"]
    assert counts == [6, 2, 1, 1]
    probabilities = [(count + 1) / 14 for count in counts]
    assert probabilities == [7 / 14, 3 / 14, 2 / 14, 2 / 14]
    assert abs(sum(probabilities) - 1.0) < 1e-15


def test_frequency_keeps_unseen_classes_positive():
    """A class absent from training keeps positive mass (§8.4); M4 blocks it
    on missing evidence rather than the model deleting the class."""
    model = _fit_unconditional(["UP"] * 10, ("UP", "DOWN", "NEUTRAL"),
                               list(FEATURE_KEYS), [], [])
    assert model["counts"]["product"] == [10, 0, 0]
    probabilities = [(count + 1) / 13 for count in model["counts"]["product"]]
    assert probabilities[1] > 0 and probabilities[2] > 0


# ---------------------------------------------------------------------------
# §8.5 hand-computed time models
# ---------------------------------------------------------------------------

def test_cumulative_incidence_hand_computed():
    cifs, survival = cumulative_incidence(
        {"RETURN": [0.2, 0.2], "BREAK": [0.1, 0.1]}, 2)
    assert abs(cifs["RETURN"][1] - 0.34) < 1e-12
    assert abs(cifs["BREAK"][1] - 0.17) < 1e-12
    assert abs(survival[1] - 0.49) < 1e-12


def test_empirical_cdf_hand_computed():
    """4 independent paths touching at steps 1, 2, 2 and one never: CDF1=0.25,
    CDF2=0.75; a farther target is never easier to touch."""
    levels = [("SHFE.RB|DAY|UNKNOWN", "SHFE.RB|DAY", "SHFE.RB")] * 4
    model = _fit_empirical_cdf([1, 2, 2, 0], levels, list(FEATURE_KEYS), [], [])
    entries = _apply_empirical_cdf(model, levels[0], 2)
    assert entries is not None
    assert abs(entries[0][0] - 0.25) < 1e-15
    assert abs(entries[1][0] - 0.75) < 1e-15
    assert entries[0][1] == 0.0


# ---------------------------------------------------------------------------
# §8.7 explicit model behaviour on degenerate inputs
# ---------------------------------------------------------------------------

def test_softmax_separable_data_direction_follows_feature():
    import math
    X = [(-2.0, 0.0), (-1.5, 0.0), (-1.0, 0.0), (1.0, 0.0), (1.5, 0.0), (2.0, 0.0)]
    y = [0, 0, 0, 1, 1, 1]  # class 1 on the positive side
    fitted = _softmax_gd(X, y, 2, l2=0.1, learning_rate=0.1, iterations=800)
    assert fitted is not None
    weights, bias = fitted
    def prob_class1(x):
        scores = [bias[k] + sum(w * v for w, v in zip(weights[k], x)) for k in range(2)]
        exps = [math.exp(s - max(scores)) for s in scores]
        return exps[1] / sum(exps)
    assert prob_class1((-2.0, 0.0)) < 0.2
    assert prob_class1((2.0, 0.0)) > 0.8


def test_softmax_extreme_overflow_returns_none():
    # class-imbalanced extreme features blow the scores up to inf -> NaN -> FAILED
    X = [(1e308, 0.0)] * 4
    y = [0, 0, 0, 1]
    assert _softmax_gd(X, y, 2, l2=1.0, learning_rate=0.01, iterations=10) is None


def test_tree_min_leaf_prevents_empty_leaves():
    X = [(float(i),) for i in range(10)]
    targets = [[float(i % 2)] for i in range(10)]
    nodes = _build_tree(X, targets, depth=2, min_leaf=10)
    assert len(nodes) == 1 and nodes[0]["feature_index"] == -1  # single root leaf
    targets = [[float(i)] for i in range(40)]
    nodes = _build_tree(X * 4, targets, depth=2, min_leaf=10)
    assert all(node["feature_index"] == -1 or
               (isinstance(node["left"], int) and isinstance(node["right"], int))
               for node in nodes)


def test_linear_quantile_crossing_rejected_at_apply():
    model = {
        "algorithm": "LINEAR_QUANTILE", "feature_keys": ["f"], "center": [0.0],
        "scale": [1.0], "classes": ["Q05", "Q25", "Q50", "Q75", "Q95"],
        "coefficients": [[0.0], [0.0], [0.0], [0.0], [0.0]],
        "intercepts": [1.0, 0.8, 0.6, 0.4, 0.2],  # strictly decreasing → crossing
        "trees": [], "counts": {}, "samples": {}, "temperature": 1.0,
        "fit_status": "CONVERGED",
    }
    assert _apply_linear_quantile(model, (0.0,)) is None


def test_standardization_frozen_from_training_only():
    vectors = [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]
    center, scale = _standardize_stats(vectors)
    assert center == [3.0, 4.0]
    standardized = _standardized((100.0, -50.0), center, scale)
    assert all(-5.0 <= value <= 5.0 for value in standardized)  # winsorised
    # extreme later values never move the frozen statistics
    assert _standardize_stats(vectors) == (center, scale)


# ---------------------------------------------------------------------------
# fit_config validation
# ---------------------------------------------------------------------------

def test_fit_config_defaults_and_rejection():
    config = default_fit_config(("A", "B"), 100, 200, 300)
    assert config["version"] == "forecast-fit-v2.1.0"
    assert config["seed"] == 7 and config["l2"] == 1.0
    assert config["quantiles"] == [0.05, 0.25, 0.5, 0.75, 0.95]
    assert config["inner_minimum_train_days"] == 120
    from ak_trading.v2.probability.models import FitError, _validate_fit_config
    with pytest.raises(ValueError):
        _validate_fit_config({**config, "unknown_key": 1})
    with pytest.raises(ValueError):
        _validate_fit_config({**config, "version": "other"})
    with pytest.raises(ValueError):
        _validate_fit_config({**config, "model_valid_until_nano": 200})
    with pytest.raises(ValueError):
        _validate_fit_config({**config, "quantiles": [0.1, 0.2, 0.3, 0.4, 0.4]})
    merged = _validate_fit_config(config)
    assert merged["feature_keys"] == ["A", "B"]


# ---------------------------------------------------------------------------
# full-pipeline behaviour
# ---------------------------------------------------------------------------

def test_fit_produces_selected_models_and_stable_identity(fitted):
    model_set = fitted["model_set"]
    assert isinstance(model_set, ForecastModelSet)
    assert model_set.model_set_id.startswith("forecast_model_set:")
    assert model_set.model_set_id.endswith(model_set.content_hash())
    assert model_set.feature_keys == FEATURE_KEYS
    selected = {key: value for key, value in model_set.models.items()
                if str(value["fit_status"]).startswith("SELECTED")}
    assert selected, "at least one selected model must exist"
    for key, model in selected.items():
        parts = key.split("|")
        assert len(parts) == 6
        assert parts[0] == PRODUCT
        assert parts[1] in ("MINUTE", "DAILY")
        assert parts[3] in ("COMMON", "ATR_0.5", "ATR_1", "ATR_2")
        assert parts[5] in ALGORITHM_PRIORITY
        assert model["feature_keys"] == list(FEATURE_KEYS)


def test_model_records_carry_the_fixed_field_set(fitted):
    fixed_fields = {"algorithm", "feature_keys", "center", "scale", "classes",
                    "coefficients", "intercepts", "trees", "counts", "samples",
                    "temperature", "fit_status"}
    for key, model in fitted["model_set"].models.items():
        assert set(model) == fixed_fields, key
        if model["trees"]:
            for tree in model["trees"]:
                for node in tree:
                    assert set(node) == {"feature_index", "threshold", "left", "right", "value"}


def test_temperature_chosen_only_from_registered_candidates(fitted):
    candidates = {0.5, 0.75, 1.0, 1.5, 2.0}
    for model in fitted["model_set"].models.values():
        assert model["temperature"] in candidates


def test_constant_binary_task_tie_breaks_by_priority(fitted):
    """§8.13: candidates whose dev losses tie within 1e-12 fall back to the
    fixed priority order; a constant UP_TREND task selects CONDITIONAL_FREQUENCY."""
    key = model_key(PRODUCT, "MINUTE", Horizon("TRADING_MINUTE", 1), "COMMON",
                    "PERSIST:UP_TREND", "CONDITIONAL_FREQUENCY")
    model = fitted["model_set"].models.get(key)
    assert model is not None and model["fit_status"].startswith("SELECTED")
    assert key.endswith("CONDITIONAL_FREQUENCY")


def test_evidence_references_model_set_and_both_policies(fitted):
    model_set = fitted["model_set"]
    assert model_set.evidence, "evidence rows must be generated at fit time"
    assert all(row.model_id == model_set.model_set_id for row in model_set.evidence)
    policy_ids = {row.policy_id for row in model_set.evidence}
    assert "confidence-ordinary-v2.1.0" in policy_ids
    assert "confidence-martingale-v2.1.0" in policy_ids
    for row in model_set.evidence:
        assert isinstance(row, CalibrationEvidence)
        assert row.valid_until_nano == model_set.valid_until_nano


def test_content_hash_keeps_evidence_statistics_drops_back_references(fitted):
    """004 §3: the set hash covers the complete evidence statistics and drops
    exactly the model_id/evidence_id back references."""
    import dataclasses
    model_set = fitted["model_set"]
    stripped = dataclasses.replace(model_set, evidence=())
    assert stripped.content_hash() != model_set.content_hash()

    relabelled = tuple(
        dataclasses.replace(dataclasses.replace(row, model_id="other:model"),
                            evidence_id="calibration_evidence:other")
        for row in model_set.evidence)
    renamed = dataclasses.replace(model_set, evidence=relabelled)
    assert renamed.content_hash() == model_set.content_hash()  # ids are not content

    changed_row = dataclasses.replace(model_set.evidence[0],
                                      sample_count=model_set.evidence[0].sample_count + 1)
    tampered = dataclasses.replace(model_set, evidence=(changed_row, *model_set.evidence[1:]))
    assert tampered.content_hash() != model_set.content_hash()


def test_direction_model_common_and_path_per_barrier(fitted):
    models = fitted["model_set"].models
    assert model_key(PRODUCT, "MINUTE", Horizon("TRADING_MINUTE", 1), "COMMON",
                     "DIRECTION", "UNCONDITIONAL_FREQUENCY") in models
    for barrier in ("ATR_0.5", "ATR_1", "ATR_2"):
        assert any(model_key(PRODUCT, "MINUTE", Horizon("TRADING_MINUTE", 1), barrier,
                             "PATH", algorithm) in models
                   for algorithm in ALGORITHM_PRIORITY)


def test_daily_and_minute_models_never_share_cells(fitted):
    models = fitted["model_set"].models
    daily_keys = [key for key in models if "|DAILY|" in key]
    minute_keys = [key for key in models if "|MINUTE|" in key]
    assert daily_keys and minute_keys
    assert not (set(daily_keys) & set(minute_keys))


def test_out_of_distribution_flag_without_touching_frozen_stats(fitted):
    from ak_trading.v2.probability.models import apply_model_set, feature_vector
    model_set = fitted["model_set"]
    request = fitted["calibration"][1][0]
    before = {key: (model["center"], model["scale"])
              for key, model in model_set.models.items()}
    extreme = type(request)(
        request_id=request.request_id, features=request.features, horizon=request.horizon,
        window_start_nano=request.window_start_nano, window_end_nano=request.window_end_nano,
        barrier_key=request.barrier_key, upper_distance=request.upper_distance,
        lower_distance=request.lower_distance, frozen_range=None)
    components = apply_model_set(model_set.models, model_set.feature_keys, 10,
                                 extreme, "UNKNOWN")
    assert components["out_of_distribution"] is False  # calibration request in-range
    snapshot = request.features
    extreme_values = (FactorValue(FEATURE_KEYS[0], 5000.0, snapshot.decision_nano, quality()),
                      FactorValue(FEATURE_KEYS[1], -5000.0, snapshot.decision_nano, quality()))
    extreme_snapshot = FeatureSnapshot(
        snapshot_id=snapshot.snapshot_id + "-x", contract_id=snapshot.contract_id,
        raw_symbol=snapshot.raw_symbol, product=snapshot.product,
        trading_day=snapshot.trading_day, session_id=snapshot.session_id,
        session_kind=snapshot.session_kind, decision_nano=snapshot.decision_nano,
        available_at_nano=snapshot.available_at_nano,
        duration_seconds=snapshot.duration_seconds, source_bar_ids=(),
        reference_price=snapshot.reference_price, atr_points=snapshot.atr_points,
        values=extreme_values, daily_background_id=None, quality=quality())
    extreme_request = type(request)(
        request_id=request.request_id + "-x", features=extreme_snapshot,
        horizon=request.horizon, window_start_nano=request.window_start_nano,
        window_end_nano=request.window_end_nano, barrier_key=request.barrier_key,
        upper_distance=request.upper_distance, lower_distance=request.lower_distance,
        frozen_range=None)
    components2 = apply_model_set(model_set.models, model_set.feature_keys, 10,
                                  extreme_request, "UNKNOWN")
    assert components2["out_of_distribution"] is True
    after = {key: (model["center"], model["scale"])
             for key, model in model_set.models.items()}
    assert before == after  # §8.8: extreme later values never move statistics


# ---------------------------------------------------------------------------
# error paths (§4 validation)
# ---------------------------------------------------------------------------

def _small_fit_inputs():
    rng = random.Random(11)
    days = DAYS[:4]
    training = build_segment(days, "err", rng)
    training_end = max(label.available_at_nano for label in training[2])
    return training, training_end


def test_fit_rejects_duplicate_snapshot_id():
    training, training_end = _small_fit_inputs()
    features = (*training[0], training[0][0])
    regimes = regimes_for(training[0], "regime_reference:inner")
    config = default_fit_config(FEATURE_KEYS, training_end, training_end + DAYN,
                                training_end + 2 * DAYN)
    with pytest.raises(ValueError, match="duplicate_snapshot_id"):
        fit_forecast_models(features, regimes, training[1], training[2],
                            training[0][:1], regimes[:1], (), (),
                            config, _reference_for(training[0], training_end), seasonality=None)


def _reference_for(features, training_end):
    return fit_regime_reference(features, training_end, training_end)


def test_fit_rejects_label_later_than_training_end():
    training, training_end = _small_fit_inputs()
    late_label = dataclasses_replace(training[2][0], available_at_nano=training_end + 1)
    labels = (late_label, *training[2][1:])
    regimes = regimes_for(training[0], "regime_reference:inner")
    config = default_fit_config(FEATURE_KEYS, training_end, training_end + DAYN,
                                training_end + 2 * DAYN)
    with pytest.raises(ValueError, match="label_late_for_training"):
        fit_forecast_models(training[0], regimes, training[1], labels,
                            training[0][:1], regimes[:1], (), (),
                            config, _reference_for(training[0], training_end), seasonality=None)


def test_fit_rejects_training_calibration_overlap():
    training, training_end = _small_fit_inputs()
    regimes = regimes_for(training[0], "regime_reference:inner")
    config = default_fit_config(FEATURE_KEYS, training_end, training_end + DAYN,
                                training_end + 2 * DAYN)
    with pytest.raises(ValueError, match="overlap"):
        fit_forecast_models(training[0], regimes, training[1], training[2],
                            training[0][:2], regimes[:2], training[1][:2], training[2][:2],
                            config, _reference_for(training[0], training_end), seasonality=None)


def test_fit_rejects_wrong_label_version():
    training, training_end = _small_fit_inputs()
    bad_label = dataclasses_replace(training[2][0], label_version="other-v1")
    labels = (bad_label, *training[2][1:])
    regimes = regimes_for(training[0], "regime_reference:inner")
    config = default_fit_config(FEATURE_KEYS, training_end, training_end + DAYN,
                                training_end + 2 * DAYN)
    with pytest.raises(ValueError, match="label_version_mismatch"):
        fit_forecast_models(training[0], regimes, training[1], labels,
                            training[0][:1], regimes[:1], (), (),
                            config, _reference_for(training[0], training_end), seasonality=None)


def test_fit_rejects_calibration_regime_reference_mismatch():
    training, training_end = _small_fit_inputs()
    rng = random.Random(5)
    calibration = build_segment(DAYS[8:10], "mismatch", rng)
    regimes = regimes_for(training[0], "regime_reference:inner")
    wrong = regimes_for(calibration[0], "regime_reference:other")
    config = default_fit_config(FEATURE_KEYS, training_end, training_end + 10 * DAYN,
                                training_end + 11 * DAYN)
    with pytest.raises(ValueError, match="calibration_regime_reference_mismatch"):
        fit_forecast_models(training[0], regimes, training[1], training[2],
                            calibration[0], wrong, calibration[1], calibration[2],
                            config, _reference_for(training[0], training_end), seasonality=None)


def test_fit_rejects_model_valid_until_not_after_inputs():
    training, training_end = _small_fit_inputs()
    regimes = regimes_for(training[0], "regime_reference:inner")
    config = default_fit_config(FEATURE_KEYS, training_end, training_end + DAYN,
                                training_end)
    with pytest.raises(ValueError):
        fit_forecast_models(training[0], regimes, training[1], training[2],
                            training[0][:2], regimes[:2], training[1][:2], training[2][:2],
                            config, _reference_for(training[0], training_end), seasonality=None)


def test_fit_insufficient_inner_split_records_failure():
    rng = random.Random(13)
    training = build_segment(DAYS[:30], "small", rng)
    training_end = max(label.available_at_nano for label in training[2])
    calibration = build_segment(DAYS[32:34], "smallcal", random.Random(3))
    calibration_end = max(label.available_at_nano for label in calibration[2])
    reference = fit_regime_reference(training[0], training_end, training_end)
    config = default_fit_config(FEATURE_KEYS, training_end, calibration_end,
                                calibration_end + 30 * DAYN)
    model_set = fit_forecast_models(
        training[0], regimes_for(training[0], "regime_reference:inner"),
        training[1], training[2], calibration[0],
        regimes_for(calibration[0], reference.reference_id),
        calibration[1], calibration[2], config, reference, seasonality=None)
    assert model_set.evidence == ()
    statuses = {value["fit_status"] for value in model_set.models.values()}
    assert statuses <= {"FAILED:insufficient_inner_split"}


import dataclasses


def dataclasses_replace(record, **overrides):
    return dataclasses.replace(record, **overrides)


# ---------------------------------------------------------------------------
# algorithm-completion evidence: each non-trivial family is genuinely fitted
# and selected when the synthetic signal demands it (engineering acceptance;
# no statistical claim about financial validity)
# ---------------------------------------------------------------------------

def build_discriminating_segment(segment_days, prefix, rng):
    """Signals that baselines cannot capture:
    PATH ← linear momentum (±10); DIRECTION ← XOR of the two features;
    QUANTILE ← terminal_atr = 2×momentum; TIME:RETURN ← return-at-step-1
    only for positive momentum."""
    from ak_trading.v2.contracts import ForecastRequest, PathLabel
    from ak_trading.v2.artifacts import stable_id
    features, requests, labels = [], [], []
    for day in segment_days:
        mode, horizon = ("minute", Horizon("TRADING_MINUTE", 1))
        momentum = 10.0 if rng.random() < 0.5 else -10.0
        bias = 1.0 if rng.random() < 0.5 else -1.0
        decision = BASE + DAY_INDEX[day] * DAYN - 1
        cache_key = (decision, horizon.unit, horizon.value)
        if cache_key not in _window_cache:
            _window_cache[cache_key] = horizon_window(decision, PRODUCT, horizon, SESSIONS)
        window_start, window_end = _window_cache[cache_key]
        snapshot_id = f"snap-{prefix}-{len(features)}"
        values = (
            FactorValue("META:price_tick:v2.1.0", Decimal("1"), decision, quality()),
            FactorValue(FEATURE_KEYS[0], momentum, decision, quality()),
            FactorValue(FEATURE_KEYS[1], bias, decision, quality()),
        )
        snapshot = FeatureSnapshot(
            snapshot_id=snapshot_id, contract_id=CONTRACT, raw_symbol="RB2605",
            product=PRODUCT, trading_day=day, session_id=f"sess-{snapshot_id}",
            session_kind="DAY", decision_nano=decision, available_at_nano=decision,
            duration_seconds=60, source_bar_ids=(), reference_price=Decimal("100"),
            atr_points=Decimal("2"), values=values, daily_background_id=None,
            quality=quality())
        features.append(snapshot)
        up_path = momentum > 0
        direction_up = (momentum > 0) != (bias > 0)  # XOR: linear models fail
        terminal_atr = 2.0 * momentum + rng.uniform(-0.05, 0.05)
        returns_first = momentum > 0
        for barrier, distance in (("ATR_0.5", "1"), ("ATR_1", "2"), ("ATR_2", "4")):
            request = ForecastRequest(
                request_id=stable_id(
                    "forecast_request",
                    f"{snapshot_id}|{horizon.unit}{horizon.value}|{barrier}"),
                features=snapshot, horizon=horizon, window_start_nano=window_start,
                window_end_nano=window_end, barrier_key=barrier,
                upper_distance=Decimal(distance), lower_distance=Decimal(distance),
                frozen_range=None)
            requests.append(request)
            labels.append(PathLabel(
                request_id=request.request_id, contract_id=CONTRACT,
                label_version="path-label-v2.1.0", horizon=horizon,
                window_start_nano=window_start, window_end_nano=window_end,
                available_at_nano=window_end + 3600 * NANO, status="MATURE",
                reasons=(), direction="UP" if direction_up else "DOWN",
                terminal_return=terminal_atr * 2 / 100,
                terminal_points=Decimal(str(terminal_atr * 2)),
                terminal_atr=terminal_atr,
                path_outcome="UP_FIRST" if up_path else "DOWN_FIRST",
                first_up_step=1 if up_path else None,
                first_down_step=None if up_path else 1,
                return_step=1 if returns_first else None,
                break_step=None,
                competing_outcome="RETURN_FIRST" if returns_first else "NEITHER",
                persistence={"UP_TREND": False, "DOWN_TREND": False,
                             "ORDERED_RANGE": None, "WIDE_RANGE": None,
                             "COMPRESSION": False,
                             "UP_BREAKOUT": None, "DOWN_BREAKOUT": None},
                stop_touched={}, range_id=None))
    return tuple(features), tuple(requests), tuple(labels)


@pytest.fixture(scope="module")
def discriminating():
    from ak_trading.v2.probability.regimes import fit_regime_reference
    rng = random.Random(23)
    training = build_discriminating_segment(DAYS[:180], "dtr", rng)
    calibration = build_discriminating_segment(DAYS[182:212], "dcal", rng)
    training_end = max(label.available_at_nano for label in training[2])
    calibration_end = max(label.available_at_nano for label in calibration[2])
    reference = fit_regime_reference(training[0], training_end, training_end)
    config = default_fit_config(FEATURE_KEYS, training_end, calibration_end,
                                calibration_end + 30 * DAYN)
    model_set = fit_forecast_models(
        training[0], regimes_for(training[0], "regime_reference:inner"),
        training[1], training[2], calibration[0],
        regimes_for(calibration[0], reference.reference_id),
        calibration[1], calibration[2], config, reference, seasonality=None)
    return model_set


def _selected_algorithm(model_set, task, barrier="COMMON"):
    for key, model in model_set.models.items():
        if str(model["fit_status"]).startswith("SELECTED") and f"|{barrier}|{task}|" in key:
            return key.split("|")[5]
    return None


def _dev_score(model_set, task, algorithm, barrier="COMMON"):
    for key, model in model_set.models.items():
        if f"|{barrier}|{task}|{algorithm}" in key:
            scores = model.get("samples", {}).get("development_score", [])
            return float(scores[0]) if scores else None
    return None


def test_algorithms_complete_real_fits_when_signal_demands_it(discriminating):
    """Engineering completion evidence (no statistical claim): on sufficient
    discriminating synthetic data every non-trivial family is actually fitted
    and ranked, boosting/hazard/quantile families are SELECTED, and the
    regularized family beats the unconditional baseline out-of-sample even
    when the tree edges it out overall."""
    models = discriminating
    path_algorithm = _selected_algorithm(models, "PATH", barrier="ATR_1")
    direction_algorithm = _selected_algorithm(models, "DIRECTION")
    quantile_algorithm = _selected_algorithm(models, "QUANTILE")
    return_algorithm = _selected_algorithm(models, "TIME:RETURN_FIRST")
    print("selected:", path_algorithm, direction_algorithm,
          quantile_algorithm, return_algorithm)
    assert direction_algorithm == "GRADIENT_BOOSTED_TREE", "XOR must select the tree"
    assert return_algorithm == "DISCRETE_COMPETING_RISK",         "feature-dependent hazard must select the competing-risk model"
    assert quantile_algorithm in ("EMPIRICAL_QUANTILE", "LINEAR_QUANTILE")
    assert path_algorithm in ("L2_SOFTMAX", "GRADIENT_BOOSTED_TREE")
    # the winner must beat the unconditional baseline on dev loss
    winner_score = _dev_score(models, "PATH", path_algorithm, barrier="ATR_1")
    baseline_score = _dev_score(models, "PATH", "UNCONDITIONAL_FREQUENCY", barrier="ATR_1")
    assert winner_score < baseline_score - 1e-12
    # regularized family completed a real fit and beats the baseline too
    softmax_score = _dev_score(models, "PATH", "L2_SOFTMAX", barrier="ATR_1")
    assert softmax_score is not None and softmax_score < baseline_score - 1e-12
    # linear quantile completed and ranked with a finite score
    linear_score = _dev_score(models, "QUANTILE", "LINEAR_QUANTILE")
    assert linear_score is not None and linear_score == linear_score
    # nothing was excluded for engineering reasons in these cells
    for key, model in models.models.items():
        status = str(model["fit_status"])
        assert "fit_failed" not in status, key
        assert "EXCLUDED" not in status, key
        assert "loss_not_finite" not in status, key


# ---------------------------------------------------------------------------
# 004 §3 independent oracles: consumers recompute the canonical hashes from
# the public fields directly, without producer helpers
# ---------------------------------------------------------------------------

def test_regime_reference_content_hash_oracle(fitted):
    from ak_trading.v2.schemas import canonical_json_bytes
    reference = fitted["reference"]
    payload = {
        "training_end_nano": reference.training_end_nano,
        "available_at_nano": reference.available_at_nano,
        "volatility_samples": {key: list(values) for key, values
                               in sorted(reference.volatility_samples.items())},
        "source_hashes": dict(sorted(reference.source_hashes.items())),
    }
    expected = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    assert reference.content_hash() == expected
    assert reference.reference_id == f"regime_reference:{expected}"


def test_forecast_model_set_content_hash_oracle(fitted):
    from ak_trading.v2.schemas import canonical_json_bytes, encode_record
    model_set = fitted["model_set"]
    evidence_projections = []
    for row in model_set.evidence:
        record = encode_record(row)
        record.pop("evidence_id")
        record.pop("model_id")
        evidence_projections.append(record)
    evidence_projections.sort(key=lambda projection: canonical_json_bytes(projection))
    payload = {
        "regime_reference_id": model_set.regime_reference_id,
        "feature_keys": list(model_set.feature_keys),
        "training_end_nano": model_set.training_end_nano,
        "calibration_end_nano": model_set.calibration_end_nano,
        "valid_until_nano": model_set.valid_until_nano,
        "fit_config": dict(model_set.fit_config),
        "models": {key: dict(value) for key, value in sorted(model_set.models.items())},
        "source_hashes": dict(sorted(model_set.source_hashes.items())),
        "evidence": evidence_projections,
    }
    expected = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    assert model_set.content_hash() == expected
    assert model_set.model_set_id == f"forecast_model_set:{expected}"
    assert all(row.model_id == model_set.model_set_id for row in model_set.evidence)


# ---------------------------------------------------------------------------
# 变更申请-006 Q3: quantiles are non-decreasing; ties legal, crossing invalid
# ---------------------------------------------------------------------------

def test_quantile_triple_accepts_ties_and_rejects_crossing():
    from ak_trading.v2.probability.models import QuantileCrossingError, quantile_triple
    reference, atr = Decimal("100"), Decimal("2")
    returns, points, atr_map = quantile_triple([0.0] * 5, reference, atr)  # all-equal
    assert set(atr_map.values()) == {0.0}
    assert len(set(points.values())) == 1
    assert len(set(returns.values())) == 1
    returns2, _, atr_map2 = quantile_triple([-2.0, -2.0, 0.0, 0.0, 4.0], reference, atr)
    assert atr_map2["Q05"] == atr_map2["Q25"] == -2.0
    assert atr_map2["Q50"] == atr_map2["Q75"] == 0.0
    assert atr_map2["Q95"] == 4.0
    with pytest.raises(QuantileCrossingError):
        quantile_triple([-2.0, 0.0, -1.0, 0.0, 4.0], reference, atr)


def test_quantile_consistency_accepts_ties_and_market_forecast_constructs():
    from ak_trading.v2.probability.models import _candidate_consistent
    from ak_trading.v2.contracts import DataQuality as DQ, Horizon, MarketForecast
    import hashlib as _hl
    assert _candidate_consistent("QUANTILE", [0.0, 0.0, 0.0, 0.0, 0.0])
    assert _candidate_consistent("QUANTILE", [-2.0, -2.0, 0.0, 0.0, 4.0])
    assert not _candidate_consistent("QUANTILE", [-2.0, 0.0, -1.0, 0.0, 4.0])
    tied_floats = {"Q05": 0.0, "Q25": 0.0, "Q50": 0.0, "Q75": 0.0, "Q95": 0.0}
    tied_points = {key: Decimal("0") for key in tied_floats}  # displacement 0
    h = _hl.sha256(b"ties").hexdigest()
    forecast = MarketForecast(
        forecast_id="fc-tie", request_id="req-tie", snapshot_id="snap-tie",
        contract_id=CONTRACT, product=PRODUCT, trading_day=DAYS[0],
        session_id="sess", session_kind="DAY", duration_seconds=60,
        assessment_id="a", base_regime="UNKNOWN", regime_reference_id="rr",
        decision_nano=1_000, published_at_nano=1_500, expires_at_nano=2_500,
        horizon=Horizon("TRADING_MINUTE", 1), window_start_nano=2_000,
        window_end_nano=3_000, barrier_key="ATR_1", reference_price=Decimal("100"),
        atr_points=Decimal("2"), upper_distance=Decimal("2"),
        lower_distance=Decimal("2"), frozen_range=None,
        label_version="path-label-v2.1.0", model_id="m",
        direction_probabilities={"UP": 0.2, "DOWN": 0.3, "NEUTRAL": 0.5},
        return_quantiles=dict(tied_floats), point_quantiles=tied_points,
        atr_quantiles=dict(tied_floats),
        path_probabilities={"UP_FIRST": 0.25, "DOWN_FIRST": 0.25,
                            "NEITHER": 0.25, "AMBIGUOUS": 0.25},
        time_cdf=(), persistence_probabilities={}, stop_touch_probabilities={},
        return_before_break_probability=None, evidence=(), out_of_distribution=False,
        quality=DQ(status="READY", reasons=(), temporal_basis="observed",
                   input_hash=h))
    assert forecast.atr_quantiles["Q05"] == forecast.atr_quantiles["Q95"] == 0.0


# ---------------------------------------------------------------------------
# 变更申请-006 Q1: seasonality binding matrix
# ---------------------------------------------------------------------------

def _seasonality_handle(fit_end_nano: int, buckets=()):
    from ak_trading.v2.seasonality import SeasonalityModel
    base = SeasonalityModel("pending", 0, fit_end_nano, "cal-v2.1.0", buckets, "0" * 64)
    return SeasonalityModel(f"seasonality:{base.content_hash()}", 0, fit_end_nano,
                            "cal-v2.1.0", buckets, "0" * 64)


def _seasonal_bucket(slot_index: int, median_volume: str = "10"):
    from ak_trading.v2.seasonality import SeasonalBucket
    return SeasonalBucket(PRODUCT, "DAY", slot_index, 60, Decimal(median_volume),
                          0.5, 20)


def _with_seasonal_column(features):
    extra_key = "SEASONAL:volume_relative:v2.1.0:d60"
    rebuilt = []
    for snapshot in features:
        extra = FactorValue(extra_key, None, snapshot.decision_nano, quality())
        rebuilt.append(FeatureSnapshot(
            snapshot_id=snapshot.snapshot_id, contract_id=snapshot.contract_id,
            raw_symbol=snapshot.raw_symbol, product=snapshot.product,
            trading_day=snapshot.trading_day, session_id=snapshot.session_id,
            session_kind=snapshot.session_kind, decision_nano=snapshot.decision_nano,
            available_at_nano=snapshot.available_at_nano,
            duration_seconds=snapshot.duration_seconds, source_bar_ids=(),
            reference_price=snapshot.reference_price, atr_points=snapshot.atr_points,
            values=(*snapshot.values, extra), daily_background_id=None,
            quality=snapshot.quality))
    return tuple(rebuilt)


def _seasonal_segment(segment):
    """Augment snapshots AND the snapshots embedded in requests together so
    request.features stays identical to the features-tuple member."""
    features, requests, labels = segment
    augmented = _with_seasonal_column(features)
    by_id = {snapshot.snapshot_id: snapshot for snapshot in augmented}
    from ak_trading.v2.contracts import ForecastRequest
    rebuilt_requests = tuple(
        ForecastRequest(
            request_id=request.request_id, features=by_id[request.features.snapshot_id],
            horizon=request.horizon, window_start_nano=request.window_start_nano,
            window_end_nano=request.window_end_nano, barrier_key=request.barrier_key,
            upper_distance=request.upper_distance, lower_distance=request.lower_distance,
            frozen_range=request.frozen_range)
        for request in requests)
    return augmented, rebuilt_requests, labels


def _seasonal_training_end() -> int:
    """The training_end _small_seasonal_fit derives (deterministic per days)."""
    training_probe = build_segment(DAYS[:30], "sfit", random.Random(11))
    return max(label.available_at_nano for label in training_probe[2])


def _small_seasonal_fit(handle, seasonal: bool, feature_keys=FEATURE_KEYS):
    rng = random.Random(11)
    training = build_segment(DAYS[:30], "sfit", rng)
    calibration = build_segment(DAYS[32:34], "scal", random.Random(3))
    training_end = max(label.available_at_nano for label in training[2])
    reference = fit_regime_reference(training[0], training_end, training_end)
    if seasonal:
        training_features, training_requests, training_labels = _seasonal_segment(training)
        calibration_features, calibration_requests, calibration_labels = _seasonal_segment(calibration)
    else:
        training_features, training_requests, training_labels = training
        calibration_features, calibration_requests, calibration_labels = calibration
    calibration_end = max(label.available_at_nano for label in calibration[2]) + DAYN
    config = default_fit_config(feature_keys, training_end, calibration_end,
                                calibration_end + DAYN)
    return fit_forecast_models(
        training_features, regimes_for(training[0], "regime_reference:inner"),
        training_requests, training_labels, calibration_features,
        regimes_for(calibration[0], reference.reference_id),
        calibration_requests, calibration_labels, config, reference,
        seasonality=handle)


def test_seasonality_parameter_is_required():
    training, training_end = _small_fit_inputs()
    regimes = regimes_for(training[0], "regime_reference:inner")
    config = default_fit_config(FEATURE_KEYS, training_end, training_end + DAYN,
                                training_end + 2 * DAYN)
    with pytest.raises(TypeError):
        fit_forecast_models(training[0], regimes, training[1], training[2],
                            training[0][:2], regimes[:2], training[1][:2],
                            training[2][:2], config,
                            _reference_for(training[0], training_end))


def test_seasonality_false_none_allowed_without_source_key(fitted):
    assert "seasonality" not in fitted["model_set"].source_hashes


def test_seasonality_false_handle_rejected():
    training_end = _seasonal_training_end()
    with pytest.raises(ValueError,
                       match="seasonality_provided_without_seasonal_columns"):
        _small_seasonal_fit(_seasonality_handle(training_end), seasonal=False)


def test_seasonality_true_none_rejected():
    training_end = _seasonal_training_end()
    with pytest.raises(ValueError,
                       match="seasonality_required_for_seasonal_columns"):
        _small_seasonal_fit(None, seasonal=True)


def test_seasonality_true_handle_binds_content_hash():
    import re as _re
    training_end = _seasonal_training_end()
    handle = _seasonality_handle(training_end)
    model_set = _small_seasonal_fit(handle, seasonal=True)
    bound = model_set.source_hashes["seasonality"]
    assert bound == handle.content_hash()
    assert _re.fullmatch(r"[0-9a-f]{64}", bound)


def test_declared_seasonal_key_without_columns_is_input_error():
    training_end = _seasonal_training_end()
    with pytest.raises(ValueError,
                       match="seasonal_feature_keys_declared_without_columns"):
        _small_seasonal_fit(None, seasonal=False,
                            feature_keys=("SEASONAL:x:v2.1.0:d60", FEATURE_KEYS[0]))


def test_invalid_seasonality_handles_rejected():
    training_end = _seasonal_training_end()
    with pytest.raises(TypeError):
        _small_seasonal_fit("", seasonal=True)
    with pytest.raises(TypeError):
        _small_seasonal_fit(123, seasonal=True)
    from ak_trading.v2.seasonality import SeasonalityModel
    wrong_id = SeasonalityModel("seasonality:not-the-hash", 0, training_end,
                                "cal-v2.1.0", (), "0" * 64)
    with pytest.raises(ValueError, match="seasonality_model_id_mismatch"):
        _small_seasonal_fit(wrong_id, seasonal=True)


def test_historical_earlier_fit_end_handle_is_legal():
    training_end = _seasonal_training_end()
    earlier = training_end - 10 * DAYN
    model_set = _small_seasonal_fit(_seasonality_handle(earlier), seasonal=True)
    assert "seasonality" in model_set.source_hashes


def test_future_fit_end_handle_rejected():
    training_end = _seasonal_training_end()
    with pytest.raises(ValueError,
                       match="seasonality_fit_end_after_training_end"):
        _small_seasonal_fit(_seasonality_handle(training_end + DAYN), seasonal=True)


def test_one_bucket_change_changes_model_identity():
    training_end = _seasonal_training_end()
    handle_a = _seasonality_handle(training_end, (_seasonal_bucket(0, "10"),))
    handle_b = _seasonality_handle(training_end, (_seasonal_bucket(0, "11"),))
    set_a = _small_seasonal_fit(handle_a, seasonal=True)
    set_b = _small_seasonal_fit(handle_b, seasonal=True)
    assert set_a.model_set_id != set_b.model_set_id
    assert set_a.source_hashes["seasonality"] != set_b.source_hashes["seasonality"]


def test_same_content_different_instances_same_identity():
    training_end = _seasonal_training_end()
    buckets = (_seasonal_bucket(0), _seasonal_bucket(1))
    first = _small_seasonal_fit(_seasonality_handle(training_end, buckets), seasonal=True)
    second = _small_seasonal_fit(_seasonality_handle(training_end, buckets), seasonal=True)
    assert first.model_set_id == second.model_set_id
    assert first.content_hash() == second.content_hash()
