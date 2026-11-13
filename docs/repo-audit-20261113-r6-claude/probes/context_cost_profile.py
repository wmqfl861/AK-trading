"""Where the time of one decision context goes after K10b (cProfile, SYNTHETIC fixtures).

Builds the same context 40 times on the K05 realistic fixture and prints the
plain time per context, the share of three groups and the most expensive
functions. It is a measurement, not a test: it always exits 0. No product file
is modified. Run from the repository root:

    python docs/repo-audit-20261113-r6-claude/probes/context_cost_profile.py [MINUTE|DAILY] [calendar days] [FAMILY]

Defaults: MINUTE 250 TREND. The fixture's models hold a few hundred training
values each; ``empirical_apply_scaling_bench.py`` shows how the model-application
share grows with real sample sizes.
"""
from __future__ import annotations

import cProfile
import pstats
import sys
import time
from pathlib import Path

ROOT = Path.cwd()
for extra in (ROOT / "src", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from ak_trading.v2.decision import pipeline  # noqa: E402
from ak_trading.v2.probability.forecast import predict_market, publish_market_forecast  # noqa: E402
import v2_realistic_fixtures as fx  # noqa: E402
from test_v2_m4_ledger import account, run_policy  # noqa: E402

CONTEXTS = 40


def _short(filename: str) -> str:
    """Path relative to the repository root when the file is inside it."""
    try:
        return Path(filename).resolve().relative_to(ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        return filename
GROUPS = (
    ("applying the forecast models", "apply_model_set"),
    ("building feature snapshots", "_features"),
    ("hashing the model set", "model_hashes_for"),
)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "MINUTE"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 250
    family = sys.argv[3] if len(sys.argv) > 3 else "TREND"
    scn = fx.scenario(family, mode, calendar=fx.exchange_calendar(days))
    suffix = "m1" if mode == "MINUTE" else "d1"
    spec = fx.strategy_registry_v2(mode)[f"{family.lower()}_{suffix}_v2"].spec
    payoff = fx.shared_seed_payoff()
    now = scn["decisions"][-1]

    def provider(model_set, request, regime):
        daily = request.features.duration_seconds == 86400
        if daily and request.horizon.unit == "TRADING_DAY" and request.horizon.value == 1:
            return predict_market(model_set, request, regime, request.features.decision_nano)
        if daily and request.horizon.unit == "TRADING_DAY" and request.horizon.value == 20:
            return publish_market_forecast(model_set, request, regime,
                                           clock_nano=lambda at=request.features.decision_nano: at)
        return publish_market_forecast(model_set, request, regime, clock_nano=lambda: now)

    def one():
        return pipeline.build_strategy_context(
            scn["bundle"], fx.CONTRACT, now, account(str(fx.EQUITY)), None, scn["base_models"],
            payoff, spec, None, scn["reference"], run_policy(), (),
            exit_binding=pipeline.legacy_exit_binding(spec), forecast_provider=provider)

    one()                                             # the first context pays the one-off builds
    started = time.perf_counter()
    for _ in range(CONTEXTS):
        one()
    plain = (time.perf_counter() - started) * 1000.0 / CONTEXTS

    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(CONTEXTS):
        one()
    profiler.disable()
    stats = pstats.Stats(profiler)
    total = stats.total_tt
    rows = []
    for (filename, line, name), (_, calls, own, cumulative, _) in stats.stats.items():
        rows.append((cumulative, own, calls, name, _short(filename), line))

    print(f"mode={mode} family={family} calendar days={days} sessions={len(scn['bundle'].sessions)} "
          f"bars={len(scn['bundle'].bars)}")
    print(f"plain time per context: {plain:.1f} ms (the profile below runs under cProfile and is slower)")
    print("\nshare of the profiled time")
    for label, function in GROUPS:
        share = max((c for c, _, _, name, _, _ in rows if name == function), default=0.0) / total
        print(f"  {share:6.1%}  {label} ({function})")
    print("\nfunctions by own time")
    for cumulative, own, calls, name, filename, line in sorted(rows, key=lambda r: -r[1])[:12]:
        print(f"  {own / total:6.1%}  calls/context={calls / CONTEXTS:10.1f}  {filename}:{line} {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
