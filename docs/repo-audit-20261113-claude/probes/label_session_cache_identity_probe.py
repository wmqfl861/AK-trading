"""Round-5 external probe (advisory, read-only): the session cache in
``v2/probability/labels.py`` is keyed by ``id(sessions)`` without keeping a
reference to the tuple.

``labels._product_sessions_ordered`` (labels.py:57-68) caches the sorted product
sessions under ``(id(sessions), product)`` and revalidates a hit only by the
NUMBER of product sessions. CPython reuses the address of a freed tuple, so a
second calendar with the same number of sessions can receive the first
calendar's cached ordering. ``_expected_minute_slots`` then enumerates the
wrong calendar and ``label_forecast`` scores a request against slots that do
not belong to it.

The other identity caches in the tree keep the object and compare with ``is``
(``clock._get_calendar``, ``factors._sessions_bundle``, ``data`` visibility
index). This one does not.

The script frees calendar A, allocates calendar B (same length, shifted by one
hour), and reports whether B was served A's slots. Address reuse is an
interpreter detail, so the script retries a few allocations and prints what it
observed; nothing in the product tree is modified.

Run (repository root):
    python docs/repo-audit-20261113-claude/probes/label_session_cache_identity_probe.py
Exit code 1 = stale slots were served (the finding), 0 = not reproduced here.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.v2.contracts import SessionWindow  # noqa: E402
from ak_trading.v2.probability import labels  # noqa: E402

NANO = 1_000_000_000
MINUTE = 60 * NANO
HOUR = 3600 * NANO
PRODUCT = "SYN.rb"
BASE = 1_768_179_600 * NANO  # 2026-01-12 09:00 CST


def session_list(shift_hours: int) -> list[SessionWindow]:
    start = BASE + shift_hours * HOUR
    return [
        SessionWindow(f"s{index}-shift{shift_hours}", PRODUCT, date(2026, 1, 12),
                      start + index * 2 * HOUR, start + index * 2 * HOUR + 10 * MINUTE,
                      "DAY", "SYNTHETIC-R5-CACHE", "SCENARIO")
        for index in range(3)]


def first_slot(sessions) -> int:
    window_start = min(s.start_nano for s in sessions)
    window_end = max(s.end_nano for s in sessions)
    slots = labels._expected_minute_slots(sessions, PRODUCT, window_start, window_end)
    return slots[0][1] if slots else -1


def main() -> int:
    stale = 0
    reused = 0
    attempts = 50
    for _ in range(attempts):
        cache = getattr(labels, "_SESSION_STARTS_CACHE", None)
        if hasattr(cache, "clear"):
            cache.clear()                    # start every attempt from an empty cache
        later = session_list(1)              # same length, every session one hour later
        a = tuple(session_list(0))
        address = id(a)
        a_first = first_slot(a)              # populates the cache under id(a)
        assert a_first == a[0].start_nano
        del a                                # the tuple is freed; the cache entry stays
        b = tuple(later)                     # the next 3-tuple usually takes the freed slot
        if id(b) != address:
            continue
        reused += 1
        if first_slot(b) != b[0].start_nano:
            stale += 1
    print(f"attempts={attempts} address_reused={reused} stale_slots_served={stale}")
    if stale:
        print("FINDING: a calendar was served the minute slots of a freed calendar "
              "that had occupied the same address")
        return 1
    print("not reproduced in this interpreter run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
