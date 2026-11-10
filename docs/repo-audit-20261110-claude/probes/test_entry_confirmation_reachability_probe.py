"""Round-4 external probe (advisory; not a product test, not a hard review).

Question: can an entry setup still be alive when its last required
confirmation is observed?

Background. Since b653f7e (2026-10-26, "count strategy confirmations once per
completed bar") an expired observation period no longer lends credit: the
shared counter restarts at 1. TREND and BREAKOUT need three confirmations and
give their setup a three-bar life, so the third observation must land before
``context.now_nano > setup.expires_at_nano`` becomes true.

Two cases are exercised with product functions imported read-only:

A. MINUTE. ``setup_expiry_nano`` puts the expiry at the END of the third bar.
   A decision taken exactly at that instant still counts; a decision taken
   any time later (bars become available after they close) finds the setup
   expired, and the counter cycles 1, 2, 1, 2 ... for ever.
   The project's own suite shows the same thing end to end:
       python scripts/test_offline.py -q tests/test_v2_forward_paper.py
   gives 68 passed at b653f7e^ and 20 passed / 48 failed from b653f7e to
   b75d220 (all 48: WAITING_ENTRY_CONFIRMATION; that fixture decides one
   second after the bar closes).

B. DAILY. For daily specs ``setup_expiry_nano`` returns the START of the
   n-th session fragment after the setup, not the end of the n-th trading
   day. On a one-session-per-day synthetic calendar that is late enough. On an
   exchange-shaped calendar (night session plus three day fragments) a
   three-bar setup created at the close expires at 10:30 the next morning,
   before the next daily bar exists, and a five-bar setup expires that
   evening.

Each test asserts what a tradable strategy needs, so at the audited revision
(main b75d220) the result is 5 failed / 4 passed: the two MINUTE cases with a
positive delay and the three DAILY cases on the exchange-shaped calendar fail;
the zero-delay case and the three single-session controls pass. That outcome
is the finding, not a broken probe. How to repair it (expiry inclusive of the
decision that observes the last bar, expiry counted in bars rather than
instants, daily expiry by trading day) is the plan author's decision.

Run (repository root):
    python scripts/test_offline.py -q \
        docs/repo-audit-20261110-claude/probes/test_entry_confirmation_reachability_probe.py
"""
from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

TESTS = Path(__file__).resolve().parents[3] / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from ak_trading.v2.contracts import SessionWindow  # noqa: E402
from ak_trading.v2.strategies.registry import (  # noqa: E402
    advance_confirmation_count,
    build_strategy_spec,
    fresh_state,
    observation_bars,
    setup_expiry_nano,
)
from ak_trading.v2.strategies.trend import TrendFollowing  # noqa: E402

import test_v2_m3b_matrix as matrix  # noqa: E402

NS = 1_000_000_000
MINUTE = 60 * NS
CST = timezone(timedelta(hours=8))
PRODUCT = "SYN.rb"


# --- A. MINUTE: three confirmations against a three-bar setup ------------------

@pytest.mark.parametrize("label,delay", [
    ("decision exactly at the bar close", 0),
    ("decision one nanosecond after the bar close", 1),
    ("decision one second after the bar close", NS),
])
def test_minute_trend_reaches_three_confirmations(label, delay):
    """Real TrendFollowing._advance_setup + advance_confirmation_count over
    three consecutive completed bars; the admission gate is held open so only
    the setup clock is under test."""
    spec = build_strategy_spec("TREND", "MINUTE")
    strategy = TrendFollowing(spec)
    closes = [100 + index for index in range(24)]
    state = fresh_state(spec.strategy_id, matrix.CONTRACT)
    counts = []
    for visible in (22, 23, 24):
        bars = matrix.minute_bars(closes[:visible])
        now = bars[-1].end_nano + delay
        context = matrix.make_context(
            spec, bars=bars, now_override=now,
            features_snap=matrix.features(Decimal(str(closes[visible - 1])), Decimal("4"),
                                          decision=now))
        setup = strategy._advance_setup(
            context, state.setup, 1, observation_bars(context), 3, Decimal("110"), Decimal("4"))
        count = advance_confirmation_count(context, state, gate_ok=True, setup=setup)
        counts.append(count)
        state = replace(state, setup=setup, confirmation_count=count)
    assert counts == [1, 2, 3], f"{label}: confirmation counts {counts}"


# --- B. DAILY: setup life against the next daily observations ------------------

def _nano(moment: datetime) -> int:
    return int(moment.timestamp()) * NS


def _exchange_shaped_calendar(days: int) -> tuple[SessionWindow, ...]:
    """Night 21:00-23:00 on the previous weekday evening, then 09:00-10:15,
    10:30-11:30 and 13:30-15:00."""
    fragments = (("NIGHT", 21, 0, 23, 0, True), ("DAY", 9, 0, 10, 15, False),
                 ("DAY", 10, 30, 11, 30, False), ("DAY", 13, 30, 15, 0, False))
    sessions = []
    current, previous, made = date(2026, 3, 3), date(2026, 3, 2), 0
    while made < days:
        if current.weekday() < 5:
            for index, (kind, sh, sm, eh, em, evening) in enumerate(fragments):
                base = previous if evening else current
                sessions.append(SessionWindow(
                    f"{current.isoformat()}-{index}", PRODUCT, current,
                    _nano(datetime(base.year, base.month, base.day, sh, sm, tzinfo=CST)),
                    _nano(datetime(base.year, base.month, base.day, eh, em, tzinfo=CST)),
                    kind, "SYNTHETIC-probe", "SCENARIO"))
            previous, made = current, made + 1
        current += timedelta(days=1)
    return tuple(sessions)


def _single_session_calendar(days: int) -> tuple[SessionWindow, ...]:
    sessions = []
    current, made = date(2026, 3, 3), 0
    while made < days:
        if current.weekday() < 5:
            sessions.append(SessionWindow(
                f"{current.isoformat()}-0", PRODUCT, current,
                _nano(datetime(current.year, current.month, current.day, 9, 0, tzinfo=CST)),
                _nano(datetime(current.year, current.month, current.day, 15, 0, tzinfo=CST)),
                "DAY", "SYNTHETIC-probe", "SCENARIO"))
            made += 1
        current += timedelta(days=1)
    return tuple(sessions)


def _daily_closes(sessions: tuple[SessionWindow, ...]) -> list[int]:
    last: dict[date, int] = {}
    for session in sessions:
        last[session.trading_day] = max(last.get(session.trading_day, 0), session.end_nano)
    return [last[day] for day in sorted(last)]


@pytest.mark.parametrize("calendar_name,calendar", [
    ("exchange-shaped (night + three day fragments)", _exchange_shaped_calendar),
    ("one session per day (shape of the synthetic fixtures)", _single_session_calendar),
])
@pytest.mark.parametrize("family,setup_bars,confirmations", [
    ("TREND / BREAKOUT", 3, 3),
    ("PULLBACK", 5, 3),
    ("REVERSION / GRID / MARTINGALE", 5, 3),
])
def test_daily_setup_outlives_its_required_observations(calendar_name, calendar, family,
                                                        setup_bars, confirmations):
    sessions = calendar(8)
    closes = _daily_closes(sessions)
    created_at = closes[1]                       # setup created on this day's daily bar
    last_needed = closes[1 + confirmations - 1]  # close of the bar giving the last confirmation
    context = SimpleNamespace(features=SimpleNamespace(product=PRODUCT), sessions=sessions,
                              spec=SimpleNamespace(mode="DAILY"))
    expiry = setup_expiry_nano(context, created_at, setup_bars)
    as_cst = lambda nano: datetime.fromtimestamp(nano / NS, CST).strftime("%m-%d %H:%M")
    assert expiry >= last_needed, (
        f"{family} on {calendar_name}: {setup_bars}-bar setup created {as_cst(created_at)} "
        f"expires {as_cst(expiry)}, but confirmation {confirmations} is observed at "
        f"{as_cst(last_needed)} at the earliest")
