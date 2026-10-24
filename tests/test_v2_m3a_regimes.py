"""M3A regime tests: matrix, overlapping flags, 50% counterexample (03-M3A §4.3, §8.2).

Synthetic SCENARIO fixtures only; the assessment is a current-state function
of the decision-time prefix, never of future data.
"""
from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest

from ak_trading.v2.contracts import (
    DataQuality,
    FactorValue,
    FeatureSnapshot,
    MacroContext,
    MacroItem,
)
from ak_trading.v2.probability.regimes import (
    RegimeReference,
    assess_regime,
    baseline_factor_key,
    fit_regime_reference,
    freeze_range,
    training_volatility_percentile,
    vol001_factor_key,
    volatility_sample_key,
)

NANO = 1_000_000_000
HASH = hashlib.sha256(b"m3a-regimes").hexdigest()
PRODUCT = "SHFE.RB"
BASE = 1_770_000_000 * NANO
DURATION = 60
DAY = date(2026, 1, 12)
TRAINING_END = BASE + 200 * 24 * 3600 * NANO


def quality() -> DataQuality:
    return DataQuality(status="READY", reasons=(), temporal_basis="observed", input_hash=HASH)


def snapshot(decision_nano: int, *, reference: float = 100.0, atr: float = 2.0,
             er: float | None = None, prior_low: float | None = 98.0,
             prior_high: float | None = 102.0, ma20: float | None = 101.0,
             ma60: float | None = 99.0, vol001: float | None = 0.05,
             volume_ratio: float | None = 1.2, session_kind: str = "DAY",
             trading_day: date = DAY, snapshot_id: str | None = None,
             status: str = "READY", opening_gap: str | float | None = "0.0",
             opening_gap_quality: str = "READY",
             opening_gap_available: int | None = None,
             duplicate_gap: bool = False) -> FeatureSnapshot:
    values = []
    if opening_gap is not None:
        gap_value = Decimal(str(opening_gap))
        gap_quality = DataQuality(status=opening_gap_quality, reasons=(),
                                  temporal_basis="observed", input_hash=HASH)
        values.append(FactorValue(
            baseline_factor_key("opening_gap_points", DURATION), gap_value,
            opening_gap_available if opening_gap_available is not None else decision_nano,
            gap_quality))
        if duplicate_gap:
            values.append(FactorValue(
                baseline_factor_key("opening_gap_points", DURATION),
                gap_value + Decimal("1"), decision_nano, quality()))
    if er is not None:
        values.append(FactorValue(baseline_factor_key("er20", DURATION),
                                  Decimal(str(er)), decision_nano, quality()))
    if prior_low is not None:
        values.append(FactorValue(baseline_factor_key("prior_low20", DURATION),
                                  Decimal(str(prior_low)), decision_nano, quality()))
    if prior_high is not None:
        values.append(FactorValue(baseline_factor_key("prior_high20", DURATION),
                                  Decimal(str(prior_high)), decision_nano, quality()))
    if ma20 is not None:
        values.append(FactorValue(baseline_factor_key("ma20", DURATION),
                                  Decimal(str(ma20)), decision_nano, quality()))
    if ma60 is not None:
        values.append(FactorValue(baseline_factor_key("ma60", DURATION),
                                  Decimal(str(ma60)), decision_nano, quality()))
    if vol001 is not None:
        values.append(FactorValue(vol001_factor_key(DURATION), vol001,
                                  decision_nano, quality()))
    if volume_ratio is not None:
        values.append(FactorValue(baseline_factor_key("volume_ratio20", DURATION),
                                  Decimal(str(volume_ratio)), decision_nano, quality()))
    return FeatureSnapshot(
        snapshot_id=snapshot_id or f"snap-{decision_nano // NANO}",
        contract_id="SHFE.RB2605", raw_symbol="RB2605", product=PRODUCT,
        trading_day=trading_day, session_id="sess-1", session_kind=session_kind,
        decision_nano=decision_nano, available_at_nano=decision_nano,
        duration_seconds=DURATION, source_bar_ids=(),
        reference_price=Decimal(str(reference)), atr_points=Decimal(str(atr)),
        values=tuple(values), daily_background_id=None,
        quality=DataQuality(status=status, reasons=(), temporal_basis="observed",
                            input_hash=HASH))


def reference_with(samples: tuple[float, ...]) -> RegimeReference:
    features = tuple(
        snapshot(BASE + index * NANO, vol001=value, er=None, prior_low=None,
                 prior_high=None, ma20=None, ma60=None, volume_ratio=None,
                 snapshot_id=f"ref-{index}")
        for index, value in enumerate(samples))
    return fit_regime_reference(features, TRAINING_END, TRAINING_END)


def macro(mode: str = "PRICE_ONLY", tags: tuple[str, ...] = ()) -> MacroContext:
    return MacroContext(
        context_id="macro-1", decision_nano=BASE, items=(), policy_version="macro-risk-v2.1.0",
        risk_tags=tags, mode=mode, quality=quality())


def assess(features: FeatureSnapshot, reference: RegimeReference, *,
           frozen=None, macro_context=None, cost: float = 0.5, previous=None,
           background=None):
    return assess_regime(features, macro_context or macro(), background, frozen,
                         Decimal(str(cost)), reference, previous)


@pytest.fixture(scope="module")
def flat_reference() -> RegimeReference:
    return reference_with((0.01, 0.02, 0.03, 0.04, 0.05))


# ---------------------------------------------------------------------------
# RegimeReference fitting and volatility percentile
# ---------------------------------------------------------------------------

def test_fit_regime_reference_groups_samples_by_key():
    features = (
        snapshot(BASE, vol001=0.03),
        snapshot(BASE + NANO, vol001=0.01),
        snapshot(BASE + 2 * NANO, vol001=0.02),
        snapshot(BASE + 3 * NANO, vol001=0.02, session_kind="NIGHT"),
    )
    reference = fit_regime_reference(features, TRAINING_END, TRAINING_END - NANO)
    key_day = volatility_sample_key(PRODUCT, "DAY", DURATION)
    key_night = volatility_sample_key(PRODUCT, "NIGHT", DURATION)
    assert reference.volatility_samples[key_day] == (0.01, 0.02, 0.03)
    assert reference.volatility_samples[key_night] == (0.02,)
    assert reference.available_at_nano <= reference.training_end_nano
    assert reference.reference_id.startswith("regime_reference:")
    assert reference.source_hashes["snapshots"]


def test_fit_regime_reference_rejects_future_features():
    features = (snapshot(TRAINING_END + NANO, vol001=0.01),)
    with pytest.raises(ValueError, match="reference_rejects_features_after_training_end"):
        fit_regime_reference(features, TRAINING_END, TRAINING_END)


def test_training_volatility_percentile_hand_computed():
    reference = reference_with((0.01, 0.02, 0.03, 0.04))
    current = snapshot(BASE, vol001=0.03)
    # 3 of 4 training values are <= 0.03 → 0.75
    assert abs(training_volatility_percentile(reference, current) - 0.75) < 1e-15
    below_all = snapshot(BASE, vol001=0.001)
    assert training_volatility_percentile(reference, below_all) == 0.0
    above_all = snapshot(BASE, vol001=9.0)
    assert training_volatility_percentile(reference, above_all) == 1.0


def test_training_volatility_percentile_no_cross_product_backfill():
    reference = reference_with((0.01, 0.02, 0.03))  # SHFE.RB|DAY|60 bucket only
    other_product = FeatureSnapshot(
        snapshot_id="snap-other", contract_id="SHFE.CU2606", raw_symbol="CU2606",
        product="SHFE.CU", trading_day=DAY, session_id="sess-1", session_kind="DAY",
        decision_nano=BASE, available_at_nano=BASE, duration_seconds=DURATION,
        source_bar_ids=(), reference_price=Decimal("100"), atr_points=Decimal("2"),
        values=(FactorValue(vol001_factor_key(DURATION), 0.02, BASE, quality()),),
        daily_background_id=None, quality=quality())
    assert training_volatility_percentile(reference, other_product) is None
    missing_factor = snapshot(BASE, vol001=None)
    assert training_volatility_percentile(reference, missing_factor) is None


def test_vol001_key_registered_parameter_versions():
    assert vol001_factor_key(60) == "VOL001:v2.1.0:d60:m1-n60"
    assert vol001_factor_key(300) == "VOL001:v2.1.0:d300:m5-n60"
    assert vol001_factor_key(86400) == "VOL001:v2.1.0:d86400:d1-n20"


def test_regime_reference_rejects_unsorted_or_non_finite_samples():
    with pytest.raises(ValueError):
        RegimeReference("r", TRAINING_END, TRAINING_END,
                        {f"{PRODUCT}|DAY|60": (0.3, 0.1)}, {})
    with pytest.raises(ValueError):
        RegimeReference("r", TRAINING_END, TRAINING_END,
                        {f"{PRODUCT}|DAY|60": (0.1, float("nan"))}, {})
    with pytest.raises(ValueError):
        RegimeReference("r", TRAINING_END, TRAINING_END + 1,
                        {f"{PRODUCT}|DAY|60": (0.1,)}, {})


# ---------------------------------------------------------------------------
# freeze_range
# ---------------------------------------------------------------------------

def test_freeze_range_boundaries_hand_computed():
    features = snapshot(BASE, prior_low=98.0, prior_high=102.0, atr=2.0)
    frozen = freeze_range(features)
    assert frozen is not None
    assert frozen.lower == Decimal("98") and frozen.upper == Decimal("102")
    assert frozen.center == Decimal("100")
    assert frozen.stop_lower == Decimal("96") and frozen.stop_upper == Decimal("104")
    assert frozen.frozen_at_nano == BASE
    assert frozen.source_snapshot_id == features.snapshot_id


def test_freeze_range_none_when_ingredients_missing_or_degenerate():
    assert freeze_range(snapshot(BASE, prior_low=None)) is None
    assert freeze_range(snapshot(BASE, prior_high=None)) is None
    assert freeze_range(snapshot(BASE, prior_low=103.0, prior_high=101.0)) is None
    assert freeze_range(snapshot(BASE, atr=0.0)) is None


def test_freeze_range_uses_prior_20_excluding_current_by_factor_definition():
    """The t-time boundaries come from the prior-20 factors; they are not
    recomputed from the current bar (§4.3)."""
    features = snapshot(BASE, reference=150.0, prior_low=98.0, prior_high=102.0, atr=2.0)
    frozen = freeze_range(features)
    assert frozen is not None and frozen.upper == Decimal("102")  # current 150 ignored


# ---------------------------------------------------------------------------
# structural base matrix
# ---------------------------------------------------------------------------

def test_matrix_up_and_down_trend(flat_reference):
    up = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0), flat_reference)
    assert up.base == "UP_TREND"
    down = assess(snapshot(BASE, er=0.6, ma20=99.0, ma60=101.0), flat_reference)
    assert down.base == "DOWN_TREND"
    unclear = assess(snapshot(BASE, er=0.6, ma20=100.0, ma60=100.0), flat_reference)
    assert unclear.base == "UNKNOWN"


def test_matrix_middle_er_is_unknown_with_reason(flat_reference):
    assessment = assess(snapshot(BASE, er=0.4), flat_reference, frozen=freeze_range(
        snapshot(BASE, er=0.4)))
    assert assessment.base == "UNKNOWN"
    assert "middle_er20" in assessment.reasons


def test_matrix_ordered_range_conditions(flat_reference):
    frozen = freeze_range(snapshot(BASE, prior_low=96.0, prior_high=104.0, atr=2.0))
    assert frozen is not None
    assessment = assess(snapshot(BASE, er=0.2, reference=100.0, vol001=0.03),
                        flat_reference, frozen=frozen)
    assert assessment.base == "ORDERED_RANGE"
    assert assessment.frozen_range is frozen  # boundaries never re-drawn


def test_matrix_compression_when_width_does_not_cover_cost(flat_reference):
    """§8.2: 窄区间往返不足费用 → COMPRESSION (waiting), not a range regime."""
    frozen = freeze_range(snapshot(BASE, prior_low=99.0, prior_high=101.0, atr=2.0))
    assert frozen is not None
    assessment = assess(snapshot(BASE, er=0.2), flat_reference, frozen=frozen, cost=1.5)
    assert assessment.base == "COMPRESSION"
    assert "width_does_not_cover_cost" in assessment.reasons


def test_matrix_compression_when_width_below_two_atr(flat_reference):
    frozen = freeze_range(snapshot(BASE, prior_low=99.5, prior_high=100.5, atr=2.0))
    assert frozen is not None
    assessment = assess(snapshot(BASE, er=0.2), flat_reference, frozen=frozen, cost=0.01)
    assert assessment.base == "COMPRESSION"
    assert "width_below_2_atr" in assessment.reasons


def test_matrix_wide_range_by_width_ratio(flat_reference):
    frozen = freeze_range(snapshot(BASE, prior_low=80.0, prior_high=120.0, atr=2.0))
    assert frozen is not None
    assessment = assess(snapshot(BASE, er=0.2, reference=100.0), flat_reference, frozen=frozen)
    assert assessment.base == "WIDE_RANGE"


def test_matrix_wide_range_by_volatility_percentile():
    high_vol = reference_with((0.01, 0.02, 0.03, 0.04, 0.05))
    frozen = freeze_range(snapshot(BASE, prior_low=96.0, prior_high=104.0, atr=2.0))
    current = snapshot(BASE, er=0.2, reference=100.0, vol001=0.05)
    assessment = assess(current, high_vol, frozen=frozen)
    assert training_volatility_percentile(high_vol, current) == 1.0
    assert assessment.base == "WIDE_RANGE"


def test_matrix_unknown_without_training_volatility_evidence(flat_reference):
    frozen = freeze_range(snapshot(BASE, prior_low=96.0, prior_high=104.0, atr=2.0))
    current = snapshot(BASE, er=0.2, reference=100.0, vol001=None)
    assessment = assess(current, flat_reference, frozen=frozen)
    assert assessment.base == "UNKNOWN"
    assert "no_training_volatility_evidence" in assessment.reasons


def test_matrix_unknown_when_decision_price_outside_range(flat_reference):
    frozen = freeze_range(snapshot(BASE, prior_low=96.0, prior_high=104.0, atr=2.0))
    assessment = assess(snapshot(BASE, er=0.2, reference=110.0), flat_reference, frozen=frozen)
    assert assessment.base == "UNKNOWN"
    assert "BREAKOUT" in assessment.flags


def test_matrix_quality_failure_forces_unknown(flat_reference):
    assessment = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0, status="MISSING"),
                        flat_reference)
    assert assessment.base == "UNKNOWN"
    assert "feature_quality_failed" in assessment.reasons


def test_matrix_missing_structural_factors_unknown(flat_reference):
    assessment = assess(snapshot(BASE, er=None), flat_reference)
    assert assessment.base == "UNKNOWN"
    assert "missing_structural_factors" in assessment.reasons


# ---------------------------------------------------------------------------
# flags and 50% counterexample
# ---------------------------------------------------------------------------

def test_flags_event_shock_only_with_valid_macro_tags(flat_reference):
    with_shock = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0), flat_reference,
                        macro_context=macro("WITH_MACRO", ("GLOBAL_EVENT",)))
    assert "EVENT_SHOCK" in with_shock.flags
    price_only = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0), flat_reference,
                        macro_context=macro("PRICE_ONLY", ("GLOBAL_EVENT",)))
    assert "EVENT_SHOCK" not in price_only.flags
    blocked = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0), flat_reference,
                     macro_context=macro("BLOCKED"))
    assert "EVENT_SHOCK" not in blocked.flags
    assert blocked.base == price_only.base  # macro never recomputes base


def test_flags_price_only_and_with_macro_share_base(flat_reference):
    frozen = freeze_range(snapshot(BASE, prior_low=96.0, prior_high=104.0, atr=2.0))
    features = snapshot(BASE, er=0.2, reference=100.0, vol001=0.03)
    a = assess(features, flat_reference, frozen=frozen, macro_context=macro("PRICE_ONLY"))
    b = assess(features, flat_reference, frozen=frozen,
               macro_context=macro("WITH_MACRO", ("GLOBAL_EVENT",)))
    assert a.base == b.base == "ORDERED_RANGE"
    assert a.flags != b.flags


def test_flags_illiquid_without_tradability_evidence(flat_reference):
    missing = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0, volume_ratio=None),
                     flat_reference)
    assert "ILLIQUID" in missing.flags
    zero = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0, volume_ratio=0.0),
                  flat_reference)
    assert "ILLIQUID" in zero.flags
    healthy = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0, volume_ratio=1.5),
                     flat_reference)
    assert "ILLIQUID" not in healthy.flags


def test_flags_breakout_when_decision_outside_frozen_range(flat_reference):
    frozen = freeze_range(snapshot(BASE, prior_low=96.0, prior_high=104.0, atr=2.0))
    assessment = assess(snapshot(BASE, er=0.2, reference=110.0), flat_reference, frozen=frozen)
    assert "BREAKOUT" in assessment.flags


def test_flags_pullback_with_clear_background_trend(flat_reference):
    """Background trend clear and C within 0.5 ATR of MA20 → PULLBACK flag."""
    features = snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0, reference=100.8)
    with_trend = assess(features, flat_reference, background=_background(0.6, 0.2))
    assert "PULLBACK" in with_trend.flags
    without_trend = assess(features, flat_reference, background=_background(0.5, 0.5))
    assert "PULLBACK" not in without_trend.flags
    far_from_ma = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0, reference=110.0),
                         flat_reference, background=_background(0.6, 0.2))
    assert "PULLBACK" not in far_from_ma.flags


def test_flags_survive_base_classification(flat_reference):
    """Risk flags are never erased by the later base classification (§4.3)."""
    frozen = freeze_range(snapshot(BASE, prior_low=96.0, prior_high=104.0, atr=2.0))
    assessment = assess(
        snapshot(BASE, er=0.2, reference=100.0, volume_ratio=None), flat_reference,
        frozen=frozen, macro_context=macro("WITH_MACRO", ("ENERGY_EVENT",)))
    assert "ILLIQUID" in assessment.flags and "EVENT_SHOCK" in assessment.flags
    assert assessment.base in ("ORDERED_RANGE", "WIDE_RANGE", "UNKNOWN", "COMPRESSION")


def test_fifty_percent_direction_is_not_a_range_derivation(flat_reference):
    """A 50/50 direction background must not turn a structural regime into a
    range (横盘 cannot be derived from direction probabilities, §1/§4.3)."""
    frozen = freeze_range(snapshot(BASE, prior_low=96.0, prior_high=104.0, atr=2.0))
    features = snapshot(BASE, er=0.2, reference=100.0, vol001=0.03)
    neutral_background = _background(0.5, 0.5)
    assessment = assess(features, flat_reference, frozen=frozen,
                        background=neutral_background)
    assert assessment.base == "ORDERED_RANGE"
    assert assessment.cross_timeframe == "UNKNOWN"  # no clear trend either way


def test_cross_timeframe_aligned_and_countertrend(flat_reference):
    frozen = None
    up_features = snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0)
    aligned = assess(up_features, flat_reference, frozen=frozen,
                     background=_background(0.6, 0.2))
    assert aligned.base == "UP_TREND" and aligned.cross_timeframe == "ALIGNED"
    counter = assess(up_features, flat_reference, frozen=frozen,
                     background=_background(0.2, 0.6))
    assert counter.cross_timeframe == "COUNTERTREND"  # not a data error
    no_trend = assess(up_features, flat_reference, frozen=frozen, background=None)
    assert no_trend.cross_timeframe == "UNKNOWN"


def test_confirmed_observations_chain_and_reset(flat_reference):
    features1 = snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0)
    first = assess(features1, flat_reference)
    assert first.confirmed_observations == 1
    features2 = snapshot(BASE + NANO, er=0.6, ma20=101.0, ma60=99.0)
    second = assess(features2, flat_reference, previous=first)
    assert second.confirmed_observations == 2
    unknown_features = snapshot(BASE + 2 * NANO, er=0.4)
    frozen = freeze_range(unknown_features)
    third = assess(unknown_features, flat_reference, frozen=frozen, previous=second)
    assert third.base == "UNKNOWN" and third.confirmed_observations == 0
    resumed = assess(features1, flat_reference, previous=third)
    assert resumed.confirmed_observations == 1


def test_confirmed_observations_stale_previous_resets(flat_reference):
    features1 = snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0)
    first = assess(features1, flat_reference)
    far_later = snapshot(BASE + 2 * 3600 * NANO, er=0.6, ma20=101.0, ma60=99.0)
    later = assess(far_later, flat_reference, previous=first)
    assert later.confirmed_observations == 1  # expiry interrupts confirmation


def test_assessment_identity_is_deterministic(flat_reference):
    features = snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0)
    first = assess(features, flat_reference)
    second = assess(features, flat_reference)
    assert first == second


def _background(p_up: float, p_down: float) -> object:
    from ak_trading.v2.contracts import Horizon, MarketForecast

    class _Dummy:  # minimal duck-typed background is rejected; build a real one
        pass

    from ak_trading.v2.contracts import DataQuality as DQ
    from ak_trading.v2.contracts import TimeProbability
    # displacement-point convention (M1 frozen): point = C_end − C
    quantiles = {"Q05": -0.02, "Q25": -0.01, "Q50": 0.0, "Q75": 0.01, "Q95": 0.02}
    points = {"Q05": Decimal("-2"), "Q25": Decimal("-1"), "Q50": Decimal("0"),
              "Q75": Decimal("1"), "Q95": Decimal("2")}
    atrs = {"Q05": -1.0, "Q25": -0.5, "Q50": 0.0, "Q75": 0.5, "Q95": 1.0}
    return MarketForecast(
        forecast_id="bg-1", request_id="bg-req", snapshot_id="bg-snap",
        contract_id="SHFE.RB2605", product=PRODUCT, trading_day=DAY,
        session_id="sess-1", session_kind="DAILY", duration_seconds=86400,
        assessment_id="bg-assess", base_regime="UP_TREND",
        regime_reference_id="regime_reference:x", decision_nano=BASE,
        published_at_nano=BASE, expires_at_nano=BASE + NANO,
        horizon=Horizon("TRADING_DAY", 1),
        window_start_nano=BASE + NANO, window_end_nano=BASE + 2 * NANO,
        barrier_key="ATR_1", reference_price=Decimal("100"), atr_points=Decimal("2"),
        upper_distance=Decimal("2"), lower_distance=Decimal("2"), frozen_range=None,
        label_version="path-label-v2.1.0", model_id="m",
        direction_probabilities={"UP": p_up, "DOWN": p_down,
                                 "NEUTRAL": 1.0 - p_up - p_down},
        return_quantiles=quantiles, point_quantiles=points, atr_quantiles=atrs,
        path_probabilities={"UP_FIRST": 0.25, "DOWN_FIRST": 0.25,
                            "NEITHER": 0.25, "AMBIGUOUS": 0.25},
        time_cdf=(TimeProbability("UP_TOUCH", 1, 0.1, 0.0),),
        persistence_probabilities={}, stop_touch_probabilities={},
        return_before_break_probability=None, evidence=(), out_of_distribution=False,
        quality=DQ(status="READY", reasons=(), temporal_basis="observed", input_hash=HASH))


# ---------------------------------------------------------------------------
# 变更申请-006 (006 #15): opening-gap EVENT_SHOCK consumption
# ---------------------------------------------------------------------------

def test_gap_at_exactly_two_atr_flags_event_shock(flat_reference):
    """|gap| == 2×ATR hits the threshold (equality, Decimal comparison)."""
    assessment = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0,
                                 opening_gap="4.0"), flat_reference)
    assert assessment.base == "UP_TREND"  # flag never erases the base
    assert "EVENT_SHOCK" in assessment.flags
    assert "opening_gap_at_least_2_atr" in assessment.reasons


def test_gap_below_threshold_no_flag(flat_reference):
    assessment = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0,
                                 opening_gap="3.9"), flat_reference)
    assert assessment.base == "UP_TREND"
    assert "EVENT_SHOCK" not in assessment.flags


def test_negative_gap_uses_absolute_value(flat_reference):
    assessment = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0,
                                 opening_gap="-5.0"), flat_reference)
    assert "EVENT_SHOCK" in assessment.flags


def test_gap_missing_key_forces_unknown_with_reason(flat_reference):
    assessment = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0,
                                 opening_gap=None), flat_reference)
    assert assessment.base == "UNKNOWN"
    assert "opening_gap_points_missing" in assessment.reasons


def test_gap_missing_keeps_macro_flag_and_does_not_pass_through(flat_reference):
    """缺键不得判false后按普通行情放行；已知宏观风险flag保留。"""
    assessment = assess(
        snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0, opening_gap=None),
        flat_reference, macro_context=macro("WITH_MACRO", ("GLOBAL_EVENT",)))
    assert assessment.base == "UNKNOWN"
    assert "opening_gap_points_missing" in assessment.reasons
    assert "EVENT_SHOCK" in assessment.flags


def test_gap_conflicting_sources_force_unknown(flat_reference):
    assessment = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0,
                                 duplicate_gap=True), flat_reference)
    assert assessment.base == "UNKNOWN"
    assert "opening_gap_points_conflicting_sources" in assessment.reasons


def test_gap_not_ready_forces_unknown(flat_reference):
    assessment = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0,
                                 opening_gap="4.0",
                                 opening_gap_quality="MISSING"), flat_reference)
    assert assessment.base == "UNKNOWN"
    assert "opening_gap_points_not_ready" in assessment.reasons


def test_gap_pit_violation_forces_unknown(flat_reference):
    assessment = assess(snapshot(BASE, er=0.6, ma20=101.0, ma60=99.0,
                                 opening_gap="4.0",
                                 opening_gap_available=BASE + NANO),
                        flat_reference)
    assert assessment.base == "UNKNOWN"
    assert "opening_gap_points_not_visible" in assessment.reasons
