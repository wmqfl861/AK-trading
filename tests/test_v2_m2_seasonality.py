"""M2-06: session seasonality — slot statistics, training boundary, freeze.

Hand case from M2 §7: twenty training days of the same slot always trade
volume 10, current 20 -> relative 1.  Night-session slots never mix into
day buckets, data after ``fit_end_nano`` never enters a fit, buckets with
fewer than 20 training days or a zero median stay missing evidence (the
neutral 1 is never substituted), slot mapping goes through the frozen
session calendar, and appending test-period extremes leaves the fitted
model byte-identical.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ak_trading.v2.contracts import DataQuality, MarketBar, SessionWindow
from ak_trading.v2.seasonality import (
    MINIMUM_SAMPLE_DAYS,
    SEASONAL_ABS_RETURN_KEY,
    SEASONAL_VOLUME_KEY,
    SeasonalityError,
    SeasonalBucket,
    SeasonalityModel,
    fit_seasonality,
    seasonal_factor_keys,
    seasonal_values,
)

NANO = 1_000_000_000
MINUTE = 60 * NANO
QUALITY = DataQuality(status="READY", reasons=(), temporal_basis="observed",
                      input_hash="0" * 64)
PRODUCT = "SHFE.RB"
CALENDAR = "cal-test-v1"


def day_start(day: date, hour: int) -> int:
    return int(datetime(day.year, day.month, day.day, hour, 0,
                        tzinfo=timezone.utc).timestamp()) * NANO


def make_day(day: date, index: int, *, day_slots: int = 3, night_slots: int = 2,
             day_volume: int = 10, night_volume: int = 100,
             close_offset: int = 1) -> tuple[SessionWindow, SessionWindow, list[MarketBar]]:
    day_session = SessionWindow(
        session_id=f"D{index}", product=PRODUCT, trading_day=day,
        start_nano=day_start(day, 1), end_nano=day_start(day, 1) + day_slots * MINUTE,
        kind="DAY", calendar_version=CALENDAR, evidence_status="VERIFIED")
    night_session = SessionWindow(
        session_id=f"N{index}", product=PRODUCT, trading_day=day,
        start_nano=day_start(day, 9), end_nano=day_start(day, 9) + night_slots * MINUTE,
        kind="NIGHT", calendar_version=CALENDAR, evidence_status="VERIFIED")
    bars: list[MarketBar] = []
    base = Decimal(100 + index)
    for slot in range(day_slots):
        # Slots >= 1 close above their open so |ln(C/O)| is nonzero.
        close = base + Decimal(close_offset) if slot >= 1 else base
        high = max(base + Decimal(1), close + Decimal(1))
        low = min(base - Decimal(1), close - Decimal(1))
        start = day_session.start_nano + slot * MINUTE
        bars.append(MarketBar(
            bar_id=f"d{index}s{slot}", contract_id="RB2610", raw_symbol="rb2610",
            product=PRODUCT, duration_seconds=60, datetime_nano=start,
            start_nano=start, end_nano=start + MINUTE, trading_day=day,
            session_id=day_session.session_id, available_at_nano=start + MINUTE,
            imported_at_nano=None, open=base, high=high, low=low,
            close=close,
            volume=Decimal(day_volume if slot == 1 else 5),
            open_oi=Decimal(1000), close_oi=Decimal(1000), complete=True,
            expected_minutes=1, actual_minutes=1, source="synthetic", quality=QUALITY))
    for slot in range(night_slots):
        start = night_session.start_nano + slot * MINUTE
        bars.append(MarketBar(
            bar_id=f"n{index}s{slot}", contract_id="RB2610", raw_symbol="rb2610",
            product=PRODUCT, duration_seconds=60, datetime_nano=start,
            start_nano=start, end_nano=start + MINUTE, trading_day=day,
            session_id=night_session.session_id, available_at_nano=start + MINUTE,
            imported_at_nano=None, open=base, high=base + 1, low=base - 1,
            close=base, volume=Decimal(night_volume if slot == 0 else 7),
            open_oi=Decimal(1000), close_oi=Decimal(1000), complete=True,
            expected_minutes=1, actual_minutes=1, source="synthetic", quality=QUALITY))
    return day_session, night_session, bars


def build_history(days: int, **kwargs) -> tuple[tuple[SessionWindow, ...], tuple[MarketBar, ...]]:
    sessions: list[SessionWindow] = []
    bars: list[MarketBar] = []
    for index in range(days):
        day = date(2026, 1, 5) + timedelta(days=index)
        day_session, night_session, day_bars = make_day(day, index, **kwargs)
        sessions.extend((day_session, night_session))
        bars.extend(day_bars)
    return tuple(sessions), tuple(bars)


def test_seasonality_hand_volume_relative_one() -> None:
    sessions, bars = build_history(MINIMUM_SAMPLE_DAYS)
    fit_end = max(session.end_nano for session in sessions)
    fit_start = min(session.start_nano for session in sessions)
    model = fit_seasonality(bars, sessions, fit_start, fit_end)
    bucket = next(b for b in model.buckets
                  if b.session_kind == "DAY" and b.slot_index == 1)
    assert bucket.median_volume == Decimal(10)
    assert bucket.sample_days == MINIMUM_SAMPLE_DAYS
    # Current day 21, slot 1, volume 20 -> 20/10 - 1 = 1.
    day = date(2026, 1, 5) + timedelta(days=MINIMUM_SAMPLE_DAYS)
    day_session, _, day_bars = make_day(day, MINIMUM_SAMPLE_DAYS, day_volume=20)
    current = next(b for b in day_bars if b.session_id == day_session.session_id
                   and b.start_nano == day_session.start_nano + MINUTE)
    previous = next(b for b in day_bars
                    if b.session_id == day_session.session_id
                    and b.start_nano == day_session.start_nano)
    values = seasonal_values(model, current, sessions + (day_session,),
                             current.end_nano, previous_bar=previous)
    by_key = {value.factor_key: value for value in values}
    volume = by_key[SEASONAL_VOLUME_KEY.format(duration_seconds=60)]
    assert volume.value == Decimal(1)
    assert volume.quality.status == "READY"
    abs_return = by_key[SEASONAL_ABS_RETURN_KEY.format(duration_seconds=60)]
    assert abs_return.quality.status == "READY"
    # Slot-1 training |ln(C/O)| = ln((101+i)/(100+i)); current day uses
    # base 120 -> |ln(121/120)| divided by the median of the training values.
    import math
    import statistics
    training = [abs(math.log((101 + i) / (100 + i))) for i in range(MINIMUM_SAMPLE_DAYS)]
    expected = abs(math.log(121 / 120)) / statistics.median(training)
    assert abs_return.value == pytest.approx(expected, rel=1e-9, abs=1e-10)
    assert [value.factor_key for value in values] == list(
        seasonal_factor_keys(60))


def evaluation_day(index: int, *, day_volume: int | None = None,
                   slot: int = 1, sessions_offset: int = 0):
    """A post-training evaluation day with sessions, anchor and its
    calendar-planned previous bar (slot 0 of the same day session)."""
    day = date(2026, 1, 5) + timedelta(days=100 + index)
    day_session, night_session, day_bars = make_day(
        day, 1000 + index, day_volume=(day_volume if day_volume is not None else 10))
    anchor = next(b for b in day_bars
                  if b.session_id == day_session.session_id
                  and b.start_nano == day_session.start_nano + slot * MINUTE)
    previous = next(b for b in day_bars
                    if b.session_id == day_session.session_id
                    and b.start_nano == day_session.start_nano + (slot - 1) * MINUTE)
    return (day_session, night_session, tuple(day_bars), anchor, previous)


def test_seasonality_night_slot_never_mixes_into_day_bucket() -> None:
    sessions, bars = build_history(MINIMUM_SAMPLE_DAYS)
    fit_end = max(session.end_nano for session in sessions)
    model = fit_seasonality(bars, sessions, min(s.start_nano for s in sessions), fit_end)
    day_slot1 = next(b for b in model.buckets if b.session_kind == "DAY" and b.slot_index == 1)
    # Q01: slot ordinals accumulate per kind across the day's fragments;
    # the DAY session occupies 0..2 and the NIGHT session its own 0..1.
    night_slot0 = next(b for b in model.buckets if b.session_kind == "NIGHT" and b.slot_index == 0)
    assert day_slot1.median_volume == Decimal(10)
    assert night_slot0.median_volume == Decimal(100)  # night volume 100 stays apart
    assert night_slot0.product == day_slot1.product == PRODUCT
    # The trading day's first slot is its own bucket (overnight/intraday gap).
    day_slot0 = next(b for b in model.buckets if b.session_kind == "DAY" and b.slot_index == 0)
    assert day_slot0.median_volume == Decimal(5)
    assert (day_slot0.session_kind, day_slot0.slot_index) != (night_slot0.session_kind,
                                                              night_slot0.slot_index)
    keys = {(b.session_kind, b.slot_index) for b in model.buckets}
    assert keys == {("DAY", 0), ("DAY", 1), ("DAY", 2), ("NIGHT", 0), ("NIGHT", 1)}


def test_seasonality_am_pm_fragments_never_collide() -> None:
    """AM and PM fragments of one trading day map to distinct slot buckets."""
    sessions: list[SessionWindow] = []
    bars: list[MarketBar] = []
    for index in range(MINIMUM_SAMPLE_DAYS):
        day = date(2026, 2, 2) + timedelta(days=index)
        morning = SessionWindow(
            session_id=f"{index}-AM", product=PRODUCT, trading_day=day,
            start_nano=day_start(day, 1), end_nano=day_start(day, 1) + 2 * MINUTE,
            kind="DAY", calendar_version=CALENDAR, evidence_status="SCENARIO")
        afternoon = SessionWindow(
            session_id=f"{index}-PM", product=PRODUCT, trading_day=day,
            start_nano=day_start(day, 5), end_nano=day_start(day, 5) + 2 * MINUTE,
            kind="DAY", calendar_version=CALENDAR, evidence_status="SCENARIO")
        sessions.extend((morning, afternoon))
        for session, volume in ((morning, 10), (afternoon, 100)):
            for slot in range(2):
                start = session.start_nano + slot * MINUTE
                close = Decimal(101) if slot else Decimal(100)
                bars.append(MarketBar(
                    bar_id=f"{session.session_id}-s{slot}", contract_id="RB2610",
                    raw_symbol="rb2610", product=PRODUCT, duration_seconds=60,
                    datetime_nano=start, start_nano=start, end_nano=start + MINUTE,
                    trading_day=day, session_id=session.session_id,
                    available_at_nano=start + MINUTE, imported_at_nano=None,
                    open=Decimal(100), high=Decimal(102), low=Decimal(99), close=close,
                    volume=Decimal(volume), open_oi=Decimal(1000),
                    close_oi=Decimal(1000), complete=True, expected_minutes=1,
                    actual_minutes=1, source="synthetic", quality=QUALITY))
    model = fit_seasonality(tuple(bars), tuple(sessions),
                            min(s.start_nano for s in sessions),
                            max(s.end_nano for s in sessions))
    day_keys = {(b.session_kind, b.slot_index): b for b in model.buckets
                if b.session_kind == "DAY"}
    assert set(day_keys) == {("DAY", 0), ("DAY", 1), ("DAY", 2), ("DAY", 3)}
    assert day_keys[("DAY", 0)].median_volume == Decimal(10)   # AM open
    assert day_keys[("DAY", 1)].median_volume == Decimal(10)   # AM second
    assert day_keys[("DAY", 2)].median_volume == Decimal(100)  # PM open
    assert day_keys[("DAY", 3)].median_volume == Decimal(100)  # PM second
    # A later PM-open bar standardizes against the PM bucket, not AM's.
    eval_day = date(2026, 2, 2) + timedelta(days=100)
    eval_am = SessionWindow(
        session_id="EVAL-AM", product=PRODUCT, trading_day=eval_day,
        start_nano=day_start(eval_day, 1), end_nano=day_start(eval_day, 1) + 2 * MINUTE,
        kind="DAY", calendar_version=CALENDAR, evidence_status="SCENARIO")
    eval_session = SessionWindow(
        session_id="EVAL-PM", product=PRODUCT, trading_day=eval_day,
        start_nano=day_start(eval_day, 5), end_nano=day_start(eval_day, 5) + 2 * MINUTE,
        kind="DAY", calendar_version=CALENDAR, evidence_status="SCENARIO")
    current = MarketBar(
        bar_id="eval-pm", contract_id="RB2610", raw_symbol="rb2610", product=PRODUCT,
        duration_seconds=60, datetime_nano=eval_session.start_nano,
        start_nano=eval_session.start_nano, end_nano=eval_session.start_nano + MINUTE,
        trading_day=eval_day, session_id=eval_session.session_id,
        available_at_nano=eval_session.start_nano + MINUTE, imported_at_nano=None,
        open=Decimal(100), high=Decimal(102), low=Decimal(99), close=Decimal(101),
        volume=Decimal(100), open_oi=Decimal(1000), close_oi=Decimal(1000),
        complete=True, expected_minutes=1, actual_minutes=1, source="synthetic",
        quality=QUALITY)
    values = {value.factor_key: value for value in
              seasonal_values(model, current, tuple(sessions) + (eval_am, eval_session),
                              current.end_nano, previous_bar=None)}
    volume = values[SEASONAL_VOLUME_KEY.format(duration_seconds=60)]
    assert volume.value == Decimal(0)  # 100 / PM median 100 - 1
    assert volume.quality.status == "READY"


def test_seasonality_fit_end_excludes_later_data() -> None:
    sessions, bars = build_history(MINIMUM_SAMPLE_DAYS + 1)
    day21 = [s for s in sessions if s.session_id == f"D{MINIMUM_SAMPLE_DAYS}"][0]
    bars_without = tuple(b for b in bars if b.session_id != day21.session_id)
    fit_end = max(s.end_nano for s in sessions if s.session_id != f"N{MINIMUM_SAMPLE_DAYS}")
    fit_end = min(fit_end, day21.start_nano)  # cut before day 21 trades
    model_cut = fit_seasonality(bars, sessions, min(s.start_nano for s in sessions), fit_end)
    model_pure = fit_seasonality(bars_without, sessions,
                                 min(s.start_nano for s in sessions), fit_end)
    bucket_cut = next(b for b in model_cut.buckets
                      if b.session_kind == "DAY" and b.slot_index == 1)
    bucket_pure = next(b for b in model_pure.buckets
                       if b.session_kind == "DAY" and b.slot_index == 1)
    assert bucket_cut == bucket_pure  # bars after fit_end never entered the fit
    assert bucket_cut.sample_days == MINIMUM_SAMPLE_DAYS
    # Extending the fit window to include day 21 does change the bucket:
    # day 21 slot-1 volume was 10 as well, so sample_days grows by one day.
    full_end = max(s.end_nano for s in sessions)
    model_full = fit_seasonality(bars, sessions, min(s.start_nano for s in sessions), full_end)
    bucket_full = next(b for b in model_full.buckets
                       if b.session_kind == "DAY" and b.slot_index == 1)
    assert bucket_full.sample_days == MINIMUM_SAMPLE_DAYS + 1


def test_seasonality_insufficient_days_is_missing_evidence() -> None:
    sessions, bars = build_history(MINIMUM_SAMPLE_DAYS - 1)
    fit_end = max(s.end_nano for s in sessions)
    model = fit_seasonality(bars, sessions, min(s.start_nano for s in sessions), fit_end)
    bucket = next(b for b in model.buckets if b.session_kind == "DAY" and b.slot_index == 1)
    assert bucket.sample_days == MINIMUM_SAMPLE_DAYS - 1
    assert bucket.median_volume is None
    assert bucket.median_abs_return is None  # no neutral 1 is substituted
    day_session, night_session, day_bars, current, previous = evaluation_day(0)
    values = {value.factor_key: value for value in
              seasonal_values(model, current, (day_session, night_session) + sessions,
                              current.end_nano, previous_bar=previous)}
    volume = values[SEASONAL_VOLUME_KEY.format(duration_seconds=60)]
    assert volume.value is None and volume.quality.status == "MISSING"
    assert "insufficient_seasonal_samples" in volume.quality.reasons


def test_seasonality_zero_median_is_missing_not_one() -> None:
    sessions, bars = build_history(MINIMUM_SAMPLE_DAYS, day_volume=0)
    fit_end = max(s.end_nano for s in sessions)
    model = fit_seasonality(bars, sessions, min(s.start_nano for s in sessions), fit_end)
    bucket = next(b for b in model.buckets if b.session_kind == "DAY" and b.slot_index == 1)
    assert bucket.median_volume == Decimal(0)
    day_session, night_session, day_bars, current, previous = evaluation_day(0)
    values = {value.factor_key: value for value in
              seasonal_values(model, current, (day_session, night_session) + sessions,
                              current.end_nano, previous_bar=previous)}
    volume = values[SEASONAL_VOLUME_KEY.format(duration_seconds=60)]
    assert volume.value is None and volume.quality.status == "INVALID"
    assert "zero_denominator" in volume.quality.reasons


def test_seasonality_slot_mapping_requires_session_calendar() -> None:
    sessions, bars = build_history(MINIMUM_SAMPLE_DAYS)
    fit_end = max(s.end_nano for s in sessions)
    model = fit_seasonality(bars, sessions, min(s.start_nano for s in sessions), fit_end)
    day_session, night_session, day_bars, current, previous = evaluation_day(0)
    # Without the calendar the slot cannot be mapped from natural time.
    values = {value.factor_key: value for value in
              seasonal_values(model, current, sessions, current.end_nano,
                              previous_bar=None)}
    assert values[SEASONAL_VOLUME_KEY.format(duration_seconds=60)].quality.reasons == (
        "slot_mapping_unavailable",)
    # A bar the calendar does not contain is rejected, and so is an
    # invisible bar (availability after the decision time).
    with pytest.raises(SeasonalityError):
        seasonal_values(model, current, (day_session,), current.end_nano - 1,
                        previous_bar=None)
    with pytest.raises(SeasonalityError):
        seasonal_values(model, current, (day_session,), current.start_nano,
                        previous_bar=None)


def test_seasonality_model_frozen_under_appended_extremes() -> None:
    sessions, bars = build_history(MINIMUM_SAMPLE_DAYS)
    fit_start = min(s.start_nano for s in sessions)
    fit_end = max(s.end_nano for s in sessions)
    model = fit_seasonality(bars, sessions, fit_start, fit_end)
    # Append extreme test-period bars; with the same fit window the fitted
    # model must stay byte-identical (frozen inside the validation fold).
    extra_sessions: list[SessionWindow] = []
    extra_bars: list[MarketBar] = []
    for index in range(3):
        day = date(2026, 3, 1) + timedelta(days=index)
        day_session, night_session, day_bars = make_day(
            day, 100 + index, day_volume=10**9, night_volume=10**9,
            close_offset=500)
        extra_sessions.extend((day_session, night_session))
        for bar_ in day_bars:
            extra_bars.append(MarketBar(
                bar_id=bar_.bar_id + "x", contract_id=bar_.contract_id,
                raw_symbol=bar_.raw_symbol, product=bar_.product,
                duration_seconds=bar_.duration_seconds,
                datetime_nano=bar_.datetime_nano, start_nano=bar_.start_nano,
                end_nano=bar_.end_nano, trading_day=bar_.trading_day,
                session_id=bar_.session_id, available_at_nano=bar_.available_at_nano,
                imported_at_nano=None, open=Decimal("1"), high=Decimal("99999"),
                low=Decimal("1"), close=Decimal("99999"),
                volume=Decimal("1000000000"), open_oi=Decimal(1),
                close_oi=Decimal(1), complete=True, expected_minutes=1,
                actual_minutes=1, source="synthetic-extreme", quality=QUALITY))
    refit = fit_seasonality(tuple(bars) + tuple(extra_bars),
                            sessions + tuple(extra_sessions), fit_start, fit_end)
    assert refit == model
    assert refit.model_id == model.model_id and refit.input_sha256 == model.input_sha256
    # Reading values from the frozen model with an extreme current bar is a
    # pure lookup: the huge volume produces a huge relative, never refits.
    day_session = extra_sessions[0]
    current = next(b for b in extra_bars
                   if b.session_id == day_session.session_id
                   and b.start_nano == day_session.start_nano + MINUTE)
    values = {value.factor_key: value for value in
              seasonal_values(model, current, sessions + tuple(extra_sessions),
                              current.end_nano, previous_bar=None)}
    volume = values[SEASONAL_VOLUME_KEY.format(duration_seconds=60)]
    assert volume.value == Decimal("1000000000") / Decimal(10) - Decimal(1)


def test_seasonality_model_types_and_validation() -> None:
    bucket = SeasonalBucket(product=PRODUCT, session_kind="DAY", slot_index=0,
                            duration_seconds=60, median_volume=Decimal("10"),
                            median_abs_return=0.01, sample_days=25)
    model = SeasonalityModel(model_id="seasonality:" + "0" * 64,
                             fit_start_nano=0, fit_end_nano=1,
                             calendar_version=CALENDAR, buckets=(bucket,),
                             input_sha256="0" * 64)
    assert model.buckets[0] is bucket
    with pytest.raises(ValueError):
        SeasonalBucket(product=PRODUCT, session_kind="LUNCH", slot_index=0,
                       duration_seconds=60, median_volume=None,
                       median_abs_return=None, sample_days=1)
    with pytest.raises(ValueError):
        SeasonalityModel(model_id="", fit_start_nano=1, fit_end_nano=0,
                         calendar_version=CALENDAR, buckets=(),
                         input_sha256="0" * 64)
    sessions, bars = build_history(3)
    with pytest.raises(SeasonalityError):
        fit_seasonality(bars, sessions, 10, 1)
    mixed_session = SessionWindow(
        session_id="X", product=PRODUCT, trading_day=date(2026, 1, 5),
        start_nano=0, end_nano=MINUTE, kind="DAY",
        calendar_version="other-calendar", evidence_status="SCENARIO")
    mixed = sessions + (mixed_session,)
    mixed_bar = MarketBar(
        bar_id="mixed", contract_id="RB2610", raw_symbol="rb2610", product=PRODUCT,
        duration_seconds=60, datetime_nano=0, start_nano=0, end_nano=MINUTE,
        trading_day=date(2026, 1, 5), session_id="X", available_at_nano=MINUTE,
        imported_at_nano=None, open=Decimal(100), high=Decimal(101),
        low=Decimal(99), close=Decimal(100), volume=Decimal(10),
        open_oi=Decimal(1000), close_oi=Decimal(1000), complete=True,
        expected_minutes=1, actual_minutes=1, source="synthetic", quality=QUALITY)
    with pytest.raises(SeasonalityError):
        fit_seasonality(tuple(bars) + (mixed_bar,), mixed,
                        0, max(s.end_nano for s in sessions))


def test_seasonality_future_fit_end_is_missing_evidence_e01() -> None:
    """A model whose training finishes after the decision is unavailable."""
    sessions, bars = build_history(MINIMUM_SAMPLE_DAYS)
    fit_start = min(s.start_nano for s in sessions)
    fit_end = max(s.end_nano for s in sessions)
    model = fit_seasonality(bars, sessions, fit_start, fit_end)
    # A decision inside the training window predates fit_end (E01).
    early_session = [s for s in sessions if s.session_id == "D0"][0]
    early = next(b for b in bars if b.session_id == "D0"
                 and b.start_nano == early_session.start_nano + MINUTE)
    values = {value.factor_key: value for value in
              seasonal_values(model, early, sessions, early.end_nano,
                              previous_bar=None)}
    for value in values.values():
        assert value.value is None and value.quality.status == "INVALID"
        assert value.quality.reasons == ("model_fit_after_decision",)
    # Once the decision passes fit_end the same model serves normally.
    day_session, night_session, day_bars, current, previous = evaluation_day(
        0, day_volume=20)
    later = {value.factor_key: value for value in
             seasonal_values(model, current, (day_session, night_session) + sessions,
                             current.end_nano, previous_bar=previous)}
    volume = later[SEASONAL_VOLUME_KEY.format(duration_seconds=60)]
    assert volume.quality.status == "READY" and volume.value == Decimal(1)


def test_seasonality_calendar_version_mismatch_is_missing_e06() -> None:
    sessions, bars = build_history(MINIMUM_SAMPLE_DAYS)
    fit_start = min(s.start_nano for s in sessions)
    fit_end = max(s.end_nano for s in sessions)
    model = fit_seasonality(bars, sessions, fit_start, fit_end)
    day_session, night_session, day_bars, current, previous = evaluation_day(0)
    changed = SessionWindow(
        session_id=day_session.session_id, product=day_session.product,
        trading_day=day_session.trading_day, start_nano=day_session.start_nano,
        end_nano=day_session.end_nano, kind=day_session.kind,
        calendar_version="probe-cal-v2", evidence_status=day_session.evidence_status)
    values = {value.factor_key: value for value in
              seasonal_values(model, current, (changed,) + tuple(sessions),
                              current.end_nano, previous_bar=previous)}
    for value in values.values():
        assert value.value is None and value.quality.status == "INVALID"
        assert value.quality.reasons == ("calendar_version_mismatch",)


def test_seasonality_future_revision_does_not_break_refit_e03() -> None:
    sessions, bars = build_history(MINIMUM_SAMPLE_DAYS)
    fit_start = min(s.start_nano for s in sessions)
    fit_end = max(s.end_nano for s in sessions)
    frozen = fit_seasonality(bars, sessions, fit_start, fit_end)
    from dataclasses import replace
    revision = replace(
        bars[0], bar_id=bars[0].bar_id + "-late-revision", volume=Decimal(9999),
        available_at_nano=fit_end + 30 * 24 * 60 * 60 * 1_000_000_000)
    refit = fit_seasonality(tuple(bars) + (revision,), sessions, fit_start, fit_end)
    assert refit == frozen  # the invisible revision never entered the fit
    # A genuinely visible conflict is still rejected.
    visible_conflict = replace(bars[0], bar_id=bars[0].bar_id + "-conflict",
                               volume=Decimal(1))
    with pytest.raises(SeasonalityError):
        fit_seasonality(tuple(bars) + (visible_conflict,), sessions, fit_start, fit_end)


def test_seasonality_content_hash_public_binding_004() -> None:
    """变更004 §3: public content_hash binds all content except model_id."""
    from dataclasses import replace

    sessions, bars = build_history(MINIMUM_SAMPLE_DAYS)
    fit_start = min(s.start_nano for s in sessions)
    fit_end = max(s.end_nano for s in sessions)
    model = fit_seasonality(bars, sessions, fit_start, fit_end)
    digest = model.content_hash()
    assert len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)
    assert model.model_id == "seasonality:" + digest  # identity derives from content
    assert "model_id" not in model.content_projection()
    # A changed bucket median, cutoff or source changes the content hash.
    changed_bucket = replace(model.buckets[0], sample_days=model.buckets[0].sample_days + 1)
    assert replace(model, buckets=(changed_bucket, *model.buckets[1:])).content_hash() != digest
    assert replace(model, fit_end_nano=model.fit_end_nano + 1).content_hash() != digest
    assert replace(model, input_sha256="1" * 64).content_hash() != digest
    # Identical content yields the identical hash regardless of order of
    # construction, and the canonical bucket order is stable.
    again = fit_seasonality(bars, sessions, fit_start, fit_end)
    assert again.content_hash() == digest
    keys = [(b.product, b.session_kind, b.slot_index, b.duration_seconds)
            for b in model.buckets]
    assert keys == sorted(keys)
