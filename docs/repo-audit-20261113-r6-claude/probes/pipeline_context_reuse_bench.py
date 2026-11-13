"""Pipeline-level check of K10b: does context construction still rebuild the clock index?

Round 5 (Z02) measured the clock module in isolation. This probe measures what K10b
was meant to change: repeated ``build_strategy_context`` calls on one input bundle.
It runs the same calls twice, once with the merged code and once with the session
filter put back to the pre-K10b expression (a fresh tuple per context), and prints
the time per context and the clock's own rebuild counters.

SYNTHETIC K05 fixtures only; no product file is modified. Run from the repository root:

    python docs/repo-audit-20261113-r6-claude/probes/pipeline_context_reuse_bench.py

Exit 0 when the merged code performs no further session validation or minute-slot
materialization after the first context; exit 1 otherwise.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path.cwd()
for extra in (ROOT / "src", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from ak_trading.v2 import clock  # noqa: E402
from ak_trading.v2.decision import pipeline  # noqa: E402
from ak_trading.v2.probability import labels  # noqa: E402
from ak_trading.v2.probability.forecast import predict_market, publish_market_forecast  # noqa: E402
import v2_realistic_fixtures as fx  # noqa: E402
from test_v2_m4_ledger import account, run_policy  # noqa: E402

CALLS = 12
REPEATS = 3


def _clear() -> None:
    pipeline._clear_product_sessions_cache()
    labels._clear_session_starts_cache()
    clock._clear_calendar_index_cache()


def _contexts(scn, strategy, payoff, calls: int) -> float:
    spec = strategy.spec
    now = scn["decisions"][-1]

    def provider(model_set, request, regime):
        daily = request.features.duration_seconds == 86400
        if daily and request.horizon.unit == "TRADING_DAY" and request.horizon.value == 1:
            return predict_market(model_set, request, regime, request.features.decision_nano)
        if daily and request.horizon.unit == "TRADING_DAY" and request.horizon.value == 20:
            return publish_market_forecast(model_set, request, regime,
                                           clock_nano=lambda at=request.features.decision_nano: at)
        return publish_market_forecast(model_set, request, regime, clock_nano=lambda: now)

    started = time.perf_counter()
    for _ in range(calls):
        pipeline.build_strategy_context(
            scn["bundle"], fx.CONTRACT, now, account(str(fx.EQUITY)), None, scn["base_models"],
            payoff, spec, None, scn["reference"], run_policy(), (),
            exit_binding=pipeline.legacy_exit_binding(spec), forecast_provider=provider)
    return (time.perf_counter() - started) * 1000.0 / calls


def _measure(days: int, legacy: bool):
    scn = fx.scenario("TREND", "MINUTE", calendar=fx.exchange_calendar(days))
    strategy = fx.strategy_registry_v2("MINUTE")["trend_m1_v2"]
    payoff = fx.shared_seed_payoff()
    original = pipeline._cached_product_sessions
    if legacy:
        pipeline._cached_product_sessions = lambda sessions, product: tuple(
            s for s in sessions if s.product == product)
    try:
        best, growth = None, None
        for _ in range(REPEATS):
            _clear()
            _contexts(scn, strategy, payoff, 1)            # first context pays the one-off build
            after_first = clock._index_stats()
            per_call = _contexts(scn, strategy, payoff, CALLS)
            after_all = clock._index_stats()
            best = per_call if best is None else min(best, per_call)
            growth = {key: after_all[key] - after_first[key] for key in after_first
                      if isinstance(after_first[key], int)}
        return best, growth
    finally:
        pipeline._cached_product_sessions = original
        _clear()


def main() -> int:
    print(f"{'days':>5} {'sessions':>9} {'pre-K10b ms/ctx':>16} {'merged ms/ctx':>14} {'ratio':>7}  "
          "extra validations / minute materializations after the first context (pre-K10b | merged)")
    clean = True
    for days in (40, 120, 250):
        sessions = len(fx.exchange_calendar(days))
        old_ms, old_growth = _measure(days, legacy=True)
        new_ms, new_growth = _measure(days, legacy=False)
        ov, om = old_growth.get("session_validations", 0), old_growth.get("minute_slot_materializations", 0)
        nv, nm = new_growth.get("session_validations", 0), new_growth.get("minute_slot_materializations", 0)
        clean = clean and nv == 0 and nm == 0
        print(f"{days:>5} {sessions:>9} {old_ms:>16.3f} {new_ms:>14.3f} {old_ms / new_ms:>6.1f}x  "
              f"{ov} / {om} | {nv} / {nm}")
    print("merged code rebuilds nothing after the first context" if clean
          else "merged code still rebuilds the clock index")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
