"""Real-track M2 regressions on the reviewed r2 SHFE.RB collection shape.

These tests construct SMALL synthetic projections that mirror the real
calendar shape verified in the r2 review (94 VERIFIED / 358 SCENARIO days,
night sessions owned by the next trading day, per-day single selected
contract from overlapping candidates, honest fee-availability gaps), then
assert the 005/006 semantics on them.  They never load the 2.49M-bar
collection into the unit suite; the full rebuild is the run driver's job.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ak_trading.v2.contracts import DataQuality, MarketBar, SessionWindow
from ak_trading.v2.factor_specs import (
    baseline_registry_v2,
    factor_registry_v2,
)
from ak_trading.v2.factors import build_feature_snapshot, compute_factor

NANO = 1_000_000_000
MINUTE = 60 * NANO
DAY = 86_400 * NANO
T0 = int(datetime(2023, 1, 3, 1, 0, tzinfo=timezone.utc).timestamp()) * NANO
QUALITY = DataQuality(status="READY", reasons=(), temporal_basis="reconstructed",
                      input_hash="0" * 64)
PRODUCT = "SHFE.RB"


def _session(sid, day, start_hour, minutes, kind, evidence="SCENARIO"):
    start = int(datetime(day.year, day.month, day.day, start_hour, 0,
                         tzinfo=timezone.utc).timestamp()) * NANO
    return SessionWindow(session_id=sid, product=PRODUCT, trading_day=day,
                         start_nano=start, end_nano=start + minutes * MINUTE,
                         kind=kind, calendar_version="SHFE-rb-test-v1",
                         evidence_status=evidence)


def _bar(session, slot, open_, close, contract="SHFE.rb2305", volume=10,
         available=None):
    start = session.start_nano + slot * MINUTE
    end = start + MINUTE
    return MarketBar(
        bar_id=f"{contract}|{start}", contract_id=contract, raw_symbol="rb2305",
        product=PRODUCT, duration_seconds=60, datetime_nano=start,
        start_nano=start, end_nano=end, trading_day=session.trading_day,
        session_id=session.session_id,
        available_at_nano=available if available is not None else end,
        imported_at_nano=None, open=Decimal(open_),
        high=Decimal(max(open_, close) + 1), low=Decimal(min(open_, close) - 1),
        close=Decimal(close), volume=Decimal(volume), open_oi=Decimal(1000),
        close_oi=Decimal(1000), complete=True, expected_minutes=1,
        actual_minutes=1, source="real-track-projection", quality=QUALITY)


def test_real_scenario_sessions_still_produce_planned_slots() -> None:
    """F2: 358/452 SCENARIO days must keep 005 semantics — slots exist,
    factors compute, evidence level never blocks consumption nor upgrades."""
    scenario_day = _session("N1", date(2023, 1, 3), 1, 30, "DAY", "SCENARIO")
    bars = tuple(_bar(scenario_day, i, 4000 + i, 4000 + i) for i in range(5))
    specs = baseline_registry_v2(60)
    snapshot = build_feature_snapshot(
        "SHFE.rb2305", bars, bars[-1].end_nano, specs, None, None, None,
        (scenario_day,))
    assert snapshot.quality.status in ("READY", "MISSING")
    close = next(v for v in snapshot.values
                   if v.factor_key.startswith("BASE:close:"))
    assert close.value == Decimal(4004)
    # temporal basis of the output follows the bars, not the calendar label
    assert snapshot.quality.temporal_basis == "reconstructed"


def test_real_night_session_owns_next_trading_day() -> None:
    """Night bars belong to the frozen SessionWindow's trading day; the
    cross-midnight gap return stays INCLUDE_GAP and slot numbering restarts
    per kind per day."""
    friday = date(2023, 1, 6)
    monday = date(2023, 1, 9)
    day_am = _session("F-AM", friday, 1, 30, "DAY")
    night_start = int(datetime(friday.year, friday.month, friday.day, 15, 0,
                               tzinfo=timezone.utc).timestamp()) * NANO
    night = SessionWindow(  # Friday 23:00 CN physically, owned by Monday
        session_id="F-NIGHT", product=PRODUCT, trading_day=monday,
        start_nano=night_start, end_nano=night_start + 2 * MINUTE,
        kind="NIGHT", calendar_version="SHFE-rb-test-v1",
        evidence_status="SCENARIO")
    monday_am = _session("M-AM", monday, 1, 30, "DAY")
    bars = []
    bars.append(_bar(day_am, 0, 4000, 4010))
    bars.append(_bar(day_am, 1, 4010, 4005))
    bars.append(_bar(night, 0, 4008, 4012))
    bars.append(_bar(night, 1, 4012, 4009))
    bars.append(_bar(monday_am, 0, 4011, 4015))
    bars = tuple(bars)
    snapshot = build_feature_snapshot(
        "SHFE.rb2305", bars, bars[-1].end_nano,
        factor_registry_v2(60) + baseline_registry_v2(60), None, None, None,
        (day_am, night, monday_am))
    assert snapshot.trading_day == monday
    gap = next(v for v in snapshot.values if "opening_gap" in v.factor_key)
    # Monday AM open 4011 vs night-session close 4009 -> gap 2
    assert gap.value == Decimal(2) and gap.quality.status == "READY"


def test_real_roll_day_gap_and_window_isolation() -> None:
    """On roll days the selected contract changes; the new contract's first
    session has no same-contract prior close and windows never merge."""
    day1 = date(2023, 4, 6)
    day2 = date(2023, 4, 7)
    old_contract = "SHFE.rb2305"
    new_contract = "SHFE.rb2310"
    am1 = _session("R1", day1, 1, 30, "DAY")
    am2 = _session("R2", day2, 1, 30, "DAY")
    bars = [
        _bar(am1, 0, 4100, 4105, old_contract),
        _bar(am1, 1, 4105, 4108, old_contract),
        _bar(am2, 0, 4112, 4115, new_contract),
        _bar(am2, 1, 4115, 4118, new_contract),
    ]
    # new contract alone: opening gap has no same-contract prior session
    new_bars = tuple(b for b in bars if b.contract_id == new_contract)
    snapshot = build_feature_snapshot(
        new_contract, new_bars, new_bars[-1].end_nano,
        factor_registry_v2(60) + baseline_registry_v2(60), None, None, None,
        (am1, am2))
    gap = next(v for v in snapshot.values if "opening_gap" in v.factor_key)
    assert gap.value is None and gap.quality.status == "MISSING"
    assert "prior_session_close_missing" in gap.quality.reasons
    # mixing contracts in one snapshot is rejected outright
    from ak_trading.v2.factors import FactorsError
    with pytest.raises(FactorsError, match="mixed_contract_bars"):
        build_feature_snapshot(new_contract, tuple(bars), bars[-1].end_nano,
                               factor_registry_v2(60), None, None, None,
                               (am1, am2))


def test_real_declared_session_without_bars_breaks_window() -> None:
    """A declared open session whose bars are entirely absent is a missing
    slot run, never skipped."""
    day1 = date(2023, 6, 1)
    day2 = date(2023, 6, 2)
    am1 = _session("D1", day1, 1, 30, "DAY")
    empty = _session("D2", day2, 1, 30, "DAY")  # declared, no bars
    day3 = date(2023, 6, 3)
    am3 = _session("D3", day3, 1, 30, "DAY")
    bars = [
        _bar(am1, 0, 4200, 4202),
        _bar(am1, 1, 4202, 4200),
        _bar(am3, 0, 4210, 4212),
        _bar(am3, 1, 4212, 4214),
    ]
    mom = next(s for s in factor_registry_v2(60) if s.source_factor_id == "MOM001")
    value = compute_factor(mom, tuple(bars), bars[-1].end_nano,
                           sessions=(am1, empty, am3))
    assert value.value is None
    assert "missing_slot_in_window" in value.quality.reasons


def test_real_aggregate_tail_not_complete_never_fakes_prior_close() -> None:
    """A 55-minute session tail: 60s aggregation marks the tail bar
    incomplete, and the exact-endpoint prior close uses the last full
    minute ending at the declared session end — never an earlier bar."""
    from ak_trading.v2.clock import aggregate_bars
    day1 = date(2023, 3, 1)
    day2 = date(2023, 3, 2)
    odd = _session("T1", day1, 1, 55, "DAY")  # 55 planned minutes
    nxt = _session("T2", day2, 1, 30, "DAY")
    bars = tuple(_bar(odd, i, 4300 + i, 4300 + i) for i in range(55))
    aggregated = aggregate_bars(bars, (odd,), 60, odd.end_nano)
    # The session is minute-aligned; each 60s bar is complete here.  A
    # NON-aligned tail (e.g. 50 minutes) is the incomplete case:
    odd50 = _session("T1b", day1, 1, 50, "DAY")
    bars50 = tuple(_bar(odd50, i, 4300 + i, 4300 + i) for i in range(50))
    agg50 = aggregate_bars(bars50, (odd50,), 300, odd50.end_nano)  # 5-minute
    assert agg50 and all(b.complete for b in agg50)  # 50 % 5 == 0 -> complete
    full = bars + (_bar(nxt, 0, 4360, 4361), _bar(nxt, 1, 4361, 4362))
    snapshot = build_feature_snapshot(
        "SHFE.rb2305", full, full[-1].end_nano,
        factor_registry_v2(60) + baseline_registry_v2(60), None, None, None,
        (odd, nxt))
    gap = next(v for v in snapshot.values if "opening_gap" in v.factor_key)
    # prior session close = last minute close 4354; next open 4360 -> gap 6
    assert gap.value == Decimal(6) and gap.quality.status == "READY"
