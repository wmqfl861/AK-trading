"""M2-05: the fourteen v1 baselines — read-only legacy adapter and v2 rewrite.

The legacy daily adapter is compared value-by-value against a direct
read-only call of the version-1 calculator (tolerance 1e-10 absolute /
1e-9 relative for float statistics); both-unavailable points are counted
separately and never as matches.  The minute version re-implements the same
windows; signed/absolute ER, arithmetic/Wilder ATR, log/simple returns,
z-score prior window and the volume-ratio shift are each pinned.
"""
from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ak_trading.probability.factors import build_factor_rows as v1_build_factor_rows
from ak_trading.futures.spec import ContractBar
from ak_trading.v2.baseline import (
    LEGACY_LABEL,
    baseline_values_v2,
    legacy_daily_baseline,
)
from ak_trading.v2.contracts import DataQuality, MarketBar, SessionWindow
from ak_trading.v2.factor_specs import BASELINE_NAMES, baseline_factor_key
from ak_trading.v2.factors import compute_factor, wilder_atr

NANO = 1_000_000_000
MINUTE = 60 * NANO
DAY = 86_400 * NANO
T0 = int(datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp()) * NANO
QUALITY = DataQuality(status="READY", reasons=(), temporal_basis="observed",
                      input_hash="0" * 64)
PRODUCT = "SHFE.RB"


def day_bars(count: int) -> tuple[MarketBar, ...]:
    bars = []
    for i in range(count):
        close = Decimal(1000 + (7 * i) % 23 + (i % 3))
        bars.append(MarketBar(
            bar_id=f"d{i}", contract_id="RB2610", raw_symbol="rb2610", product=PRODUCT,
            duration_seconds=86400, datetime_nano=T0 + i * DAY, start_nano=T0 + i * DAY,
            end_nano=T0 + (i + 1) * DAY,
            trading_day=date(2026, 1, 5) + timedelta(days=i), session_id=None,
            available_at_nano=T0 + (i + 1) * DAY, imported_at_nano=None,
            open=close, high=close + Decimal(5), low=close - Decimal(5),
            close=close + Decimal(2), volume=Decimal(1000 + 3 * i),
            open_oi=Decimal(50000), close_oi=Decimal(50000 + 3 * i),
            complete=True, expected_minutes=1, actual_minutes=1,
            source="synthetic", quality=QUALITY))
    return tuple(bars)


def minute_bars(count: int, session_id: str = "S1",
                closes: list[Decimal] | None = None) -> tuple[MarketBar, ...]:
    bars = []
    for i in range(count):
        close = closes[i] if closes is not None else Decimal(100 + (5 * i) % 17)
        bars.append(MarketBar(
            bar_id=f"m{i}", contract_id="RB2610", raw_symbol="rb2610", product=PRODUCT,
            duration_seconds=60, datetime_nano=T0 + i * MINUTE,
            start_nano=T0 + i * MINUTE, end_nano=T0 + (i + 1) * MINUTE,
            trading_day=date(2026, 1, 5), session_id=session_id,
            available_at_nano=T0 + (i + 1) * MINUTE, imported_at_nano=None,
            open=close, high=close + Decimal(2), low=close - Decimal(2),
            close=close, volume=Decimal(100 + (i % 5)),
            open_oi=Decimal(900), close_oi=Decimal(900 + i),
            complete=True, expected_minutes=1, actual_minutes=1,
            source="synthetic", quality=QUALITY))
    return tuple(bars)


def minute_calendar() -> tuple[SessionWindow, ...]:
    """Declared calendar matching minute_bars() (session S1 from T0)."""
    return (SessionWindow(
        session_id="S1", product=PRODUCT, trading_day=date(2026, 1, 5),
        start_nano=T0, end_nano=T0 + 400 * MINUTE, kind="DAY",
        calendar_version="cal-test-v1", evidence_status="SCENARIO"),)


def daily_calendar(count: int) -> tuple[SessionWindow, ...]:
    """One declared open trading day per calendar day from 2026-01-05."""
    return tuple(
        SessionWindow(
            session_id=f"DL{i}", product=PRODUCT,
            trading_day=date(2026, 1, 5) + timedelta(days=i),
            start_nano=T0 + i * DAY, end_nano=T0 + i * DAY + 3600,
            kind="DAY", calendar_version="cal-test-v1",
            evidence_status="SCENARIO")
        for i in range(count))


def to_contract_bars(bars: tuple[MarketBar, ...]) -> list[ContractBar]:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return [ContractBar(
        symbol=bar.raw_symbol, duration_seconds=86400, datetime_nano=bar.datetime_nano,
        ts=epoch + timedelta(microseconds=bar.datetime_nano // 1000),
        source="synthetic", trading_day=bar.trading_day, open=bar.open, high=bar.high,
        low=bar.low, close=bar.close, volume=bar.volume, open_oi=bar.open_oi,
        close_oi=bar.close_oi) for bar in bars]


def compare_status(legacy: float | None, v2: Decimal | float | None) -> str:
    """MATCH / DIFFERENT / UNAVAILABLE with one-sided unavailability split."""
    if legacy is None and v2 is None:
        return "UNAVAILABLE"
    if legacy is None or v2 is None:
        return "UNAVAILABLE_ONE_SIDED"
    legacy_f = float(legacy)
    v2_f = float(v2)
    error = abs(legacy_f - v2_f)
    if error <= 1e-10 or error <= 1e-9 * max(abs(legacy_f), abs(v2_f)):
        return "MATCH"
    return "DIFFERENT"


def test_baseline_legacy_daily_matches_v1_read_only() -> None:
    bars = day_bars(80)
    snapshots = legacy_daily_baseline("RB2610", bars,
                                    decision_nano=T0 + 200 * DAY,
                                    sessions=daily_calendar(80))
    assert len(snapshots) == len(bars)
    rows = v1_build_factor_rows("RB2610", "rb2610", PRODUCT, to_contract_bars(bars))
    assert len(rows) == len(bars)
    match = differ = unavailable = one_sided = 0
    for snapshot, row in zip(snapshots, rows):
        legacy_values = row.values
        for value in snapshot.values:
            assert value.quality.reasons[0] == LEGACY_LABEL
            name = value.factor_key.split(":")[1]
            status = compare_status(legacy_values[name], value.value)
            if status == "MATCH":
                match += 1
            elif status == "DIFFERENT":
                differ += 1
                raise AssertionError(f"legacy mismatch {name}: {legacy_values[name]} vs {value.value}")
            elif status == "UNAVAILABLE":
                unavailable += 1
            else:
                one_sided += 1
                raise AssertionError(f"one-sided unavailability {name}")
    assert differ == 0 and one_sided == 0
    assert match > 0 and unavailable > 0  # warmup rows stay unavailable on both sides


def test_baseline_legacy_snapshot_identity_and_labels() -> None:
    bars = day_bars(70)
    snapshots = legacy_daily_baseline("RB2610", bars,
                                    decision_nano=T0 + 200 * DAY,
                                    sessions=daily_calendar(80))
    last = snapshots[-1]
    assert last.duration_seconds == 86400
    assert last.session_kind == "DAILY"
    assert last.session_id == f"DAILY:{PRODUCT}:{last.trading_day.isoformat()}"
    assert last.trading_day == bars[-1].trading_day
    assert last.decision_nano == bars[-1].end_nano
    assert last.available_at_nano <= last.decision_nano
    from ak_trading.v2.factor_specs import LEGACY_COMPARISON_NAMES
    assert {value.factor_key for value in last.values} == {
        baseline_factor_key(name, 86400) for name in LEGACY_COMPARISON_NAMES}
    assert last.quality.reasons[0] == LEGACY_LABEL
    # Warmup rows exist and keep every registered factor row (v1 always
    # produces the current close itself; every windowed factor is null).
    first = snapshots[0]
    assert first.quality.status == "WARMUP"
    assert len(first.values) == 14  # v1 comparison rows stay at fourteen (006)
    close_key = baseline_factor_key("close", 86400)
    assert all(value.value is None for value in first.values
               if value.factor_key != close_key)
    assert first.values is not None
    # Incomplete daily bars never produce comparison snapshots.
    incomplete = bars[-1]
    patched = MarketBar(
        **{**incomplete.__dict__, "complete": False}) if False else MarketBar(
        bar_id=incomplete.bar_id, contract_id=incomplete.contract_id,
        raw_symbol=incomplete.raw_symbol, product=incomplete.product,
        duration_seconds=incomplete.duration_seconds,
        datetime_nano=incomplete.datetime_nano, start_nano=incomplete.start_nano,
        end_nano=incomplete.end_nano, trading_day=incomplete.trading_day,
        session_id=incomplete.session_id, available_at_nano=incomplete.available_at_nano,
        imported_at_nano=incomplete.imported_at_nano, open=incomplete.open,
        high=incomplete.high, low=incomplete.low, close=incomplete.close,
        volume=incomplete.volume, open_oi=incomplete.open_oi,
        close_oi=incomplete.close_oi, complete=False,
        expected_minutes=incomplete.expected_minutes,
        actual_minutes=incomplete.actual_minutes, source=incomplete.source,
        quality=incomplete.quality)
    assert len(legacy_daily_baseline("RB2610", bars[:-1] + (patched,),
                                 decision_nano=T0 + 200 * DAY,
                                 sessions=daily_calendar(80))) == len(bars) - 1
    with pytest.raises(Exception):
        legacy_daily_baseline("RB2610", minute_bars(30),  # daily adapter rejects minutes
                          decision_nano=T0 + DAY, sessions=minute_calendar())


def test_baseline_minute_version_same_windows() -> None:
    bars = minute_bars(80)
    decision = T0 + 80 * MINUTE
    values = {value.factor_key: value for value in baseline_values_v2(bars, decision,
                                    sessions=minute_calendar())}
    assert set(values) == {baseline_factor_key(name, 60) for name in BASELINE_NAMES}
    closes = [bar.close for bar in bars]
    # close / ma windows
    assert values[baseline_factor_key("close", 60)].value == closes[-1]
    assert values[baseline_factor_key("ma20", 60)].value == sum(closes[-20:], Decimal(0)) / Decimal(20)
    assert values[baseline_factor_key("ma60", 60)].value == sum(closes[-60:], Decimal(0)) / Decimal(60)
    # prior channel excludes the current bar
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    assert values[baseline_factor_key("prior_high20", 60)].value == max(highs[-21:-1])
    assert values[baseline_factor_key("prior_low20", 60)].value == min(lows[-21:-1])
    # volume ratio: current / prior-20 mean, without -1
    volumes = [bar.volume for bar in bars]
    expected_ratio = volumes[-1] / (sum(volumes[-21:-1], Decimal(0)) / Decimal(20))
    assert values[baseline_factor_key("volume_ratio20", 60)].value == expected_ratio
    # OI change over five bars
    ois = [bar.close_oi for bar in bars]
    assert values[baseline_factor_key("oi_change5", 60)].value == ois[-1] / ois[-6] - Decimal(1)
    # vol20: sample std of the last 20 simple returns, unannualized
    returns = [float(b / a) - 1.0 for a, b in zip(closes[-21:], closes[-20:])]
    vol = values[baseline_factor_key("vol20", 60)]
    assert vol.value == pytest.approx(statistics.stdev(returns), rel=1e-9, abs=1e-10)
    # zscore20 references the prior 20 closes excluding the current bar
    reference = [float(value) for value in closes[-21:-1]]
    expected_z = (float(closes[-1]) - statistics.fmean(reference)) / statistics.stdev(reference)
    assert values[baseline_factor_key("zscore20", 60)].value == pytest.approx(
        expected_z, rel=1e-9, abs=1e-10)
    # atr14 is the arithmetic mean of the last 14 true ranges
    trs = [max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
           for a, b in zip(bars[-15:-1], bars[-14:])]
    expected_atr = sum(trs, Decimal(0)) / Decimal(14)
    assert values[baseline_factor_key("atr14", 60)].value == expected_atr
    atr = expected_atr
    assert values[baseline_factor_key("ma_spread_atr", 60)].value == (
        sum(closes[-20:], Decimal(0)) / Decimal(20)
        - sum(closes[-60:], Decimal(0)) / Decimal(60)) / atr
    assert values[baseline_factor_key("distance_high_atr", 60)].value == (
        max(highs[-21:-1]) - closes[-1]) / atr
    assert values[baseline_factor_key("distance_low_atr", 60)].value == (
        closes[-1] - min(lows[-21:-1])) / atr


def test_baseline_zscore_uses_prior_window_not_including_current() -> None:
    closes = [Decimal(100 + (i % 4) * 3) for i in range(21)]
    bars = minute_bars(1)[:0] + tuple(
        MarketBar(bar_id=f"z{i}", contract_id="RB2610", raw_symbol="rb2610",
                  product=PRODUCT, duration_seconds=60, datetime_nano=T0 + i * MINUTE,
                  start_nano=T0 + i * MINUTE, end_nano=T0 + (i + 1) * MINUTE,
                  trading_day=date(2026, 1, 5), session_id="S1",
                  available_at_nano=T0 + (i + 1) * MINUTE, imported_at_nano=None,
                  open=value, high=value + Decimal(1), low=value - Decimal(1),
                  close=value, volume=Decimal(10), open_oi=Decimal(100),
                  close_oi=Decimal(100), complete=True, expected_minutes=1,
                  actual_minutes=1, source="synthetic", quality=QUALITY)
        for i, value in enumerate(closes))
    decision = T0 + 21 * MINUTE
    values = {value.factor_key: value for value in baseline_values_v2(bars, decision,
                                    sessions=minute_calendar())}
    prior = [float(v) for v in closes[:-1]]
    prior_mean = statistics.fmean(prior)
    prior_std = statistics.stdev(prior)
    including = [float(v) for v in closes]
    including_mean = statistics.fmean(including)
    including_std = statistics.stdev(including)
    expected_prior = (float(closes[-1]) - prior_mean) / prior_std
    expected_including = (float(closes[-1]) - including_mean) / including_std
    actual = values[baseline_factor_key("zscore20", 60)].value
    assert actual == pytest.approx(expected_prior, rel=1e-9, abs=1e-10)
    assert expected_prior != pytest.approx(expected_including, rel=1e-6, abs=1e-8)


def test_baseline_prior_channel_excludes_current_breakout_bar() -> None:
    bars = list(minute_bars(21))
    breakout = MarketBar(
        bar_id="breakout", contract_id="RB2610", raw_symbol="rb2610", product=PRODUCT,
        duration_seconds=60, datetime_nano=T0 + 21 * MINUTE, start_nano=T0 + 21 * MINUTE,
        end_nano=T0 + 22 * MINUTE, trading_day=date(2026, 1, 5), session_id="S1",
        available_at_nano=T0 + 22 * MINUTE, imported_at_nano=None,
        open=Decimal(500), high=Decimal(520), low=Decimal(495), close=Decimal(510),
        volume=Decimal(99), open_oi=Decimal(1000), close_oi=Decimal(1000),
        complete=True, expected_minutes=1, actual_minutes=1, source="synthetic",
        quality=QUALITY)
    bars.append(breakout)
    values = {value.factor_key: value for value in
              baseline_values_v2(tuple(bars), T0 + 22 * MINUTE,
                              sessions=minute_calendar())}
    prior_highs = max(bar.high for bar in bars[:-1])
    prior_lows = min(bar.low for bar in bars[:-1])
    assert values[baseline_factor_key("prior_high20", 60)].value == prior_highs
    assert values[baseline_factor_key("prior_low20", 60)].value == prior_lows
    assert prior_highs < Decimal(520)  # the breakout high is not its own channel


def test_baseline_er_absolute_and_volume_ratio_shift() -> None:
    # Doc hand case: volumes 10/20/30 prior, current 40 -> ratio 2 (no -1).
    bars = list(minute_bars(4))
    volumes = [Decimal(10), Decimal(20), Decimal(30), Decimal(40)]
    patched = tuple(
        MarketBar(bar_id=bar.bar_id, contract_id=bar.contract_id,
                  raw_symbol=bar.raw_symbol, product=bar.product,
                  duration_seconds=bar.duration_seconds,
                  datetime_nano=bar.datetime_nano, start_nano=bar.start_nano,
                  end_nano=bar.end_nano, trading_day=bar.trading_day,
                  session_id=bar.session_id, available_at_nano=bar.available_at_nano,
                  imported_at_nano=bar.imported_at_nano, open=bar.open, high=bar.high,
                  low=bar.low, close=bar.close, volume=volume,
                  open_oi=bar.open_oi, close_oi=bar.close_oi, complete=True,
                  expected_minutes=1, actual_minutes=1, source=bar.source,
                  quality=bar.quality)
        for bar, volume in zip(bars, volumes))
    values = {value.factor_key: value for value in
              baseline_values_v2(patched, T0 + 4 * MINUTE,
                              sessions=minute_calendar())}
    ratio = values[baseline_factor_key("volume_ratio20", 60)]
    assert ratio.quality.status == "WARMUP"  # 21 volumes are required
    longer = patched + tuple(
        MarketBar(bar_id=f"x{i}", contract_id="RB2610", raw_symbol="rb2610",
                  product=PRODUCT, duration_seconds=60,
                  datetime_nano=T0 + (4 + i) * MINUTE, start_nano=T0 + (4 + i) * MINUTE,
                  end_nano=T0 + (5 + i) * MINUTE, trading_day=date(2026, 1, 5),
                  session_id="S1", available_at_nano=T0 + (5 + i) * MINUTE,
                  imported_at_nano=None, open=Decimal(100), high=Decimal(101),
                  low=Decimal(99), close=Decimal(100), volume=Decimal(10),
                  open_oi=Decimal(100), close_oi=Decimal(100), complete=True,
                  expected_minutes=1, actual_minutes=1, source="synthetic",
                  quality=QUALITY) for i in range(17))
    values = {value.factor_key: value for value in
              baseline_values_v2(longer, T0 + 21 * MINUTE,
                              sessions=minute_calendar())}
    # Reference window is the prior 20 volumes; the ratio has no "-1", so
    # on the doc's 10/20/30-then-40 window it is exactly one above VOI001.
    assert longer[-1].volume == Decimal(10)
    ratio = values[baseline_factor_key("volume_ratio20", 60)]
    reference_mean = sum((bar.volume for bar in longer[:-1]), Decimal(0)) / Decimal(20)
    assert ratio.value == longer[-1].volume / reference_mean
    assert ratio.value + Decimal(1) == longer[-1].volume / reference_mean + Decimal(1)
    # ER is unsigned; MOM010 on the same closes carries the sign.
    rising_closes = [Decimal(100 + i) for i in range(21)]
    falling_closes = list(reversed(rising_closes))
    rising = minute_bars(21, closes=rising_closes)
    falling = minute_bars(21, closes=falling_closes)
    rising_values = {value.factor_key: value for value in
                     baseline_values_v2(rising, T0 + 21 * MINUTE,
                              sessions=minute_calendar())}
    er = rising_values[baseline_factor_key("er20", 60)]
    closes = [bar.close for bar in rising]
    displacement = abs(closes[-1] - closes[-21])
    path = sum((abs(b - a) for a, b in zip(closes, closes[1:])), Decimal(0))
    assert er.value == displacement / path
    assert er.value >= 0
    falling_values = {value.factor_key: value for value in
                      baseline_values_v2(falling, T0 + 21 * MINUTE,
                               sessions=minute_calendar())}
    er_down = falling_values[baseline_factor_key("er20", 60)]
    assert er_down.value == er.value  # absolute value: direction-free
    from ak_trading.v2.contracts import FactorSpec
    mom010 = FactorSpec(
        factor_key="MOM010:hand:d60:n20", source_factor_id="MOM010",
        operator_version="m2-v2.1.0", parameter_version="hand-n20",
        duration_seconds=60, lookback=21, parameters={"n": 20},
        unit="signed_efficiency_ratio", required_fields=("close",),
        formula="(P-lag(P,n))/sum(abs(diff(P)),n)", source_sha256="0" * 64)
    signed_up = compute_factor(mom010, rising, T0 + 21 * MINUTE,
                               sessions=minute_calendar()).value
    signed_down = compute_factor(mom010, falling, T0 + 21 * MINUTE,
                                 sessions=minute_calendar()).value
    assert signed_up == er.value and signed_down == -er.value


def test_baseline_atr14_arithmetic_not_wilder() -> None:
    bars = minute_bars(30)
    decision = T0 + 30 * MINUTE
    values = {value.factor_key: value for value in baseline_values_v2(bars, decision,
                                    sessions=minute_calendar())}
    trs = [max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
           for a, b in zip(bars[-15:-1], bars[-14:])]
    arithmetic = sum(trs, Decimal(0)) / Decimal(14)
    wilder, reason = wilder_atr(bars, 14)
    assert reason == ""
    assert values[baseline_factor_key("atr14", 60)].value == arithmetic
    assert wilder != arithmetic  # the two smoothers provably differ here
    assert wilder is not None and arithmetic > 0


def test_baseline_legacy_unavailable_counted_separately() -> None:
    bars = day_bars(10)  # far below the 60-bar slow window
    snapshots = legacy_daily_baseline("RB2610", bars,
                                    decision_nano=T0 + 200 * DAY,
                                    sessions=daily_calendar(80))
    rows = v1_build_factor_rows("RB2610", "rb2610", PRODUCT, to_contract_bars(bars))
    v2 = baseline_values_v2(bars, T0 + 10 * DAY,
                                      sessions=daily_calendar(80))
    v2_values = {value.factor_key: value for value in v2}
    both_unavailable = 0
    from ak_trading.v2.factor_specs import LEGACY_COMPARISON_NAMES
    for name in LEGACY_COMPARISON_NAMES:
        key = baseline_factor_key(name, 86400)
        legacy_side = rows[-1].values[name]
        v2_side = v2_values[key].value
        if legacy_side is None and v2_side is None:
            both_unavailable += 1
            assert v2_values[key].quality.status in ("WARMUP", "MISSING", "INVALID")
    assert both_unavailable > 0
    # The comparison never counts both-empty as a numeric match.
    assert compare_status(None, None) == "UNAVAILABLE"
    assert compare_status(1.0, None) == "UNAVAILABLE_ONE_SIDED"
    assert compare_status(1.0, Decimal("1.0")) == "MATCH"


def test_baseline_warmup_and_zero_boundaries() -> None:
    short = minute_bars(3)
    values = {value.factor_key: value for value in
              baseline_values_v2(short, T0 + 3 * MINUTE,
                              sessions=minute_calendar())}
    assert values[baseline_factor_key("close", 60)].quality.status == "READY"
    assert values[baseline_factor_key("ma20", 60)].quality.status == "WARMUP"
    flat = tuple(
        MarketBar(bar_id=f"f{i}", contract_id="RB2610", raw_symbol="rb2610",
                  product=PRODUCT, duration_seconds=60, datetime_nano=T0 + i * MINUTE,
                  start_nano=T0 + i * MINUTE, end_nano=T0 + (i + 1) * MINUTE,
                  trading_day=date(2026, 1, 5), session_id="S1",
                  available_at_nano=T0 + (i + 1) * MINUTE, imported_at_nano=None,
                  open=Decimal(100), high=Decimal(100), low=Decimal(100),
                  close=Decimal(100), volume=Decimal(10), open_oi=Decimal(10),
                  close_oi=Decimal(10), complete=True, expected_minutes=1,
                  actual_minutes=1, source="synthetic", quality=QUALITY)
        for i in range(65))
    flat_values = {value.factor_key: value for value in
                   baseline_values_v2(flat, T0 + 65 * MINUTE,
                              sessions=minute_calendar())}
    er = flat_values[baseline_factor_key("er20", 60)]
    assert er.value is None and er.quality.status == "INVALID"
    assert "zero_total_path" in er.quality.reasons
    vol = flat_values[baseline_factor_key("vol20", 60)]
    assert vol.value is None and "zero_variance" in vol.quality.reasons
    zscore = flat_values[baseline_factor_key("zscore20", 60)]
    assert zscore.value is None and "zero_variance" in zscore.quality.reasons
    atr = flat_values[baseline_factor_key("atr14", 60)]
    assert atr.value == Decimal(0) and atr.quality.status == "DEGRADED"
    spread = flat_values[baseline_factor_key("ma_spread_atr", 60)]
    assert spread.value is None and "zero_denominator" in spread.quality.reasons


# ---------------------------------------------------------------------------
# Precheck-20260914-r1 regressions (E04/E09/E10)
# ---------------------------------------------------------------------------

def test_precheck_e04_legacy_baseline_respects_input_visibility() -> None:
    """A delayed daily input is not read before it becomes visible (P07)."""
    from dataclasses import replace
    daily = day_bars(32)
    delayed = (replace(daily[0], available_at_nano=T0 + 30 * DAY),) + daily[1:]
    legacy = legacy_daily_baseline("RB2610", delayed,
                                 decision_nano=T0 + 200 * DAY,
                                 sessions=daily_calendar(40))[19]
    ma = next(v for v in legacy.values if ":ma20:" in v.factor_key)
    # At snapshot 19's issue time bar 0 was still invisible: the visible
    # prefix holds 19 bars, so the twenty-bar mean is unavailable.
    assert ma.value is None and ma.quality.status == "WARMUP"
    # Once every input is visible the same window computes normally.
    late = legacy_daily_baseline("RB2610", delayed,
                               decision_nano=T0 + 200 * DAY,
                               sessions=daily_calendar(40))[29]
    ma_late = next(v for v in late.values if ":ma20:" in v.factor_key)
    assert ma_late.value is not None and ma_late.quality.status == "READY"


def test_precheck_e04_unknown_availability_is_never_fabricated() -> None:
    """005 §3: unknown first-seen rows are missing breakpoints, never
    anchors; an anchor with an unknown-visibility window input sees only
    the shorter visible prefix instead of a fabricated visibility time."""
    from dataclasses import replace
    daily = day_bars(2)
    unknown_snapshots = legacy_daily_baseline(
        "RB2610", (replace(daily[0], available_at_nano=None),),
        decision_nano=T0 + 200 * DAY, sessions=daily_calendar(30))
    assert unknown_snapshots == ()  # unknown first-seen never becomes an anchor
    partial = (replace(daily[0], available_at_nano=None),) + tuple(
        replace(bar, available_at_nano=bar.end_nano) for bar in daily[1:2])
    snapshots = legacy_daily_baseline("RB2610", partial,
                                      decision_nano=T0 + 200 * DAY,
                                      sessions=daily_calendar(30))
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    # The anchor's own close is known; the twenty-bar mean is not, because
    # the unknown-visibility predecessor never entered the visible prefix.
    close = next(v for v in snapshot.values if ":close:" in v.factor_key)
    assert close.value is not None and close.quality.status == "READY"
    ma20 = next(v for v in snapshot.values if ":ma20:" in v.factor_key)
    assert ma20.value is None and ma20.quality.status == "WARMUP"


def test_precheck_e09_huge_finite_decimals_keep_all_fourteen_rows() -> None:
    from dataclasses import replace
    bars = minute_bars(60)
    huge = tuple(replace(b, open=Decimal("1e308"), high=Decimal("1e308"),
                         low=Decimal("1e308"), close=Decimal("1e308")) for b in bars)
    values = {value.factor_key.split(":")[1]: value
              for value in baseline_values_v2(huge, huge[-1].end_nano,
                                        sessions=minute_calendar())}
    assert len(values) == 15  # 14 rolling + opening gap, no OverflowError
    assert values["ma20"].value == Decimal("1e308")
    assert values["ma60"].value == Decimal("1e308")
    assert values["close"].value == Decimal("1e308")
    assert values["zscore20"].value is None
    assert "zero_variance" in values["zscore20"].quality.reasons


def test_precheck_e10_atr_dependencies_register_and_report_missing_inputs() -> None:
    from dataclasses import replace
    from ak_trading.v2.factor_specs import baseline_registry_v2
    deps = {spec.source_factor_id: set(spec.required_fields)
            for spec in baseline_registry_v2(60)
            if spec.source_factor_id in ("atr14", "ma_spread_atr",
                                          "distance_high_atr", "distance_low_atr")}
    assert all(fields == {"high", "low", "close"} for fields in deps.values())
    bars = minute_bars(60)
    missing_low = bars[:-1] + (replace(bars[-1], low=None),)
    values = {value.factor_key.split(":")[1]: value
              for value in baseline_values_v2(missing_low, bars[-1].end_nano,
                                        sessions=minute_calendar())}
    for name in ("atr14", "ma_spread_atr", "distance_high_atr", "distance_low_atr"):
        assert values[name].value is None, name
        assert values[name].quality.status == "MISSING", name
        assert "missing_source_value" in values[name].quality.reasons, name
    # A legitimate zero true-range mean stays a distinct zero-denominator.
    flat = tuple(
        replace(b, high=Decimal(100), low=Decimal(100), close=Decimal(100),
                open=Decimal(100)) for b in bars)
    flat_values = {value.factor_key.split(":")[1]: value
                   for value in baseline_values_v2(flat, flat[-1].end_nano,
                                        sessions=minute_calendar())}
    assert "zero_denominator" in flat_values["ma_spread_atr"].quality.reasons
    assert flat_values["atr14"].quality.status == "DEGRADED"


# ---------------------------------------------------------------------------
# 变更申请-006 §2/§7: BASE:opening_gap_points
# ---------------------------------------------------------------------------

def _gap_session_pair(first_open=104, first_close=106, second=(106, 107),
                      prior_closes=(100, 102, 100), available_delay=0):
    """AM(prior closes) + PM(open/close) on one declared day."""
    session_am = SessionWindow(
        session_id="GAP-AM", product=PRODUCT, trading_day=date(2026, 1, 5),
        start_nano=T0, end_nano=T0 + 3 * MINUTE, kind="DAY",
        calendar_version="cal-test-v1", evidence_status="SCENARIO")
    session_pm = SessionWindow(
        session_id="GAP-PM", product=PRODUCT, trading_day=date(2026, 1, 5),
        start_nano=T0 + 100 * MINUTE, end_nano=T0 + 102 * MINUTE, kind="DAY",
        calendar_version="cal-test-v1", evidence_status="SCENARIO")

    def mk(session, slot, open_, close, delay=0):
        start = session.start_nano + slot * MINUTE
        end = start + MINUTE
        return MarketBar(
            bar_id=f"gap-{session.session_id}-{slot}", contract_id="RB2610",
            raw_symbol="rb2610", product=PRODUCT, duration_seconds=60,
            datetime_nano=start, start_nano=start, end_nano=end,
            trading_day=session.trading_day, session_id=session.session_id,
            available_at_nano=end + delay, imported_at_nano=None,
            open=Decimal(open_), high=Decimal(max(open_, close) + 2),
            low=Decimal(min(open_, close) - 2), close=Decimal(close),
            volume=Decimal(10), open_oi=Decimal(100), close_oi=Decimal(100),
            complete=True, expected_minutes=1, actual_minutes=1,
            source="synthetic", quality=QUALITY)

    bars = [mk(session_am, slot, close_, close_)
            for slot, close_ in enumerate(prior_closes)]
    bars.append(mk(session_pm, 0, first_open, first_close, available_delay))
    bars.append(mk(session_pm, 1, second[0], second[1], available_delay))
    return (session_am, session_pm), tuple(bars)


def _gap_value(sessions, bars, decision):
    from ak_trading.v2.factor_specs import baseline_registry_v2
    from ak_trading.v2.factors import build_feature_snapshot
    specs = baseline_registry_v2(60)
    snapshot = build_feature_snapshot(
        "RB2610", bars, decision, specs, None, None, None, sessions)
    return next(v for v in snapshot.values if "opening_gap" in v.factor_key), specs


def test_opening_gap_006_hand_calculations() -> None:
    from ak_trading.v2.factors import build_feature_snapshot
    sessions, bars = _gap_session_pair()
    # First PM bar O=104/C=106 with prior session close 100: gap=4, not 6.
    value, _specs = _gap_value(sessions, bars, T0 + 101 * MINUTE)
    assert value.value == Decimal(4) and value.quality.status == "READY"
    # The second bar O=106/C=107 repeats the same session gap.
    value2, _ = _gap_value(sessions, bars, T0 + 102 * MINUTE)
    assert value2.value == Decimal(4) and value2.quality.status == "READY"
    # Mirror open 96 gives -4; an open equal to the prior close is a known 0.
    sessions_m, bars_m = _gap_session_pair(first_open=96, first_close=99)
    value_m, _ = _gap_value(sessions_m, bars_m, T0 + 101 * MINUTE)
    assert value_m.value == Decimal(-4) and value_m.quality.status == "READY"
    sessions_z, bars_z = _gap_session_pair(first_open=100, first_close=103)
    value_z, _ = _gap_value(sessions_z, bars_z, T0 + 101 * MINUTE)
    assert value_z.value == Decimal(0) and value_z.quality.status == "READY"


def test_opening_gap_006_visibility_timing() -> None:
    from ak_trading.v2.factors import build_feature_snapshot
    sessions, bars = _gap_session_pair(available_delay=3 * (10 ** 9))
    # First bar covers 09:00–09:01 with availability 09:01:03.
    early, _ = _gap_value(sessions, bars, T0 + 100 * MINUTE + 30 * (10 ** 9))
    assert early.value is None and early.quality.status == "MISSING"
    not_yet, _ = _gap_value(sessions, bars, T0 + 101 * MINUTE + 2 * (10 ** 9))
    assert not_yet.value is None and not_yet.quality.status == "MISSING"
    ready, _ = _gap_value(sessions, bars, T0 + 101 * MINUTE + 3 * (10 ** 9))
    assert ready.value == Decimal(4) and ready.quality.status == "READY"
    # A prior close visible one nanosecond late keeps the row missing.
    from dataclasses import replace
    decision = T0 + 101 * MINUTE + 3 * (10 ** 9)
    delayed = tuple(
        replace(bar, available_at_nano=decision + 1)
        if bar.session_id == "GAP-AM" and bar.start_nano == T0 + 2 * MINUTE
        else bar for bar in bars)
    value, _ = _gap_value(sessions, delayed, decision)
    assert value.value is None and value.quality.status == "MISSING"
    assert "prior_session_close_missing" in value.quality.reasons


def test_opening_gap_006_calendar_semantics() -> None:
    from ak_trading.v2.factor_specs import baseline_registry_v2
    from ak_trading.v2.factors import build_feature_snapshot
    sessions, bars = _gap_session_pair()
    # Bars outside the declared calendar are calendar_unknown (005 §3).
    value, _ = _gap_value((sessions[1],), bars, T0 + 101 * MINUTE)
    assert value.value is None and value.quality.status == "MISSING"
    assert "calendar_unknown" in value.quality.reasons
    # A declared previous session without any bar is not skipped.
    empty_am = SessionWindow(
        session_id="GAP-AM", product=PRODUCT, trading_day=date(2026, 1, 5),
        start_nano=T0, end_nano=T0 + 3 * MINUTE, kind="DAY",
        calendar_version="cal-test-v1", evidence_status="SCENARIO")
    only_pm = tuple(bar for bar in bars if bar.session_id == "GAP-PM")
    value, _ = _gap_value((empty_am, sessions[1]), only_pm, T0 + 101 * MINUTE)
    assert value.value is None and value.quality.status == "MISSING"
    assert "prior_session_close_missing" in value.quality.reasons
    # Missing first planned PM slot cannot fall back to the second bar.
    missing_first = tuple(bar for bar in bars if not (
        bar.session_id == "GAP-PM" and bar.start_nano == sessions[1].start_nano))
    value, _ = _gap_value(sessions, missing_first, T0 + 102 * MINUTE)
    assert value.value is None and value.quality.status == "MISSING"
    # Empty calendar evidence is calendar_unknown, never a silent READY
    # (checked through compute_factor; the snapshot identity gate itself
    # refuses an undeclared anchor session, which is also asserted).
    specs = baseline_registry_v2(60)
    gap_spec = next(s for s in specs
                    if s.source_factor_id == "BASE:opening_gap_points")
    with pytest.raises(Exception, match="session_identity_missing"):
        build_feature_snapshot("RB2610", bars, T0 + 101 * MINUTE, specs,
                               None, None, None, ())
    from ak_trading.v2.factors import compute_factor
    direct = compute_factor(gap_spec, bars, T0 + 101 * MINUTE, sessions=())
    assert direct.value is None and direct.quality.status == "MISSING"
    assert "calendar_unknown" in direct.quality.reasons




def test_opening_gap_006_daily_endpoints() -> None:
    from ak_trading.v2.factors import build_feature_snapshot
    from ak_trading.v2.factor_specs import baseline_registry_v2
    day1 = date(2026, 1, 5)
    day2 = date(2026, 1, 6)
    sessions = tuple(
        SessionWindow(session_id=f"GD{i}", product=PRODUCT, trading_day=day,
                      start_nano=T0 + i * DAY, end_nano=T0 + i * DAY + 3600,
                      kind="DAY", calendar_version="cal-test-v1",
                      evidence_status="SCENARIO")
        for i, day in enumerate((day1, day2)))
    bars = []
    for i, (open_, close) in enumerate(((100, 100), (104, 106))):
        bars.append(MarketBar(
            bar_id=f"gd{i}", contract_id="RB2610", raw_symbol="rb2610",
            product=PRODUCT, duration_seconds=86400, datetime_nano=T0 + i * DAY,
            start_nano=T0 + i * DAY, end_nano=T0 + i * DAY + 3600,
            trading_day=day1 if i == 0 else day2, session_id=None,
            available_at_nano=T0 + i * DAY + 3600, imported_at_nano=None,
            open=Decimal(open_), high=Decimal(max(open_, close) + 2),
            low=Decimal(min(open_, close) - 2), close=Decimal(close),
            volume=Decimal(10), open_oi=Decimal(100), close_oi=Decimal(100),
            complete=True, expected_minutes=1, actual_minutes=1,
            source="synthetic", quality=QUALITY))
    specs = baseline_registry_v2(86400)
    snapshot = build_feature_snapshot(
        "RB2610", tuple(bars), bars[-1].end_nano, specs, None, None, None, sessions)
    gap = next(v for v in snapshot.values if "opening_gap" in v.factor_key)
    assert gap.value == Decimal(4) and gap.quality.status == "READY"
    early = build_feature_snapshot(
        "RB2610", (bars[0],), bars[0].end_nano, specs, None, None, None, sessions)
    gap = next(v for v in early.values if "opening_gap" in v.factor_key)
    assert gap.value is None and gap.quality.status == "MISSING"


def test_opening_gap_006_registration_binding() -> None:
    from ak_trading.v2.factor_specs import (
        OPENING_GAP_DEFINITION,
        baseline_registry_v2,
        opening_gap_definition_sha256,
    )
    for duration in (60, 300, 900, 1800, 3600, 86400):
        spec = {s.source_factor_id: s for s in baseline_registry_v2(duration)}[
            "BASE:opening_gap_points"]
        assert spec.factor_key == f"BASE:opening_gap_points:v2.1.0:d{duration}"
        assert spec.operator_version == "opening-gap-v2.1.0"
        assert spec.parameter_version == "session-pair-v1"
        assert spec.lookback == 2
        assert spec.unit == "price_points"
        assert spec.required_fields == ("open", "close")
        assert spec.formula == "session_open - prior_session_close"
        assert spec.source_sha256 == opening_gap_definition_sha256()
        assert spec.parameters == {
            "return_kind": "not_applicable", "std_ddof": "not_applicable",
            "include_current": True, "window_unit": "SESSION_PAIR",
            "annualization": "not_applicable",
            "annualization_factor": "not_applicable",
            "session_boundary": "IMMEDIATE_PREVIOUS_SESSION",
            "zero_policy": "KEEP_VALID_ZERO",
            "value_scope": "CURRENT_SESSION",
            "prior_close_anchor": "EXACT_SESSION_END",
        }
    assert OPENING_GAP_DEFINITION["definition_id"] == "BASE:opening_gap_points:v2.1.0"


def test_precheck_fr2_opening_gap_name_in_warmup_and_unknown_rows() -> None:
    """F-R2: the fifteenth baseline keeps its row in every unavailable set.

    ``BASELINE_NAMES`` stores the short name while the opening-gap spec
    registers ``source_factor_id="BASE:opening_gap_points"``; warmup and
    calendar-unknown row sets must resolve both without KeyError and the
    public wrapper must not duplicate the gap row.
    """
    session = SessionWindow(
        session_id="FR2", product=PRODUCT, trading_day=date(2026, 1, 5),
        start_nano=T0, end_nano=T0 + 30 * MINUTE, kind="DAY",
        calendar_version="cal-test-v1", evidence_status="SCENARIO")
    bar = MarketBar(
        bar_id="fr2", contract_id="RB2610", raw_symbol="rb2610", product=PRODUCT,
        duration_seconds=60, datetime_nano=T0, start_nano=T0,
        end_nano=T0 + MINUTE, trading_day=date(2026, 1, 5),
        session_id="FR2", available_at_nano=T0 + 2 * MINUTE, imported_at_nano=None,
        open=Decimal(4100), high=Decimal(4102), low=Decimal(4098),
        close=Decimal(4100), volume=Decimal(10), open_oi=Decimal(1000),
        close_oi=Decimal(1000), complete=True, expected_minutes=1,
        actual_minutes=1, source="synthetic", quality=QUALITY)
    # No visible bar yet -> planned anchor completed, observation missing.
    rows = baseline_values_v2((bar,), T0 + MINUTE, sessions=(session,))
    assert len(rows) == 15
    assert len({value.factor_key for value in rows}) == 15
    gap = next(v for v in rows if "opening_gap" in v.factor_key)
    assert gap.value is None and gap.quality.status == "MISSING"
    # Empty calendar -> calendar_unknown for all fifteen rows.
    rows = baseline_values_v2((bar,), T0 + 3 * MINUTE, sessions=())
    assert len(rows) == 15
    assert all("calendar_unknown" in v.quality.reasons for v in rows)
    # ok path still appends exactly one gap row (fifteen total, no dupes).
    bars = tuple(
        MarketBar(bar_id=f"fr2-{i}", contract_id="RB2610", raw_symbol="rb2610",
                  product=PRODUCT, duration_seconds=60,
                  datetime_nano=T0 + i * MINUTE, start_nano=T0 + i * MINUTE,
                  end_nano=T0 + (i + 1) * MINUTE, trading_day=date(2026, 1, 5),
                  session_id="FR2", available_at_nano=T0 + (i + 1) * MINUTE,
                  imported_at_nano=None, open=Decimal(4100 + i),
                  high=Decimal(4102 + i), low=Decimal(4098 + i),
                  close=Decimal(4100 + i), volume=Decimal(10),
                  open_oi=Decimal(1000), close_oi=Decimal(1000), complete=True,
                  expected_minutes=1, actual_minutes=1, source="synthetic",
                  quality=QUALITY) for i in range(3))
    rows = baseline_values_v2(bars, bars[-1].end_nano, sessions=(session,))
    assert len(rows) == 15
    assert len({value.factor_key for value in rows}) == 15
