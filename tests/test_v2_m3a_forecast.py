"""M3A forecast tests: request construction, publication, consistency (03-M3A §8.1 coverage for M3A-01/07).

Requests first (`-k request`), then multi-horizon/multi-barrier consistency,
freshness/expiry, late publication and out-of-distribution behaviour.
"""
from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ak_trading.v2.contracts import (
    DataQuality,
    FactorValue,
    FeatureSnapshot,
    Horizon,
    RegimeAssessment,
    SessionWindow,
)
from ak_trading.v2.clock import horizon_window
from ak_trading.v2.probability.forecast import (
    ForecastError,
    UnpublishableForecastError,
    build_forecast_requests,
    check_forecast_consistency,
    predict_market,
    publish_market_forecast,
)
from ak_trading.v2.probability.models import default_fit_config, fit_forecast_models

NANO = 1_000_000_000
MINUTE = 60 * NANO
DAYN = 24 * 3600 * NANO
HASH = hashlib.sha256(b"m3a-forecast").hexdigest()
PRODUCT = "SHFE.RB"
CONTRACT = "SHFE.RB2605"
BASE = 1_770_000_000 * NANO
FEATURE_KEYS = ("TESTF:momentum", "TESTF:bias")


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


DAYS = trading_days(260)
DAY_INDEX = {day: index for index, day in enumerate(DAYS)}
SESSIONS = tuple(
    SessionWindow(
        session_id=f"{PRODUCT}-{day.isoformat()}", product=PRODUCT, trading_day=day,
        start_nano=BASE + index * DAYN, end_nano=BASE + index * DAYN + 60 * MINUTE,
        kind="DAY", calendar_version="cal-v2.1.0", evidence_status="SCENARIO")
    for index, day in enumerate(DAYS))

_window_cache: dict[tuple[int, str, int], tuple[int, int]] = {}


def make_snapshot(day: date, mode: str, momentum: float, bias: float,
                  snapshot_id: str, tick: bool = True,
                  decision_offset_nano: int = 0) -> FeatureSnapshot:
    decision = BASE + DAY_INDEX[day] * DAYN - 1 + decision_offset_nano
    values = []
    if tick:
        values.append(FactorValue("META:price_tick:v2.1.0", Decimal("1"),
                                  decision, quality()))
    values.extend((
        FactorValue(FEATURE_KEYS[0], momentum, decision, quality()),
        FactorValue(FEATURE_KEYS[1], bias, decision, quality())))
    return FeatureSnapshot(
        snapshot_id=snapshot_id, contract_id=CONTRACT, raw_symbol="RB2605",
        product=PRODUCT, trading_day=day, session_id=f"sess-{snapshot_id}",
        session_kind="DAILY" if mode == "daily" else "DAY",
        decision_nano=decision, available_at_nano=decision,
        duration_seconds=86400 if mode == "daily" else 60, source_bar_ids=(),
        reference_price=Decimal("100"), atr_points=Decimal("2"), values=tuple(values),
        daily_background_id=None, quality=quality())


def window_for(snapshot: FeatureSnapshot, horizon: Horizon) -> tuple[int, int]:
    key = (snapshot.decision_nano, horizon.unit, horizon.value)
    if key not in _window_cache:
        _window_cache[key] = horizon_window(snapshot.decision_nano, PRODUCT, horizon, SESSIONS)
    return _window_cache[key]


def build_segment(segment_days, prefix, rng, horizons=None):
    from ak_trading.v2.contracts import ForecastRequest, PathLabel
    from ak_trading.v2.artifacts import stable_id
    if horizons is None:
        horizons = (("minute", Horizon("TRADING_MINUTE", 1)),
                    ("daily", Horizon("TRADING_DAY", 1)))
    features, requests, labels = [], [], []
    for day in segment_days:
        for mode, horizon in horizons:
            momentum = rng.uniform(-1.5, 1.5)
            bias = rng.uniform(-0.3, 0.3)
            snapshot = make_snapshot(day, mode, momentum, bias, f"snap-{prefix}-{mode}-{len(features)}")
            features.append(snapshot)
            window_start, window_end = window_for(snapshot, horizon)
            up = momentum > 0.3
            down = momentum < -0.3
            terminal_atr = momentum * 0.5 + rng.uniform(-0.2, 0.2)
            from ak_trading.v2.contracts import FrozenRange
            frozen = FrozenRange(f"range-{snapshot.snapshot_id}", snapshot.decision_nano, Decimal("98"),
                                 Decimal("100"), Decimal("102"), Decimal("96"),
                                 Decimal("104"), snapshot.snapshot_id)
            competing = ("RETURN_FIRST" if momentum > 0.8 else
                         "BREAK_FIRST" if momentum < -0.8 else "NEITHER")
            for barrier, distance in (("ATR_0.5", "1"), ("ATR_1", "2"), ("ATR_2", "4")):
                request = ForecastRequest(
                    request_id=stable_id("forecast_request",
                                         f"{snapshot.snapshot_id}|{horizon.unit}{horizon.value}|{barrier}"),
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
                    reasons=(), direction="UP" if up else ("DOWN" if down else "NEUTRAL"),
                    terminal_return=terminal_atr * 2 / 100,
                    terminal_points=Decimal(str(terminal_atr * 2)),
                    terminal_atr=terminal_atr,
                    path_outcome="UP_FIRST" if up else ("DOWN_FIRST" if down else "NEITHER"),
                    first_up_step=1 if up else None, first_down_step=1 if down else None,
                    return_step=1 if competing == "RETURN_FIRST" else None,
                    break_step=1 if competing == "BREAK_FIRST" else None,
                    competing_outcome=competing,
                    persistence={"UP_TREND": False, "DOWN_TREND": False,
                                 "ORDERED_RANGE": abs(momentum) < 0.5,
                                 "WIDE_RANGE": abs(momentum) < 0.5,
                                 "COMPRESSION": momentum > 0.8,
                                 "UP_BREAKOUT": None, "DOWN_BREAKOUT": None},
                    stop_touched={"LONG": momentum < -1.2, "SHORT": momentum > 1.2},
                    range_id=frozen.range_id))
    return tuple(features), tuple(requests), tuple(labels)


def regimes_for(features, reference_id):
    return tuple(
        RegimeAssessment(assessment_id=f"assess-{snapshot.snapshot_id}",
                         snapshot_id=snapshot.snapshot_id, reference_id=reference_id,
                         decision_nano=snapshot.decision_nano, base="UNKNOWN", flags=(),
                         frozen_range=None, cross_timeframe="UNKNOWN",
                         confirmed_observations=1, reasons=("synthetic",), quality=quality())
        for snapshot in features)


@pytest.fixture(scope="module")
def published_world():
    from ak_trading.v2.probability.regimes import fit_regime_reference
    rng = random.Random(7)
    horizons = (("minute", Horizon("TRADING_MINUTE", 1)),
                ("minute", Horizon("TRADING_MINUTE", 5)),
                ("daily", Horizon("TRADING_DAY", 1)))
    training = build_segment(DAYS[:180], "tr", rng, horizons)
    calibration = build_segment(DAYS[182:212], "cal", rng, horizons)
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
    # a fresh post-calibration request whose window opens after the model's
    # valid_until, so on-time publication, expiry and staleness are separable
    day = DAYS[250]
    # decided mid-session so a real (decision, window_start) publication gap
    # exists for on-time / TTL / staleness tests
    snapshot = make_snapshot(day, "minute", 1.2, 0.1, "snap-future",
                             decision_offset_nano=1770 * NANO)  # mid-minute
    from ak_trading.v2.contracts import FrozenRange
    future_range = FrozenRange("range-snap-future", snapshot.decision_nano, Decimal("98"),
                               Decimal("100"), Decimal("102"), Decimal("96"),
                               Decimal("104"), snapshot.snapshot_id)
    requests = build_forecast_requests(
        snapshot, (Horizon("TRADING_MINUTE", 1), Horizon("TRADING_MINUTE", 5)),
        future_range, SESSIONS)
    regime = RegimeAssessment(
        assessment_id="assess-future", snapshot_id=snapshot.snapshot_id,
        reference_id=reference.reference_id, decision_nano=snapshot.decision_nano,
        base="UNKNOWN", flags=(), frozen_range=None, cross_timeframe="UNKNOWN",
        confirmed_observations=1, reasons=("synthetic",), quality=quality())
    # a same-day minute request whose on-time publication window still lies
    # inside the calibration segment (for the causality guard)
    early_day = DAYS[211]
    early_snapshot = make_snapshot(early_day, "minute", 0.4, 0.0, "snap-early")
    early_requests = build_forecast_requests(early_snapshot, (Horizon("TRADING_MINUTE", 1),), None, SESSIONS)
    early_regime = RegimeAssessment(
        assessment_id="assess-early", snapshot_id=early_snapshot.snapshot_id,
        reference_id=reference.reference_id, decision_nano=early_snapshot.decision_nano,
        base="UNKNOWN", flags=(), frozen_range=None, cross_timeframe="UNKNOWN",
        confirmed_observations=1, reasons=("synthetic",), quality=quality())
    return {
        "model_set": model_set, "reference": reference, "snapshot": snapshot,
        "requests": requests, "regime": regime, "calibration_end": calibration_end,
        "valid_until": model_set.valid_until_nano,
        "publish": snapshot.decision_nano + 1,
        "early_requests": early_requests, "early_regime": early_regime,
    }


# ---------------------------------------------------------------------------
# M3A-01: request identity, barriers, horizons (-k request)
# ---------------------------------------------------------------------------

def test_request_three_distances_per_horizon_in_order():
    day = DAYS[0]
    snapshot = make_snapshot(day, "minute", 0.5, 0.0, "snap-req")
    requests = build_forecast_requests(
        snapshot, (Horizon("TRADING_MINUTE", 1), Horizon("TRADING_MINUTE", 5)), None, SESSIONS)
    assert len(requests) == 6
    assert [request.horizon.value for request in requests] == [1, 1, 1, 5, 5, 5]
    assert [request.barrier_key for request in requests] == [
        "ATR_0.5", "ATR_1", "ATR_2", "ATR_0.5", "ATR_1", "ATR_2"]
    assert all(request.upper_distance == request.lower_distance for request in requests)
    assert requests[0].upper_distance == Decimal("1")   # 0.5 × ATR 2
    assert requests[1].upper_distance == Decimal("2")
    assert requests[2].upper_distance == Decimal("4")
    assert len({request.request_id for request in requests}) == 6


def test_request_identity_binds_anchor_and_label_version():
    day = DAYS[0]
    snapshot = make_snapshot(day, "minute", 0.5, 0.0, "snap-req")
    first = build_forecast_requests(snapshot, (Horizon("TRADING_MINUTE", 1),), None, SESSIONS)
    second = build_forecast_requests(snapshot, (Horizon("TRADING_MINUTE", 1),), None, SESSIONS)
    assert first == second  # deterministic identity
    changed_atr = FeatureSnapshot(
        snapshot_id=snapshot.snapshot_id, contract_id=snapshot.contract_id,
        raw_symbol=snapshot.raw_symbol, product=snapshot.product,
        trading_day=snapshot.trading_day, session_id=snapshot.session_id,
        session_kind=snapshot.session_kind, decision_nano=snapshot.decision_nano,
        available_at_nano=snapshot.available_at_nano,
        duration_seconds=snapshot.duration_seconds, source_bar_ids=(),
        reference_price=snapshot.reference_price, atr_points=Decimal("3"),
        values=snapshot.values, daily_background_id=None, quality=snapshot.quality)
    other = build_forecast_requests(changed_atr, (Horizon("TRADING_MINUTE", 1),), None, SESSIONS)
    assert other[0].request_id != first[0].request_id


def test_request_requires_price_tick_metadata():
    day = DAYS[0]
    snapshot = make_snapshot(day, "minute", 0.5, 0.0, "snap-notick", tick=False)
    with pytest.raises(ForecastError, match="missing_price_tick"):
        build_forecast_requests(snapshot, (Horizon("TRADING_MINUTE", 1),), None, SESSIONS)


def test_request_mode_matches_snapshot_kind():
    day = DAYS[0]
    minute_snapshot = make_snapshot(day, "minute", 0.5, 0.0, "snap-minute")
    daily_snapshot = make_snapshot(day, "daily", 0.5, 0.0, "snap-daily")
    with pytest.raises(ForecastError, match="daily_horizon_requires_daily_snapshot"):
        build_forecast_requests(minute_snapshot, (Horizon("TRADING_DAY", 1),), None, SESSIONS)
    with pytest.raises(ForecastError, match="minute_horizon_requires_intraday_snapshot"):
        build_forecast_requests(daily_snapshot, (Horizon("TRADING_MINUTE", 1),), None, SESSIONS)


def test_request_all_eight_horizons_produce_requests():
    day = DAYS[0]
    minute_snapshot = make_snapshot(day, "minute", 0.5, 0.0, "snap-eight-minute")
    daily_snapshot = make_snapshot(day, "daily", 0.5, 0.0, "snap-eight-daily")
    minute_requests = build_forecast_requests(
        minute_snapshot,
        (Horizon("TRADING_MINUTE", 1), Horizon("TRADING_MINUTE", 5),
         Horizon("TRADING_MINUTE", 15), Horizon("TRADING_MINUTE", 30),
         Horizon("TRADING_MINUTE", 60)), None, SESSIONS)
    daily_requests = build_forecast_requests(
        daily_snapshot,
        (Horizon("TRADING_DAY", 1), Horizon("TRADING_DAY", 5), Horizon("TRADING_DAY", 20)),
        None, SESSIONS)
    assert len(minute_requests) == 15
    assert len(daily_requests) == 9
    assert len({request.request_id for request in (*minute_requests, *daily_requests)}) == 24


# ---------------------------------------------------------------------------
# M3A-07: publication, freshness, late records, consistency
# ---------------------------------------------------------------------------

def test_predict_market_publishes_on_time_with_valid_probabilities(published_world):
    world = published_world
    request = world["requests"][1]  # ATR_1, horizon M1
    published = world["publish"]
    assert request.window_start_nano > published
    forecast = predict_market(world["model_set"], request, world["regime"], published)
    # the model itself has expired by this late-day window: on-time but STALE
    assert forecast.quality.status in ("READY", "DEGRADED", "STALE")
    assert forecast.quality.status != "INVALID"
    assert "FORECAST_LATE" not in forecast.quality.reasons
    assert abs(sum(forecast.direction_probabilities.values()) - 1.0) <= 1e-12
    assert abs(sum(forecast.path_probabilities.values()) - 1.0) <= 1e-12
    quantiles = [forecast.atr_quantiles[key] for key in ("Q05", "Q25", "Q50", "Q75", "Q95")]
    assert all(quantiles[i] < quantiles[i + 1] for i in range(4))
    assert forecast.model_id == world["model_set"].model_set_id
    assert forecast.published_at_nano == published


def test_predict_market_evidence_rows_reference_model_set(published_world):
    world = published_world
    request = world["requests"][1]
    forecast = predict_market(world["model_set"], request, world["regime"],
                              world["publish"])
    assert forecast.evidence
    assert all(row.model_id == world["model_set"].model_set_id for row in forecast.evidence)


def test_predict_market_minute_ttl_expiry(published_world):
    world = published_world
    request = world["requests"][4]  # ATR_1 of the M5 horizon
    published = request.window_start_nano - 1  # == decision instant
    forecast = predict_market(world["model_set"], request, world["regime"], published)
    assert forecast.expires_at_nano == min(published + 120 * NANO, request.window_end_nano)
    assert published < forecast.expires_at_nano <= request.window_end_nano


def test_predict_market_late_at_window_start_is_invalid_forecast_late(published_world):
    """§8.11: exactly published == window_start must be FORECAST_LATE."""
    world = published_world
    request = world["requests"][1]
    forecast = predict_market(world["model_set"], request, world["regime"],
                              request.window_start_nano)
    assert forecast.quality.status == "INVALID"
    assert "FORECAST_LATE" in forecast.quality.reasons
    assert forecast.expires_at_nano == request.window_end_nano
    assert forecast.request_id == request.request_id
    assert forecast.published_at_nano == request.window_start_nano
    assert abs(sum(forecast.direction_probabilities.values()) - 1.0) <= 1e-12


def test_predict_market_late_after_window_end_keeps_window_end_expiry(published_world):
    world = published_world
    request = world["requests"][1]
    forecast = predict_market(world["model_set"], request, world["regime"],
                              request.window_end_nano + 5 * NANO)
    assert forecast.quality.status == "INVALID"
    assert "FORECAST_LATE" in forecast.quality.reasons
    assert forecast.expires_at_nano == request.window_end_nano  # audit expiry may precede published


def test_predict_market_rejects_publication_before_calibration_end(published_world):
    """An on-time publication at or before the calibration end is rejected:
    published must postdate every calibration label's availability."""
    world = published_world
    request = world["early_requests"][1]
    assert request.features.decision_nano <= world["calibration_end"]
    with pytest.raises(ForecastError, match="prediction_not_after_calibration"):
        predict_market(world["model_set"], request, world["early_regime"],
                       request.window_start_nano - 1)


def test_predict_market_rejects_decision_inside_training(published_world):
    world = published_world
    training_request = world["model_set"]
    # a request decided inside the training window (re-using a training snapshot)
    from ak_trading.v2.contracts import ForecastRequest
    day = DAYS[10]
    snapshot = make_snapshot(day, "minute", 0.4, 0.0, "snap-inside")
    window_start, window_end = window_for(snapshot, Horizon("TRADING_MINUTE", 1))
    request = ForecastRequest(
        request_id="req-inside", features=snapshot, horizon=Horizon("TRADING_MINUTE", 1),
        window_start_nano=window_start, window_end_nano=window_end,
        barrier_key="ATR_1", upper_distance=Decimal("2"), lower_distance=Decimal("2"),
        frozen_range=None)
    regime = RegimeAssessment(
        assessment_id="assess-inside", snapshot_id=snapshot.snapshot_id,
        reference_id=world["reference"].reference_id,
        decision_nano=snapshot.decision_nano, base="UNKNOWN", flags=(),
        frozen_range=None, cross_timeframe="UNKNOWN", confirmed_observations=1,
        reasons=("synthetic",), quality=quality())
    with pytest.raises(ForecastError, match="decision_within_training_window"):
        predict_market(training_request, request, regime, window_start - 1)


def test_predict_market_rejects_regime_mismatch(published_world):
    world = published_world
    request = world["requests"][1]
    wrong_snapshot = RegimeAssessment(
        assessment_id="assess-other", snapshot_id="snap-other",
        reference_id=world["reference"].reference_id,
        decision_nano=world["snapshot"].decision_nano, base="UNKNOWN", flags=(),
        frozen_range=None, cross_timeframe="UNKNOWN", confirmed_observations=1,
        reasons=(), quality=quality())
    with pytest.raises(ForecastError, match="regime_snapshot_mismatch"):
        predict_market(world["model_set"], request, wrong_snapshot,
                       world["calibration_end"] + NANO)
    wrong_reference = RegimeAssessment(
        assessment_id="assess-future", snapshot_id=world["snapshot"].snapshot_id,
        reference_id="regime_reference:other",
        decision_nano=world["snapshot"].decision_nano, base="UNKNOWN", flags=(),
        frozen_range=None, cross_timeframe="UNKNOWN", confirmed_observations=1,
        reasons=(), quality=quality())
    with pytest.raises(ForecastError, match="regime_reference_mismatch"):
        predict_market(world["model_set"], request, wrong_reference,
                       world["calibration_end"] + NANO)


def test_predict_market_stale_when_model_expired(published_world):
    """等号过期: an on-time publication exactly at valid_until is STALE."""
    import dataclasses
    world = published_world
    request = world["requests"][1]
    decision = request.features.decision_nano
    assert decision + 1 < request.window_start_nano
    expiring = dataclasses.replace(world["model_set"],
                                   valid_until_nano=decision + 30 * NANO)
    published = expiring.valid_until_nano
    assert decision < published < request.window_start_nano
    forecast = predict_market(expiring, request, world["regime"], published)
    assert forecast.quality.status == "STALE"
    assert "model_expired" in forecast.quality.reasons
    assert "FORECAST_LATE" not in forecast.quality.reasons


def test_common_components_identical_across_barriers(published_world):
    """§8.11: the three barrier forecasts of one snapshot share every COMMON
    component byte-for-byte; only path/touch entries may differ."""
    world = published_world
    published = world["publish"]
    m1_requests = [request for request in world["requests"]
                   if request.horizon == Horizon("TRADING_MINUTE", 1)]
    forecasts = tuple(predict_market(world["model_set"], request, world["regime"], published)
                      for request in m1_requests)
    assert len(forecasts) == 3
    reference = forecasts[0]
    for forecast in forecasts[1:]:
        assert forecast.direction_probabilities == reference.direction_probabilities
        assert forecast.return_quantiles == reference.return_quantiles
        assert forecast.point_quantiles == reference.point_quantiles
        assert forecast.atr_quantiles == reference.atr_quantiles
        assert forecast.persistence_probabilities == reference.persistence_probabilities
        assert forecast.stop_touch_probabilities == reference.stop_touch_probabilities
    assert check_forecast_consistency(forecasts) == ()


def test_check_consistency_detects_unit_mismatch_and_divergence(published_world):
    world = published_world
    published = world["publish"]
    m1_requests = [request for request in world["requests"]
                   if request.horizon == Horizon("TRADING_MINUTE", 1)]
    forecasts = list(predict_market(world["model_set"], request, world["regime"], published)
                     for request in m1_requests)
    import dataclasses
    # M1 now enforces point/return/ATR unit consistency at construction, so a
    # genuinely inconsistent triple cannot become a legal record at all.
    broken_return = dict(forecasts[0].return_quantiles)
    broken_return["Q50"] = (broken_return["Q25"] + broken_return["Q50"]) / 2.0
    with pytest.raises(ValueError, match="quantile_unit_inconsistency"):
        dataclasses.replace(forecasts[0], return_quantiles=broken_return)
    # consistent records from one anchor produce no violations
    assert check_forecast_consistency((forecasts[0], forecasts[1], forecasts[2])) == ()
    # divergence of a COMMON component across barriers is still detected
    tampered_common = dataclasses.replace(
        forecasts[1],
        direction_probabilities={"UP": 0.6, "DOWN": 0.2, "NEUTRAL": 0.2})
    violations2 = check_forecast_consistency((forecasts[0], tampered_common, forecasts[2]))
    assert any("common_component_diverges" in item for item in violations2)


def test_check_consistency_detects_barrier_monotonicity_violation():
    from ak_trading.v2.contracts import MarketForecast, TimeProbability
    # displacement convention: point = C_end − C with C=100/ATR=2
    quantiles = {"Q05": -0.02, "Q25": -0.01, "Q50": 0.0, "Q75": 0.01, "Q95": 0.02}
    points = {"Q05": Decimal("-2"), "Q25": Decimal("-1"), "Q50": Decimal("0"),
              "Q75": Decimal("1"), "Q95": Decimal("2")}
    atrs = {"Q05": -1.0, "Q25": -0.5, "Q50": 0.0, "Q75": 0.5, "Q95": 1.0}

    def forecast(barrier: str, up_touch: float, down_touch: float) -> MarketForecast:
        return MarketForecast(
            forecast_id=f"fc-{barrier}", request_id=f"req-{barrier}", snapshot_id="snap-1",
            contract_id=CONTRACT, product=PRODUCT, trading_day=DAYS[0], session_id="sess-1",
            session_kind="DAY", duration_seconds=60, assessment_id="a", base_regime="UNKNOWN",
            regime_reference_id="rr", decision_nano=1_000, published_at_nano=1_500,
            expires_at_nano=2_500, horizon=Horizon("TRADING_MINUTE", 1),
            window_start_nano=2_000, window_end_nano=3_000, barrier_key=barrier,
            reference_price=Decimal("100"), atr_points=Decimal("2"),
            upper_distance=Decimal("2"), lower_distance=Decimal("2"), frozen_range=None,
            label_version="path-label-v2.1.0", model_id="m",
            direction_probabilities={"UP": 0.2, "DOWN": 0.3, "NEUTRAL": 0.5},
            return_quantiles=dict(quantiles), point_quantiles=dict(points),
            atr_quantiles=dict(atrs),
            path_probabilities={"UP_FIRST": 0.25, "DOWN_FIRST": 0.25,
                                "NEITHER": 0.25, "AMBIGUOUS": 0.25},
            time_cdf=(TimeProbability("UP_TOUCH", 1, up_touch, 0.0),
                      TimeProbability("DOWN_TOUCH", 1, down_touch, 0.0)),
            persistence_probabilities={}, stop_touch_probabilities={},
            return_before_break_probability=None, evidence=(), out_of_distribution=False,
            quality=quality())

    # the farther barrier is easier to touch than the nearer one → violation
    trio = (forecast("ATR_0.5", 0.4, 0.4), forecast("ATR_1", 0.5, 0.5),
            forecast("ATR_2", 0.9, 0.5))
    violations = check_forecast_consistency(trio)
    assert any("farther_barrier_touch_exceeds_nearer:UP_TOUCH:ATR_2>" in item
               for item in violations)
    assert any("farther_barrier_touch_exceeds_nearer:UP_TOUCH:ATR_1>" in item
               for item in violations)


# ---------------------------------------------------------------------------
# 变更申请-004 §4: live publication entry with the injected clock
# ---------------------------------------------------------------------------

def test_publish_market_forecast_takes_clock_after_inference(published_world):
    """The clock is read at the publication boundary; a deterministic clock
    yields the same record as the research entry at that time."""
    world = published_world
    request = world["requests"][1]
    research = predict_market(world["model_set"], request, world["regime"],
                              world["publish"])
    live = publish_market_forecast(
        world["model_set"], request, world["regime"],
        clock_nano=lambda: world["publish"])
    assert live == research
    assert live.published_at_nano == world["publish"]


def test_publish_market_forecast_late_clock_yields_invalid_record(published_world):
    """计算开始在start−2ns、完成在start+1ns → 只能发布迟发记录 (004 §4)."""
    world = published_world
    request = world["requests"][1]
    live = publish_market_forecast(
        world["model_set"], request, world["regime"],
        clock_nano=lambda: request.window_start_nano + 1)
    assert live.quality.status == "INVALID"
    assert "FORECAST_LATE" in live.quality.reasons
    assert live.expires_at_nano == request.window_end_nano
    assert live.published_at_nano == request.window_start_nano + 1


def test_publish_market_forecast_rejects_backwards_clock(published_world):
    world = published_world
    request = world["requests"][1]
    with pytest.raises(ForecastError, match="CLOCK_UNVERIFIED"):
        publish_market_forecast(
            world["model_set"], request, world["regime"],
            clock_nano=lambda: request.features.decision_nano - 1)


def test_publish_market_forecast_requires_callable_clock(published_world):
    world = published_world
    request = world["requests"][1]
    with pytest.raises(TypeError):
        publish_market_forecast(world["model_set"], request, world["regime"],
                                clock_nano=12345)


# ---------------------------------------------------------------------------
# 变更申请-006: unsupported cells never publish a record (M5 unit-15 regression)
# ---------------------------------------------------------------------------

def test_unsupported_horizon_with_range_never_constructs_a_record(published_world):
    """M5 unit-15 regression: the model set supports M1/M5/D1; an M60 request
    carrying a frozen range must NOT construct a MarketForecast (M1 would
    reject it with range_forecast_requires_both_stop_probabilities) — the
    per-request non-publication result carries the reason instead."""
    from ak_trading.v2.contracts import FrozenRange
    world = published_world
    snapshot = make_snapshot(DAYS[250], "minute", 0.9, 0.0, "snap-m60",
                             decision_offset_nano=1770 * NANO)
    frozen = FrozenRange("range-m60", snapshot.decision_nano, Decimal("98"),
                         Decimal("100"), Decimal("102"), Decimal("96"),
                         Decimal("104"), snapshot.snapshot_id)
    requests = build_forecast_requests(
        snapshot, (Horizon("TRADING_MINUTE", 60),), frozen, SESSIONS)
    m60_regime = RegimeAssessment(
        assessment_id="assess-m60", snapshot_id=snapshot.snapshot_id,
        reference_id=world["reference"].reference_id,
        decision_nano=snapshot.decision_nano, base="UNKNOWN", flags=(),
        frozen_range=frozen, cross_timeframe="UNKNOWN", confirmed_observations=1,
        reasons=("synthetic",), quality=quality())
    for request in requests:
        with pytest.raises(UnpublishableForecastError) as excinfo:
            predict_market(world["model_set"], request, m60_regime,
                           request.features.decision_nano + 1)
        assert excinfo.value.request_id == request.request_id
        assert "horizon_unsupported_by_model" in excinfo.value.reasons
    # without a range the unsupported cell is still never published
    bare = build_forecast_requests(snapshot, (Horizon("TRADING_MINUTE", 60),),
                                   None, SESSIONS)
    with pytest.raises(UnpublishableForecastError):
        publish_market_forecast(world["model_set"], bare[0], m60_regime,
                                clock_nano=lambda: bare[0].features.decision_nano + 1)


def test_supported_range_request_publishes_both_stop_probabilities(published_world):
    """Positive control: a supported cell with a frozen range carries BOTH stop
    probabilities (never the empty dict that crashed the M5 loop)."""
    from ak_trading.v2.contracts import FrozenRange
    world = published_world
    snapshot = make_snapshot(DAYS[250], "minute", 0.9, 0.0, "snap-range-ok",
                             decision_offset_nano=1770 * NANO)
    frozen = FrozenRange("range-ok", snapshot.decision_nano, Decimal("98"),
                         Decimal("100"), Decimal("102"), Decimal("96"),
                         Decimal("104"), snapshot.snapshot_id)
    requests = build_forecast_requests(
        snapshot, (Horizon("TRADING_MINUTE", 1),), frozen, SESSIONS)
    regime = RegimeAssessment(
        assessment_id="assess-range-ok", snapshot_id=snapshot.snapshot_id,
        reference_id=world["reference"].reference_id,
        decision_nano=snapshot.decision_nano, base="UNKNOWN", flags=(),
        frozen_range=frozen, cross_timeframe="UNKNOWN", confirmed_observations=1,
        reasons=("synthetic",), quality=quality())
    forecast = predict_market(world["model_set"], requests[1], regime,
                              snapshot.decision_nano + 1)
    assert forecast.frozen_range is not None
    assert set(forecast.stop_touch_probabilities) == {"LONG", "SHORT"}
