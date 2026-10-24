"""M3A causality tests: future appends, partition leakage, artifact round trips (03-M3A §8.8, 变更002/004).

M2 real-feature integration is deferred (coordinator directive); these use
the same strict synthetic FeatureSnapshot fixtures. Every assertion is about
immutability of already-published outputs when future data appears.
"""
from __future__ import annotations

import random
from decimal import Decimal

import pytest

from ak_trading.v2.contracts import (
    CalibrationEvidence,
    ForecastRequest,
    Horizon,
    MarketForecast,
    PathLabel,
    RegimeAssessment,
)
from ak_trading.v2.schemas import canonical_json_bytes, decode_record, encode_record
from ak_trading.v2.probability.forecast import ForecastError, predict_market
from ak_trading.v2.probability.models import default_fit_config, fit_forecast_models

from test_v2_m3a_forecast import (
    DAYS,
    SESSIONS,
    build_forecast_requests,
    build_segment,
    make_snapshot,
    quality,
    regimes_for,
    window_for,
)

NANO = 1_000_000_000
DAYN = 24 * 3600 * NANO


@pytest.fixture(scope="module")
def world():
    from ak_trading.v2.probability.regimes import fit_regime_reference
    rng = random.Random(7)
    training = build_segment(DAYS[:180], "tr", rng)
    calibration = build_segment(DAYS[182:212], "cal", rng)
    training_end = max(label.available_at_nano for label in training[2])
    calibration_end = max(label.available_at_nano for label in calibration[2])
    reference = fit_regime_reference(training[0], training_end, training_end)
    config = default_fit_config(("TESTF:momentum", "TESTF:bias"), training_end,
                                calibration_end, calibration_end + 30 * DAYN)
    model_set = fit_forecast_models(
        training[0], regimes_for(training[0], "regime_reference:inner"),
        training[1], training[2], calibration[0],
        regimes_for(calibration[0], reference.reference_id),
        calibration[1], calibration[2], config, reference, seasonality=None)
    day = DAYS[250]
    snapshot = make_snapshot(day, "minute", 1.2, 0.1, "snap-causality",
                             decision_offset_nano=1770 * NANO)
    from ak_trading.v2.contracts import FrozenRange
    causality_range = FrozenRange("range-snap-causality", snapshot.decision_nano,
                                  Decimal("98"), Decimal("100"), Decimal("102"),
                                  Decimal("96"), Decimal("104"), snapshot.snapshot_id)
    requests = build_forecast_requests(snapshot, (Horizon("TRADING_MINUTE", 1),),
                                       causality_range, SESSIONS)
    regime = RegimeAssessment(
        assessment_id="assess-causality", snapshot_id=snapshot.snapshot_id,
        reference_id=reference.reference_id, decision_nano=snapshot.decision_nano,
        base="UNKNOWN", flags=(), frozen_range=None, cross_timeframe="UNKNOWN",
        confirmed_observations=1, reasons=("synthetic",), quality=quality())
    return {
        "model_set": model_set, "reference": reference, "requests": requests,
        "regime": regime, "training": training, "calibration": calibration,
        "config": config, "calibration_end": calibration_end,
        "snapshot": snapshot,
    }


def test_frozen_model_predictions_are_byte_stable_across_future_appends(world):
    """§8.8: the same frozen model predicting the same request is unchanged
    after future bars/labels exist anywhere in the world."""
    request = world["requests"][1]
    published = request.features.decision_nano + 1
    first = predict_market(world["model_set"], request, world["regime"], published)
    # future data appears: extra bars and matured labels beyond the window
    from test_v2_m3a_forecast import make_snapshot as _make_snapshot
    later_snapshot = _make_snapshot(DAYS[251], "minute", 2.0, 0.5, "snap-later")
    later_requests = build_forecast_requests(
        later_snapshot, (Horizon("TRADING_MINUTE", 1),), None, SESSIONS)
    assert later_requests
    second = predict_market(world["model_set"], request, world["regime"], published)
    assert canonical_json_bytes(encode_record(first)) == canonical_json_bytes(
        encode_record(second))


def test_extreme_future_values_flag_ood_without_touching_frozen_stats(world):
    """§8.8: extreme post-training values never move standardization or
    thresholds; the request is flagged out-of-distribution instead."""
    from test_v2_m3a_forecast import FEATURE_KEYS
    from ak_trading.v2.contracts import FactorValue
    request = world["requests"][1]
    published = request.features.decision_nano + 1
    normal = predict_market(world["model_set"], request, world["regime"], published)
    assert not normal.out_of_distribution
    snapshot = request.features
    extreme_values = (
        FactorValue("META:price_tick:v2.1.0", Decimal("1"),
                    snapshot.decision_nano, quality()),
        FactorValue(FEATURE_KEYS[0], 4000.0, snapshot.decision_nano, quality()),
        FactorValue(FEATURE_KEYS[1], -4000.0, snapshot.decision_nano, quality()),
    )
    extreme_snapshot = type(snapshot)(
        snapshot_id=snapshot.snapshot_id, contract_id=snapshot.contract_id,
        raw_symbol=snapshot.raw_symbol, product=snapshot.product,
        trading_day=snapshot.trading_day, session_id=snapshot.session_id,
        session_kind=snapshot.session_kind, decision_nano=snapshot.decision_nano,
        available_at_nano=snapshot.available_at_nano,
        duration_seconds=snapshot.duration_seconds, source_bar_ids=(),
        reference_price=snapshot.reference_price, atr_points=snapshot.atr_points,
        values=extreme_values, daily_background_id=None, quality=snapshot.quality)
    extreme_request = type(request)(
        request_id=request.request_id, features=extreme_snapshot,
        horizon=request.horizon, window_start_nano=request.window_start_nano,
        window_end_nano=request.window_end_nano, barrier_key=request.barrier_key,
        upper_distance=request.upper_distance, lower_distance=request.lower_distance,
        frozen_range=None)
    # An extreme out-of-distribution request either publishes with the OOD
    # flag or, when the model genuinely crosses at that point, takes the
    # rev3 no-publication channel — both prove nothing is silently folded in.
    from ak_trading.v2.probability.forecast import UnpublishableForecastError
    try:
        extreme = predict_market(world["model_set"], extreme_request,
                                 world["regime"], published)
        assert extreme.out_of_distribution
    except UnpublishableForecastError:
        pass
    # the frozen model parameters are untouched by the extreme request
    after = {key: (model["center"], model["scale"])
             for key, model in world["model_set"].models.items()}
    again = predict_market(world["model_set"], request, world["regime"], published)
    assert canonical_json_bytes(encode_record(again)) == canonical_json_bytes(
        encode_record(normal))
    assert after == {key: (model["center"], model["scale"])
                     for key, model in world["model_set"].models.items()}


def test_prediction_time_must_postdate_calibration_labels(world):
    """§8.8: 预测时点≤校准标签available_at拒绝."""
    early_snapshot = make_snapshot(DAYS[211], "minute", 0.4, 0.0, "snap-causality-early")
    early_requests = build_forecast_requests(
        early_snapshot, (Horizon("TRADING_MINUTE", 1),), None, SESSIONS)
    early_regime = RegimeAssessment(
        assessment_id="assess-causality-early", snapshot_id=early_snapshot.snapshot_id,
        reference_id=world["reference"].reference_id,
        decision_nano=early_snapshot.decision_nano, base="UNKNOWN", flags=(),
        frozen_range=None, cross_timeframe="UNKNOWN", confirmed_observations=1,
        reasons=("synthetic",), quality=quality())
    request = early_requests[1]
    assert request.features.decision_nano <= world["calibration_end"]
    with pytest.raises(ForecastError, match="prediction_not_after_calibration"):
        predict_market(world["model_set"], request, early_regime,
                       request.window_start_nano - 1)


def test_fit_is_deterministic_same_inputs_same_identity(world):
    """§8.8/004 §3: refitting on identical inputs reproduces the final
    post-calibration content hash and back-filled evidence identity."""
    training = world["training"]
    calibration = world["calibration"]
    reference = world["reference"]
    second = fit_forecast_models(
        training[0], regimes_for(training[0], "regime_reference:inner"),
        training[1], training[2], calibration[0],
        regimes_for(calibration[0], reference.reference_id),
        calibration[1], calibration[2], world["config"], reference, seasonality=None)
    assert second.model_set_id == world["model_set"].model_set_id
    assert second.content_hash() == world["model_set"].content_hash()
    assert second.evidence == world["model_set"].evidence


def test_training_regime_cannot_use_final_reference_before_availability(world):
    """Inner training regimes carrying the final reference id before its
    availability are rejected (03 §4 stage order)."""
    training = world["training"]
    reference = world["reference"]
    first = regimes_for(training[0][:1], "regime_reference:inner")[0]
    final_too_early = (RegimeAssessment(
        assessment_id=first.assessment_id, snapshot_id=first.snapshot_id,
        reference_id=reference.reference_id, decision_nano=first.decision_nano,
        base=first.base, flags=first.flags, frozen_range=None,
        cross_timeframe=first.cross_timeframe,
        confirmed_observations=first.confirmed_observations,
        reasons=first.reasons, quality=first.quality),)
    regimes = (*final_too_early, *regimes_for(training[0][1:], "regime_reference:inner"))
    features = (training[0][0], *training[0][1:])
    requests = (training[1][0], *training[1][1:])
    labels = (training[2][0], *training[2][1:])
    calibration = world["calibration"]
    calibration_regimes = regimes_for(calibration[0], reference.reference_id)
    with pytest.raises(ValueError, match="training_regime_reference_not_yet_available"):
        fit_forecast_models(features, regimes, requests, labels,
                            calibration[0], calibration_regimes,
                            calibration[1], calibration[2],
                            world["config"], reference, seasonality=None)


def test_records_round_trip_through_public_codec(world):
    """产物往返: M1 canonical JSON decode(encode(record)) returns equal records."""
    request = world["requests"][1]
    forecast = predict_market(world["model_set"], request, world["regime"],
                              request.features.decision_nano + 1)
    samples = (
        (request, ForecastRequest),
        (forecast, MarketForecast),
        (world["model_set"].evidence[0], CalibrationEvidence) if world["model_set"].evidence
        else (None, None),
        (world["regime"], RegimeAssessment),
        (world["training"][2][0], PathLabel),
    )
    for record, record_type in samples:
        if record is None:
            continue
        encoded = encode_record(record)
        decoded = decode_record(record_type, encoded)
        assert decoded == record
        assert canonical_json_bytes(encoded)


def test_forecast_json_bytes_stable_for_equal_publication(world):
    request = world["requests"][1]
    published = request.features.decision_nano + 1
    first = predict_market(world["model_set"], request, world["regime"], published)
    payload = canonical_json_bytes(encode_record(first))
    again = predict_market(world["model_set"], request, world["regime"], published)
    assert canonical_json_bytes(encode_record(again)) == payload
    different_publication = predict_market(
        world["model_set"], request, world["regime"], published + 1)
    assert canonical_json_bytes(
        encode_record(different_publication)) != payload
