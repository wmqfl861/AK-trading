"""Out-of-time calibration evidence for M3A (devplan-v2.1.1 03-M3A §5.2, 变更申请-002 §2).

Predictions and labels arrive already separated from training: the calibration
segment only selects temperatures / pre-registered monotone calibration
parameters and never feeds counts back into training. ``features`` is a
required keyword argument carrying the decision-time ``FeatureSnapshot``
records; geometry keys are generated exclusively by M1's
``calibration_geometry_key`` from the connected snapshot (C =
``features.reference_price``) and the prediction's frozen range — no shims,
no guessed fifth bucket. Structural/identity mismatches raise before any row
is produced; INVALID/late predictions and non-mature labels are excluded only
after those checks.
"""
from __future__ import annotations

import dataclasses
from typing import Mapping

from ak_trading.v2.artifacts import stable_id
from ak_trading.v2.contracts import (
    PERSISTENCE_EVENTS,
    CalibrationEvidence,
    ConfidencePolicy,
    Horizon,
    MarketForecast,
    PathLabel,
    FeatureSnapshot,
)
from ak_trading.v2.schemas import calibration_geometry_key, probability_in_bucket

LABEL_VERSION = "path-label-v2.1.0"

HIGH_PROBABILITY_BUCKETS = (
    "[0.70,0.75)", "[0.75,0.80)", "[0.80,0.85)",
    "[0.85,0.90)", "[0.90,0.95)", "[0.95,1.00]",
)
STOP_LOW_BUCKETS = ("[0,0.05)", "[0.05,0.10]", "(0.10,0.20]")
ALL_BUCKET = "ALL"

# Events whose M1 geometry key ignores the frozen range (M1 RANGE_FREE_EVENTS
# uses the bare event name, so PERSIST:E rows pass the bare E).
RANGE_FREE_EVENTS = frozenset({
    "DIRECTION", "PATH", "UP_TOUCH", "DOWN_TOUCH",
    "UP_TREND", "DOWN_TREND", "COMPRESSION",
})

# Equal-width per-class buckets for the multiclass ALL row (M1 §4.1 wording:
# DIRECTION/PATH整体证据逐类别用 [0,0.05),…,[0.95,1] 等宽桶).
CLASS_EQUAL_WIDTH_BUCKETS = tuple(
    f"[{index / 20:.2f},{(index + 1) / 20:.2f}" + ("]" if index == 19 else ")")
    for index in range(20)
)


class CalibrationError(ValueError):
    """Fixed-reason calibration evidence violation (local ValueError subclass)."""


def evidence_projection(row: CalibrationEvidence) -> dict:
    """Canonical evidence projection minus its own ``evidence_id`` only.

    Used to derive ``evidence_id`` (变更申请-004 §3: 按完整证据去自身
    evidence_id 生成证据ID); the model back-reference is removed only inside
    the set-level content-hash projection, not here.
    """
    from ak_trading.v2.schemas import encode_record

    projection = encode_record(row)
    projection.pop("evidence_id", None)
    return projection


def evidence_projection_without_back_references(row: CalibrationEvidence) -> dict:
    """Evidence projection minus ``model_id`` and ``evidence_id`` (004 §3).

    Everything else — policy/product/session/event/barrier/geometry/horizon/
    label_version, sample counts, errors, cut-offs, status — is preserved.
    """
    projection = evidence_projection(row)
    projection.pop("model_id", None)
    return projection


def canonical_evidence_id(row: CalibrationEvidence) -> str:
    """``calibration_evidence:<sha256>`` over the row minus its own id."""
    from ak_trading.v2.schemas import canonical_json_bytes

    return stable_id("calibration_evidence", canonical_json_bytes(evidence_projection(row)))


def horizon_code(horizon: Horizon) -> str:
    """Canonical short horizon code used in identity strings."""
    prefix = "M" if horizon.unit == "TRADING_MINUTE" else "D"
    return f"{prefix}{horizon.value}"


def geometry_event(event: str) -> str:
    """Bare event name handed to M1 ``calibration_geometry_key`` (002 §3).

    ``PERSIST:E`` maps to ``E``; ``STOP_TOUCH:LONG/SHORT`` share one bare
    ``STOP_TOUCH`` geometry (geometry is side-independent, probability
    buckets are not).
    """
    if event.startswith("PERSIST:"):
        return event.split(":", 1)[1]
    if event.startswith("STOP_TOUCH:"):
        return "STOP_TOUCH"
    return event


def _validate_connections(
        predictions: tuple[MarketForecast, ...],
        labels: tuple[PathLabel, ...],
        block_ids: Mapping[str, str],
        features: tuple[FeatureSnapshot, ...],
        as_of_nano: int) -> dict[str, FeatureSnapshot]:
    """Full ID-exact and field-exact connection checks (002 §2 items 1-4).

    Returns the snapshot map on success; raises CalibrationError on any
    duplicate, missing, extra or field-mismatched input.
    """
    forecast_ids: set[str] = set()
    request_ids: set[str] = set()
    for prediction in predictions:
        if prediction.forecast_id in forecast_ids:
            raise CalibrationError("duplicate_forecast_id")
        forecast_ids.add(prediction.forecast_id)
        if prediction.request_id in request_ids:
            raise CalibrationError("duplicate_prediction_request_id")
        request_ids.add(prediction.request_id)
    label_map: dict[str, PathLabel] = {}
    for label in labels:
        if label.request_id in label_map:
            raise CalibrationError("duplicate_label_request_id")
        label_map[label.request_id] = label
    if set(label_map) != request_ids:
        raise CalibrationError("label_key_set_must_equal_prediction_request_ids")
    if set(block_ids) != request_ids:
        raise CalibrationError("block_id_key_set_must_equal_prediction_request_ids")
    snapshot_map: dict[str, FeatureSnapshot] = {}
    for snapshot in features:
        if snapshot.snapshot_id in snapshot_map:
            raise CalibrationError("duplicate_feature_snapshot_id")
        snapshot_map[snapshot.snapshot_id] = snapshot
    referenced = {prediction.snapshot_id for prediction in predictions}
    if set(snapshot_map) != referenced:
        raise CalibrationError("feature_key_set_must_equal_prediction_snapshot_ids")

    for prediction in predictions:
        snapshot = snapshot_map[prediction.snapshot_id]
        if (prediction.contract_id != snapshot.contract_id
                or prediction.product != snapshot.product
                or prediction.trading_day != snapshot.trading_day
                or prediction.session_id != snapshot.session_id
                or prediction.session_kind != snapshot.session_kind
                or prediction.duration_seconds != snapshot.duration_seconds
                or prediction.decision_nano != snapshot.decision_nano
                or prediction.reference_price != snapshot.reference_price
                or prediction.atr_points != snapshot.atr_points):
            raise CalibrationError("prediction_snapshot_field_mismatch")
        if snapshot.available_at_nano > snapshot.decision_nano:
            raise CalibrationError("snapshot_not_visible_at_decision")
        if prediction.published_at_nano > as_of_nano:
            raise CalibrationError("prediction_published_after_as_of")
        label = label_map[prediction.request_id]
        if (label.contract_id != prediction.contract_id
                or label.horizon != prediction.horizon
                or label.window_start_nano != prediction.window_start_nano
                or label.window_end_nano != prediction.window_end_nano
                or label.label_version != LABEL_VERSION
                or label.range_id != (prediction.frozen_range.range_id
                                      if prediction.frozen_range is not None else None)):
            raise CalibrationError("label_prediction_field_mismatch")
    return snapshot_map


class _Geometry:
    """Resolves the M1 geometry key per (prediction, evidence event)."""

    def __init__(self, features_by_snapshot: Mapping[str, FeatureSnapshot]) -> None:
        self._features = dict(features_by_snapshot)

    def key(self, prediction: MarketForecast, event: str) -> str | None:
        """Exact M1 key from the connected snapshot; None when M1 declines.

        Range-dependent events that lack geometry inputs generate no row —
        the fifth bucket is never filled with a placeholder (002 §2).
        """
        bare = geometry_event(event)
        snapshot = self._features[prediction.snapshot_id]
        return calibration_geometry_key(snapshot, prediction.frozen_range, bare)


def _binary_bucket(probability: float, buckets: tuple[str, ...]) -> str | None:
    for bucket in buckets:
        if probability_in_bucket(probability, bucket):
            return bucket
    return None


def _usable_pairs(predictions: tuple[MarketForecast, ...],
                  labels: tuple[PathLabel, ...], as_of_nano: int
                  ) -> list[tuple[MarketForecast, PathLabel]]:
    """Post-validation exclusions only: INVALID/late predictions and labels
    that are not mature as of ``as_of_nano`` never enter a bucket."""
    by_request = {label.request_id: label for label in labels}
    pairs: list[tuple[MarketForecast, PathLabel]] = []
    for prediction in predictions:
        if prediction.quality.status == "INVALID":
            continue
        label = by_request[prediction.request_id]
        if label.status != "MATURE" or label.available_at_nano is None \
                or label.available_at_nano > as_of_nano:
            continue
        pairs.append((prediction, label))
    return pairs


def _row_status(sample_count: int, effective_blocks: int, calibration_error: float | None,
                policy: ConfidencePolicy, as_of_nano: int, valid_until_nano: int) -> str:
    if as_of_nano >= valid_until_nano:
        return "EXPIRED"
    if sample_count < policy.minimum_samples or effective_blocks < policy.minimum_blocks:
        return "INSUFFICIENT"
    if calibration_error is None:
        return "INSUFFICIENT"
    if calibration_error > policy.maximum_calibration_error:
        return "FAILED"
    return "VALID"


def _block_of(block_ids: Mapping[str, str], prediction: MarketForecast) -> str:
    return block_ids[prediction.request_id]


def _multiclass_all_rows(model_id: str, policy_id: str, pairs, event_name: str,
                         class_keys: tuple[str, ...], probability_get, outcome_get,
                         barrier_get, block_ids: Mapping[str, str], geometry: _Geometry,
                         training_end_nano: int, calibration_end_nano: int,
                         valid_until_nano: int, policy: ConfidencePolicy,
                         as_of_nano: int) -> list[CalibrationEvidence]:
    groups: dict[tuple, list[tuple[MarketForecast, PathLabel]]] = {}
    for prediction, label in pairs:
        outcome = outcome_get(label)
        if outcome is None:
            continue
        key = (prediction.product, prediction.session_kind, prediction.duration_seconds,
               prediction.base_regime, horizon_code(prediction.horizon), barrier_get(prediction))
        groups.setdefault(key, []).append((prediction, label))
    rows: list[CalibrationEvidence] = []
    for key in sorted(groups):
        members = groups[key]
        classes_present = [value for value in class_keys
                           if any(outcome_get(label) == value for _, label in members)]
        per_class_ece: list[float] = []
        brier_sum = 0.0
        for prediction, label in members:
            probabilities = probability_get(prediction)
            outcome = outcome_get(label)
            brier_sum += sum((probabilities[c] - (1.0 if c == outcome else 0.0)) ** 2
                             for c in classes_present)
        for class_key in classes_present:
            weighted = 0.0
            for bucket in CLASS_EQUAL_WIDTH_BUCKETS:
                bucket_p: list[float] = []
                bucket_y: list[float] = []
                for prediction, label in members:
                    probabilities = probability_get(prediction)
                    if not probability_in_bucket(probabilities[class_key], bucket):
                        continue
                    bucket_p.append(probabilities[class_key])
                    bucket_y.append(1.0 if outcome_get(label) == class_key else 0.0)
                if not bucket_p:
                    continue
                weighted += (len(bucket_p) / len(members)) * abs(
                    sum(bucket_p) / len(bucket_p) - sum(bucket_y) / len(bucket_y))
            per_class_ece.append(weighted)
        calibration_error = sum(per_class_ece) / len(per_class_ece) if per_class_ece else None
        brier = brier_sum / len(members) if members else None
        representative = members[0][0]
        effective_blocks = len({_block_of(block_ids, member[0]) for member in members})
        status = _row_status(len(members), effective_blocks, calibration_error,
                             policy, as_of_nano, valid_until_nano)
        geometry_key = geometry.key(representative, event_name)
        if geometry_key is None:
            continue
        rows.append(CalibrationEvidence(
            evidence_id="pending",
            model_id=model_id,
            policy_id=policy_id,
            product=representative.product,
            session_kind=representative.session_kind,
            duration_seconds=representative.duration_seconds,
            base_regime=representative.base_regime,
            barrier_key=key[5],
            geometry_key=geometry_key,
            event=event_name,
            horizon=representative.horizon,
            label_version=LABEL_VERSION,
            bucket=ALL_BUCKET,
            sample_count=len(members),
            effective_blocks=effective_blocks,
            calibration_error=calibration_error,
            brier=brier,
            training_end_nano=training_end_nano,
            calibration_end_nano=calibration_end_nano,
            valid_until_nano=valid_until_nano,
            status=status,
        ))
    return rows


def _binary_rows(model_id: str, policy_id: str, pairs, event_name: str,
                 probability_get, outcome_get, buckets: tuple[str, ...],
                 barrier_get, block_ids: Mapping[str, str], geometry: _Geometry,
                 training_end_nano: int, calibration_end_nano: int,
                 valid_until_nano: int, policy: ConfidencePolicy,
                 as_of_nano: int) -> list[CalibrationEvidence]:
    groups: dict[tuple, dict[str, list[tuple[float, float, MarketForecast]]]] = {}
    for prediction, label in pairs:
        outcome = outcome_get(label)
        probability = probability_get(prediction)
        if outcome is None or probability is None:
            continue
        bucket = _binary_bucket(probability, buckets)
        if bucket is None:
            continue
        key = (prediction.product, prediction.session_kind, prediction.duration_seconds,
               prediction.base_regime, horizon_code(prediction.horizon), barrier_get(prediction))
        groups.setdefault(key, {}).setdefault(bucket, []).append(
            (probability, 1.0 if outcome else 0.0, prediction))
    rows: list[CalibrationEvidence] = []
    for key in sorted(groups):
        for bucket in sorted(groups[key]):
            members = groups[key][bucket]
            mean_p = sum(member[0] for member in members) / len(members)
            mean_y = sum(member[1] for member in members) / len(members)
            error = abs(mean_p - mean_y)
            brier = sum((member[0] - member[1]) ** 2 for member in members) / len(members)
            effective_blocks = len({_block_of(block_ids, member[2]) for member in members})
            representative = members[0][2]
            status = _row_status(len(members), effective_blocks, error,
                                 policy, as_of_nano, valid_until_nano)
            geometry_key = geometry.key(representative, event_name)
            if geometry_key is None:
                continue
            rows.append(CalibrationEvidence(
                evidence_id="pending",
                model_id=model_id,
                policy_id=policy_id,
                product=representative.product,
                session_kind=representative.session_kind,
                duration_seconds=representative.duration_seconds,
                base_regime=representative.base_regime,
                barrier_key=key[5],
                geometry_key=geometry_key,
                event=event_name,
                horizon=representative.horizon,
                label_version=LABEL_VERSION,
                bucket=bucket,
                sample_count=len(members),
                effective_blocks=effective_blocks,
                calibration_error=error,
                brier=brier,
                training_end_nano=training_end_nano,
                calibration_end_nano=calibration_end_nano,
                valid_until_nano=valid_until_nano,
                status=status,
            ))
    return rows


def build_calibration_evidence(predictions: tuple[MarketForecast, ...],
                               labels: tuple[PathLabel, ...],
                               block_ids: Mapping[str, str],
                               policy: ConfidencePolicy, as_of_nano: int,
                               valid_until_nano: int, *,
                               features: tuple[FeatureSnapshot, ...]) -> tuple[CalibrationEvidence, ...]:
    """Build per-bucket calibration evidence from out-of-time predictions.

    ``training_end_nano`` is derived as the earliest calibration decision
    (training must have ended before it); ``calibration_end_nano`` is
    ``as_of_nano``. ``valid_until_nano`` must postdate every calibration
    input's visible time, else the registration itself is invalid. Geometry
    keys come exclusively from the connected snapshots via M1's
    ``calibration_geometry_key``; a None key refuses that evidence row.
    """
    if not isinstance(predictions, tuple) or any(
            not isinstance(p, MarketForecast) for p in predictions):
        raise TypeError("predictions_must_be_tuple_of_MarketForecast")
    if not isinstance(labels, tuple) or any(not isinstance(l, PathLabel) for l in labels):
        raise TypeError("labels_must_be_tuple_of_PathLabel")
    if not isinstance(block_ids, Mapping):
        raise TypeError("block_ids_must_be_mapping")
    if not isinstance(policy, ConfidencePolicy):
        raise TypeError("policy_must_be_ConfidencePolicy")
    if not isinstance(features, tuple) or any(not isinstance(f, FeatureSnapshot) for f in features):
        raise TypeError("features_must_be_tuple_of_FeatureSnapshot")
    for name, value in (("as_of_nano", as_of_nano), ("valid_until_nano", valid_until_nano)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name}_must_be_int")
    if not predictions and not labels and not features and not block_ids:
        return ()
    if not predictions:
        raise CalibrationError("features_required_whenever_predictions_exist")

    model_ids = {prediction.model_id for prediction in predictions}
    if len(model_ids) > 1:
        raise CalibrationError("mixed_model_id_in_calibration_inputs")
    model_id = model_ids.pop()

    snapshot_map = _validate_connections(predictions, labels, block_ids, features, as_of_nano)

    latest_visible = max(
        [prediction.published_at_nano for prediction in predictions]
        + [label.available_at_nano for label in labels if label.available_at_nano is not None])
    if valid_until_nano <= latest_visible:
        raise CalibrationError("valid_until_not_after_calibration_inputs")

    geometry = _Geometry(snapshot_map)
    training_end_nano = min(prediction.decision_nano for prediction in predictions)
    calibration_end_nano = as_of_nano
    pairs = _usable_pairs(predictions, labels, as_of_nano)
    policy_id = policy.policy_id
    rows: list[CalibrationEvidence] = []

    direction_keys = ("UP", "DOWN", "NEUTRAL")
    path_keys = ("UP_FIRST", "DOWN_FIRST", "NEITHER", "AMBIGUOUS")

    # COMMON events dedupe by contract/snapshot/horizon/event/geometry so the
    # three generic barriers never triple-count one truth (§5.2); this runs
    # only after the full connection checks above.
    def common_pairs(event: str) -> list[tuple[MarketForecast, PathLabel]]:
        seen: set[tuple[str, str, str, str]] = set()
        selected: list[tuple[MarketForecast, PathLabel]] = []
        for prediction, label in pairs:
            key = (prediction.contract_id, prediction.snapshot_id,
                   horizon_code(prediction.horizon),
                   geometry.key(prediction, event) or "")
            if key in seen:
                continue
            seen.add(key)
            selected.append((prediction, label))
        return selected

    rows.extend(_multiclass_all_rows(
        model_id, policy_id, common_pairs("DIRECTION"), "DIRECTION", direction_keys,
        lambda p: dict(p.direction_probabilities), lambda l: l.direction,
        lambda p: "COMMON", block_ids, geometry,
        training_end_nano, calibration_end_nano, valid_until_nano, policy, as_of_nano))

    rows.extend(_multiclass_all_rows(
        model_id, policy_id, pairs, "PATH", path_keys,
        lambda p: dict(p.path_probabilities), lambda l: l.path_outcome,
        lambda p: p.barrier_key, block_ids, geometry,
        training_end_nano, calibration_end_nano, valid_until_nano, policy, as_of_nano))

    for event in PERSISTENCE_EVENTS:
        rows.extend(_binary_rows(
            model_id, policy_id, common_pairs(f"PERSIST:{event}"), f"PERSIST:{event}",
            lambda p, e=event: p.persistence_probabilities.get(e),
            lambda l, e=event: l.persistence.get(e),
            HIGH_PROBABILITY_BUCKETS, lambda p: "COMMON", block_ids, geometry,
            training_end_nano, calibration_end_nano, valid_until_nano, policy, as_of_nano))

    for side in ("LONG", "SHORT"):
        rows.extend(_binary_rows(
            model_id, policy_id, pairs, f"STOP_TOUCH:{side}",
            lambda p, s=side: p.stop_touch_probabilities.get(s),
            lambda l, s=side: l.stop_touched.get(s),
            STOP_LOW_BUCKETS, lambda p: "COMMON", block_ids, geometry,
            training_end_nano, calibration_end_nano, valid_until_nano, policy, as_of_nano))

    rows.extend(_binary_rows(
        model_id, policy_id, common_pairs("RETURN_BEFORE_BREAK"), "RETURN_BEFORE_BREAK",
        lambda p: p.return_before_break_probability,
        lambda l: (None if l.competing_outcome in (None, "AMBIGUOUS")
                   else l.competing_outcome == "RETURN_FIRST"),
        HIGH_PROBABILITY_BUCKETS, lambda p: "COMMON", block_ids, geometry,
        training_end_nano, calibration_end_nano, valid_until_nano, policy, as_of_nano))

    def final_cdf(prediction: MarketForecast, event: str) -> float | None:
        entries = [entry for entry in prediction.time_cdf if entry.event == event]
        if not entries:
            return None
        return max(entries, key=lambda entry: entry.step).probability

    for touch_event, step_field in (("UP_TOUCH", "first_up_step"),
                                    ("DOWN_TOUCH", "first_down_step")):
        rows.extend(_binary_rows(
            model_id, policy_id, pairs, touch_event,
            lambda p, e=touch_event: final_cdf(p, e),
            lambda l, f=step_field: getattr(l, f) is not None,
            HIGH_PROBABILITY_BUCKETS, lambda p: p.barrier_key, block_ids, geometry,
            training_end_nano, calibration_end_nano, valid_until_nano, policy, as_of_nano))

    from ak_trading.v2.schemas import canonical_json_bytes

    identified = []
    for row in rows:
        identified.append(dataclasses.replace(
            row, evidence_id=canonical_evidence_id(row)))
    identified.sort(key=lambda row: canonical_json_bytes(
        evidence_projection_without_back_references(row)))
    return tuple(identified)
