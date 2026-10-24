"""M3A calibration tests: buckets, samples, evidence expiry (03-M3A §8.6, 变更申请-002 §2/§6).

Connection checks run before any row is produced; geometry keys come from
M1's ``calibration_geometry_key`` on the connected snapshots (C anchor,
geometry-v2.1.1 hand examples included).
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ak_trading.v2.contracts import (
    ConfidencePolicy,
    DataQuality,
    FactorValue,
    FeatureSnapshot,
    Horizon,
    MarketForecast,
    PathLabel,
    TimeProbability,
)
from ak_trading.v2.schemas import calibration_geometry_key
from ak_trading.v2.probability.calibration import (
    CalibrationError,
    build_calibration_evidence,
)

NANO = 1_000_000_000
HASH = hashlib.sha256(b"m3a-calib").hexdigest()
PRODUCT = "SHFE.RB"
DAY0 = date(2026, 1, 12)
MODEL_ID = "forecast_model_set:abc"
AS_OF = 5_000 * NANO
VALID_UNTIL = 100_000 * NANO

# displacement-point convention (M1 frozen): point = C_end − C; return and
# ATR units derive from each snapshot's own anchor values.
QUANTILE_POINTS = {"Q05": Decimal("-2"), "Q25": Decimal("-1"), "Q50": Decimal("0"),
                   "Q75": Decimal("1"), "Q95": Decimal("2")}


def _quantile_triple_for(snapshot):
    returns = {key: float(value / snapshot.reference_price)
               for key, value in QUANTILE_POINTS.items()}
    atrs = {key: float(value / snapshot.atr_points)
            for key, value in QUANTILE_POINTS.items()}
    return returns, atrs


def quality(status: str = "READY", reasons: tuple[str, ...] = ()) -> DataQuality:
    return DataQuality(status=status, reasons=reasons, temporal_basis="observed",
                       input_hash=HASH)


def make_snapshot(index: int, *, reference: float = 99.0, atr: float = 2.0,
                  tick: str | None = "0.1", contract: str | None = None,
                  session_kind: str = "DAY", duration: int = 60,
                  decision: int | None = None, day: date | None = None) -> FeatureSnapshot:
    decision_nano = decision if decision is not None else 1_000 * NANO + index
    values = () if tick is None else (
        FactorValue("META:price_tick:v2.1.0", Decimal(tick), decision_nano - 1, quality()),)
    return FeatureSnapshot(
        snapshot_id=f"snap-{index}", contract_id=contract or f"SHFE.RB26{index:02d}",
        raw_symbol="RB", product=PRODUCT, trading_day=day or DAY0,
        session_id="sess-1", session_kind=session_kind, decision_nano=decision_nano,
        available_at_nano=decision_nano, duration_seconds=duration, source_bar_ids=(),
        reference_price=Decimal(str(reference)), atr_points=Decimal(str(atr)),
        values=values,
        daily_background_id=None, quality=quality())


def make_prediction(snapshot: FeatureSnapshot, *, request_id: str, published: int,
                    window_start: int = 2_000 * NANO, window_end: int = 3_000 * NANO,
                    horizon: Horizon | None = None, barrier: str = "ATR_1",
                    direction: tuple[float, float, float] = (0.2, 0.3, 0.5),
                    path: tuple[float, float, float, float] = (0.25, 0.25, 0.25, 0.25),
                    persistence: dict[str, float] | None = None,
                    stop: dict[str, float] | None = None,
                    range_id: str | None = None, model_id: str = MODEL_ID,
                    quality_status: str = "READY") -> MarketForecast:
    from ak_trading.v2.contracts import FrozenRange
    frozen = None
    if range_id is not None:
        frozen = FrozenRange(range_id, snapshot.decision_nano, Decimal("98"),
                             Decimal("100"), Decimal("102"), Decimal("96"),
                             Decimal("104"), snapshot.snapshot_id)
    quantile_returns, quantile_atrs = _quantile_triple_for(snapshot)
    return MarketForecast(
        forecast_id=f"fc-{request_id}", request_id=request_id,
        snapshot_id=snapshot.snapshot_id, contract_id=snapshot.contract_id,
        product=snapshot.product, trading_day=snapshot.trading_day,
        session_id=snapshot.session_id, session_kind=snapshot.session_kind,
        duration_seconds=snapshot.duration_seconds, assessment_id="assess-1",
        base_regime="UNKNOWN", regime_reference_id="rr-1",
        decision_nano=snapshot.decision_nano, published_at_nano=published,
        expires_at_nano=min(published + 120 * NANO, window_end),
        horizon=horizon or Horizon("TRADING_MINUTE", 1),
        window_start_nano=window_start, window_end_nano=window_end,
        barrier_key=barrier, reference_price=snapshot.reference_price,
        atr_points=snapshot.atr_points, upper_distance=Decimal("2"),
        lower_distance=Decimal("2"), frozen_range=frozen,
        label_version="path-label-v2.1.0", model_id=model_id,
        direction_probabilities={"UP": direction[0], "DOWN": direction[1],
                                 "NEUTRAL": direction[2]},
        return_quantiles=quantile_returns, point_quantiles=dict(QUANTILE_POINTS),
        atr_quantiles=quantile_atrs,
        path_probabilities={"UP_FIRST": path[0], "DOWN_FIRST": path[1],
                            "NEITHER": path[2], "AMBIGUOUS": path[3]},
        time_cdf=(), persistence_probabilities=persistence or {},
        stop_touch_probabilities=stop or {}, return_before_break_probability=None,
        evidence=(), out_of_distribution=False,
        quality=quality(quality_status))


def make_label(prediction: MarketForecast, *, direction: str = "UP",
               persistence: dict[str, bool | None] | None = None,
               stop: dict[str, bool | None] | None = None,
               status: str = "MATURE", available_at: int | None = None,
               horizon: Horizon | None = None,
               window: tuple[int, int] | None = None) -> PathLabel:
    window_start, window_end = window or (prediction.window_start_nano,
                                           prediction.window_end_nano)
    return PathLabel(
        request_id=prediction.request_id, contract_id=prediction.contract_id,
        label_version="path-label-v2.1.0",
        horizon=horizon or prediction.horizon,
        window_start_nano=window_start, window_end_nano=window_end,
        available_at_nano=available_at if available_at is not None else AS_OF - 1,
        status=status, reasons=(), direction=direction,
        terminal_return=0.01, terminal_points=Decimal("1"), terminal_atr=0.5,
        path_outcome="NEITHER", first_up_step=None, first_down_step=None,
        return_step=None, break_step=None, competing_outcome=None,
        persistence=persistence or {key: None for key in (
            "UP_TREND", "DOWN_TREND", "ORDERED_RANGE", "WIDE_RANGE",
            "COMPRESSION", "UP_BREAKOUT", "DOWN_BREAKOUT")},
        stop_touched=stop or {},
        range_id=prediction.frozen_range.range_id
        if prediction.frozen_range is not None else None)


def ordinary_policy() -> ConfidencePolicy:
    return ConfidencePolicy(
        policy_id="confidence-ordinary-v2.1.0", minimum_persistence=0.70,
        maximum_stop_touch=None, minimum_samples=200, minimum_blocks=30,
        maximum_calibration_error=0.05, maximum_ambiguity=0.05,
        confirmation_observations=3, exit_persistence=0.65, cooldown_minutes=30,
        minimum_conservative_ev_cny=Decimal("0"))


def martingale_policy() -> ConfidencePolicy:
    return ConfidencePolicy(
        policy_id="confidence-martingale-v2.1.0", minimum_persistence=0.85,
        maximum_stop_touch=0.10, minimum_samples=500, minimum_blocks=60,
        maximum_calibration_error=0.03, maximum_ambiguity=0.02,
        confirmation_observations=3, exit_persistence=0.80, cooldown_minutes=30,
        minimum_conservative_ev_cny=Decimal("0"))


def bundle(predictions, labels, features, block_ids):
    return build_calibration_evidence(tuple(predictions), tuple(labels), block_ids,
                                      ordinary_policy(), AS_OF, VALID_UNTIL,
                                      features=tuple(features))


def simple_triple(index: int, *, p: float = 0.8, outcome: bool = True,
                  day: date | None = None, reference: float = 99.0):
    day = day or DAY0
    snapshot = make_snapshot(index, day=day, reference=reference)
    prediction = make_prediction(
        snapshot, request_id=f"req-{index}", published=1_500 * NANO,
        persistence={"COMPRESSION": p})
    label = make_label(prediction, persistence={"COMPRESSION": outcome} | {
        key: None for key in ("UP_TREND", "DOWN_TREND", "ORDERED_RANGE", "WIDE_RANGE",
                              "UP_BREAKOUT", "DOWN_BREAKOUT")})
    return snapshot, prediction, label


# ---------------------------------------------------------------------------
# required features argument and connection validation
# ---------------------------------------------------------------------------

def test_features_argument_is_required():
    snapshot, prediction, label = simple_triple(1)
    with pytest.raises(TypeError):
        build_calibration_evidence((prediction,), (label,), {"req-1": DAY0.isoformat()},
                                   ordinary_policy(), AS_OF, VALID_UNTIL)


def test_all_empty_inputs_return_no_evidence():
    assert build_calibration_evidence((), (), {}, ordinary_policy(), AS_OF,
                                      VALID_UNTIL, features=()) == ()


def test_predictions_with_empty_features_rejected():
    snapshot, prediction, label = simple_triple(1)
    with pytest.raises(ValueError):
        bundle((prediction,), (label,), (), {"req-1": DAY0.isoformat()})


def test_duplicate_snapshot_rejected_even_if_identical():
    snapshot, prediction, label = simple_triple(1)
    with pytest.raises(ValueError, match="duplicate_feature_snapshot_id"):
        bundle((prediction,), (label,), (snapshot, snapshot), {"req-1": DAY0.isoformat()})


def test_missing_and_extra_snapshots_rejected():
    snapshot, prediction, label = simple_triple(1)
    other = make_snapshot(2)
    with pytest.raises(ValueError, match="feature_key_set"):
        bundle((prediction,), (label,), (), {"req-1": DAY0.isoformat()})
    with pytest.raises(ValueError, match="feature_key_set"):
        bundle((prediction,), (label,), (snapshot, other), {"req-1": DAY0.isoformat()})


def test_duplicate_forecast_and_request_ids_rejected():
    snapshot, prediction, label = simple_triple(1)
    prediction2 = make_prediction(snapshot, request_id="req-1", published=1_501 * NANO)
    with pytest.raises(ValueError, match="duplicate_forecast_id"):
        bundle((prediction, prediction2), (label, label), (snapshot,),
               {"req-1": DAY0.isoformat()})


def test_label_and_block_key_sets_must_match_exactly():
    snapshot, prediction, label = simple_triple(1)
    with pytest.raises(ValueError, match="label_key_set"):
        bundle((prediction,), (), (snapshot,), {"req-1": DAY0.isoformat()})
    with pytest.raises(ValueError, match="block_id_key_set"):
        bundle((prediction,), (label,), (snapshot,), {})
    with pytest.raises(ValueError, match="block_id_key_set"):
        bundle((prediction,), (label,), (snapshot,),
               {"req-1": DAY0.isoformat(), "req-2": DAY0.isoformat()})


def test_prediction_snapshot_field_mismatch_rejected():
    snapshot, prediction, label = simple_triple(1)
    tampered = make_snapshot(1, atr=3.0)
    with pytest.raises(ValueError, match="prediction_snapshot_field_mismatch"):
        bundle((prediction,), (label,), (tampered,), {"req-1": DAY0.isoformat()})


def test_label_window_or_horizon_mismatch_rejected():
    snapshot, prediction, label = simple_triple(1)
    wrong_window = make_label(prediction, window=(2_001 * NANO, 3_000 * NANO))
    with pytest.raises(ValueError, match="label_prediction_field_mismatch"):
        bundle((prediction,), (wrong_window,), (snapshot,), {"req-1": DAY0.isoformat()})
    wrong_horizon = make_label(prediction, horizon=Horizon("TRADING_MINUTE", 5))
    with pytest.raises(ValueError, match="label_prediction_field_mismatch"):
        bundle((prediction,), (wrong_horizon,), (snapshot,), {"req-1": DAY0.isoformat()})


def test_published_after_as_of_rejected():
    snapshot = make_snapshot(1)
    late = make_prediction(snapshot, request_id="req-1", published=AS_OF + NANO,
                           window_start=20_000 * NANO, window_end=21_000 * NANO)
    label = make_label(late, window=(20_000 * NANO, 21_000 * NANO))
    with pytest.raises(ValueError, match="prediction_published_after_as_of"):
        bundle((late,), (label,), (snapshot,), {"req-1": DAY0.isoformat()})


def test_valid_until_not_after_inputs_rejected():
    snapshot, prediction, label = simple_triple(1)
    with pytest.raises(ValueError, match="valid_until_not_after"):
        build_calibration_evidence((prediction,), (label,), {"req-1": DAY0.isoformat()},
                                   ordinary_policy(), AS_OF, 1_500 * NANO,
                                   features=(snapshot,))


def test_mixed_model_id_rejected():
    snapshot, prediction, label = simple_triple(1)
    other = make_prediction(snapshot, request_id="req-1", published=1_500 * NANO,
                            model_id="forecast_model_set:zzz")
    with pytest.raises(ValueError, match="mixed_model_id"):
        bundle((prediction, other), (label, label), (snapshot,),
               {"req-1": DAY0.isoformat()})


def test_snapshot_order_does_not_change_connection():
    """Reordered snapshots never misconnect; ids drive the join (002 §2)."""
    snapshots, predictions, labels = [], [], []
    for index in range(3):
        snapshot, prediction, label = simple_triple(index)
        snapshots.append(snapshot)
        predictions.append(prediction)
        labels.append(label)
    blocks = {f"req-{i}": DAY0.isoformat() for i in range(3)}
    forward = bundle(predictions, labels, snapshots, blocks)
    reverse = bundle(predictions, labels, list(reversed(snapshots)), blocks)
    assert forward == reverse


def test_multi_horizon_predictions_share_one_snapshot_legally():
    snapshot, p1, l1 = simple_triple(1)
    p2 = make_prediction(snapshot, request_id="req-1b", published=1_500 * NANO,
                         horizon=Horizon("TRADING_MINUTE", 5))
    l2 = make_label(p2, horizon=Horizon("TRADING_MINUTE", 5),
                    persistence={"COMPRESSION": True} | {
                        key: None for key in ("UP_TREND", "DOWN_TREND", "ORDERED_RANGE",
                                              "WIDE_RANGE", "UP_BREAKOUT", "DOWN_BREAKOUT")})
    rows = bundle((p1, p2), (l1, l2), (snapshot,),
                  {"req-1": DAY0.isoformat(), "req-1b": DAY0.isoformat()})
    assert rows  # legal: one snapshot referenced by two horizons


# ---------------------------------------------------------------------------
# §8.6 hand-computed bucket calibration
# ---------------------------------------------------------------------------

def _bucket_rows(p_value: float, outcomes: list[bool], days: int, policy=None):
    predictions, labels, snapshots = [], [], []
    day = DAY0
    blocks: dict[str, str] = {}
    index = 0
    for day_index in range(days):
        for _ in range(len(outcomes) // days if days else 0):
            snapshot, prediction, label = simple_triple(
                index, p=p_value, outcome=outcomes[index], day=day)
            predictions.append(prediction)
            labels.append(label)
            snapshots.append(snapshot)
            blocks[prediction.request_id] = day.isoformat()
            index += 1
        day += timedelta(days=1)
    rows = build_calibration_evidence(tuple(predictions), tuple(labels), blocks,
                                      policy or ordinary_policy(), AS_OF, VALID_UNTIL,
                                      features=tuple(snapshots))
    return [row for row in rows if row.event == "PERSIST:COMPRESSION"]


def test_bucket_error_zero_but_too_few_blocks_is_insufficient():
    outcomes = [True] * 800 + [False] * 200
    rows = _bucket_rows(0.8, outcomes, days=10)
    row = next(row for row in rows if row.bucket == "[0.80,0.85)")
    assert row.sample_count == 1000
    assert row.effective_blocks == 10
    assert abs(row.calibration_error - 0.0) < 1e-12
    assert row.status == "INSUFFICIENT"


def test_bucket_error_above_limit_is_failed():
    outcomes = [True] * 700 + [False] * 300
    rows = _bucket_rows(0.85, outcomes, days=40)
    row = next(row for row in rows if row.bucket == "[0.85,0.90)")
    assert row.sample_count == 1000 and row.effective_blocks == 40
    assert abs(row.calibration_error - 0.15) < 1e-12
    assert row.status == "FAILED"


def test_bucket_valid_when_samples_blocks_and_error_pass():
    outcomes = [True] * 800 + [False] * 200
    rows = _bucket_rows(0.8, outcomes, days=40)
    row = next(row for row in rows if row.bucket == "[0.80,0.85)")
    assert row.status == "VALID"


def test_bucket_membership_boundaries():
    outcomes = [True] * 800 + [False] * 200
    rows = _bucket_rows(0.85, outcomes, days=40)
    buckets = {row.bucket for row in rows}
    assert "[0.85,0.90)" in buckets
    assert "[0.80,0.85)" not in buckets  # 0.85 only enters the higher bucket


def test_expired_when_as_of_reaches_valid_until():
    snapshot, prediction, label = simple_triple(1)
    rows = build_calibration_evidence((prediction,), (label,),
                                      {"req-1": DAY0.isoformat()}, ordinary_policy(),
                                      VALID_UNTIL, VALID_UNTIL, features=(snapshot,))
    assert rows and all(row.status == "EXPIRED" for row in rows)


def test_ordinary_and_martingale_policies_provide_separate_rows():
    outcomes = [True] * 800 + [False] * 200
    ordinary_rows = _bucket_rows(0.8, outcomes, days=40, policy=ordinary_policy())
    martingale_rows = _bucket_rows(0.8, outcomes, days=40, policy=martingale_policy())
    ordinary_row = next(row for row in ordinary_rows if row.bucket == "[0.80,0.85)")
    martingale_row = next(row for row in martingale_rows if row.bucket == "[0.80,0.85)")
    assert ordinary_row.policy_id != martingale_row.policy_id
    assert ordinary_row.status == "VALID"
    # martingale needs 500/60/0.03 → 1000/40 fails blocks → INSUFFICIENT
    assert martingale_row.status == "INSUFFICIENT"
    assert ordinary_row.evidence_id != martingale_row.evidence_id


def test_stop_touch_uses_low_probability_buckets():
    snapshots, predictions, labels = [], [], []
    blocks = {}
    outcomes = [True] * 50 + [False] * 950  # true stop frequency 0.05
    day = DAY0
    index = 0
    for day_index in range(40):
        for _ in range(25):
            snapshot = make_snapshot(index, day=day, reference=99.0)
            prediction = make_prediction(snapshot, request_id=f"req-{index}",
                                         published=1_500 * NANO,
                                         range_id=f"range-{index % 7}",
                                         stop={"LONG": 0.05, "SHORT": 0.05})
            label = make_label(prediction, stop={"LONG": outcomes[index], "SHORT": False})
            snapshots.append(snapshot)
            predictions.append(prediction)
            labels.append(label)
            blocks[prediction.request_id] = day.isoformat()
            index += 1
        day += timedelta(days=1)
    rows = build_calibration_evidence(tuple(predictions), tuple(labels), blocks,
                                      ordinary_policy(), AS_OF, VALID_UNTIL,
                                      features=tuple(snapshots))
    stop_rows = [row for row in rows if row.event == "STOP_TOUCH:LONG"]
    bucket_row = next(row for row in stop_rows if row.bucket == "[0.05,0.10]")
    assert bucket_row.sample_count == 1000
    assert abs(bucket_row.calibration_error - 0.0) < 1e-12
    # boundary 0.05 falls into [0,0.05), never into [0.05,0.10]
    assert not any(row.bucket == "[0,0.05)" and row.event == "STOP_TOUCH:LONG"
                   for row in rows)


# ---------------------------------------------------------------------------
# geometry via M1 (002 §3 hand examples)
# ---------------------------------------------------------------------------

def _stop_geometry_rows(reference: float):
    snapshot = make_snapshot(1, reference=reference)
    prediction = make_prediction(snapshot, request_id="req-1", published=1_500 * NANO,
                                 range_id="range-geo", stop={"LONG": 0.06, "SHORT": 0.06})
    label = make_label(prediction, stop={"LONG": True, "SHORT": False})
    rows = bundle((prediction,), (label,), (snapshot,), {"req-1": DAY0.isoformat()})
    return snapshot, prediction, [row for row in rows if row.event == "STOP_TOUCH:LONG"]


def test_geometry_hand_example_c99_and_c100():
    snapshot99, prediction99, rows99 = _stop_geometry_rows(99.0)
    assert rows99
    expected = calibration_geometry_key(snapshot99, prediction99.frozen_range, "STOP_TOUCH")
    assert expected == "geometry-v2.1.1|STOP_TOUCH|1-2-2-3-2"
    assert all(row.geometry_key == expected for row in rows99)
    _, _, rows100 = _stop_geometry_rows(100.0)
    assert all(row.geometry_key == "geometry-v2.1.1|STOP_TOUCH|1-3-3-3-2"
               for row in rows100)


def test_geometry_range_free_events_use_anchor_atr():
    snapshot, prediction, label = simple_triple(1)
    rows = bundle((prediction,), (label,), (snapshot,), {"req-1": DAY0.isoformat()})
    direction_rows = [row for row in rows if row.event == "DIRECTION"]
    assert direction_rows
    assert all(row.geometry_key == "geometry-v2.1.1|DIRECTION|ANCHOR_ATR"
               for row in direction_rows)


def test_range_event_row_skipped_when_geometry_unavailable():
    """A range event whose connected snapshot lacks the tick has no geometry
    inputs; the row is refused rather than filled with a placeholder (002 §2)."""
    snapshot = make_snapshot(1, tick=None)
    prediction = make_prediction(snapshot, request_id="req-1", published=1_500 * NANO,
                                 range_id="range-1", stop={"LONG": 0.06, "SHORT": 0.06})
    label = make_label(prediction, stop={"LONG": True, "SHORT": False})
    rows = bundle((prediction,), (label,), (snapshot,), {"req-1": DAY0.isoformat()})
    assert not any(row.event.startswith("STOP_TOUCH") for row in rows)


# ---------------------------------------------------------------------------
# exclusions after validation
# ---------------------------------------------------------------------------

def test_invalid_and_unmatured_excluded_after_checks():
    snapshot = make_snapshot(1)
    good = make_prediction(snapshot, request_id="req-1", published=1_500 * NANO,
                           persistence={"COMPRESSION": 0.8})
    invalid = make_prediction(snapshot, request_id="req-2", published=1_500 * NANO,
                              persistence={"COMPRESSION": 0.8}, quality_status="INVALID")
    unmatured = make_prediction(snapshot, request_id="req-3", published=1_500 * NANO,
                                persistence={"COMPRESSION": 0.8})
    none_persistence = {key: None for key in ("UP_TREND", "DOWN_TREND", "ORDERED_RANGE",
                                              "WIDE_RANGE", "COMPRESSION",
                                              "UP_BREAKOUT", "DOWN_BREAKOUT")}
    labels = (
        make_label(good, persistence={"COMPRESSION": True} | {
            key: None for key in none_persistence if key != "COMPRESSION"}),
        make_label(invalid, persistence={"COMPRESSION": True} | {
            key: None for key in none_persistence if key != "COMPRESSION"}),
        make_label(unmatured, status="UNMATURED", available_at=None,
                   persistence=none_persistence),
    )
    blocks = {"req-1": DAY0.isoformat(), "req-2": DAY0.isoformat(),
              "req-3": DAY0.isoformat()}
    rows = bundle((good, invalid, unmatured), labels, (snapshot,), blocks)
    compression = [row for row in rows if row.event == "PERSIST:COMPRESSION"]
    assert compression and all(row.sample_count == 1 for row in compression)


def test_common_dedupe_keeps_one_truth_per_snapshot_event():
    snapshot = make_snapshot(1)
    predictions, labels = [], []
    for index, barrier in enumerate(("ATR_0.5", "ATR_1", "ATR_2")):
        prediction = make_prediction(snapshot, request_id=f"req-{index}",
                                     published=1_500 * NANO, barrier=barrier,
                                     persistence={"COMPRESSION": 0.8})
        predictions.append(prediction)
        labels.append(make_label(prediction, persistence={"COMPRESSION": True} | {
            key: None for key in ("UP_TREND", "DOWN_TREND", "ORDERED_RANGE",
                                  "WIDE_RANGE", "UP_BREAKOUT", "DOWN_BREAKOUT")}))
    blocks = {prediction.request_id: DAY0.isoformat() for prediction in predictions}
    rows = bundle(predictions, labels, (snapshot,), blocks)
    compression = [row for row in rows if row.event == "PERSIST:COMPRESSION"]
    assert compression and all(row.sample_count == 1 for row in compression)


def test_direction_all_row_hand_computed():
    snapshot = make_snapshot(1)
    probabilities = [(0.6, 0.3, 0.1), (0.6, 0.3, 0.1), (0.2, 0.6, 0.2), (0.2, 0.6, 0.2)]
    directions = ["UP", "DOWN", "UP", "DOWN"]
    predictions, labels = [], []
    for index, (prob, direction) in enumerate(zip(probabilities, directions)):
        snap = make_snapshot(index)
        prediction = make_prediction(snap, request_id=f"req-{index}",
                                     published=1_500 * NANO, direction=prob)
        predictions.append(prediction)
        labels.append(make_label(prediction, direction=direction))
    blocks = {prediction.request_id: DAY0.isoformat() for prediction in predictions}
    rows = bundle(predictions, labels,
                  [make_snapshot(i) for i in range(4)], blocks)
    row = next(item for item in rows if item.event == "DIRECTION")
    assert row.bucket == "ALL"
    # per-class equal-width ECE averaged over present classes (UP/DOWN only)
    # UP: buckets [0.6,0.65) n=2 p̄=0.6 ȳ=0.5 → 0.1 weight 2/4; [0.2,0.25) same → 0.1
    # DOWN: [0.3,0.35) n=2 p̄=0.3 ȳ=0.5 → 0.2; [0.6,0.65) → 0.1 → class ECE 0.15... computed below
    up_ece = (2 / 4) * abs(0.6 - 0.5) + (2 / 4) * abs(0.2 - 0.5)
    down_ece = (2 / 4) * abs(0.3 - 0.5) + (2 / 4) * abs(0.6 - 0.5)
    expected = (up_ece + down_ece) / 2
    assert abs(row.calibration_error - expected) < 1e-12
    brier = sum(
        sum((probabilities[i][k] - (1.0 if class_key == directions[i] else 0.0)) ** 2
            for k, class_key in enumerate(("UP", "DOWN")))
        for i in range(4)) / 4
    assert abs(row.brier - brier) < 1e-12
