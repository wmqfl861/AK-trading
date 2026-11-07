"""R2 probe: cost of ``v2.data.visible_bars`` per call, and an equivalent variant.

External review round 3 (2026-11-07), see ../REVIEW.md section R2.

``visible_bars`` scans every bar of the bundle on each call and, for every
minute bar, linearly searches the product's sessions. ``replay.py`` and
``payoff_paths.py`` call it once per execution event only to feed
``capacity_before`` the last 20 bars. This script measures the per-call cost on
the SYNTHETIC world built by ``tests/test_v2_m5_experiments.py::_t7cpu_bundle``
and compares it with a variant that applies the decision-independent filter
once per (bundle, contract, duration) and bisects on max(end, available).
The variant must return exactly the same tuple at every sampled decision time.

It is NOT a fix and changes no product file. It only covers the call shape
used by replay/payoff (``intraday=False``). No network, data or credentials.

Run from the repository root (a plain script, not a pytest module):

    python docs/repo-audit-20261107-claude/probes/visible_bars_bench.py

Observed 2026-11-07 (Linux, 2 vCPU, Python 3.13.17): 0.74 -> 16.47 ms per call
for 1,200 -> 9,600 minute bars (8x data, 22x time); the variant was 9-13x faster
and equal at all 30 sampled decision times for every size.
"""
from __future__ import annotations

import random
import sys
import time
from bisect import bisect_right
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from ak_trading.v2.contracts import MarketBar  # noqa: E402
from ak_trading.v2.data import visible_bars  # noqa: E402
import test_v2_m5_experiments as world  # noqa: E402

_STATIC: dict[tuple[int, str, int], tuple] = {}


def visible_bars_indexed(bundle, contract_id, duration_seconds, decision_nano, intraday):
    """Same result as ``visible_bars`` for ``intraday=False``."""
    if intraday:
        raise NotImplementedError("probe covers the replay/payoff call shape only")
    key = (id(bundle), contract_id, duration_seconds)
    hit = _STATIC.get(key)
    if hit is None or hit[0] is not bundle:
        products = {b.product for b in bundle.bars if b.contract_id == contract_id}
        sessions = [s for s in bundle.sessions if s.product in products]

        def on_grid(bar):  # identical rule and session order as the original
            if not isinstance(bar, MarketBar) or bar.duration_seconds != 60:
                return True
            for session in sessions:
                if session.start_nano <= bar.start_nano < session.end_nano:
                    return (bar.start_nano - session.start_nano) % 60_000_000_000 == 0
            return True

        static = [b for b in bundle.bars
                  if b.contract_id == contract_id and b.duration_seconds == duration_seconds
                  and b.complete and b.available_at_nano is not None
                  and b.quality.status == "READY" and b.quality.temporal_basis != "unknown"
                  and on_grid(b)]
        static.sort(key=lambda b: max(b.end_nano, b.available_at_nano))
        keys = [max(b.end_nano, b.available_at_nano) for b in static]
        hit = (bundle, static, keys)
        _STATIC[key] = hit
    _, static, keys = hit
    prefix = static[:bisect_right(keys, decision_nano)]
    return tuple(sorted(prefix, key=lambda b: (b.start_nano, b.datetime_nano)))


def main() -> int:
    print(f"{'days':>5} {'minute bars':>11} {'orig ms/call':>13} "
          f"{'indexed ms/call':>16} {'speedup':>8} {'equal':>6}")
    all_equal = True
    for days in (5, 10, 20, 40):
        bundle, _sessions, contract = world._t7cpu_bundle(days, 240)
        minutes = sorted((b for b in bundle.bars if b.duration_seconds == 60),
                         key=lambda b: b.end_nano)
        rng = random.Random(7)
        points = [minutes[-1].end_nano] + [rng.choice(minutes).end_nano for _ in range(29)]
        _STATIC.clear()
        equal = all(visible_bars(bundle, contract, 60, p, False)
                    == visible_bars_indexed(bundle, contract, 60, p, False) for p in points)
        all_equal = all_equal and equal
        calls = 20
        start = time.perf_counter()
        for _ in range(calls):
            visible_bars(bundle, contract, 60, points[0], False)
        original = (time.perf_counter() - start) / calls * 1000
        start = time.perf_counter()
        for _ in range(calls):
            visible_bars_indexed(bundle, contract, 60, points[0], False)
        indexed = (time.perf_counter() - start) / calls * 1000
        print(f"{days:>5} {len(minutes):>11} {original:>13.2f} {indexed:>16.3f} "
              f"{original / indexed:>7.0f}x {str(equal):>6}")
    return 0 if all_equal else 1


if __name__ == "__main__":
    raise SystemExit(main())
