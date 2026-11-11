"""K01 observation-clock regression (r4-plan01 §3): setup expiry runs on
the shared bar-end predicate, never wall time.

Covers the binding semantics directly: delivery delays (0, 1ns, 1s, 59s)
never change which bar an observation counts; a bar ending exactly at the
expiry keeps the period (TREND retains its frozen setup there); only a bar
ending strictly later expires it; DAILY setups expire at the nth distinct
trading-day close (establishment day counts as #1) with no fallback; an
insufficient DAILY calendar declines establishment; and with no eligible
(completed, available, same-frequency) bar everything fails closed at zero
credit. Delivery latency is not another bar.

Unit worlds come from the A2 duplicate-bar module (SYNTHETIC); realistic
calendar arithmetic from the K05 fixture module (SYNTHETIC).
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import v2_realistic_fixtures as rf
from ak_trading.v2.strategies.registry import (
    _planned_setup_expiry_nano,
    advance_confirmation_count,
    build_strategy_spec,
    fresh_state,
    observation_bars,
    setup_expired,
)
from test_v2_m3b_duplicate_bar import build_world, run
from test_v2_m3b_matrix import MINUTE, NANO

ENTRY_FAMILIES = ("TREND", "BREAKOUT", "PULLBACK", "REVERSION", "GRID", "MARTINGALE")
DELAYS = {"0s": 0, "1ns": 1, "1s": 1_000_000_000, "59s": 59 * 1_000_000_000}


@pytest.mark.parametrize("delay_ns", DELAYS.values(), ids=list(DELAYS))
@pytest.mark.parametrize("mode", ["MINUTE", "DAILY"])
@pytest.mark.parametrize("family", ENTRY_FAMILIES)
def test_delayed_decision_confirms_on_the_same_bar(family, mode, delay_ns):
    """Each decision is delivered ``delay_ns`` after its bar's close; the
    counts and the third-bar OPEN are identical to the zero-delay run."""
    world = build_world(family, mode)
    trace = run(world, ("s0", "s1", "s2"),
                per_step={0: {"delay_ns": delay_ns},
                          1: {"delay_ns": delay_ns},
                          2: {"delay_ns": delay_ns}})
    assert trace == [("WAIT", 1), ("WAIT", 2), ("OPEN", 3)], trace


@pytest.mark.parametrize("mode", ["MINUTE", "DAILY"])
@pytest.mark.parametrize("family", ENTRY_FAMILIES)
def test_duplicate_eligible_bar_delivery_holds(family, mode):
    world = build_world(family, mode)
    trace = run(world, ("s0", "s0", "s1", "s1", "s2"))
    assert trace == [("WAIT", 1), ("WAIT", 1), ("WAIT", 2), ("WAIT", 2), ("OPEN", 3)]


def test_trend_equality_retains_frozen_setup():
    """At a bar ending exactly at the setup expiry, TREND keeps the SAME
    observation period (setup identity and frozen fields), and the counter
    reaches 3 — the A2-era reconstruction at equality is gone."""
    strategy, steps = build_world("TREND", "MINUTE")
    from ak_trading.v2.strategies.state import commit_observation
    state = None
    for key in ("s0", "s1"):
        proposal = strategy.evaluate(steps[key](), state)
        state = commit_observation(state, proposal)
    setup = state.setup
    assert setup is not None
    expiry = setup.expires_at_nano
    # the third bar ends exactly at the expiry
    context = steps["s2"]()
    bars = observation_bars(context)
    assert bars[-1].end_nano == expiry
    assert not setup_expired(context, setup)
    proposal = strategy.evaluate(context, state)
    state = commit_observation(state, proposal)
    assert proposal.action == "OPEN" and state.confirmation_count == 3
    carried = state.setup
    assert carried is not None and carried.setup_id == setup.setup_id
    assert carried.frozen_ma == setup.frozen_ma and carried.frozen_atr == setup.frozen_atr


def test_setup_expired_is_bar_end_not_wall_clock():
    """setup_expired compares the latest eligible bar END with the expiry:
    wall-clock lateness after the bar close never expires the period."""
    strategy, steps = build_world("REVERSION", "MINUTE")
    from ak_trading.v2.strategies.state import commit_observation
    state = None
    proposal = strategy.evaluate(steps["s0"](), state)
    state = commit_observation(state, proposal)
    setup = state.setup
    assert setup is not None
    late = steps["s0"](delay_ns=59 * 1_000_000_000)
    assert not setup_expired(late, setup)
    # a genuinely later bar end (the far re-arming bar) does expire it
    later = steps["arm"]()
    assert setup_expired(later, setup)


def test_daily_expiry_is_nth_trading_day_close():
    """Establishment day counts as #1; nights, the lunch break and the
    weekend never create extra DAILY observations."""
    calendar = rf.exchange_calendar(12)
    bounds = rf.trading_day_bounds(calendar)
    day0_close = bounds[0][1]
    context = _calendar_context(calendar, day0_close, "DAILY")
    three = _planned_setup_expiry_nano(rf.PRODUCT, "DAILY", day0_close, 3,
                                       calendar)
    five = _planned_setup_expiry_nano(rf.PRODUCT, "DAILY", day0_close, 5,
                                      calendar)
    assert three == bounds[2][1]
    assert five == bounds[4][1]
    assert context is not None  # silence linter on the helper's use below


def test_daily_insufficient_calendar_returns_none():
    calendar = rf.exchange_calendar(4)
    bounds = rf.trading_day_bounds(calendar)
    start = bounds[3][1]
    assert _planned_setup_expiry_nano(rf.PRODUCT, "DAILY", start, 5, calendar) is None
    # zero-remaining spans resolve at the establishment close itself
    assert _planned_setup_expiry_nano(rf.PRODUCT, "DAILY", start, 1, calendar) == start


def test_insufficient_calendar_declines_range_establishment():
    """range_entry_setup declines arming with SETUP_CALENDAR_INSUFFICIENT
    and zero confirmation credit when the DAILY calendar is too short."""
    from ak_trading.v2.strategies.registry import range_entry_setup
    calendar = rf.exchange_calendar(4)
    bounds = rf.trading_day_bounds(calendar)
    day3_close = bounds[3][1]
    context = _calendar_context(calendar, day3_close, "DAILY")
    state = fresh_state("reversion_d1_v2", rf.CONTRACT)
    setup, side, reason = range_entry_setup(context, state, Decimal("0.2"), 5)
    assert setup is None and side == 0
    assert reason == "SETUP_CALENDAR_INSUFFICIENT"


def test_no_eligible_bar_fails_closed():
    """A completed-but-unavailable bar is not an observation: the counter
    fails closed at zero, and a created setup earns no credit."""
    strategy, steps = build_world("REVERSION", "MINUTE")
    from ak_trading.v2.strategies.state import commit_observation
    state = None
    proposal = strategy.evaluate(steps["s0"](), state)
    state = commit_observation(state, proposal)
    assert state.confirmation_count == 1

    first = steps["s0"]()
    from dataclasses import replace as _replace
    before_first_bar = _replace(first, now_nano=first.bars[0].available_at_nano - 1)
    assert observation_bars(before_first_bar) == ()
    # no eligible bar at all: the creation shortcut yields no credit and
    # the counter fails closed at zero
    assert advance_confirmation_count(
        before_first_bar, state, gate_ok=True, setup=None, created=True) == 0
    assert advance_confirmation_count(
        before_first_bar, state, gate_ok=True, setup=None, created=False) == 0


def _calendar_context(calendar, decision_nano, mode):
    from ak_trading.v2.contracts import FactorValue, FeatureSnapshot, FrozenRange
    from test_v2_m3b_matrix import make_context, quality, regime
    spec = build_strategy_spec("REVERSION", mode)
    features = FeatureSnapshot(
        "snap-clock", rf.CONTRACT, "rb2605", rf.PRODUCT,
        rf.calendar_trading_days(calendar)[0], "sess", "DAY", decision_nano,
        decision_nano - 1, 86400, ("b0",), Decimal("100"), Decimal("1"),
        (FactorValue("META:price_tick:v2.1.0", Decimal("0.1"),
                     decision_nano - 1, quality()),),
        None, quality())
    frozen = FrozenRange("range-clock", decision_nano, Decimal("96"),
                         Decimal("100"), Decimal("104"), Decimal("95"),
                         Decimal("105"), "snap-clock")
    observation = rf.daily_bar(3, Decimal("97.0"), calendar)
    return make_context(spec, bars=(observation,), forecasts=(),
                        features_snap=features,
                        regime_obj=regime("ORDERED_RANGE", (), frozen),
                        sessions_override=calendar, now_override=decision_nano)
