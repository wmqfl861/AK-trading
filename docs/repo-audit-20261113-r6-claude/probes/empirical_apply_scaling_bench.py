"""Z13: three costs in the forecast path grow with the size of the training sample.

``probability/models.py`` keeps the raw training values inside the two empirical
models and inside the model set, and walks them again on every use:

* ``_apply_empirical_cdf`` counts ``0 < value <= step`` over the whole list once per
  horizon step, so one prediction costs (horizon x sample size) comparisons;
* ``_apply_empirical_quantile`` converts the whole list to float once per quantile;
* ``ForecastModelSet.content_hash`` re-serializes the whole model set, and
  ``decision/pipeline.model_hashes_for`` calls it once per decision context.

Model selection applies each candidate to every validation sample, so fitting cost
grows with (validation size x horizon x training size).

This probe times the production functions on SYNTHETIC models of three sizes and
compares the first two with a one-pass reference that returns exactly the same
values. It reads and changes no product file. Run from the repository root:

    python docs/repo-audit-20261113-r6-claude/probes/empirical_apply_scaling_bench.py

Exit 1 = the finding is present: one CDF prediction gets more than 20 times slower
         when the training sample gets 100 times larger, or a second content_hash
         call on the same object costs more than 5% of the first;
exit 0 = neither is the case any more;
exit 2 = the reference disagrees with production (then the probe itself is wrong
         and its timings must not be used).
"""
from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path

ROOT = Path.cwd()
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ak_trading.v2.probability import models  # noqa: E402

NS = 1_000_000_000
LEVELS = ("SYN|DAY|TREND", "SYN|DAY", "SYN")
SIZES = (1_000, 10_000, 100_000)
HORIZONS = (20, 60)
TAUS = list(models.CANONICAL_QUANTILE_TAUS)


def reference_cdf(samples: list[float], horizon: int) -> list[tuple[float, float]]:
    """One pass over the sample, then a running total: same counts, same floats."""
    total = len(samples)
    ambiguous = 0
    histogram = [0] * (horizon + 2)
    for value in samples:
        if value < 0:
            ambiguous += 1
        elif value > 0:
            bucket = math.ceil(value)
            if bucket <= horizon:
                histogram[bucket] += 1
    unknown = ambiguous / total
    out, running = [], 0
    for step in range(1, horizon + 1):
        running += histogram[step]
        out.append((running / total, unknown))
    return out


def reference_quantile(samples: list[float], taus: list[float]) -> list[float]:
    values = [float(v) for v in samples]            # converted once, not once per quantile
    return [models._empirical_quantile(values, q) for q in taus]


def best_ms(function, repeats: int) -> float:
    best = None
    for _ in range(3):
        started = time.perf_counter()
        for _ in range(repeats):
            function()
        elapsed = (time.perf_counter() - started) * 1000.0 / repeats
        best = elapsed if best is None else min(best, elapsed)
    return best


def main() -> int:
    rng = random.Random(20261113)
    mismatch = False

    print("1. time-to-event model (EMPIRICAL_CDF), one prediction")
    print(f"{'train n':>9} {'horizon':>8} {'production ms':>14} {'one-pass ms':>12} {'ratio':>8}")
    cdf_ms: dict[tuple[int, int], float] = {}
    for size in SIZES:
        for horizon in HORIZONS:
            steps = [rng.choice((-1, 0, *range(1, horizon + 1))) for _ in range(size)]
            model = models._fit_empirical_cdf(steps, [LEVELS] * size, [], [], [])
            samples = [float(v) for v in model["samples"][LEVELS[0]]]
            if models._apply_empirical_cdf(model, LEVELS, horizon) != reference_cdf(samples, horizon):
                mismatch = True
            repeats = max(1, 200_000 // (size * horizon // 20))
            production = best_ms(lambda: models._apply_empirical_cdf(model, LEVELS, horizon), repeats)
            reference = best_ms(lambda: reference_cdf(samples, horizon), max(3, repeats))
            cdf_ms[(size, horizon)] = production
            print(f"{size:>9} {horizon:>8} {production:>14.3f} {reference:>12.3f} {production / reference:>7.1f}x")

    print(f"\n2. ATR quantile model (EMPIRICAL_QUANTILE), one prediction of {len(TAUS)} quantiles")
    print(f"{'distinct n':>10} {'production ms':>14} {'convert-once ms':>16} {'ratio':>8}")
    for size in SIZES:
        values = [rng.random() * 5 for _ in range(size)]
        model = models._fit_empirical_quantile(values, [LEVELS] * size, TAUS, [], [], [], 10)
        stored = model["samples"][LEVELS[0]]
        if models._apply_empirical_quantile(model, LEVELS, TAUS) != reference_quantile(stored, TAUS):
            mismatch = True
        repeats = max(1, 20_000 // (size // 100))
        production = best_ms(lambda: models._apply_empirical_quantile(model, LEVELS, TAUS), repeats)
        reference = best_ms(lambda: reference_quantile(stored, TAUS), max(3, repeats))
        print(f"{len(stored):>10} {production:>14.3f} {reference:>16.3f} {production / reference:>7.1f}x")

    print("\n3. ForecastModelSet.content_hash(), called once per decision context")
    print(f"{'models':>7} {'n each':>8} {'stored values':>14} {'first call ms':>14} {'second call ms':>15}")
    second_share = 0.0
    for count, size in ((8, 1_000), (8, 10_000), (8, 100_000)):
        built = {}
        for k in range(count):
            steps = [rng.choice((-1, 0, *range(1, 61))) for _ in range(size)]
            built[f"SYN|MINUTE|{k}"] = models._fit_empirical_cdf(steps, [LEVELS] * size, [], [], [])
        model_set = models.ForecastModelSet(
            "set", "reference", (), NS, 2 * NS, 1000 * NS,
            models.default_fit_config((), NS, 2 * NS, 1000 * NS), built, (), {})
        stored = sum(len(v) for m in built.values() for v in m["samples"].values())
        started = time.perf_counter(); first_hash = model_set.content_hash()
        first = (time.perf_counter() - started) * 1000.0
        started = time.perf_counter(); second_hash = model_set.content_hash()
        second = (time.perf_counter() - started) * 1000.0
        if first_hash != second_hash:
            mismatch = True
        second_share = second / first
        print(f"{count:>7} {size:>8} {stored:>14,} {first:>14.1f} {second:>15.1f}")

    if mismatch:
        print("\nreference and production disagree: do not use these timings")
        return 2
    growth = cdf_ms[(SIZES[-1], 60)] / cdf_ms[(SIZES[0], 60)]
    print(f"\nCDF prediction, horizon 60: {SIZES[-1] // SIZES[0]}x more training data "
          f"-> {growth:.0f}x more time per prediction")
    validation = 20_000
    hours = cdf_ms[(SIZES[-1], 60)] * validation / 1000.0 / 3600.0
    print(f"selecting one such model against {validation:,} validation samples would spend "
          f"about {hours:.1f} hours in this function alone on this machine")
    print(f"content_hash: the second call on the same object costs {second_share:.0%} of the first")
    return 1 if growth > 20 or second_share > 0.05 else 0


if __name__ == "__main__":
    raise SystemExit(main())
