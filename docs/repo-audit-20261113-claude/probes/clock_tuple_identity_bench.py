"""Round-5 external benchmark (advisory, read-only): does the K10-core calendar
index help the real call pattern?

``v2/clock.py`` (main e4fe37b) caches its calendar index by the IDENTITY of the
``sessions`` tuple. ``decision/pipeline.build_strategy_context`` builds a new
tuple on every call (pipeline.py:390,
``sessions=tuple(s for s in bundle.sessions if s.product==instrument.product)``),
so every context starts with a cache miss and rebuilds the index once.

This script times three call patterns on SYNTHETIC calendars shaped like a
night-session product (345 planned minutes per trading day):

    legacy      the pre-index implementation kept in tests/v2_clock_legacy_oracle.py
    same tuple  one tuple object reused for every call (index hit)
    new tuple   an equal but new tuple for every "context", then CALLS_PER_CONTEXT
                clock calls on it (what the pipeline does today)

and reports the cost per context. No product file is modified.

Run (repository root):
    python docs/repo-audit-20261113-claude/probes/clock_tuple_identity_bench.py
"""
from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from ak_trading.v2 import clock  # noqa: E402
from ak_trading.v2.contracts import HoldingPolicy, Horizon, SessionWindow  # noqa: E402
import v2_clock_legacy_oracle as legacy  # noqa: E402

NANO = 1_000_000_000
MINUTE = 60 * NANO
HOUR = 3600 * NANO
DAY = 86400 * NANO
PRODUCT = "SYN.rb"
CALLS_PER_CONTEXT = 12      # forecast requests, context deadline, per-strategy checks
CONTEXTS = 30
REPEATS = 3
FRAGMENTS = ((21, 0, 23, 0, True), (9, 0, 10, 15, False),
             (10, 30, 11, 30, False), (13, 30, 15, 0, False))


def calendar(days: int) -> tuple[SessionWindow, ...]:
    base = date(2026, 1, 12)
    origin = 1_768_147_200 * NANO  # 2026-01-11T16:00Z = 2026-01-12 00:00 CST
    sessions, current, made = [], base, 0
    while made < days:
        if current.weekday() < 5:
            for index, (sh, sm, eh, em, evening) in enumerate(FRAGMENTS):
                anchor = current - timedelta(days=1) if evening else current
                midnight = origin + (anchor - base).days * DAY
                sessions.append(SessionWindow(
                    f"{current.isoformat()}-{index}", PRODUCT, current,
                    midnight + sh * HOUR + sm * MINUTE, midnight + eh * HOUR + em * MINUTE,
                    "NIGHT" if evening else "DAY", "SYNTHETIC-R5-BENCH", "SCENARIO"))
            made += 1
        current += timedelta(days=1)
    return tuple(sessions)


def one_context(decision: int, sessions, module) -> None:
    """The mix of clock calls one strategy context makes."""
    policy = HoldingPolicy(intraday=True, allow_lunch=True, allow_night_to_day=True,
                           stop_open_before_minutes=5, flatten_before_minutes=1)
    for _ in range(CALLS_PER_CONTEXT // 3):
        module.horizon_window(decision, PRODUCT, Horizon("TRADING_MINUTE", 30), sessions)
        module.holding_deadline(decision, PRODUCT, policy, sessions)
        module.can_open_before_close(decision, PRODUCT, policy, sessions)


def per_context_ms(days: int, pattern: str) -> float:
    base = calendar(days)
    day_sessions = [s for s in base if s.kind == "DAY"]
    middle = day_sessions[len(day_sessions) // 2]
    decisions = [middle.start_nano + (3 + i) * MINUTE + 1 for i in range(CONTEXTS)]
    clock._clear_calendar_index_cache()
    if pattern == "same tuple":
        one_context(decisions[0], base, clock)      # warm the index once
    started = time.perf_counter()
    for decision in decisions:
        if pattern == "legacy":
            one_context(decision, base, legacy)
        elif pattern == "same tuple":
            one_context(decision, base, clock)
        else:
            # the pipeline's own expression: equal content, new identity
            fresh = tuple(s for s in base if s.product == PRODUCT)
            assert fresh is not base and fresh == base
            one_context(decision, fresh, clock)
    return (time.perf_counter() - started) * 1000 / CONTEXTS


def main() -> int:
    print(f"calls per context: {CALLS_PER_CONTEXT}; contexts timed: {CONTEXTS}; best of {REPEATS}; "
          f"{sum((eh - sh) * 60 + em - sm for sh, sm, eh, em, _ in FRAGMENTS)} planned minutes/day")
    print(f"{'days':>5} {'minute slots':>13} {'legacy ms/ctx':>14} {'new tuple ms/ctx':>17} "
          f"{'same tuple ms/ctx':>18} {'new/same':>9}")
    for days in (5, 20, 60, 250):
        # best of REPEATS runs per cell: the minimum is the least noisy estimate
        legacy_ms = min(per_context_ms(days, "legacy") for _ in range(REPEATS))
        fresh_ms = min(per_context_ms(days, "new tuple") for _ in range(REPEATS))
        same_ms = min(per_context_ms(days, "same tuple") for _ in range(REPEATS))
        print(f"{days:>5} {days * 345:>13} {legacy_ms:>14.2f} {fresh_ms:>17.3f} "
              f"{same_ms:>18.4f} {fresh_ms / same_ms:>9.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
