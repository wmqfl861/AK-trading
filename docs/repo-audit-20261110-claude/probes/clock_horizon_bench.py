"""Round-4 external probe: per-call cost of the v2 trading-clock functions.

Measures ``ak_trading.v2.clock.horizon_window`` / ``can_open_before_close`` /
``holding_deadline`` against calendar length and checks an equivalent
pre-indexed variant (bisect over a per-(calendar, product) index) on random
decision times.

SYNTHETIC calendar only (RB-like: 345 planned minutes per trading day in four
fragments). Read-only: imports product code, writes nothing, changes nothing.

Usage (repository root):
    python docs/repo-audit-20261110-claude/probes/clock_horizon_bench.py
    python docs/repo-audit-20261110-claude/probes/clock_horizon_bench.py --days 5 20 60
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ak_trading.v2 import clock  # noqa: E402
from ak_trading.v2.contracts import Horizon, SessionWindow  # noqa: E402
from ak_trading.v2.policy import holding_policy  # noqa: E402

NS = 1_000_000_000
M = 60 * NS
PRODUCT = "SYN.rb"
CST = timezone(timedelta(hours=8))
# (kind, start hour, start minute, end hour, end minute, belongs to previous evening)
FRAGMENTS = (
    ("NIGHT", 21, 0, 23, 0, True),
    ("DAY", 9, 0, 10, 15, False),
    ("DAY", 10, 30, 11, 30, False),
    ("DAY", 13, 30, 15, 0, False),
)


def _nano(moment: datetime) -> int:
    return int(moment.timestamp()) * NS


def build_calendar(days: int) -> tuple[SessionWindow, ...]:
    """``days`` consecutive weekdays; the night fragment starts on the
    previous weekday evening and belongs to the next trading day."""
    sessions: list[SessionWindow] = []
    current = date(2025, 1, 2)
    previous_weekday = date(2025, 1, 1)
    produced = 0
    while produced < days:
        if current.weekday() < 5:
            for index, (kind, sh, sm, eh, em, evening) in enumerate(FRAGMENTS):
                base = previous_weekday if evening else current
                start = _nano(datetime(base.year, base.month, base.day, sh, sm, tzinfo=CST))
                end = _nano(datetime(base.year, base.month, base.day, eh, em, tzinfo=CST))
                sessions.append(SessionWindow(
                    session_id=f"{current.isoformat()}-{index}", product=PRODUCT,
                    trading_day=current, start_nano=start, end_nano=end, kind=kind,
                    calendar_version="SYNTHETIC-probe-calendar", evidence_status="SCENARIO"))
            previous_weekday = current
            produced += 1
        current += timedelta(days=1)
    return tuple(sessions)


class CalendarIndex:
    """Equivalent pre-index for one (calendar, product).

    Built once: O(total planned minutes). Queries: O(log n).
    Mirrors clock.horizon_window exactly for a non-overlapping calendar
    (the product function rejects overlapping sessions before anything else).
    """

    def __init__(self, sessions: tuple[SessionWindow, ...], product: str) -> None:
        ordered = sorted((s for s in sessions if s.product == product),
                         key=lambda s: (s.start_nano, s.end_nano))
        slots: list[tuple[int, int]] = []
        for session in ordered:
            span = session.end_nano - session.start_nano
            if span % M:
                raise clock.ClockError("session_not_minute_aligned")
            slots.extend((session.start_nano + i * M, session.start_nano + (i + 1) * M)
                         for i in range(span // M))
        slots.sort()
        self.slot_starts = [slot[0] for slot in slots]
        self.slot_ends = [slot[1] for slot in slots]
        self.session_starts = [s.start_nano for s in ordered]
        self.sessions = ordered
        first: dict[date, int] = {}
        last: dict[date, int] = {}
        for session in ordered:
            first.setdefault(session.trading_day, session.start_nano)
            first[session.trading_day] = min(first[session.trading_day], session.start_nano)
            last[session.trading_day] = max(last.get(session.trading_day, session.end_nano),
                                            session.end_nano)
        self.days = sorted(first, key=lambda day: first[day])
        self.day_first = [first[day] for day in self.days]
        self.day_last = [last[day] for day in self.days]
        self.day_position = {day: i for i, day in enumerate(self.days)}

    def horizon_window(self, decision_nano: int, horizon: Horizon) -> tuple[int, int]:
        if horizon.unit == "TRADING_MINUTE":
            first = bisect_right(self.slot_starts, decision_nano)
            if first >= len(self.slot_starts):
                raise clock.ClockError("no_future_trading_minutes")
            if len(self.slot_starts) - first < horizon.value:
                raise clock.ClockError("horizon_exceeds_calendar")
            return self.slot_starts[first], self.slot_ends[first + horizon.value - 1]
        if not self.days:
            raise clock.ClockError("no_trading_days_in_calendar")
        position = bisect_right(self.session_starts, decision_nano) - 1
        containing = None
        if position >= 0 and decision_nano < self.sessions[position].end_nano:
            containing = self.sessions[position]
        if containing is not None:
            current = self.day_position[containing.trading_day]
        else:
            current = bisect_right(self.day_first, decision_nano) - 1
        upcoming_from = current + 1  # current == -1 -> every day is upcoming
        if len(self.days) - upcoming_from < horizon.value:
            raise clock.ClockError("horizon_exceeds_calendar")
        return self.day_first[upcoming_from], self.day_last[upcoming_from + horizon.value - 1]


def _call(function, *args):
    try:
        return ("ok", function(*args))
    except clock.ClockError as error:
        return ("ClockError", str(error))


def _time_per_call(function, calls: int) -> float:
    started = time.perf_counter()
    for _ in range(calls):
        function()
    return (time.perf_counter() - started) * 1000.0 / calls


def measure(days: int, samples: int, seed: int) -> dict:
    sessions = build_calendar(days)
    index = CalendarIndex(sessions, PRODUCT)
    rng = random.Random(seed)
    low = sessions[0].start_nano - 3 * 3600 * NS
    high = sessions[-1].end_nano + 3 * 3600 * NS
    decisions = [rng.randrange(low, high) // NS * NS for _ in range(samples)]
    # boundary decisions: exact session starts/ends and minute edges
    for session in rng.sample(sessions, min(len(sessions), 12)):
        decisions.extend((session.start_nano, session.end_nano, session.start_nano - M,
                          session.end_nano - M, session.start_nano + M))
    horizons = (Horizon("TRADING_MINUTE", 1), Horizon("TRADING_MINUTE", 30),
                Horizon("TRADING_MINUTE", 60), Horizon("TRADING_DAY", 1),
                Horizon("TRADING_DAY", 5), Horizon("TRADING_DAY", 20))
    checked = 0
    for decision in decisions:
        for horizon in horizons:
            expected = _call(clock.horizon_window, decision, PRODUCT, horizon, sessions)
            produced = _call(index.horizon_window, decision, horizon)
            if expected != produced:
                raise AssertionError(
                    f"MISMATCH days={days} decision={decision} horizon={horizon}: "
                    f"{expected} != {produced}")
            checked += 1
    middle = sessions[len(sessions) // 2].start_nano + 17 * M
    minute30 = Horizon("TRADING_MINUTE", 30)
    day1 = Horizon("TRADING_DAY", 1)
    policy = holding_policy("MINUTE")
    calls = max(3, min(40, 4000 // max(days, 1)))
    between = sessions[len(sessions) // 2].start_nano - 20 * M  # outside every session
    return {
        "days": days,
        "planned_minutes": len(index.slot_starts),
        "equal_checks": checked,
        "minute_ms": _time_per_call(
            lambda: clock.horizon_window(middle, PRODUCT, minute30, sessions), calls),
        "day_inside_ms": _time_per_call(
            lambda: clock.horizon_window(middle, PRODUCT, day1, sessions), calls),
        "day_outside_ms": _time_per_call(
            lambda: clock.horizon_window(between, PRODUCT, day1, sessions), calls),
        "can_open_ms": _time_per_call(
            lambda: clock.can_open_before_close(middle, PRODUCT, policy, sessions), calls),
        "indexed_minute_ms": _time_per_call(
            lambda: index.horizon_window(middle, minute30), 20000),
        "indexed_day_ms": _time_per_call(
            lambda: index.horizon_window(between, day1), 20000),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, nargs="*", default=[5, 20, 60, 120, 250])
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--seed", type=int, default=20261110)
    args = parser.parse_args()
    print("SYNTHETIC calendar; product code is imported read-only.")
    header = ("days", "minutes", "minute h=30", "day h=1 in-session", "day h=1 off-session",
              "can_open", "indexed minute", "indexed day", "speedup(minute)", "equal checks")
    print(" | ".join(header))
    for days in args.days:
        row = measure(days, args.samples, args.seed)
        print(" | ".join((
            str(row["days"]), str(row["planned_minutes"]),
            f"{row['minute_ms']:.3f} ms", f"{row['day_inside_ms']:.3f} ms",
            f"{row['day_outside_ms']:.3f} ms", f"{row['can_open_ms']:.3f} ms",
            f"{row['indexed_minute_ms'] * 1000:.2f} us", f"{row['indexed_day_ms'] * 1000:.2f} us",
            f"{row['minute_ms'] / row['indexed_minute_ms']:.0f}x", str(row["equal_checks"]))))
    print("All sampled results equal the product implementation (values and ClockError reasons).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
