"""Replay one concrete contract's frozen daily history under explicit assumptions.

Only donchian_trend, ma_trend and zscore_reversion are supported; carry requires
far legs. Signals use completed observation prefixes, fill at the next stored
observation open, and close at the precommitted final observation close. The
result is an unverified execution scenario, never strict historical execution.
All nine economic config fields are required; no settings or services are used.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} must be provided exactly once")
        setattr(namespace, self.dest, values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--input-bundle", type=Path, required=True, action=_Once)
    parser.add_argument("--symbol", required=True, action=_Once, help="one exact raw concrete symbol, e.g. SHFE.rb2101")
    parser.add_argument("--strategy", required=True, action=_Once,
                        choices=("donchian_trend", "ma_trend", "zscore_reversion"))
    parser.add_argument("--config", type=Path, required=True, action=_Once,
                        help="explicit JSON: initial_cash, lots, multiplier, price_tick, fee_per_lot, fee_rate, slippage_ticks, margin_rate, max_margin_ratio")
    parser.add_argument("--output", type=Path, required=True, action=_Once, help="new directory outside the input bundle")
    args = parser.parse_args(argv)
    from ak_trading.futures.scenario_backtest import run_history_scenario
    try:
        result = run_history_scenario(args.input_bundle, args.output, symbol=args.symbol,
                                      strategy=args.strategy, config_path=args.config)
    except (ValueError, OSError, RuntimeError, ArithmeticError) as exc:
        print(json.dumps({"status": "rejected", "error_type": type(exc).__name__,
                          "reason": str(exc)[:500], "historical_strict_execution": False},
                         ensure_ascii=False, allow_nan=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
