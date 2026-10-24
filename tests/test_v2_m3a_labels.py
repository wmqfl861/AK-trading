"""M3A label tests: hand-computed path/range/competing/stop events (03-M3A §8.1-8.3, §8.9, §8.12).

All fixtures are synthetic (SCENARIO evidence); no database, network or
wall-clock access. Hand arithmetic follows the document examples exactly.
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ak_trading.v2.contracts import (
    DataQuality,
    FactorValue,
    FeatureSnapshot,
    ForecastRequest,
    FrozenRange,
    Horizon,
    MarketBar,
    SessionWindow,
)
from ak_trading.v2.probability.labels import LABEL_VERSION, label_forecast

NANO = 1_000_000_000
MINUTE = 60 * NANO
HASH = hashlib.sha256(b"m3a-labels").hexdigest()
PRODUCT = "SHFE.RB"
CONTRACT = "SHFE.RB2605"
DAY1 = date(2026, 1, 12)
DAY2 = date(2026, 1, 13)
BASE = 1_770_000_000 * NANO  # abstract session-1 start


def quality(status: str = "READY") -> DataQuality:
    return DataQuality(status=status, reasons=(), temporal_basis="observed", input_hash=HASH)


def make_session(start_nano: int, minutes: int, trading_day: date = DAY1,
                 kind: str = "DAY") -> SessionWindow:
    return SessionWindow(
        session_id=f"{PRODUCT}-{trading_day.isoformat()}-{start_nano // NANO}",
        product=PRODUCT, trading_day=trading_day, start_nano=start_nano,
        end_nano=start_nano + minutes * MINUTE, kind=kind,
        calendar_version="cal-v2.1.0", evidence_status="SCENARIO")


def minute_bar(start_nano: int, open_: float, high: float, low: float, close: float,
               trading_day: date = DAY1, contract: str = CONTRACT,
               duration: int = 60) -> MarketBar:
    seconds = duration
    return MarketBar(
        bar_id=f"bar-{contract}-{duration}-{start_nano // NANO}",
        contract_id=contract, raw_symbol=CONTRACT, product=PRODUCT,
        duration_seconds=seconds, datetime_nano=start_nano, start_nano=start_nano,
        end_nano=start_nano + seconds * NANO, trading_day=trading_day,
        session_id=None, available_at_nano=start_nano + seconds * NANO,
        imported_at_nano=None,
        open=Decimal(str(open_)), high=Decimal(str(high)), low=Decimal(str(low)),
        close=Decimal(str(close)), volume=Decimal("1"), open_oi=Decimal("10"),
        close_oi=Decimal("10"), complete=True,
        expected_minutes=duration // 60, actual_minutes=duration // 60,
        source="synthetic", quality=quality())


def daily_bar(trading_day: date, open_: float, high: float, low: float, close: float,
              start_nano: int, end_nano: int, contract: str = CONTRACT) -> MarketBar:
    return MarketBar(
        bar_id=f"bar-{contract}-86400-{start_nano // NANO}",
        contract_id=contract, raw_symbol=CONTRACT, product=PRODUCT,
        duration_seconds=86400, datetime_nano=start_nano, start_nano=start_nano,
        end_nano=end_nano, trading_day=trading_day, session_id=None,
        available_at_nano=end_nano, imported_at_nano=None,
        open=Decimal(str(open_)), high=Decimal(str(high)), low=Decimal(str(low)),
        close=Decimal(str(close)), volume=Decimal("10"), open_oi=Decimal("10"),
        close_oi=Decimal("10"), complete=True,
        expected_minutes=1440, actual_minutes=1440, source="synthetic", quality=quality())


def make_features(decision_nano: int, reference: float, atr: float, tick: float = 1.0,
                  session_kind: str = "DAY", trading_day: date = DAY1,
                  values: tuple[FactorValue, ...] = ()) -> FeatureSnapshot:
    meta = FactorValue(
        factor_key="META:price_tick:v2.1.0", value=Decimal(str(tick)),
        available_at_nano=decision_nano, quality=quality())
    return FeatureSnapshot(
        snapshot_id=f"snap-{decision_nano // NANO}", contract_id=CONTRACT,
        raw_symbol=CONTRACT, product=PRODUCT, trading_day=trading_day,
        session_id=f"sess-{decision_nano // NANO}", session_kind=session_kind,
        decision_nano=decision_nano, available_at_nano=decision_nano,
        duration_seconds=86400 if session_kind == "DAILY" else 60,
        source_bar_ids=(), reference_price=Decimal(str(reference)),
        atr_points=Decimal(str(atr)), values=(meta, *values),
        daily_background_id=None, quality=quality())


def make_request(window_start: int, window_end: int, horizon: Horizon,
                 reference: float = 100.0, atr: float = 2.0, tick: float = 1.0,
                 frozen_range: FrozenRange | None = None,
                 session_kind: str = "DAY", trading_day: date = DAY1,
                 barrier: str = "ATR_1", upper: float = 2.0, lower: float = 2.0,
                 contract: str = CONTRACT) -> ForecastRequest:
    features = make_features(window_start - 1, reference, atr, tick,
                             session_kind, trading_day)
    features = FeatureSnapshot(
        snapshot_id=features.snapshot_id, contract_id=contract,
        raw_symbol=features.raw_symbol, product=features.product,
        trading_day=features.trading_day, session_id=features.session_id,
        session_kind=features.session_kind, decision_nano=window_start - 1,
        available_at_nano=window_start - 1, duration_seconds=features.duration_seconds,
        source_bar_ids=(), reference_price=features.reference_price,
        atr_points=features.atr_points, values=features.values,
        daily_background_id=None, quality=features.quality)
    return ForecastRequest(
        request_id=f"req-{window_start // NANO}-{horizon.value}-{barrier}",
        features=features, horizon=horizon, window_start_nano=window_start,
        window_end_nano=window_end, barrier_key=barrier,
        upper_distance=Decimal(str(upper)), lower_distance=Decimal(str(lower)),
        frozen_range=frozen_range)


def frozen(low: float = 98.0, high: float = 102.0, atr: float = 2.0,
           snapshot: str = "snap-range") -> FrozenRange:
    lower, upper = Decimal(str(low)), Decimal(str(high))
    return FrozenRange(
        range_id="range-1", frozen_at_nano=BASE - 1, lower=lower,
        center=(lower + upper) / 2, upper=upper,
        stop_lower=lower - Decimal(str(atr)), stop_upper=upper + Decimal(str(atr)),
        source_snapshot_id=snapshot)


def as_of_after(window_end: int) -> int:
    return window_end + NANO


# ---------------------------------------------------------------------------
# M3A-02: direction / magnitude / first touch (§8.1)
# ---------------------------------------------------------------------------

def test_path_gap_open_first_touch_stays_up_first():
    """§8.1: C=100 ATR=2 distance 2 → upper 102 lower 98; open 103 first, later 97."""
    request = make_request(BASE, BASE + 5 * MINUTE, Horizon("TRADING_MINUTE", 5))
    bars = (
        minute_bar(BASE, 103, 103, 103, 103),
        minute_bar(BASE + MINUTE, 102, 102, 97, 97),
        minute_bar(BASE + 2 * MINUTE, 97, 98, 96.5, 97.5),
        minute_bar(BASE + 3 * MINUTE, 97.5, 98, 97, 97.5),
        minute_bar(BASE + 4 * MINUTE, 97.5, 98, 97, 98),
    )
    label = label_forecast(request, bars, (make_session(BASE, 240),), (), as_of_after(BASE + 5 * MINUTE))
    assert label.status == "MATURE"
    assert label.path_outcome == "UP_FIRST"
    assert label.first_up_step == 1
    assert label.first_down_step == 2


def test_path_same_bar_double_touch_without_finer_is_ambiguous():
    """§8.1: open 100, high 103, low 97 in one bar, no finer data → AMBIGUOUS."""
    request = make_request(BASE, BASE + MINUTE, Horizon("TRADING_MINUTE", 1))
    bars = (minute_bar(BASE, 100, 103, 97, 100),)
    label = label_forecast(request, bars, (make_session(BASE, 240),), (), as_of_after(BASE + MINUTE))
    assert label.status == "MATURE"
    assert label.path_outcome == "AMBIGUOUS"
    assert label.first_up_step == 1 and label.first_down_step == 1
    assert "same_bar_double_touch_unresolved" in label.reasons


def test_path_daily_label_finer_minute_orders_down_first():
    """§8.1: legal finer minute bars touch 98 before 102 → DOWN_FIRST."""
    window_start = BASE + 240 * MINUTE  # next trading day session
    window_end = window_start + 240 * MINUTE
    request = make_request(window_start, window_end, Horizon("TRADING_DAY", 1),
                           session_kind="DAILY", trading_day=DAY2)
    day_bar = daily_bar(DAY2, 100, 103, 97, 101, window_start, window_end)
    finer = (
        minute_bar(window_start, 100, 100, 97, 99, trading_day=DAY2),
        minute_bar(window_start + MINUTE, 99, 102.5, 99, 102, trading_day=DAY2),
    )
    sessions = (make_session(window_start, 240, trading_day=DAY2),)
    label = label_forecast(request, (day_bar,), sessions, finer, as_of_after(window_end))
    assert label.status == "MATURE"
    assert label.path_outcome == "DOWN_FIRST"
    assert label.first_down_step is not None and label.first_up_step is not None
    assert label.available_at_nano >= max(b.available_at_nano for b in (*finer, day_bar))


def test_path_neither_when_no_barrier_touched():
    request = make_request(BASE, BASE + MINUTE, Horizon("TRADING_MINUTE", 1))
    bars = (minute_bar(BASE, 100, 101, 99.5, 100.5),)
    label = label_forecast(request, bars, (make_session(BASE, 240),), (), as_of_after(BASE + MINUTE))
    assert label.path_outcome == "NEITHER"
    assert label.first_up_step is None and label.first_down_step is None


def test_path_direction_theta_hand_computed():
    """θ = max(2×tick, 0.1×ATR) = 2 with tick 1 / ATR 2: +3 UP, −1 NEUTRAL, −2.5 DOWN."""
    sessions = (make_session(BASE, 240),)

    def direction_for(close: float):
        request = make_request(BASE, BASE + MINUTE, Horizon("TRADING_MINUTE", 1))
        bars = (minute_bar(BASE, 100, max(101.0, close), min(99.0, close), close),)
        return label_forecast(request, bars, sessions, (), as_of_after(BASE + MINUTE))

    assert direction_for(103.0).direction == "UP"
    assert direction_for(99.0).direction == "NEUTRAL"
    assert direction_for(97.5).direction == "DOWN"


def test_path_terminal_quantities_hand_computed():
    request = make_request(BASE, BASE + MINUTE, Horizon("TRADING_MINUTE", 1))
    bars = (minute_bar(BASE, 100, 101, 99, 101),)
    label = label_forecast(request, bars, (make_session(BASE, 240),), (), as_of_after(BASE + MINUTE))
    assert label.terminal_points == Decimal("1")
    assert abs(label.terminal_return - 0.01) < 1e-15
    assert abs(label.terminal_atr - 0.5) < 1e-15


def test_path_missing_meta_tick_is_invalid_not_guessed():
    request = make_request(BASE, BASE + MINUTE, Horizon("TRADING_MINUTE", 1))
    features = request.features
    stripped = FeatureSnapshot(
        snapshot_id=features.snapshot_id, contract_id=features.contract_id,
        raw_symbol=features.raw_symbol, product=features.product,
        trading_day=features.trading_day, session_id=features.session_id,
        session_kind=features.session_kind, decision_nano=features.decision_nano,
        available_at_nano=features.available_at_nano,
        duration_seconds=features.duration_seconds, source_bar_ids=(),
        reference_price=features.reference_price, atr_points=features.atr_points,
        values=(), daily_background_id=None, quality=features.quality)
    bare = ForecastRequest(
        request_id=request.request_id, features=stripped, horizon=request.horizon,
        window_start_nano=request.window_start_nano, window_end_nano=request.window_end_nano,
        barrier_key=request.barrier_key, upper_distance=request.upper_distance,
        lower_distance=request.lower_distance, frozen_range=None)
    bars = (minute_bar(BASE, 100, 103, 97, 100),)
    label = label_forecast(bare, bars, (make_session(BASE, 240),), (), as_of_after(BASE + MINUTE))
    assert label.status == "INVALID"
    assert "missing_decision_anchor_metadata" in label.reasons
    assert label.direction is None


def test_path_unmatured_when_window_not_elapsed():
    request = make_request(BASE, BASE + 5 * MINUTE, Horizon("TRADING_MINUTE", 5))
    bars = (minute_bar(BASE, 100, 101, 99, 100),)
    label = label_forecast(request, bars, (make_session(BASE, 240),), (), BASE + 4 * MINUTE)
    assert label.status == "UNMATURED"
    assert "window_not_elapsed" in label.reasons
    assert label.available_at_nano is None


def test_path_missing_when_expected_bar_absent():
    request = make_request(BASE, BASE + 5 * MINUTE, Horizon("TRADING_MINUTE", 5))
    bars = (minute_bar(BASE, 100, 101, 99, 100),)  # four minutes absent
    label = label_forecast(request, bars, (make_session(BASE, 240),), (), as_of_after(BASE + 5 * MINUTE))
    assert label.status == "MISSING"
    assert any(reason.startswith("missing_future_bars:4") for reason in label.reasons)


def test_path_invalid_on_wrong_contract_or_duration_or_alignment():
    sessions = (make_session(BASE, 240),)
    request = make_request(BASE, BASE + MINUTE, Horizon("TRADING_MINUTE", 1))
    wrong_contract = (minute_bar(BASE, 100, 101, 99, 100, contract="SHFE.RB2606"),)
    assert label_forecast(request, wrong_contract, sessions, (),
                          as_of_after(BASE + MINUTE)).status == "INVALID"
    wrong_duration = daily_bar(DAY1, 100, 101, 99, 100, BASE, BASE + MINUTE)
    assert label_forecast(request, (wrong_duration,), sessions, (),
                          as_of_after(BASE + MINUTE)).status == "INVALID"
    misaligned = (minute_bar(BASE + 30 * NANO, 100, 101, 99, 100),)
    assert label_forecast(request, misaligned, sessions, (),
                          as_of_after(BASE + MINUTE)).status == "INVALID"


def test_path_window_external_append_never_changes_matured_label():
    """§8.10: appended bars beyond the window leave the matured label intact."""
    request = make_request(BASE, BASE + MINUTE, Horizon("TRADING_MINUTE", 1))
    sessions = (make_session(BASE, 240),)
    base_bars = (minute_bar(BASE, 100, 101, 99, 100.5),)
    first = label_forecast(request, base_bars, sessions, (), as_of_after(BASE + MINUTE))
    appended = (*base_bars, minute_bar(BASE + MINUTE, 120, 130, 80, 120),
                minute_bar(BASE + 2 * MINUTE, 200, 210, 5, 200))
    second = label_forecast(request, appended, sessions, (), as_of_after(BASE + 3 * MINUTE))
    assert first == second


def test_path_minute_steps_span_lunch_without_compression():
    """Missing minutes occupy planned slots across the lunch gap (§8.9)."""
    morning = make_session(BASE, 150)
    afternoon = make_session(BASE + 165 * MINUTE, 90)
    window_start = BASE + 149 * MINUTE
    window_end = BASE + 169 * MINUTE  # 11:29-11:30 then 13:30..13:34 trading slots
    request = make_request(window_start, window_end, Horizon("TRADING_MINUTE", 5))
    # last morning minute present; the lunch gap is not five missing minutes —
    # the four afternoon slots (13:30..13:34) are absent
    bars = (minute_bar(window_start, 100, 101, 99, 100),)
    label = label_forecast(request, bars, (morning, afternoon), (),
                           as_of_after(BASE + 169 * MINUTE))
    assert label.status == "MISSING"
    assert any(reason.startswith("missing_future_bars:4") for reason in label.reasons)


# ---------------------------------------------------------------------------
# M3A-03: range persistence / competing events / stop touch (§8.2, §8.3, §8.12)
# ---------------------------------------------------------------------------

def test_regime_ordered_range_persistence_broken_by_excursion():
    """§8.2: L98/M100/U102 stop 96/104; low 97.5 then close 100 → sustained False,
    terminal neutral True, stop untouched — returning to the origin is not维持."""
    request = make_request(BASE, BASE + 5 * MINUTE, Horizon("TRADING_MINUTE", 5),
                           reference=100.0, frozen_range=frozen())
    bars = (
        minute_bar(BASE, 100, 100.5, 97.5, 99),
        minute_bar(BASE + MINUTE, 99, 100.2, 98.5, 100),
        minute_bar(BASE + 2 * MINUTE, 100, 100.5, 99, 100.5),
        minute_bar(BASE + 3 * MINUTE, 100.5, 101, 99.5, 100.5),
        minute_bar(BASE + 4 * MINUTE, 100.5, 101.5, 99.5, 100),
    )
    label = label_forecast(request, bars, (make_session(BASE, 240),), (),
                           as_of_after(BASE + 5 * MINUTE))
    assert label.status == "MATURE"
    assert label.persistence["ORDERED_RANGE"] is False
    assert label.persistence["WIDE_RANGE"] is False
    assert label.direction == "NEUTRAL"
    assert label.stop_touched["LONG"] is False
    assert label.stop_touched["SHORT"] is False
    assert label.range_id == "range-1"


def test_regime_range_persistence_sustained_when_all_inside():
    request = make_request(BASE, BASE + 5 * MINUTE, Horizon("TRADING_MINUTE", 5),
                           reference=100.0, frozen_range=frozen())
    bars = (
        minute_bar(BASE, 100, 101.5, 98.5, 101),
        minute_bar(BASE + MINUTE, 101, 101.9, 99, 100.5),
        minute_bar(BASE + 2 * MINUTE, 100.5, 101.8, 98.8, 101),
        minute_bar(BASE + 3 * MINUTE, 101, 101.5, 99, 100.8),
        minute_bar(BASE + 4 * MINUTE, 100.8, 101.9, 98.6, 100.5),
    )
    label = label_forecast(request, bars, (make_session(BASE, 240),), (),
                           as_of_after(BASE + 5 * MINUTE))
    assert label.persistence["ORDERED_RANGE"] is True
    assert label.persistence["WIDE_RANGE"] is True


def test_regime_range_persistence_none_when_decision_outside_range():
    request = make_request(BASE, BASE + MINUTE, Horizon("TRADING_MINUTE", 1),
                           reference=103.0, frozen_range=frozen())
    bars = (minute_bar(BASE, 103, 103.5, 102.5, 103),)
    label = label_forecast(request, bars, (make_session(BASE, 240),), (),
                           as_of_after(BASE + MINUTE))
    assert label.persistence["ORDERED_RANGE"] is None
    assert label.persistence["WIDE_RANGE"] is None


def test_regime_trend_persistence_up_and_down():
    up_request = make_request(BASE, BASE + 5 * MINUTE, Horizon("TRADING_MINUTE", 5))
    up_bars = (
        minute_bar(BASE, 100, 102, 100, 101),
        minute_bar(BASE + MINUTE, 101, 103.5, 100.5, 103),
        minute_bar(BASE + 2 * MINUTE, 103, 103.8, 102, 103.5),
        minute_bar(BASE + 3 * MINUTE, 103.5, 104, 102.5, 103.8),
        minute_bar(BASE + 4 * MINUTE, 103.8, 104.2, 103, 104),
    )
    up_label = label_forecast(up_request, up_bars, (make_session(BASE, 240),), (),
                              as_of_after(BASE + 5 * MINUTE))
    assert up_label.persistence["UP_TREND"] is True
    assert up_label.persistence["DOWN_TREND"] is False

    down_bars = (
        minute_bar(BASE, 100, 100.5, 98.5, 99),
        minute_bar(BASE + MINUTE, 99, 99.5, 96.5, 97),
        minute_bar(BASE + 2 * MINUTE, 97, 97.5, 96, 96.5),
        minute_bar(BASE + 3 * MINUTE, 96.5, 97, 95.5, 96),
        minute_bar(BASE + 4 * MINUTE, 96, 96.5, 95, 95.5),
    )
    down_label = label_forecast(up_request, down_bars, (make_session(BASE, 240),), (),
                                as_of_after(BASE + 5 * MINUTE))
    assert down_label.persistence["DOWN_TREND"] is True
    assert down_label.persistence["UP_TREND"] is False


def test_regime_compression_persistence_definition():
    """All highs/lows within [C−0.5ATR, C+0.5ATR] AND terminal inside."""
    request = make_request(BASE, BASE + 5 * MINUTE, Horizon("TRADING_MINUTE", 5))
    inside = (
        minute_bar(BASE, 100, 100.9, 99.2, 100.5),
        minute_bar(BASE + MINUTE, 100.5, 100.8, 99.5, 100.2),
        minute_bar(BASE + 2 * MINUTE, 100.2, 100.7, 99.3, 100.4),
        minute_bar(BASE + 3 * MINUTE, 100.4, 100.6, 99.4, 100.1),
        minute_bar(BASE + 4 * MINUTE, 100.1, 100.9, 99.5, 100.2),
    )
    label = label_forecast(request, inside, (make_session(BASE, 240),), (),
                           as_of_after(BASE + 5 * MINUTE))
    assert label.persistence["COMPRESSION"] is True
    escaped = (
        minute_bar(BASE, 100, 100.9, 99.2, 100.5),
        minute_bar(BASE + MINUTE, 100.5, 100.8, 99.5, 100.2),
        minute_bar(BASE + 2 * MINUTE, 100.2, 101.2, 99.3, 100.4),  # high beyond +0.5ATR
        minute_bar(BASE + 3 * MINUTE, 100.4, 100.6, 99.4, 100.1),
        minute_bar(BASE + 4 * MINUTE, 100.1, 100.9, 99.5, 100.2),
    )
    label2 = label_forecast(request, escaped, (make_session(BASE, 240),), (),
                            as_of_after(BASE + 5 * MINUTE))
    assert label2.persistence["COMPRESSION"] is False


def test_regime_stop_touch_long_and_short():
    request = make_request(BASE, BASE + 5 * MINUTE, Horizon("TRADING_MINUTE", 5),
                           frozen_range=frozen())
    long_stop = (
        minute_bar(BASE, 100, 101, 95.5, 96),
        minute_bar(BASE + MINUTE, 96, 97, 95.8, 96.5),
        minute_bar(BASE + 2 * MINUTE, 96.5, 97.5, 96, 97),
        minute_bar(BASE + 3 * MINUTE, 97, 98, 96.5, 97.5),
        minute_bar(BASE + 4 * MINUTE, 97.5, 99, 97, 98.5),
    )
    label = label_forecast(request, long_stop, (make_session(BASE, 240),), (),
                           as_of_after(BASE + 5 * MINUTE))
    assert label.stop_touched == {"LONG": True, "SHORT": False}
    short_stop = (
        minute_bar(BASE, 100, 104.5, 99.5, 104),
        minute_bar(BASE + MINUTE, 104, 104.2, 103, 103.5),
        minute_bar(BASE + 2 * MINUTE, 103.5, 104, 103, 103.5),
        minute_bar(BASE + 3 * MINUTE, 103.5, 103.8, 102.5, 103),
        minute_bar(BASE + 4 * MINUTE, 103, 103.5, 102, 102.5),
    )
    label2 = label_forecast(request, short_stop, (make_session(BASE, 240),), (),
                            as_of_after(BASE + 5 * MINUTE))
    assert label2.stop_touched == {"LONG": False, "SHORT": True}


def test_regime_stop_touch_empty_without_range():
    request = make_request(BASE, BASE + MINUTE, Horizon("TRADING_MINUTE", 1))
    bars = (minute_bar(BASE, 100, 101, 99, 100),)
    label = label_forecast(request, bars, (make_session(BASE, 240),), (), as_of_after(BASE + MINUTE))
    assert label.stop_touched == {}
    assert label.range_id is None


def test_regime_competing_return_first_from_below_center():
    """§8.3: from 99 (below M=100), touch 100 first then 97 → RETURN_FIRST."""
    request = make_request(BASE, BASE + 5 * MINUTE, Horizon("TRADING_MINUTE", 5),
                           reference=99.0, frozen_range=frozen())
    bars = (
        minute_bar(BASE, 99, 100.5, 98.8, 100),
        minute_bar(BASE + MINUTE, 100, 100.5, 97, 97.5),
        minute_bar(BASE + 2 * MINUTE, 97.5, 98, 97.2, 97.8),
        minute_bar(BASE + 3 * MINUTE, 97.8, 98.2, 97.5, 98),
        minute_bar(BASE + 4 * MINUTE, 98, 98.5, 97.8, 98.2),
    )
    label = label_forecast(request, bars, (make_session(BASE, 240),), (),
                           as_of_after(BASE + 5 * MINUTE))
    assert label.competing_outcome == "RETURN_FIRST"
    assert label.return_step == 1 and label.break_step == 2


def test_regime_competing_break_first():
    request = make_request(BASE, BASE + 5 * MINUTE, Horizon("TRADING_MINUTE", 5),
                           reference=99.0, frozen_range=frozen())
    bars = (
        minute_bar(BASE, 99, 99.5, 97, 97.5),
        minute_bar(BASE + MINUTE, 97.5, 100.5, 97.2, 100),
        minute_bar(BASE + 2 * MINUTE, 100, 100.2, 99, 99.5),
        minute_bar(BASE + 3 * MINUTE, 99.5, 99.8, 99, 99.5),
        minute_bar(BASE + 4 * MINUTE, 99.5, 99.9, 99.2, 99.8),
    )
    label = label_forecast(request, bars, (make_session(BASE, 240),), (),
                           as_of_after(BASE + 5 * MINUTE))
    assert label.competing_outcome == "BREAK_FIRST"
    assert label.break_step == 1 and label.return_step == 2


def test_regime_competing_ambiguous_on_same_bar_both_sides():
    request = make_request(BASE, BASE + MINUTE, Horizon("TRADING_MINUTE", 1),
                           reference=99.0, frozen_range=frozen())
    bars = (minute_bar(BASE, 99, 100.5, 97, 99),)
    label = label_forecast(request, bars, (make_session(BASE, 240),), (),
                           as_of_after(BASE + MINUTE))
    assert label.competing_outcome == "AMBIGUOUS"
    assert "competing_event_unresolved" in label.reasons


def test_regime_competing_not_applicable_at_center():
    """Decision price exactly at M: no return event, no fabricated successes."""
    request = make_request(BASE, BASE + MINUTE, Horizon("TRADING_MINUTE", 1),
                           reference=100.0, frozen_range=frozen())
    bars = (minute_bar(BASE, 100, 100.5, 99.5, 100),)
    label = label_forecast(request, bars, (make_session(BASE, 240),), (),
                           as_of_after(BASE + MINUTE))
    assert label.competing_outcome is None
    assert label.return_step is None and label.break_step is None


def test_regime_competing_neither_when_stuck_between():
    request = make_request(BASE, BASE + 5 * MINUTE, Horizon("TRADING_MINUTE", 5),
                           reference=99.0, frozen_range=frozen())
    bars = (
        minute_bar(BASE, 99, 99.5, 98.5, 99.2),
        minute_bar(BASE + MINUTE, 99.2, 99.8, 98.8, 99.5),
        minute_bar(BASE + 2 * MINUTE, 99.5, 99.6, 98.9, 99.3),
        minute_bar(BASE + 3 * MINUTE, 99.3, 99.7, 99, 99.6),
        minute_bar(BASE + 4 * MINUTE, 99.6, 99.9, 99.1, 99.4),
    )
    label = label_forecast(request, bars, (make_session(BASE, 240),), (),
                           as_of_after(BASE + 5 * MINUTE))
    assert label.competing_outcome == "NEITHER"


def test_regime_breakout_persistence_hand_computed():
    """§8.12: U=102 tick=1 C=104; every close 104/105 → true; 104/103/106 → false
    (any close back inside kills it; later breakout does not restore)."""
    request = make_request(BASE, BASE + 5 * MINUTE, Horizon("TRADING_MINUTE", 5),
                           reference=104.0, frozen_range=frozen(), tick=1.0)
    good = (
        minute_bar(BASE, 104, 105, 103.5, 104),
        minute_bar(BASE + MINUTE, 104, 106, 103.8, 105),
        minute_bar(BASE + 2 * MINUTE, 105, 106.5, 104.5, 106),
        minute_bar(BASE + 3 * MINUTE, 106, 106.8, 105, 105.5),
        minute_bar(BASE + 4 * MINUTE, 105.5, 107, 105.2, 106.5),
    )
    label = label_forecast(request, good, (make_session(BASE, 240),), (),
                           as_of_after(BASE + 5 * MINUTE))
    assert label.persistence["UP_BREAKOUT"] is True
    assert label.persistence["DOWN_BREAKOUT"] is None

    bad = (
        minute_bar(BASE, 104, 105, 103.5, 104),
        minute_bar(BASE + MINUTE, 104, 106, 103.8, 105),
        minute_bar(BASE + 2 * MINUTE, 105, 108, 103, 103),  # close back inside
        minute_bar(BASE + 3 * MINUTE, 103, 108.5, 103, 106),  # later breakout no restore
        minute_bar(BASE + 4 * MINUTE, 106, 108.9, 105.5, 108),
    )
    label2 = label_forecast(request, bad, (make_session(BASE, 240),), (),
                            as_of_after(BASE + 5 * MINUTE))
    assert label2.persistence["UP_BREAKOUT"] is False


def test_regime_breakout_not_applicable_inside_thresholds():
    request = make_request(BASE, BASE + MINUTE, Horizon("TRADING_MINUTE", 1),
                           reference=100.0, frozen_range=frozen(), tick=1.0)
    bars = (minute_bar(BASE, 100, 101, 99, 100),)
    label = label_forecast(request, bars, (make_session(BASE, 240),), (),
                           as_of_after(BASE + MINUTE))
    assert label.persistence["UP_BREAKOUT"] is None
    assert label.persistence["DOWN_BREAKOUT"] is None


def test_regime_persistence_lists_all_events_with_label_version():
    request = make_request(BASE, BASE + MINUTE, Horizon("TRADING_MINUTE", 1))
    bars = (minute_bar(BASE, 100, 101, 99, 100),)
    label = label_forecast(request, bars, (make_session(BASE, 240),), (), as_of_after(BASE + MINUTE))
    assert label.label_version == LABEL_VERSION == "path-label-v2.1.0"
    assert set(label.persistence) == {
        "UP_TREND", "DOWN_TREND", "ORDERED_RANGE", "WIDE_RANGE",
        "COMPRESSION", "UP_BREAKOUT", "DOWN_BREAKOUT"}
