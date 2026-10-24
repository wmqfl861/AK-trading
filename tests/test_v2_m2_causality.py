"""M2-07: snapshot construction, cross-frequency association, immutability.

Prefix invariance (appending future bars, extreme volumes or a late daily
bar leaves every already-published snapshot byte-identical), legal session
gaps versus missing minutes, contract isolation, daily-background as-of
association, rule metadata passthrough with ``COST_UNKNOWN``, and the
feature record round-trip through the M1 codecs.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ak_trading.v2.baseline import legacy_daily_baseline
from ak_trading.v2.contracts import (
    DataQuality,
    FeeLeg,
    FrozenInput,
    Instrument,
    MarketBar,
    SessionWindow,
    TradingRule,
)
from ak_trading.v2.data import visible_bars
from ak_trading.v2.factor_specs import factor_registry_v2
from ak_trading.v2.factors import (
    COST_UNKNOWN_REASON,
    META_MULTIPLIER_FACTOR_KEY,
    FactorsError,
    build_feature_snapshot,
    compute_factor,
)
from ak_trading.v2.schemas import (
    META_TICK_FACTOR_KEY,
    decode_record_file,
    encode_record,
    encode_record_file,
)
from ak_trading.v2.seasonality import fit_seasonality, seasonal_values

NANO = 1_000_000_000
MINUTE = 60 * NANO
DAY = 86_400 * NANO
T0 = int(datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc).timestamp()) * NANO
QUALITY = DataQuality(status="READY", reasons=(), temporal_basis="observed",
                      input_hash="0" * 64)
PRODUCT = "SHFE.RB"
FEATURES_SCHEMA = "ak_probability_features_v2.1.0"


def session_for(day: date, index: int, slots: int = 400, kind: str = "DAY",
                hour: int = 1) -> SessionWindow:
    start = int(datetime(day.year, day.month, day.day, hour, 0,
                         tzinfo=timezone.utc).timestamp()) * NANO
    return SessionWindow(session_id=f"{kind[0]}{index}", product=PRODUCT,
                         trading_day=day, start_nano=start,
                         end_nano=start + slots * MINUTE, kind=kind,
                         calendar_version="cal-test-v1", evidence_status="VERIFIED")


def minute_bar(start: int, close: Decimal, volume: Decimal = Decimal(10),
               close_oi: Decimal = Decimal(1000), session: SessionWindow | None = None,
               contract: str = "RB2610", available_at: int | None = None) -> MarketBar:
    end = start + MINUTE
    return MarketBar(
        bar_id=f"{contract}|{start}", contract_id=contract, raw_symbol="rb2610",
        product=PRODUCT, duration_seconds=60, datetime_nano=start,
        start_nano=start, end_nano=end,
        trading_day=session.trading_day if session is not None else date(2026, 1, 5),
        session_id=session.session_id if session is not None else None,
        available_at_nano=available_at if available_at is not None else end,
        imported_at_nano=None, open=close, high=close + Decimal(1),
        low=close - Decimal(1), close=close, volume=volume,
        open_oi=close_oi, close_oi=close_oi, complete=True, expected_minutes=1,
        actual_minutes=1, source="synthetic", quality=QUALITY)


def trading_rule(effective_from: int, effective_until: int,
                 available_at: int | None = None) -> TradingRule:
    return TradingRule(
        rule_id="rule-1", contract_id="RB2610", effective_from_nano=effective_from,
        effective_until_nano=effective_until, available_at_nano=(
            available_at if available_at is not None else effective_from),
        multiplier=Decimal(10), price_tick=Decimal("1"), lot_step=1,
        min_open_lots=1, open_fee=FeeLeg(Decimal("4"), Decimal("0"), False),
        close_fee=FeeLeg(Decimal("4"), Decimal("0"), False), close_today_fee=None,
        long_margin_rate=Decimal("0.1"), short_margin_rate=Decimal("0.1"),
        limit_up=None, limit_down=None, slippage_ticks=Decimal("1"),
        impact_ticks=Decimal("0"), participation_rate=Decimal("0.1"),
        evidence="SCENARIO", source_sha256="0" * 64)


def one_day_history(count: int = 150, session: SessionWindow | None = None) \
        -> tuple[SessionWindow, tuple[MarketBar, ...]]:
    session = session or session_for(date(2026, 1, 5), 0)
    bars = tuple(
        minute_bar(session.start_nano + index * MINUTE,
                   Decimal(100 + (index % 11)),
                   volume=Decimal(10 + index % 4),
                   close_oi=Decimal(1000 + index), session=session)
        for index in range(count))
    return session, bars


def test_causality_prefix_invariance_snapshot_bytes() -> None:
    session, bars = one_day_history()
    specs = factor_registry_v2(60)
    decision = session.start_nano + 80 * MINUTE
    rule = trading_rule(session.start_nano, session.end_nano)
    first = build_feature_snapshot("RB2610", bars, decision, specs, None, None,
                                   rule, (session,))
    encoded_first = encode_record(first)

    extreme = tuple(
        minute_bar(session.start_nano + (150 + index) * MINUTE,
                   Decimal("9999"), volume=Decimal("1000000000"),
                   close_oi=Decimal(1), session=session)
        for index in range(100))
    second = build_feature_snapshot("RB2610", bars + extreme, decision, specs,
                                    None, None, rule, (session,))
    assert second == first
    assert second.snapshot_id == first.snapshot_id
    assert encode_record(second) == encoded_first
    assert second.quality.input_hash == first.quality.input_hash
    assert [value.quality.input_hash for value in second.values] == \
        [value.quality.input_hash for value in first.values]
    assert [value.value for value in second.values] == \
        [value.value for value in first.values]


def test_causality_daily_isolation_and_late_daily_bar() -> None:
    session, bars = one_day_history(90)
    decision = session.start_nano + 90 * MINUTE
    specs = factor_registry_v2(60)
    # A final daily bar of the current trading day, even if the package
    # already carries it, is invisible intraday (M1 visible_bars).
    same_day_daily = MarketBar(
        bar_id="same-day-daily", contract_id="RB2610", raw_symbol="rb2610",
        product=PRODUCT, duration_seconds=86400,
        datetime_nano=session.start_nano, start_nano=session.start_nano,
        end_nano=session.end_nano, trading_day=date(2026, 1, 5), session_id=None,
        available_at_nano=session.start_nano + MINUTE, imported_at_nano=None,
        open=Decimal(100), high=Decimal(110), low=Decimal(99),
        close=Decimal(105), volume=Decimal(10 ** 6), open_oi=Decimal(1000),
        close_oi=Decimal(1100), complete=True, expected_minutes=1,
        actual_minutes=1, source="synthetic", quality=QUALITY)
    instrument = Instrument(
        contract_id="RB2610", raw_symbol="rb2610", product=PRODUCT, sector="metal",
        risk_groups=("ferrous",), listing_nano=None, expiry_nano=None,
        last_trade_nano=None, delivery_cutoff_nano=None, quality=QUALITY)
    bundle = FrozenInput(
        root=Path("."), manifest_sha256="0" * 64, instruments=(instrument,),
        sessions=(session,), bars=bars + (same_day_daily,), macro_items=(),
        rules=(trading_rule(session.start_nano, session.end_nano),))
    visible_minute = visible_bars(bundle, "RB2610", 60, decision, True)
    assert visible_minute == bars
    # The intraday filter drops the current day's final daily bar even
    # though it claims early availability.
    assert all(bar.duration_seconds == 60 for bar in visible_minute)
    assert visible_bars(bundle, "RB2610", 86400, decision, True) == ()
    # A same-trading-day daily background is refused, not silently linked.
    same_day_snapshot = build_feature_snapshot(
        "RB2610", (same_day_daily,), same_day_daily.end_nano,
        factor_registry_v2(86400), None, None, None, ())
    with pytest.raises(FactorsError, match="daily_background"):
        build_feature_snapshot("RB2610", bars, decision, specs,
                               same_day_snapshot, None, None, (session,))
    # A late-arriving daily bar of the same day never leaks in either.
    late_daily = MarketBar(
        bar_id="late-daily", contract_id="RB2610", raw_symbol="rb2610",
        product=PRODUCT, duration_seconds=86400, datetime_nano=session.start_nano,
        start_nano=session.start_nano, end_nano=session.end_nano,
        trading_day=date(2026, 1, 5), session_id=None,
        available_at_nano=decision + DAY, imported_at_nano=None,
        open=Decimal(100), high=Decimal(110), low=Decimal(99), close=Decimal(105),
        volume=Decimal(1), open_oi=Decimal(1), close_oi=Decimal(1),
        complete=True, expected_minutes=1, actual_minutes=1, source="synthetic",
        quality=QUALITY)
    late_bundle = FrozenInput(
        root=Path("."), manifest_sha256="0" * 64, instruments=(instrument,),
        sessions=(session,), bars=bars + (late_daily,), macro_items=(),
        rules=(), )
    assert visible_bars(late_bundle, "RB2610", 86400, decision, True) == ()


def test_causality_daily_background_asof_association() -> None:
    day1 = date(2026, 1, 5)
    day2 = date(2026, 1, 6)
    session1 = session_for(day1, 0)
    session2 = session_for(day2, 1)
    daily_bar = MarketBar(
        bar_id="daily1", contract_id="RB2610", raw_symbol="rb2610", product=PRODUCT,
        duration_seconds=86400, datetime_nano=session1.start_nano,
        start_nano=session1.start_nano, end_nano=session1.start_nano + DAY,
        trading_day=day1, session_id=None, available_at_nano=session1.end_nano,
        imported_at_nano=None, open=Decimal(100), high=Decimal(120),
        low=Decimal(95), close=Decimal(110), volume=Decimal(5000),
        open_oi=Decimal(1000), close_oi=Decimal(1050), complete=True,
        expected_minutes=1, actual_minutes=1, source="synthetic", quality=QUALITY)
    daily_snapshot = build_feature_snapshot(
        "RB2610", (daily_bar,), daily_bar.end_nano, factor_registry_v2(86400),
        None, None, None, ())
    assert daily_snapshot.session_kind == "DAILY"
    assert daily_snapshot.session_id == "DAILY:SHFE.RB:2026-01-05"
    _, minute_history = one_day_history(90, session=session2)
    decision = session2.start_nano + 90 * MINUTE
    snapshot = build_feature_snapshot(
        "RB2610", minute_history, decision, factor_registry_v2(60),
        daily_snapshot, None, None, (session2,))
    assert snapshot.daily_background_id == daily_snapshot.snapshot_id
    assert snapshot.daily_background_id != snapshot.snapshot_id
    assert daily_snapshot.trading_day < snapshot.trading_day
    assert snapshot.available_at_nano <= decision


def test_causality_all_frequencies_keep_durations_separate() -> None:
    day = date(2026, 1, 5)
    session = session_for(day, 0)
    _, bars = one_day_history(120, session=session)
    decision = session.start_nano + 120 * MINUTE
    registry60 = factor_registry_v2(60)
    snapshot60 = build_feature_snapshot("RB2610", bars, decision, registry60,
                                        None, None, None, (session,))
    assert snapshot60.duration_seconds == 60
    mixed_specs = (*factor_registry_v2(60)[:2], *factor_registry_v2(300)[:2])
    with pytest.raises(FactorsError, match="mixed_spec_durations"):
        build_feature_snapshot("RB2610", bars, decision, mixed_specs,
                               None, None, None, (session,))
    from ak_trading.v2.clock import aggregate_bars
    five_minute = aggregate_bars(bars, (session,), 300, decision)
    snapshot300 = build_feature_snapshot("RB2610", five_minute, decision,
                                         factor_registry_v2(300), None, None,
                                         None, (session,))
    assert snapshot300.duration_seconds == 300
    assert snapshot300.snapshot_id != snapshot60.snapshot_id
    with pytest.raises(FactorsError):
        build_feature_snapshot("RB2610", bars + five_minute, decision,
                               registry60, None, None, None, (session,))


def test_causality_session_gap_returns_include_gap() -> None:
    day = date(2026, 1, 5)
    morning = SessionWindow(
        session_id="AM", product=PRODUCT, trading_day=day,
        start_nano=T0, end_nano=T0 + 30 * MINUTE, kind="DAY",
        calendar_version="cal-test-v1", evidence_status="VERIFIED")
    afternoon = SessionWindow(
        session_id="PM", product=PRODUCT, trading_day=day,
        start_nano=T0 + 150 * MINUTE, end_nano=T0 + 180 * MINUTE, kind="DAY",
        calendar_version="cal-test-v1", evidence_status="VERIFIED")
    morning_bars = tuple(
        minute_bar(T0 + index * MINUTE, Decimal(100 + index), session=morning)
        for index in range(30))
    afternoon_bars = tuple(
        minute_bar(afternoon.start_nano + index * MINUTE, Decimal(200 + index),
                   session=afternoon)
        for index in range(5))
    import math
    from ak_trading.v2.contracts import FactorSpec
    mom = FactorSpec(
        factor_key="MOM001:hand:d60:n4", source_factor_id="MOM001",
        operator_version="m2-v2.1.0", parameter_version="hand-n4",
        duration_seconds=60, lookback=5, parameters={"n": 4}, unit="log_return_sum",
        required_fields=("close",), formula="sum(r,n)", source_sha256="0" * 64)
    decision = afternoon.start_nano + MINUTE
    value = compute_factor(mom, morning_bars + afternoon_bars, decision,
                           sessions=(morning, afternoon))
    assert value.quality.status == "READY"
    # The window spans the legal lunch gap: the last morning close 129 to
    # the first afternoon close 200 contributes the gap return.
    closes = [Decimal(100 + i) for i in range(26, 30)] + [Decimal(200)]
    expected = sum(math.log(float(b / a)) for a, b in zip(closes, closes[1:]))
    assert value.value == pytest.approx(expected, rel=1e-12, abs=1e-12)
    snapshot = build_feature_snapshot(
        "RB2610", morning_bars + afternoon_bars, decision, factor_registry_v2(60),
        None, None, None, (morning, afternoon))
    assert snapshot.session_id == "PM"


def test_causality_missing_minute_rewarms_and_never_crosses() -> None:
    day = date(2026, 1, 5)
    session = session_for(day, 0)
    bars = tuple(
        minute_bar(session.start_nano + index * MINUTE, Decimal(100 + index % 9),
                   session=session)
        for index in range(70))
    # Remove minute 40: any window spanning it is missing, never stitched.
    holey = bars[:40] + bars[41:]
    decision_at_hole = session.start_nano + 65 * MINUTE
    specs = factor_registry_v2(60)
    spec_by_id = {spec.source_factor_id: spec for spec in specs}
    value = compute_factor(spec_by_id["MOM001"], holey, decision_at_hole,
                           sessions=(session,))
    assert value.quality.status == "MISSING"
    assert "missing_slot_in_window" in value.quality.reasons
    # Wilder resets and re-warms across the hole.
    wilder = compute_factor(spec_by_id["VOL004"], holey, decision_at_hole,
                           sessions=(session,))
    assert wilder.quality.status == "WARMUP"
    assert "wilder_reseeding" in wilder.quality.reasons
    # Later, once the window no longer spans the hole, output resumes.
    later = session.start_nano + 102 * MINUTE
    extended = holey + tuple(
        minute_bar(session.start_nano + index * MINUTE, Decimal(100 + index % 9),
                   session=session)
        for index in range(70, 103))
    resumed = compute_factor(spec_by_id["MOM001"], extended, later,
                            sessions=(session,))
    assert resumed.quality.status == "READY"


def test_causality_contract_switch_rejected() -> None:
    day = date(2026, 1, 5)
    session = session_for(day, 0)
    bars_a = tuple(
        minute_bar(session.start_nano + index * MINUTE, Decimal(100), session=session)
        for index in range(30))
    bars_b = tuple(
        minute_bar(session.start_nano + (30 + index) * MINUTE, Decimal(120),
                   session=session, contract="RB2605")
        for index in range(30))
    specs = factor_registry_v2(60)
    with pytest.raises(FactorsError, match="mixed_contract_bars"):
        compute_factor(specs[0], bars_a + bars_b,
                       session.start_nano + 60 * MINUTE, sessions=(session,))
    # Same raw symbol under a different contract id is still a switch.
    rolled = tuple(
        minute_bar(session.start_nano + (30 + index) * MINUTE, Decimal(120),
                   session=session, contract="RB2605")
        for index in range(30))
    with pytest.raises(FactorsError, match="mixed_contract_bars"):
        build_feature_snapshot("RB2610", bars_a + rolled,
                               session.start_nano + 60 * MINUTE, specs,
                               None, None, None, (session,))
    with pytest.raises(FactorsError, match="bars_contract_mismatch"):
        build_feature_snapshot("RB2610", bars_b, session.start_nano + 60 * MINUTE,
                               specs, None, None, None, (session,))


def test_causality_rule_meta_passthrough_and_cost_unknown() -> None:
    session, bars = one_day_history(70)
    specs = factor_registry_v2(60)
    decision = session.start_nano + 70 * MINUTE
    rule = trading_rule(session.start_nano, session.end_nano)
    with_rule = build_feature_snapshot("RB2610", bars, decision, specs, None,
                                       None, rule, (session,))
    meta = {value.factor_key: value for value in with_rule.values
            if value.factor_key.startswith("META:")}
    assert meta[META_TICK_FACTOR_KEY].value == Decimal("1")
    assert meta[META_MULTIPLIER_FACTOR_KEY].value == Decimal(10)
    assert all(value.quality.status == "READY" for value in meta.values())
    assert with_rule.available_at_nano == max(
        max(bar.available_at_nano for bar in bars), rule.available_at_nano)
    without_rule = build_feature_snapshot("RB2610", bars, decision, specs, None,
                                          None, None, (session,))
    meta_none = {value.factor_key: value for value in without_rule.values
                 if value.factor_key.startswith("META:")}
    assert set(meta_none) == {META_TICK_FACTOR_KEY, META_MULTIPLIER_FACTOR_KEY}
    assert all(value.value is None for value in meta_none.values())
    assert all(COST_UNKNOWN_REASON in value.quality.reasons
               for value in meta_none.values())
    assert all(value.quality.status == "MISSING" for value in meta_none.values())
    # The meta pair is never counted among the ten or fourteen factors.
    assert len({value.factor_key for value in without_rule.values
                if not value.factor_key.startswith(("META:", "SEASONAL:"))}) == 10
    # A rule that does not cover the decision time is rejected.
    expired = trading_rule(session.start_nano, session.start_nano + MINUTE)
    with pytest.raises(FactorsError, match="rule_not_effective"):
        build_feature_snapshot("RB2610", bars, decision, specs, None, None,
                               expired, (session,))
    invisible = trading_rule(session.start_nano, session.end_nano,
                             available_at=decision + MINUTE)
    with pytest.raises(FactorsError, match="rule_not_visible"):
        build_feature_snapshot("RB2610", bars, decision, specs, None, None,
                               invisible, (session,))


def test_causality_features_json_roundtrip_through_m1_codecs() -> None:
    session, bars = one_day_history(70)
    specs = factor_registry_v2(60)
    decision = session.start_nano + 70 * MINUTE
    rule = trading_rule(session.start_nano, session.end_nano)
    snapshot = build_feature_snapshot("RB2610", bars, decision, specs, None,
                                      None, rule, (session,))
    payload = encode_record_file("FeatureSnapshot", FEATURES_SCHEMA, (snapshot,))
    decoded = decode_record_file(payload, type(snapshot), FEATURES_SCHEMA)
    assert decoded == (snapshot,)
    warmup_bars = bars[:10]
    warmup = build_feature_snapshot("RB2610", warmup_bars,
                                    session.start_nano + 10 * MINUTE, specs,
                                    None, None, None, (session,))
    # Every registered factor keeps a row at a warmup point.
    assert len([value for value in warmup.values
                if not value.factor_key.startswith("META:")]) == 10
    assert warmup.quality.status != "READY"


def test_causality_snapshot_identity_requires_session() -> None:
    session, bars = one_day_history(70)
    specs = factor_registry_v2(60)
    decision = session.start_nano + 70 * MINUTE
    with pytest.raises(FactorsError, match="session_identity_missing"):
        build_feature_snapshot("RB2610", bars, decision, specs, None, None,
                               None, ())
    unknown_session = (SessionWindow(
        session_id="OTHER", product=PRODUCT, trading_day=date(2026, 1, 5),
        start_nano=T0, end_nano=T0 + 10 * MINUTE, kind="NIGHT",
        calendar_version="cal-test-v1", evidence_status="VERIFIED"),)
    with pytest.raises(FactorsError, match="session_identity_missing"):
        build_feature_snapshot("RB2610", bars, decision, specs, None, None,
                               None, unknown_session)
    with pytest.raises(FactorsError, match="no_visible_bars"):
        build_feature_snapshot("RB2610", bars[:0], decision, specs, None, None,
                               None, (session,))
    daily_specs = factor_registry_v2(86400)
    daily = MarketBar(
        bar_id="noday", contract_id="RB2610", raw_symbol="rb2610", product=PRODUCT,
        duration_seconds=86400, datetime_nano=T0, start_nano=T0,
        end_nano=T0 + DAY, trading_day=None, session_id=None,
        available_at_nano=T0 + DAY, imported_at_nano=None, open=Decimal(100),
        high=Decimal(101), low=Decimal(99), close=Decimal(100),
        volume=Decimal(10), open_oi=Decimal(100), close_oi=Decimal(100),
        complete=True, expected_minutes=1, actual_minutes=1, source="synthetic",
        quality=QUALITY)
    with pytest.raises(FactorsError, match="trading_day_missing"):
        build_feature_snapshot("RB2610", (daily,), T0 + DAY, daily_specs,
                               None, None, None, ())


def test_causality_seasonal_columns_and_frozen_model_in_snapshot() -> None:
    day = date(2026, 1, 5)
    train_sessions = tuple(
        session_for(day + timedelta(days=index), index, 4) for index in range(21))
    train_bars: list[MarketBar] = []
    for index, session in enumerate(train_sessions):
        for slot in range(4):
            close = Decimal(100 + index) + (Decimal(2) if slot >= 1 else Decimal(0))
            train_bars.append(minute_bar(
                session.start_nano + slot * MINUTE, close,
                volume=Decimal(10 if slot == 1 else 5),
                close_oi=Decimal(1000), session=session))
    fit_start = min(s.start_nano for s in train_sessions)
    fit_end = max(s.end_nano for s in train_sessions)
    model = fit_seasonality(tuple(train_bars), train_sessions, fit_start, fit_end)
    live_session = session_for(day + timedelta(days=30), 30, 4)
    live_bars = tuple(
        minute_bar(live_session.start_nano + slot * MINUTE,
                   Decimal(130) + (Decimal(2) if slot >= 1 else Decimal(0)),
                   volume=Decimal(20 if slot == 1 else 5),
                   close_oi=Decimal(1000), session=live_session)
        for slot in range(4))
    decision = live_session.start_nano + 4 * MINUTE
    specs = factor_registry_v2(60)
    with_seasonal = build_feature_snapshot(
        "RB2610", live_bars, decision, specs, None, model,
        trading_rule(live_session.start_nano, live_session.end_nano + 10 * MINUTE),
        (live_session,) + train_sessions)
    seasonal = {value.factor_key: value for value in with_seasonal.values
                if value.factor_key.startswith("SEASONAL:")}
    assert set(seasonal) == {
        "SEASONAL:volume_relative:v2.1.1:d60",
        "SEASONAL:abs_return_relative:v2.1.1:d60",
    }
    # Appending extreme future bars does not change the frozen columns.
    extreme = tuple(
        minute_bar(live_session.start_nano + (4 + index) * MINUTE,
                   Decimal("9999"), volume=Decimal("10") ** 12,
                   close_oi=Decimal(1), session=live_session)
        for index in range(50))
    again = build_feature_snapshot(
        "RB2610", live_bars + extreme, decision, specs, None, model,
        trading_rule(live_session.start_nano, live_session.end_nano + 10 * MINUTE),
        (live_session,) + train_sessions)
    assert again == with_seasonal
    assert {value.factor_key: value.value for value in seasonal.values()} == {
        value.factor_key: value.value for value in again.values
        if value.factor_key.startswith("SEASONAL:")}


# ---------------------------------------------------------------------------
# Precheck-20260914-r1 regressions (E01/E02/E03/E05/E07/E08)
# ---------------------------------------------------------------------------

DAY_NANO = 86_400 * NANO


def _daily_bar_for(day: date, close: Decimal) -> MarketBar:
    start = int(datetime(day.year, day.month, day.day, 1, 0,
                         tzinfo=timezone.utc).timestamp()) * NANO
    end = start + 60 * MINUTE
    return MarketBar(
        bar_id=f"RB2610|86400|{start}", contract_id="RB2610", raw_symbol="rb2610",
        product=PRODUCT, duration_seconds=86400, datetime_nano=start,
        start_nano=start, end_nano=end, trading_day=day, session_id=None,
        available_at_nano=end, imported_at_nano=None, open=close,
        high=close + 1, low=close - 1, close=close, volume=Decimal(10),
        open_oi=Decimal(100), close_oi=Decimal(100), complete=True,
        expected_minutes=1, actual_minutes=1, source="synthetic", quality=QUALITY)


def _hand_specs(n: int) -> tuple:
    from ak_trading.v2.contracts import FactorSpec
    from ak_trading.v2.factor_specs import factor_registry_v2

    def make(factor_id: str) -> FactorSpec:
        template = next(s for s in factor_registry_v2(60)
                        if s.source_factor_id == factor_id)
        parameters = dict(template.parameters)
        parameters["n"] = n
        if factor_id == "REV001":
            parameters["k"] = n
        return FactorSpec(
            factor_key=f"{factor_id}:hand:d60:n{n}", source_factor_id=factor_id,
            operator_version=template.operator_version,
            parameter_version=f"hand-n{n}", duration_seconds=60, lookback=n + 1,
            parameters=parameters, unit=template.unit,
            required_fields=template.required_fields, formula=template.formula,
            source_sha256=template.source_sha256)
    return (make("MOM001"), make("VOL004"))


def _baseline_map(bars, decision, sessions=()) -> dict:
    from ak_trading.v2.baseline import baseline_values_v2
    return {value.factor_key: (value.value, value.quality)
            for value in baseline_values_v2(bars, decision, sessions=sessions)}


def test_precheck_e02_missing_scheduled_slot_at_session_tail() -> None:
    """AM plans 3 slots, only 2 arrive, PM follows: the missing scheduled
    minute must interrupt windows and reset Wilder (P04)."""
    import math
    am = SessionWindow(session_id="AM", product=PRODUCT, trading_day=date(2026, 1, 5),
                       start_nano=T0, end_nano=T0 + 3 * MINUTE, kind="DAY",
                       calendar_version="cal-test-v1", evidence_status="VERIFIED")
    pm = SessionWindow(session_id="PM", product=PRODUCT, trading_day=date(2026, 1, 5),
                       start_nano=T0 + 100 * MINUTE, end_nano=T0 + 103 * MINUTE,
                       kind="DAY", calendar_version="cal-test-v1",
                       evidence_status="VERIFIED")
    gap_bars = (minute_bar(T0, Decimal(100), session=am),
                minute_bar(T0 + MINUTE, Decimal(110), session=am),
                minute_bar(pm.start_nano, Decimal(121), session=pm))
    decision = gap_bars[-1].end_nano
    snapshot = build_feature_snapshot(
        "RB2610", gap_bars, decision, _hand_specs(2), None, None,
        trading_rule(am.start_nano, pm.end_nano + 10 * MINUTE), (am, pm))
    factors = {value.factor_key: value for value in snapshot.values
               if not value.factor_key.startswith("META:")}
    assert all(value.value is None for value in factors.values())
    assert "missing_slot_in_window" in factors["MOM001:hand:d60:n2"].quality.reasons
    assert "wilder_reseeding" in factors["VOL004:hand:d60:n2"].quality.reasons
    # With the third AM minute actually present the same cross-session
    # window is a legal INCLUDE_GAP observation.
    filled = (minute_bar(T0, Decimal(100), session=am),
              minute_bar(T0 + MINUTE, Decimal(110), session=am),
              minute_bar(T0 + 2 * MINUTE, Decimal(115), session=am),
              minute_bar(pm.start_nano, Decimal(121), session=pm))
    snapshot = build_feature_snapshot(
        "RB2610", filled, decision, _hand_specs(2), None, None,
        trading_rule(am.start_nano, pm.end_nano + 10 * MINUTE), (am, pm))
    factors = {value.factor_key: value for value in snapshot.values
               if not value.factor_key.startswith("META:")}
    # The n=2 window is the last three bars (110, 115, 121): the legal
    # lunch gap return ln(121/115) is included, no slot is missing.
    assert factors["MOM001:hand:d60:n2"].value == pytest.approx(
        math.log(115 / 110) + math.log(121 / 115), rel=1e-12)


def test_precheck_e02_missing_declared_trading_day_daily() -> None:
    """A calendar-declared trading day with no bar is not compressed (P05)."""
    from ak_trading.v2.contracts import FactorSpec
    from ak_trading.v2.factor_specs import factor_registry_v2
    days = [date(2026, 1, 5) + timedelta(days=index) for index in range(4)]
    sessions = tuple(
        SessionWindow(session_id=f"D{index}", product=PRODUCT, trading_day=day,
                      start_nano=T0 + index * DAY_NANO, kind="DAY",
                      end_nano=T0 + index * DAY_NANO + 60 * MINUTE,
                      calendar_version="cal-test-v1", evidence_status="VERIFIED")
        for index, day in enumerate(days))
    template = next(s for s in factor_registry_v2(86400)
                    if s.source_factor_id == "MOM001")
    spec = FactorSpec(
        factor_key="MOM001:hand:d86400:n2", source_factor_id="MOM001",
        operator_version=template.operator_version, parameter_version="hand-n2",
        duration_seconds=86400, lookback=3,
        parameters={**template.parameters, "n": 2},
        unit=template.unit, required_fields=template.required_fields,
        formula=template.formula, source_sha256=template.source_sha256)
    daily = tuple(
        _daily_bar_for(day, close) for day, close in
        zip([days[0], days[1], days[3]], [Decimal(100), Decimal(110), Decimal(121)]))
    snapshot = build_feature_snapshot(
        "RB2610", daily, daily[-1].end_nano, (spec,), None, None,
        trading_rule(sessions[-1].start_nano, sessions[-1].end_nano + 10 * MINUTE),
        sessions)
    value = next(v for v in snapshot.values if v.factor_key.startswith("MOM001"))
    assert value.value is None and value.quality.status == "MISSING"
    assert "missing_slot_in_window" in value.quality.reasons


def test_precheck_e03_future_revision_never_breaks_history() -> None:
    """An invisible future revision leaves every historical result intact (P06)."""
    from dataclasses import replace
    session = session_for(date(2026, 1, 5), 0, slots=10)
    bars = tuple(minute_bar(session.start_nano + index * MINUTE,
                            Decimal(100 + index), session=session)
                 for index in range(3))
    decision = bars[-1].end_nano
    specs = _hand_specs(2)
    before = compute_factor(specs[0], bars, decision, sessions=(session,))
    baseline_before = _baseline_map(bars, decision, (session,))
    snapshot_before = build_feature_snapshot(
        "RB2610", bars, decision, specs, None, None,
        trading_rule(session.start_nano, session.end_nano + 10 * MINUTE), (session,))
    revision = replace(bars[1], bar_id=bars[1].bar_id + "-late-revision",
                       high=Decimal(201), close=Decimal(200),
                       available_at_nano=decision + DAY_NANO)
    extended = bars + (revision,)
    assert compute_factor(specs[0], extended, decision,
                         sessions=(session,)) == before
    assert _baseline_map(extended, decision, (session,)) == baseline_before
    after = build_feature_snapshot(
        "RB2610", extended, decision, specs, None, None,
        trading_rule(session.start_nano, session.end_nano + 10 * MINUTE), (session,))
    assert after == snapshot_before
    with pytest.raises(FactorsError, match="conflicting_duplicate_bar"):
        compute_factor(specs[0], bars + (replace(bars[1], bar_id="clash"),),
                       decision, sessions=(session,))


def test_precheck_e05_anchor_bar_outside_declared_session() -> None:
    """The anchor bar must lie inside its declared session (P03)."""
    session = session_for(date(2026, 1, 5), 0, slots=3)
    bars = tuple(minute_bar(session.start_nano + index * MINUTE,
                            Decimal(100 + index), session=session)
                 for index in range(3))
    shortened = SessionWindow(
        session_id=session.session_id, product=PRODUCT,
        trading_day=session.trading_day, start_nano=session.start_nano,
        end_nano=session.start_nano + MINUTE, kind=session.kind,
        calendar_version=session.calendar_version,
        evidence_status=session.evidence_status)
    snapshot = build_feature_snapshot(
        "RB2610", bars, bars[-1].end_nano, _hand_specs(2), None, None,
        trading_rule(session.start_nano, session.end_nano + 10 * MINUTE), (shortened,))
    assert snapshot.quality.status == "INVALID"
    assert "bar_outside_declared_session" in snapshot.quality.reasons


def test_precheck_e07_identity_binds_content_and_spec_versions() -> None:
    """Content and spec changes move hashes; future appends do not (P10)."""
    from dataclasses import replace
    from ak_trading.v2.clock import aggregate_bars
    from ak_trading.v2.contracts import FactorSpec
    from ak_trading.v2.factor_specs import factor_registry_v2
    session = session_for(date(2026, 1, 5), 0)
    bars = tuple(minute_bar(session.start_nano + index * MINUTE,
                            Decimal(100 + index), session=session)
                 for index in range(3))
    decision = bars[-1].end_nano
    voi009 = next(s for s in factor_registry_v2(60) if s.source_factor_id == "VOI009")
    base = compute_factor(voi009, (bars[-1],), decision, sessions=(session,))
    altered = replace(bars[-1], volume=Decimal(20))
    alternate = compute_factor(voi009, (altered,), decision, sessions=(session,))
    assert altered.bar_id == bars[-1].bar_id  # bar ids are slot identities
    assert alternate.value != base.value
    assert alternate.quality.input_hash != base.quality.input_hash
    # M1 aggregate bar ids are content-stable; the content hash is not.
    raw = tuple(minute_bar(session.start_nano + index * MINUTE, Decimal(100),
                           volume=Decimal(10), session=session)
                for index in range(5))
    revised = raw[:-1] + (replace(raw[-1], bar_id="distinct-content-id",
                                  volume=Decimal(20)),)
    aggregated_a = aggregate_bars(raw, (session,), 300, raw[-1].end_nano)
    aggregated_b = aggregate_bars(revised, (session,), 300, raw[-1].end_nano)
    assert aggregated_a[0].bar_id == aggregated_b[0].bar_id
    template300 = next(s for s in factor_registry_v2(300)
                       if s.source_factor_id == "VOI009")
    voi300 = FactorSpec(
        factor_key="VOI009:hand:d300:instant", source_factor_id="VOI009",
        operator_version=template300.operator_version,
        parameter_version="hand-instant", duration_seconds=300, lookback=1,
        parameters=dict(template300.parameters), unit=template300.unit,
        required_fields=template300.required_fields, formula=template300.formula,
        source_sha256=template300.source_sha256)
    value_a = compute_factor(voi300, aggregated_a, raw[-1].end_nano,
                             sessions=(session,))
    value_b = compute_factor(voi300, aggregated_b, raw[-1].end_nano,
                             sessions=(session,))
    assert value_a.quality.input_hash != value_b.quality.input_hash
    # Spec version changes move the snapshot identity.
    specs = _hand_specs(2)
    snap_a = build_feature_snapshot("RB2610", bars, decision, specs, None, None,
                                    trading_rule(session.start_nano,
                                                 session.end_nano + 10 * MINUTE),
                                    (session,))
    changed = tuple(replace(spec, source_sha256="f" * 64,
                            operator_version="changed-operator") for spec in specs)
    snap_b = build_feature_snapshot("RB2610", bars, decision, changed, None, None,
                                    trading_rule(session.start_nano,
                                                 session.end_nano + 10 * MINUTE),
                                    (session,))
    assert snap_a != snap_b and snap_a.snapshot_id != snap_b.snapshot_id
    # Future distinct appends change nothing.
    futures = tuple(minute_bar(session.start_nano + (10 + index) * MINUTE,
                               Decimal(99999), volume=Decimal(10 ** 9),
                               session=session)
                    for index in range(100))
    snap_c = build_feature_snapshot("RB2610", bars + futures, decision, specs,
                                    None, None,
                                    trading_rule(session.start_nano,
                                                 session.end_nano + 10 * MINUTE),
                                    (session,))
    assert snap_c == snap_a


def test_precheck_e08_registered_baselines_keep_rows_in_snapshots() -> None:
    """All registered same-duration specs - ten plus baselines - keep rows (P11)."""
    from ak_trading.v2.factor_specs import baseline_registry_v2
    session = session_for(date(2026, 1, 5), 0)
    bars = tuple(minute_bar(session.start_nano + index * MINUTE,
                            Decimal(100 + index % 7), session=session)
                 for index in range(60))
    decision = bars[-1].end_nano
    full = factor_registry_v2(60) + baseline_registry_v2(60)
    snapshot = build_feature_snapshot(
        "RB2610", bars, decision, full, None, None,
        trading_rule(session.start_nano, session.end_nano + 10 * MINUTE), (session,))
    keys = {value.factor_key for value in snapshot.values}
    assert {spec.factor_key for spec in full} <= keys
    assert sum(1 for key in keys if key.startswith("BASE:")) == 15  # 006
    # The warmup point also keeps every registered row.
    warm = build_feature_snapshot(
        "RB2610", bars[:10], bars[9].end_nano, full, None, None, None, (session,))
    assert {spec.factor_key for spec in full} <= {value.factor_key for value in warm.values}
    assert warm.quality.status != "READY"


def test_precheck_e01_future_seasonal_model_snapshot_not_ready() -> None:
    """A snapshot fed a model whose training ends later is not READY (P01)."""
    train_sessions = tuple(
        session_for(date(2026, 1, 5) + timedelta(days=index), index, 4)
        for index in range(20))
    train_bars = tuple(
        minute_bar(session.start_nano + slot * MINUTE,
                   Decimal(100) + (Decimal(2) if slot else Decimal(0)),
                   volume=Decimal(10 if slot == 1 else 5), session=session)
        for session in train_sessions for slot in range(4))
    model = fit_seasonality(train_bars, train_sessions,
                            min(s.start_nano for s in train_sessions),
                            max(s.end_nano for s in train_sessions))
    early_session = train_sessions[0]
    early_bars = tuple(minute_bar(early_session.start_nano + slot * MINUTE,
                                  Decimal(100), session=early_session)
                       for slot in range(2))
    snapshot = build_feature_snapshot(
        "RB2610", early_bars, early_bars[-1].end_nano, factor_registry_v2(60),
        None, model,
        trading_rule(early_session.start_nano, early_session.end_nano + 10 * MINUTE),
        train_sessions)
    assert snapshot.quality.status != "READY"
    seasonal = [value for value in snapshot.values
                if value.factor_key.startswith("SEASONAL:")]
    assert seasonal and all(value.quality.status == "INVALID" for value in seasonal)
    assert all("model_fit_after_decision" in value.quality.reasons
               for value in seasonal)

