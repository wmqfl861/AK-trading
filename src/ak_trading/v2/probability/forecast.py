"""Request construction, prediction publication and consistency checks (M3A §4.1/§5.2/§5.3).

``predict_market`` never receives future bars, labels, test payoffs or
account snapshots; the only future-facing entry is ``label_forecast`` in
``labels.py`` (research scoring boundary). Late publication
(``published >= window_start``) returns the same public ``MarketForecast``
type with ``quality.status="INVALID"``, ``FORECAST_LATE`` in reasons and
``expires_at_nano`` pinned to the original ``window_end`` — never an
alternative cross-node protocol (变更申请-002 §4). Consumers exclude it
from calibration/scoring and never open risk from it.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import Decimal

from ak_trading.v2.artifacts import stable_id
from ak_trading.v2.clock import horizon_window
from ak_trading.v2.contracts import (
    CalibrationEvidence,
    DataQuality,
    FeatureSnapshot,
    ForecastRequest,
    FrozenRange,
    Horizon,
    MarketForecast,
    RegimeAssessment,
    SessionWindow,
)
from ak_trading.v2.schemas import calibration_geometry_key

from ak_trading.v2.probability.calibration import geometry_event
from ak_trading.v2.probability.labels import LABEL_VERSION, price_tick_from_features
from ak_trading.v2.probability.models import (
    MINUTE_EXPIRY_NANO,
    ForecastModelSet,
    apply_model_set,
    assemble_forecast,
)

BARRIER_MULTIPLIERS: tuple[tuple[str, Decimal], ...] = (
    ("ATR_0.5", Decimal("0.5")),
    ("ATR_1", Decimal("1")),
    ("ATR_2", Decimal("2")),
)
MINUTE_SESSION_KINDS = frozenset({"DAY", "NIGHT"})
FORECAST_LATE = "FORECAST_LATE"
_UNIT_TOLERANCE = 1e-9
_MONOTONIC_TOLERANCE = 1e-12


class ForecastError(ValueError):
    """Fixed-reason forecast construction violation."""


class UnpublishableForecastError(ForecastError):
    """No legal ``MarketForecast`` can be constructed for this request.

    Per 03 §5.2 (L174) seemingly-valid probabilities are never published: a
    request whose model cell is unsupported (e.g. an M60 request against an
    M1/D1 model set), whose required features are missing, or which carries a
    frozen range without both stop-touch models would otherwise produce a
    record M1 must reject (``range_forecast_requires_both_stop_probabilities``).
    Nothing is constructed — not even a degraded placeholder — and downstream
    consumers use their existing no-forecast → NO_TRADE channel (006).
    """

    def __init__(self, request_id: str, reasons: tuple[str, ...]) -> None:
        super().__init__(f"FORECAST_NOT_PUBLISHED:{request_id}:{','.join(reasons)}")
        self.request_id = request_id
        self.reasons = tuple(reasons)


def build_forecast_requests(features: FeatureSnapshot, horizons: tuple[Horizon, ...],
                            frozen_range: FrozenRange | None,
                            sessions: tuple[SessionWindow, ...]) -> tuple[ForecastRequest, ...]:
    """Three symmetric barrier distances per horizon, ordered horizon-then-distance.

    The request identity binds contract, decision time, reference price C,
    decision ATR, horizon, window, barrier distance, range id, source
    snapshot and the label version ``path-label-v2.1.0``. Requests cannot be
    generated without the decision-time ``META:price_tick:v2.1.0`` factor.
    """
    if not isinstance(features, FeatureSnapshot):
        raise TypeError("features_must_be_FeatureSnapshot")
    if not isinstance(horizons, tuple) or any(not isinstance(h, Horizon) for h in horizons):
        raise TypeError("horizons_must_be_tuple_of_Horizon")
    if frozen_range is not None and not isinstance(frozen_range, FrozenRange):
        raise TypeError("frozen_range_must_be_FrozenRange_or_None")
    if not isinstance(sessions, tuple) or any(not isinstance(s, SessionWindow) for s in sessions):
        raise TypeError("sessions_must_be_tuple_of_SessionWindow")
    reference_price = features.reference_price
    atr_points = features.atr_points
    if reference_price is None or atr_points is None or atr_points <= 0:
        raise ForecastError("missing_reference_price_or_atr")
    for horizon in horizons:
        if horizon.unit == "TRADING_DAY" and features.session_kind != "DAILY":
            raise ForecastError("daily_horizon_requires_daily_snapshot")
        if horizon.unit == "TRADING_MINUTE" and features.session_kind not in MINUTE_SESSION_KINDS:
            raise ForecastError("minute_horizon_requires_intraday_snapshot")
    probe = ForecastRequest(
        request_id="probe", features=features, horizon=Horizon("TRADING_MINUTE", 1),
        window_start_nano=features.decision_nano + 1,
        window_end_nano=features.decision_nano + 2,
        barrier_key="ATR_1", upper_distance=atr_points, lower_distance=atr_points,
        frozen_range=frozen_range)
    if price_tick_from_features(probe) is None:
        raise ForecastError("missing_price_tick")

    requests: list[ForecastRequest] = []
    for horizon in horizons:
        window_start, window_end = horizon_window(
            features.decision_nano, features.product, horizon, sessions)
        for barrier_key, multiplier in BARRIER_MULTIPLIERS:
            distance = multiplier * atr_points
            identity = "|".join((
                features.contract_id, features.snapshot_id, str(features.decision_nano),
                str(reference_price), str(atr_points), horizon.unit, str(horizon.value),
                str(window_start), str(window_end), barrier_key,
                frozen_range.range_id if frozen_range is not None else "NONE",
                LABEL_VERSION))
            requests.append(ForecastRequest(
                request_id=stable_id("forecast_request", identity),
                features=features,
                horizon=horizon,
                window_start_nano=window_start,
                window_end_nano=window_end,
                barrier_key=barrier_key,
                upper_distance=distance,
                lower_distance=distance,
                frozen_range=frozen_range,
            ))
    return tuple(requests)


def _matching_evidence(models: ForecastModelSet, request: ForecastRequest,
                       regime: RegimeAssessment,
                       features: FeatureSnapshot | None = None) -> tuple[CalibrationEvidence, ...]:
    snapshot = features if features is not None else request.features
    matched: list[CalibrationEvidence] = []
    for row in models.evidence:
        if row.model_id != models.model_set_id:
            continue
        if (row.product != snapshot.product or row.session_kind != snapshot.session_kind
                or row.duration_seconds != snapshot.duration_seconds
                or row.base_regime != regime.base or row.horizon != request.horizon):
            continue
        event = row.event
        needs_actual_barrier = event in ("PATH", "UP_TOUCH", "DOWN_TOUCH")
        if needs_actual_barrier and row.barrier_key != request.barrier_key:
            continue
        if not needs_actual_barrier and row.barrier_key != "COMMON":
            continue
        geometry = calibration_geometry_key(
            snapshot, request.frozen_range, geometry_event(event))
        if geometry is None or row.geometry_key != geometry:
            continue
        matched.append(row)
    return tuple(matched)


def _prepare_publication(models: ForecastModelSet, request: ForecastRequest,
                         regime: RegimeAssessment) -> dict[str, object]:
    """Run every join check and the full numeric inference/evidence pass.

    Nothing here depends on the publication time: the injected clock is read
    only afterwards at the single publication boundary (变更申请-004 §4).
    """
    if not isinstance(models, ForecastModelSet):
        raise TypeError("models_must_be_ForecastModelSet")
    if not isinstance(request, ForecastRequest):
        raise TypeError("request_must_be_ForecastRequest")
    if not isinstance(regime, RegimeAssessment):
        raise TypeError("regime_must_be_RegimeAssessment")
    features = request.features
    if regime.snapshot_id != features.snapshot_id:
        raise ForecastError("regime_snapshot_mismatch")
    if regime.reference_id != models.regime_reference_id:
        raise ForecastError("regime_reference_mismatch")
    minimum_leaf_blocks = int(models.fit_config.get("minimum_leaf_blocks", 10))
    components = apply_model_set(
        models.models, models.feature_keys, minimum_leaf_blocks, request, regime.base)
    reasons = _unpublishable_reasons(components, request)
    if reasons:
        raise UnpublishableForecastError(request.request_id, reasons)
    evidence = _matching_evidence(models, request, regime)
    return {"components": dict(components), "evidence": evidence}


def _unpublishable_reasons(components: dict[str, object],
                           request: ForecastRequest) -> tuple[str, ...]:
    """Why no legal record can be constructed; empty when publishable."""
    missing = list(components.get("missing", []))
    reasons: list[str] = []
    if any(item.startswith("features_missing:") for item in missing):
        reasons.append("features_missing")
    if any(item in ("task_missing:DIRECTION", "task_missing:PATH",
                    "task_missing:QUANTILE") for item in missing):
        reasons.append("horizon_unsupported_by_model")
    stop = components.get("stop", {})
    if request.frozen_range is not None and (
            not isinstance(stop, Mapping) or "LONG" not in stop or "SHORT" not in stop):
        reasons.append("stop_touch_missing_with_frozen_range")
    return tuple(reasons)


def _finish_publication(prepared: dict[str, object], models: ForecastModelSet,
                        request: ForecastRequest, regime: RegimeAssessment,
                        published_at_nano: int) -> MarketForecast:
    """Construct the official record exactly once at the real publication time."""
    if isinstance(published_at_nano, bool) or not isinstance(published_at_nano, int):
        raise TypeError("published_at_nano_must_be_int")
    features = request.features
    if published_at_nano < features.decision_nano:
        raise ForecastError("published_before_decision")
    late = published_at_nano >= request.window_start_nano
    if not late:
        if features.decision_nano <= models.training_end_nano:
            raise ForecastError("decision_within_training_window")
        if published_at_nano <= models.calibration_end_nano:
            raise ForecastError("prediction_not_after_calibration")

    components = dict(prepared["components"])  # type: ignore[arg-type]
    missing = list(components.get("missing", []))
    expired = published_at_nano >= models.valid_until_nano
    if expired:
        missing.append("model_expired")
    if components.get("out_of_distribution"):
        missing.append("out_of_distribution")
    components["missing"] = missing

    forecast = assemble_forecast(
        components, request, regime, models.model_set_id, published_at_nano,
        models.regime_reference_id, prepared["evidence"],  # type: ignore[arg-type]
        force_late=late)
    if late:
        return forecast

    if models.valid_until_nano > published_at_nano \
            and forecast.expires_at_nano > models.valid_until_nano:
        forecast = replace(forecast, expires_at_nano=models.valid_until_nano)
    if expired:
        forecast = replace(forecast, quality=DataQuality(
            status="STALE",
            reasons=tuple(sorted(set(forecast.quality.reasons) | {"model_expired"})),
            temporal_basis=forecast.quality.temporal_basis,
            input_hash=forecast.quality.input_hash))
    return forecast


def predict_market(models: ForecastModelSet, request: ForecastRequest,
                   regime: RegimeAssessment,
                   published_at_nano: int) -> MarketForecast:
    """Deterministic research publication at an explicit research clock.

    Same numeric rules and hard checks as the live entry
    (``decision <= published``, ``decision < window_start < window_end``;
    ``published >= window_start`` → INVALID/FORECAST_LATE with expiry pinned
    to the original ``window_end``); the explicit ``published_at_nano`` makes
    replays byte-reproducible and must not be used for SHADOW_ACCEPTED runs.
    """
    prepared = _prepare_publication(models, request, regime)
    return _finish_publication(prepared, models, request, regime, published_at_nano)


def publish_market_forecast(models: ForecastModelSet, request: ForecastRequest,
                            regime: RegimeAssessment, *,
                            clock_nano: Callable[[], int]) -> MarketForecast:
    """Live publication entry: read the injected clock at the publication boundary.

    Model inference, evidence matching and the join checks run first; the
    actual publication time is taken only once after they complete, and the
    final id/quality/expiry are constructed exactly once at that time — never
    by building an on-time record first and swapping the time afterwards
    (变更申请-004 §4). A clock that runs backwards below the decision time
    cannot produce a legal record: CLOCK_UNVERIFIED, no clamping, and the
    caller records a missing publication for the original request.
    """
    if not callable(clock_nano):
        raise TypeError("clock_nano_must_be_callable")
    prepared = _prepare_publication(models, request, regime)
    published_at_nano = clock_nano()
    if isinstance(published_at_nano, bool) or not isinstance(published_at_nano, int):
        raise TypeError("clock_nano_must_return_int")
    if published_at_nano < request.features.decision_nano:
        raise ForecastError("CLOCK_UNVERIFIED")
    return _finish_publication(prepared, models, request, regime, published_at_nano)


def _final_cdf(forecast: MarketForecast, event: str) -> float | None:
    entries = [entry for entry in forecast.time_cdf if entry.event == event]
    if not entries:
        return None
    return max(entries, key=lambda entry: entry.step).probability


def check_forecast_consistency(
        forecasts: tuple[MarketForecast, ...]) -> tuple[str, ...]:
    """Structural consistency checks (§5.3); violations as fixed strings.

    Checks: per-anchor/barrier CDF monotonicity over time; same-direction
    farther-barrier touch probability not above the nearer barrier; the
    RETURN_FIRST/BREAK_FIRST competing cumulative sum bounded by 1; identity
    of COMMON components across the three barriers of one snapshot/horizon;
    return/point/ATR quantile unit reconciliation. Terminal UP probability is
    deliberately not required to be monotonic across horizons.
    """
    if not isinstance(forecasts, tuple) or any(
            not isinstance(f, MarketForecast) for f in forecasts):
        raise TypeError("forecasts_must_be_tuple_of_MarketForecast")
    violations: list[str] = []

    for forecast in forecasts:
        tag = f"{forecast.snapshot_id}|{forecast.horizon.unit}{forecast.horizon.value}|{forecast.barrier_key}"
        by_event: dict[str, list[float]] = {}
        for entry in forecast.time_cdf:
            by_event.setdefault(entry.event, []).append(entry.probability)
        for event, probabilities in sorted(by_event.items()):
            if any(probabilities[i] > probabilities[i + 1] + _MONOTONIC_TOLERANCE
                   for i in range(len(probabilities) - 1)):
                violations.append(f"cdf_not_monotonic:{event}:{tag}")
        return_steps: dict[int, tuple[float, float]] = {}
        break_steps: dict[int, tuple[float, float]] = {}
        for entry in forecast.time_cdf:
            if entry.event == "RETURN_FIRST":
                return_steps[entry.step] = (entry.probability, entry.unknown_probability)
            if entry.event == "BREAK_FIRST":
                break_steps[entry.step] = (entry.probability, entry.unknown_probability)
        for step in sorted(set(return_steps) & set(break_steps)):
            total = sum(return_steps[step]) + sum(break_steps[step])
            if total > 1.0 + _MONOTONIC_TOLERANCE:
                violations.append(f"competing_cumulative_exceeds_one:{tag}:step{step}")
        reference_price = forecast.reference_price
        atr_points = forecast.atr_points
        for key in ("Q05", "Q25", "Q50", "Q75", "Q95"):
            point = forecast.point_quantiles[key]  # displacement relative to C
            expected_return = float(point / reference_price)
            if abs(expected_return - forecast.return_quantiles[key]) > _UNIT_TOLERANCE:
                violations.append(f"quantile_unit_mismatch:return:{key}:{tag}")
            expected_atr = float(point / atr_points)
            if abs(expected_atr - forecast.atr_quantiles[key]) > _UNIT_TOLERANCE:
                violations.append(f"quantile_unit_mismatch:atr:{key}:{tag}")

    by_anchor: dict[tuple[str, str], dict[str, MarketForecast]] = {}
    for forecast in forecasts:
        key = (forecast.snapshot_id, f"{forecast.horizon.unit}{forecast.horizon.value}")
        by_anchor.setdefault(key, {})[forecast.barrier_key] = forecast
    for key, members in sorted(by_anchor.items()):
        tag = f"{key[0]}|{key[1]}"
        for event in ("UP_TOUCH", "DOWN_TOUCH"):
            values = {barrier: _final_cdf(forecast, event)
                      for barrier, forecast in members.items()}
            if "ATR_2" in values and "ATR_1" in values and values["ATR_2"] is not None \
                    and values["ATR_1"] is not None \
                    and values["ATR_2"] > values["ATR_1"] + _MONOTONIC_TOLERANCE:
                violations.append(f"farther_barrier_touch_exceeds_nearer:{event}:ATR_2>{tag}")
            if "ATR_1" in values and "ATR_0.5" in values and values["ATR_1"] is not None \
                    and values["ATR_0.5"] is not None \
                    and values["ATR_1"] > values["ATR_0.5"] + _MONOTONIC_TOLERANCE:
                violations.append(f"farther_barrier_touch_exceeds_nearer:{event}:ATR_1>{tag}")
        if len(members) < 2:
            continue
        reference_barrier = next(iter(sorted(members)))
        reference = members[reference_barrier]
        for barrier, forecast in sorted(members.items()):
            if barrier == reference_barrier:
                continue
            for field in ("direction_probabilities", "return_quantiles", "point_quantiles",
                          "atr_quantiles", "persistence_probabilities",
                          "stop_touch_probabilities"):
                if getattr(reference, field) != getattr(forecast, field):
                    violations.append(f"common_component_diverges:{field}:{tag}")
            if reference.return_before_break_probability != forecast.return_before_break_probability:
                violations.append(f"common_component_diverges:return_before_break:{tag}")
            reference_rb = tuple(e for e in reference.time_cdf
                                 if e.event in ("RETURN_FIRST", "BREAK_FIRST"))
            forecast_rb = tuple(e for e in forecast.time_cdf
                                if e.event in ("RETURN_FIRST", "BREAK_FIRST"))
            if reference_rb != forecast_rb:
                violations.append(f"common_component_diverges:time_cdf_competing:{tag}")

    return tuple(sorted(set(violations)))
