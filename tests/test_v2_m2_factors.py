"""M2-02/03/04: hand-computed values, warmup and zero/edge discipline.

The ``-k momentum`` / ``-k volatility`` / ``-k volume_oi`` subgroups match
the acceptance commands of the M2 task table; every hand case comes from
M2 §7.  Float statistics allow 1e-10 absolute / 1e-9 relative error; price,
volume/OI and path arithmetic is exact ``Decimal``.
"""
from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ak_trading.v2.contracts import DataQuality, FactorSpec, MarketBar, SessionWindow
from ak_trading.v2.factor_specs import DAILY_DURATION, factor_registry_v2
from ak_trading.v2.factors import FactorsError, compute_factor

NANO = 1_000_000_000
MINUTE = 60 * NANO
DAY = 86_400 * NANO
T0 = int(datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc).timestamp()) * NANO
QUALITY = DataQuality(status="READY", reasons=(), temporal_basis="observed",
                      input_hash="0" * 64)


def daily_sessions(count: int) -> tuple[SessionWindow, ...]:
    """One declared trading day per calendar day (declared open days)."""
    return tuple(
        SessionWindow(session_id=f"DD{i}", product="SHFE.RB",
                      trading_day=date(2026, 1, 5) + timedelta(days=i),
                      start_nano=T0 + i * DAY,
                      end_nano=T0 + i * DAY + 3600,
                      kind="DAY", calendar_version="cal-test-v1",
                      evidence_status="SCENARIO")
        for i in range(count))


def minute_session(session_id: str, day: date, slots: int, kind: str = "DAY",
                   start_hour: int = 1) -> SessionWindow:
    start = int(datetime(day.year, day.month, day.day, start_hour, 0,
                         tzinfo=timezone.utc).timestamp()) * NANO
    return SessionWindow(session_id=session_id, product="SHFE.RB", trading_day=day,
                         start_nano=start, end_nano=start + slots * MINUTE, kind=kind,
                         calendar_version="cal-test-v1", evidence_status="VERIFIED")

DEFAULT_SESSIONS = (minute_session("S1", date(2026, 1, 5), 600),)


_AUTO = object()


def bar(index: int, *, close: Decimal | int | None = None, open_: object = _AUTO,
        high: object = _AUTO, low: object = _AUTO,
        volume: Decimal | int | None = None, close_oi: Decimal | int | None = None,
        session: SessionWindow | None = None, contract: str = "RB2610",
        start: int | None = None, complete: bool = True,
        available_delay: int = 0) -> MarketBar:
    """Synthetic minute bar; ``None`` explicitly means a missing field."""
    close_v = None if close is None else Decimal(close)
    open_v = close_v if open_ is _AUTO else (None if open_ is None else Decimal(open_))
    high_v = ((close_v + 1 if close_v is not None else None) if high is _AUTO
              else (None if high is None else Decimal(high)))
    low_v = ((close_v - 1 if close_v is not None else None) if low is _AUTO
             else (None if low is None else Decimal(low)))
    anchor = session or minute_session("S1", date(2026, 1, 5), 600)
    start_nano = start if start is not None else anchor.start_nano + index * MINUTE
    end_nano = start_nano + MINUTE
    return MarketBar(
        bar_id=f"{contract}|{start_nano}",
        contract_id=contract,
        raw_symbol="rb2610",
        product="SHFE.RB",
        duration_seconds=60,
        datetime_nano=start_nano,
        start_nano=start_nano,
        end_nano=end_nano,
        trading_day=anchor.trading_day,
        session_id=anchor.session_id,
        available_at_nano=end_nano + available_delay,
        imported_at_nano=None,
        open=open_v,
        high=high_v,
        low=low_v,
        close=close_v,
        volume=None if volume is None else Decimal(volume),
        open_oi=None if close_oi is None else Decimal(close_oi),
        close_oi=None if close_oi is None else Decimal(close_oi),
        complete=complete,
        expected_minutes=1,
        actual_minutes=1,
        source="synthetic",
        quality=QUALITY,
    )


def spec(factor_id: str, n: int | None = None, duration: int = 60,
         extra: dict | None = None) -> FactorSpec:
    parameters: dict = {"n": n} if n is not None else {}
    if factor_id == "REV001" and n is not None:
        parameters = {"n": n, "k": n, "alias": "k_equals_n"}
    if extra:
        parameters = {**parameters, **extra}
    registry = {s.source_factor_id: s for s in factor_registry_v2(duration)}
    template = registry[factor_id]
    return FactorSpec(
        factor_key=f"{factor_id}:hand:d{duration}:{n or 'instant'}",
        source_factor_id=factor_id,
        operator_version=template.operator_version,
        parameter_version=f"hand-n{n}" if n is not None else "hand-instant",
        duration_seconds=duration,
        lookback=(n + 1) if n is not None else 1,
        parameters=parameters or dict(template.parameters),
        unit=template.unit,
        required_fields=template.required_fields,
        formula=template.formula,
        source_sha256=template.source_sha256,
    )


def closes_bars(closes: list[Decimal], **kwargs) -> tuple[MarketBar, ...]:
    return tuple(bar(i, close=value, **kwargs) for i, value in enumerate(closes))


def assert_close_float(actual: float, expected: float) -> None:
    assert abs(actual - expected) <= 1e-10 or \
        abs(actual - expected) <= 1e-9 * max(abs(actual), abs(expected))


# ---------------------------------------------------------------------------
# -k momentum : MOM001 / MOM003 / MOM010 / REV001
# ---------------------------------------------------------------------------

def test_momentum_mom001_and_rev001_hand_values() -> None:
    bars = closes_bars([Decimal(100), Decimal(110), Decimal(121)])
    decision = T0 + 3 * MINUTE
    momentum = compute_factor(spec("MOM001", 2), bars, decision,
                               sessions=DEFAULT_SESSIONS)
    assert momentum.quality.status == "READY"
    assert isinstance(momentum.value, float)
    assert_close_float(momentum.value, math.log(1.21))  # 0.1906203596...
    reversion = compute_factor(spec("REV001", 2), bars, decision,
                               sessions=DEFAULT_SESSIONS)
    assert_close_float(reversion.value, -math.log(1.21))
    # REV001 is exactly the negated cumulative return: no hidden second
    # sign, never a current z-score.
    assert_close_float(reversion.value, -momentum.value)
    assert momentum.available_at_nano == decision
    assert momentum.quality.input_hash != "0" * 64


def test_momentum_mom003_normalized_hand_case() -> None:
    bars = closes_bars([Decimal(100), Decimal(110), Decimal(99)])
    r1, r2 = math.log(1.1), math.log(0.9)
    value = compute_factor(spec("MOM003", 2), bars, T0 + 3 * MINUTE,
                           sessions=DEFAULT_SESSIONS).value
    assert_close_float(value, (r1 + r2) / abs(r1 - r2))
    # Cross-check against the frozen formula shape.
    returns = [r1, r2]
    denominator = statistics.stdev(returns) * math.sqrt(2)
    assert_close_float(value, math.fsum(returns) / denominator)


def test_momentum_mom003_zero_std_returns_none_invalid() -> None:
    # Identical returns ln(1.1), ln(1.1): momentum is ln(1.21) but the
    # sample standard deviation is zero, so no infinite trend is emitted.
    bars = closes_bars([Decimal(100), Decimal(110), Decimal(121)])
    value = compute_factor(spec("MOM003", 2), bars, T0 + 3 * MINUTE,
                           sessions=DEFAULT_SESSIONS)
    assert value.value is None
    assert value.quality.status == "INVALID"
    assert value.quality.reasons == ("zero_variance",)
    momentum = compute_factor(spec("MOM001", 2), bars, T0 + 3 * MINUTE,
                               sessions=DEFAULT_SESSIONS)
    assert_close_float(momentum.value, math.log(1.21))


def test_momentum_mom010_signed_efficiency_ratio_hand() -> None:
    rising = closes_bars([Decimal(100), Decimal(102), Decimal(101), Decimal(105)])
    falling = closes_bars([Decimal(105), Decimal(101), Decimal(102), Decimal(100)])
    decision = T0 + 4 * MINUTE
    up = compute_factor(spec("MOM010", 3), rising, decision,
                        sessions=DEFAULT_SESSIONS)
    down = compute_factor(spec("MOM010", 3), falling, decision,
                           sessions=DEFAULT_SESSIONS)
    assert up.value == Decimal(5) / Decimal(7)
    assert down.value == Decimal(-5) / Decimal(7)
    assert down.value == -up.value  # absolute values agree, signs carry direction
    flat = closes_bars([Decimal(100)] * 4)
    value = compute_factor(spec("MOM010", 3), flat, decision,
                            sessions=DEFAULT_SESSIONS)
    assert value.value is None
    assert value.quality.status == "INVALID"
    assert value.quality.reasons == ("zero_total_path",)


def test_momentum_daily_spec_uses_twenty_day_window() -> None:
    registry = {s.source_factor_id: s for s in factor_registry_v2(DAILY_DURATION)}
    mom = registry["MOM001"]
    daily = []
    for i in range(21):
        day = _day(i)
        start = T0 + i * DAY
        daily.append(MarketBar(
            bar_id=f"d{i}", contract_id="RB2610", raw_symbol="rb2610", product="SHFE.RB",
            duration_seconds=86400, datetime_nano=start, start_nano=start,
            end_nano=start + DAY, trading_day=day, session_id=None,
            available_at_nano=start + DAY, imported_at_nano=None,
            open=Decimal(100 + i), high=Decimal(101 + i), low=Decimal(99 + i),
            close=Decimal(100 + i), volume=Decimal(10), open_oi=Decimal(100),
            close_oi=Decimal(100), complete=True, expected_minutes=1, actual_minutes=1,
            source="synthetic", quality=QUALITY))
    decision = T0 + 21 * DAY
    value = compute_factor(mom, tuple(daily), decision,
                           sessions=daily_sessions(22))
    assert value.quality.status == "READY"
    expected = sum(math.log((101 + i) / (100 + i)) for i in range(20))
    assert_close_float(value.value, expected)
    warmup = compute_factor(mom, tuple(daily[:20]), T0 + 20 * DAY,
                            sessions=daily_sessions(22))
    assert warmup.quality.status == "WARMUP"
    assert warmup.value is None


def _day(offset: int) -> date:
    from datetime import timedelta
    return date(2026, 1, 5) + timedelta(days=offset)


# ---------------------------------------------------------------------------
# -k volatility : VOL001 / VOL004 / VOL005
# ---------------------------------------------------------------------------

def test_volatility_vol001_per_bar_and_annualized() -> None:
    closes = [Decimal(100), Decimal(110), Decimal(99)]
    bars = closes_bars(closes)
    returns = [math.log(1.1), math.log(0.9)]
    per_bar = compute_factor(
        spec("VOL001", 2, extra={"annualization": "PER_BAR", "annualization_factor": 1}),
        bars, T0 + 3 * MINUTE, sessions=DEFAULT_SESSIONS)
    assert_close_float(per_bar.value, statistics.stdev(returns))
    annualized = compute_factor(
        spec("VOL001", 2, extra={"annualization": "NOMINAL_252", "annualization_factor": 252}),
        bars, T0 + 3 * MINUTE, sessions=DEFAULT_SESSIONS)
    assert_close_float(annualized.value, statistics.stdev(returns) * math.sqrt(252))
    assert per_bar.value != annualized.value


def test_volatility_vol001_zero_std_outputs_zero_degraded() -> None:
    bars = closes_bars([Decimal(100), Decimal(110), Decimal(121)])
    value = compute_factor(
        spec("VOL001", 2, extra={"annualization": "PER_BAR", "annualization_factor": 1}),
        bars, T0 + 3 * MINUTE, sessions=DEFAULT_SESSIONS)
    assert value.value == 0.0
    assert value.quality.status == "DEGRADED"
    assert value.quality.reasons == ("zero_variance",)
    # Distinct from MOM003's INVALID None on the same window.
    mom003 = compute_factor(spec("MOM003", 2), bars, T0 + 3 * MINUTE,
                            sessions=DEFAULT_SESSIONS)
    assert mom003.value is None and mom003.quality.status == "INVALID"


def test_volatility_wilder_atr_hand_case() -> None:
    # TRs 2, 4, 6 -> seed 4; next TR 8 -> (4*2+8)/3 = 16/3; C_t = 100.
    bars = (
        bar(0, open_=100, high=102, low=100, close=100),
        bar(1, open_=100, high=101, low=99, close=100),
        bar(2, open_=101, high=103, low=99, close=101),
        bar(3, open_=104, high=106, low=100, close=104),
        bar(4, open_=100, high=108, low=100, close=100),
    )
    decision = T0 + 5 * MINUTE
    value = compute_factor(spec("VOL004", 3), bars, decision,
                           sessions=DEFAULT_SESSIONS)
    assert value.value == Decimal(16) / Decimal(300)
    assert value.quality.status == "READY"
    # The arithmetic mean of the last three TRs is 6: proving the Wilder
    # recursion and the arithmetic mean are never mixed.
    arithmetic = (Decimal(4) + Decimal(6) + Decimal(8)) / Decimal(3)
    assert arithmetic == Decimal(6)
    assert value.value * Decimal(100) == Decimal(16) / Decimal(3) != arithmetic


def test_volatility_wilder_resets_on_bad_row_and_missing_minute() -> None:
    session = minute_session("S1", date(2026, 1, 5), 600)
    good = [
        bar(0, open_=100, high=102, low=100, close=100, session=session),
        bar(1, open_=100, high=101, low=99, close=100, session=session),
        bar(2, open_=101, high=103, low=99, close=101, session=session),
        bar(3, open_=104, high=106, low=100, close=104, session=session),
        bar(4, open_=100, high=108, low=100, close=100, session=session),
    ]
    decision = T0 + 5 * MINUTE
    seeded = compute_factor(spec("VOL004", 3), tuple(good), decision,
                            sessions=(session,))
    assert seeded.quality.status == "READY"

    # A bad row (missing highs/lows) resets; the row is never deleted.
    bad_row = bar(3, open_=104, high=None, low=None, close=104, session=session)
    after_bad = compute_factor(spec("VOL004", 3),
                               tuple(good[:3]) + (bad_row, good[4]), decision,
                               sessions=(session,))
    assert after_bad.quality.status == "WARMUP"
    assert "wilder_reseeding" in after_bad.quality.reasons

    # A missing expected minute inside the session also resets.
    holey = tuple(good[:3]) + (good[4],)  # minute 4 (index 3) never arrived
    after_hole = compute_factor(spec("VOL004", 3), holey, decision,
                                sessions=(session,))
    assert after_hole.quality.status == "WARMUP"

    # After the hole, three fresh TRs re-seed the recursion.
    recovery = good[:3] + [
        bar(4, open_=100, high=108, low=100, close=100, session=session),
        bar(5, open_=100, high=101, low=99, close=100, session=session),
        bar(6, open_=100, high=102, low=100, close=101, session=session),
        bar(7, open_=101, high=104, low=101, close=103, session=session),
    ]
    resumed = compute_factor(spec("VOL004", 3), tuple(recovery), T0 + 8 * MINUTE,
                             sessions=(session,))
    assert resumed.quality.status == "READY"
    # TR(m4,m5)=2, TR(m5,m6)=2, TR(m6,m7)=3 -> seed 7/3 at minute 7.
    seed = (Decimal(2) + Decimal(2) + Decimal(3)) / Decimal(3)
    assert resumed.value == seed / Decimal(103)
    # One more TR continues the recursion: (7/3*2+2)/3 = 20/9.
    extended = recovery + [
        bar(8, open_=103, high=105, low=103, close=104, session=session),
    ]
    recursed = compute_factor(spec("VOL004", 3), tuple(extended), T0 + 9 * MINUTE,
                              sessions=(session,))
    assert recursed.value == (seed * Decimal(2) + Decimal(2)) / Decimal(3) / Decimal(104)


def test_volatility_vol005_semi_variance_hand_injected() -> None:
    from ak_trading.v2.factors import _negative_squared_sum
    assert_close_float(_negative_squared_sum([-0.1, 0.2, -0.3], 1), 1 / 30)
    assert_close_float(_negative_squared_sum([-0.1, 0.2, -0.3], 252), 8.4)
    # Overflowing squared returns are rejected, never truncated to inf.
    with pytest.raises(FactorsError):
        _negative_squared_sum([-1e200, -1e200, -1e200], 1)


def test_volatility_vol005_from_positive_prices_matches_injected_returns() -> None:
    returns = [-0.1, 0.2, -0.3]
    closes = [Decimal(1000)]
    for value in returns:
        closes.append(Decimal(str(float(closes[-1]) * math.exp(value))))
    bars = closes_bars(closes)
    decision = T0 + 4 * MINUTE
    per_bar = compute_factor(
        spec("VOL005", 3, extra={"annualization": "PER_BAR", "annualization_factor": 1}),
        bars, decision, sessions=DEFAULT_SESSIONS)
    assert_close_float(per_bar.value, 1 / 30)
    annualized = compute_factor(
        spec("VOL005", 3, extra={"annualization": "NOMINAL_252", "annualization_factor": 252}),
        bars, decision, sessions=DEFAULT_SESSIONS)
    assert_close_float(annualized.value, 8.4)
    # No square root: the output is a mean of squared negative returns.
    assert per_bar.value > 0.033 and per_bar.value < 0.034
    assert math.sqrt(per_bar.value) != pytest.approx(per_bar.value)


def test_volatility_vol004_non_positive_close_invalid() -> None:
    session = minute_session("S1", date(2026, 1, 5), 600)
    bars = (
        bar(0, open_=100, high=102, low=100, close=100, session=session),
        bar(1, open_=100, high=101, low=99, close=100, session=session),
        bar(2, open_=101, high=103, low=99, close=101, session=session),
        bar(3, open_=104, high=106, low=100, close=104, session=session),
        bar(4, open_=0, high=1, low=0, close=0, session=session),
    )
    value = compute_factor(spec("VOL004", 3), bars, T0 + 5 * MINUTE,
                           sessions=(session,))
    assert value.value is None
    assert value.quality.status == "INVALID"
    assert "non_positive_price" in value.quality.reasons


# ---------------------------------------------------------------------------
# -k volume_oi : VOI001 / VOI007 / VOI009
# ---------------------------------------------------------------------------

def test_volume_oi_voi001_reference_excludes_current() -> None:
    session = minute_session("S1", date(2026, 1, 5), 600)
    bars = tuple(
        bar(i, close=100, volume=v, session=session)
        for i, v in enumerate([10, 20, 30, 40]))
    decision = T0 + 4 * MINUTE
    value = compute_factor(spec("VOI001", 3), bars, decision,
                           sessions=(session,))
    assert value.value == Decimal(1)  # 40 / mean(10,20,30) - 1
    assert value.quality.status == "READY"
    doubled = tuple(
        bar(i, close=100, volume=v, session=session)
        for i, v in enumerate([10, 20, 30, 60]))
    assert compute_factor(spec("VOI001", 3), doubled, decision,
                        sessions=(session,)).value == Decimal(2)
    zero_current = tuple(
        bar(i, close=100, volume=v, session=session)
        for i, v in enumerate([10, 20, 30, 0]))
    assert compute_factor(spec("VOI001", 3), zero_current, decision,
                        sessions=(session,)).value == Decimal(-1)


def test_volume_oi_voi007_and_voi009_hand_values() -> None:
    session = minute_session("S1", date(2026, 1, 5), 600)
    bars = tuple(
        bar(i, close=100, volume=v, close_oi=oi, session=session)
        for i, (v, oi) in enumerate(zip([10, 10, 10, 40], [100, 101, 102, 110])))
    decision = T0 + 4 * MINUTE
    voi007 = compute_factor(spec("VOI007", 3), bars, decision,
                            sessions=(session,))
    assert voi007.value == Decimal(110) / Decimal(100) - Decimal(1)
    assert_close_float(float(voi007.value), 0.1)
    voi009 = compute_factor(spec("VOI009"), bars, decision, sessions=(session,))
    assert voi009.value == Decimal(40) / Decimal(110) == Decimal(4) / Decimal(11)
    assert isinstance(voi009.value, Decimal)


def test_volume_oi_zero_denominators_are_invalid() -> None:
    session = minute_session("S1", date(2026, 1, 5), 600)
    zero_mean = tuple(
        bar(i, close=100, volume=v, session=session)
        for i, v in enumerate([0, 0, 0, 40]))
    value = compute_factor(spec("VOI001", 3), zero_mean, T0 + 4 * MINUTE,
                           sessions=(session,))
    assert value.value is None and value.quality.status == "INVALID"
    assert "zero_denominator" in value.quality.reasons
    zero_lag_oi = tuple(
        bar(i, close=100, volume=10, close_oi=oi, session=session)
        for i, oi in enumerate([0, 1, 2, 3]))
    value = compute_factor(spec("VOI007", 3), zero_lag_oi, T0 + 4 * MINUTE,
                           sessions=(session,))
    assert value.value is None and value.quality.status == "INVALID"
    zero_oi = tuple(
        bar(i, close=100, volume=40, close_oi=oi, session=session)
        for i, oi in enumerate([10, 11, 12, 0]))
    value = compute_factor(spec("VOI009"), zero_oi, T0 + 4 * MINUTE,
                           sessions=(session,))
    assert value.value is None and value.quality.status == "INVALID"


def test_volume_oi_voi009_ignores_window_parameter() -> None:
    session = minute_session("S1", date(2026, 1, 5), 600)
    bars = tuple(
        bar(i, close=100, volume=10, close_oi=110, session=session)
        for i in range(4))
    decision = T0 + 4 * MINUTE
    instant = compute_factor(spec("VOI009"), bars, decision, sessions=(session,))
    with_n = compute_factor(spec("VOI009", extra={"n": 60}), bars, decision,
                            sessions=(session,))
    # The instant ratio ignores the inert window parameter in VALUE...
    assert instant.value == with_n.value == Decimal(10) / Decimal(110)
    # ...while the spec binding (E07) still separates the two identities.
    assert instant.quality.input_hash != with_n.quality.input_hash


def test_volume_oi_missing_volume_does_not_poison_price_factors() -> None:
    session = minute_session("S1", date(2026, 1, 5), 600)
    bars = tuple(
        bar(i, close=Decimal(100 + i), volume=None, session=session)
        for i in range(4))
    decision = T0 + 4 * MINUTE
    volume_value = compute_factor(spec("VOI001", 3), bars, decision,
                                  sessions=DEFAULT_SESSIONS)
    assert volume_value.quality.status == "MISSING"
    assert "missing_source_value" in volume_value.quality.reasons
    momentum = compute_factor(spec("MOM001", 3), bars, decision,
                             sessions=DEFAULT_SESSIONS)
    assert momentum.quality.status == "READY"
    assert_close_float(momentum.value, sum(
        math.log((101 + i) / (100 + i)) for i in range(3)))


# ---------------------------------------------------------------------------
# warmup and data boundaries (run in the full-file commands)
# ---------------------------------------------------------------------------

def test_warmup_insufficient_history_returns_warmup_rows() -> None:
    session = minute_session("S1", date(2026, 1, 5), 600)
    registry = factor_registry_v2(60)
    bars = tuple(bar(i, close=100 + i, volume=10, close_oi=100, session=session)
                 for i in range(10))
    decision = T0 + 10 * MINUTE
    for factor_spec in registry:
        value = compute_factor(factor_spec, bars, decision,
                               sessions=DEFAULT_SESSIONS)
        assert value is not None
        if factor_spec.source_factor_id == "VOI009":
            assert value.quality.status == "READY"
        else:
            assert value.quality.status == "WARMUP"
            assert value.quality.reasons == ("insufficient_history",)
        assert value.value is None or value.quality.status == "READY"


def test_boundaries_nonpositive_price_missing_fields_and_n_one_std() -> None:
    session = minute_session("S1", date(2026, 1, 5), 600)
    negative = closes_bars([Decimal(100), Decimal(-110), Decimal(121)])
    value = compute_factor(spec("MOM001", 2), negative, T0 + 3 * MINUTE,
                           sessions=DEFAULT_SESSIONS)
    assert value.value is None and value.quality.status == "INVALID"
    assert "non_positive_price" in value.quality.reasons
    missing_close = (bar(0, close=100, session=session),
                     bar(1, close=None, session=session),
                     bar(2, close=121, session=session))
    value = compute_factor(spec("MOM001", 2), missing_close, T0 + 3 * MINUTE,
                           sessions=(session,))
    assert value.quality.status == "MISSING"
    assert "missing_source_value" in value.quality.reasons
    # n = 1 cannot produce a sample standard deviation.
    bars = closes_bars([Decimal(100), Decimal(110)])
    value = compute_factor(spec("MOM003", 1), bars, T0 + 2 * MINUTE,
                           sessions=DEFAULT_SESSIONS)
    assert value.value is None and value.quality.status == "INVALID"
    assert "std_requires_n_ge_2" in value.quality.reasons


def test_boundaries_huge_decimals_and_zero_change() -> None:
    session = minute_session("S1", date(2026, 1, 5), 600)
    huge = Decimal("98765432109876543210987.654321")
    bars = (bar(0, close=huge, session=session),
            bar(1, close=huge * 2, session=session),
            bar(2, close=huge, session=session))
    value = compute_factor(spec("MOM001", 2), bars, T0 + 3 * MINUTE,
                           sessions=(session,))
    assert value.quality.status == "READY"
    assert_close_float(value.value, math.log(2.0) + math.log(0.5))  # exactly 0
    zero_volume = tuple(
        bar(i, close=100, volume=0, close_oi=5, session=session) for i in range(2))
    value = compute_factor(spec("VOI009"), zero_volume, T0 + 2 * MINUTE,
                           sessions=(session,))
    assert value.value == Decimal(0) and value.quality.status == "READY"


def test_all_frequencies_compute_through_aggregation() -> None:
    from ak_trading.v2.clock import aggregate_bars
    sessions = []
    minute_bars = []
    from datetime import timedelta
    for d in range(7):
        day = date(2026, 1, 5) + timedelta(days=d)
        sessions.append(minute_session(f"D{d}", day, 360, "DAY", start_hour=1))
        sessions.append(minute_session(f"N{d}", day, 240, "NIGHT", start_hour=9))
        for offset, sid in ((1, f"D{d}"), (9, f"N{d}")):
            base = int(datetime(day.year, day.month, day.day, offset, 0,
                                tzinfo=timezone.utc).timestamp()) * NANO
            for i in range(360 if sid.startswith("D") else 240):
                minute_bars.append(MarketBar(
                    bar_id=f"{sid}-m{i}", contract_id="RB2610", raw_symbol="rb2610",
                    product="SHFE.RB", duration_seconds=60,
                    datetime_nano=base + i * MINUTE, start_nano=base + i * MINUTE,
                    end_nano=base + (i + 1) * MINUTE, trading_day=day, session_id=sid,
                    available_at_nano=base + (i + 1) * MINUTE, imported_at_nano=None,
                    open=Decimal(100 + (i % 13)), high=Decimal(101 + (i % 13)),
                    low=Decimal(99 + (i % 13)), close=Decimal(100 + (i % 13)),
                    volume=Decimal(10 + (i % 7)), open_oi=Decimal(1000),
                    close_oi=Decimal(1000 + i), complete=True, expected_minutes=1,
                    actual_minutes=1, source="synthetic", quality=QUALITY))
    minute_bars.sort(key=lambda b: b.start_nano)
    as_of = max(b.end_nano for b in minute_bars)
    for duration in (60, 300, 900, 1800, 3600):
        aggregated = aggregate_bars(tuple(minute_bars), tuple(sessions), duration, as_of)
        registry = factor_registry_v2(duration)
        for factor_spec in registry:
            value = compute_factor(factor_spec, aggregated, as_of,
                                   sessions=tuple(sessions))
            assert value is not None
            if factor_spec.source_factor_id in ("VOL004",):
                # Wilder walks the whole aggregate prefix; seven days of
                # continuous sessions seed every frequency.
                assert value.quality.status == "READY", (duration, value.quality)
            elif factor_spec.source_factor_id == "VOI009":
                assert value.quality.status == "READY"
            else:
                assert value.quality.status in ("READY", "WARMUP", "INVALID"), (
                    duration, factor_spec.factor_key, value.quality)
                if value.quality.status == "READY":
                    assert value.value is not None
