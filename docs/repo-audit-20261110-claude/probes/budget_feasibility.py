"""Round-4 external probe: smallest account equity at which a v2 strategy can
freeze a round budget, under the frozen research policy.

For one contract description (price, ATR, multiplier, tick, margin, fees) this
script calls the product's own money formulas -- ``strategies.state.
round_loss_cny`` / ``peak_margin_cny`` and ``policy.calculate_stress_loss`` --
and applies the three caps that ``strategies.state.freeze_round_budget``
checks before any OPEN proposal can exist:

    planned round loss  <= equity * round_risk_ratio              (0.005)
    stress loss         <= equity * maximum_stress_loss_to_equity (0.02)
    stressed peak margin<= equity * maximum_strategy_margin_ratio (0.10)

Below the printed equity the strategy answers BUDGET_INFEASIBLE on every bar,
whatever the market does. Directional families (TREND / BREAKOUT / PULLBACK)
need one lot with a 1-ATR stop; MARTINGALE needs the whole 1+2+4 = 7-lot
template. Range geometry (stop one ATR outside the frozen range) depends on
the range width, so it is entered explicitly with --range-width-atr.

Inputs are the caller's ASSUMPTIONS, printed back with the result. The
built-in rows are illustrative round numbers, not market data and not
verified exchange parameters. Read-only: imports product code, writes nothing.

Usage (repository root):
    python docs/repo-audit-20261110-claude/probes/budget_feasibility.py
    python docs/repo-audit-20261110-claude/probes/budget_feasibility.py \
        --name RB --price 3200 --atr 45 --multiplier 10 --tick 1 \
        --margin 0.13 --fee-rate 0.0001 --fee-per-lot 0
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ak_trading.v2 import policy as m1_policy  # noqa: E402
from ak_trading.v2.contracts import FeeLeg, LayerSpec, TradingRule  # noqa: E402
from ak_trading.v2.strategies.registry import COMMON_PARAMETER_DEFAULTS  # noqa: E402
from ak_trading.v2.strategies.state import peak_margin_cny, round_loss_cny  # noqa: E402

D = Decimal
# (name, price, daily ATR, minute ATR, multiplier, tick, margin rate, fee rate, fee per lot)
ILLUSTRATIVE = (
    ("RB", "3200", "45", "4", "10", "1", "0.13", "0.0001", "0"),
    ("M", "3000", "40", "3", "10", "1", "0.12", "0", "1.5"),
    ("I", "780", "18", "1.5", "100", "0.5", "0.15", "0.0001", "0"),
    ("CU", "78000", "900", "60", "5", "10", "0.12", "0.00005", "0"),
    ("AU", "780", "9", "0.6", "1000", "0.02", "0.13", "0", "10"),
    ("SC", "520", "12", "0.8", "1000", "0.1", "0.15", "0", "20"),
)


def _rule(multiplier: D, tick: D, margin: D, fee_rate: D, fee_per_lot: D) -> TradingRule:
    leg = FeeLeg(per_lot_cny=fee_per_lot, notional_rate=fee_rate,
                 explicit_zero=fee_per_lot == 0 and fee_rate == 0)
    return TradingRule(
        rule_id="probe-rule", contract_id="PROBE", effective_from_nano=0,
        effective_until_nano=10**18, available_at_nano=0, multiplier=multiplier,
        price_tick=tick, lot_step=1, min_open_lots=1, open_fee=leg, close_fee=leg,
        close_today_fee=leg, long_margin_rate=margin, short_margin_rate=margin,
        limit_up=None, limit_down=None, slippage_ticks=D("1"), impact_ticks=D("0"),
        participation_rate=D("0.1"), evidence="SCENARIO", source_sha256="a" * 64)


def minimum_equity(layers: tuple[LayerSpec, ...], stop: D, atr: D, rule: TradingRule) -> dict:
    research = m1_policy.research_policy()
    stress_policy = m1_policy.stress_policy()
    planned = round_loss_cny(layers, 1, stop, rule, atr)
    stress = m1_policy.calculate_stress_loss(layers, 1, stop, atr, rule, stress_policy)
    margin = peak_margin_cny(layers, 1, rule, atr, stress_policy)
    caps = {
        "round loss": planned / D(str(COMMON_PARAMETER_DEFAULTS["round_risk_ratio"])),
        "stress loss": stress / D(str(research["maximum_stress_loss_to_equity"])),
        "margin": margin / D(str(research["maximum_strategy_margin_ratio"])),
    }
    binding = max(caps, key=lambda name: caps[name])
    return {"planned": planned, "stress": stress, "margin": margin,
            "minimum_equity": caps[binding], "binding": binding}


def directional(price: D, atr: D, rule: TradingRule) -> dict:
    layers = (LayerSpec(index=0, trigger_price=price, reference_entry_price=price, lots=1),)
    stop = price - D(str(COMMON_PARAMETER_DEFAULTS["stop_loss_multiple"])) * atr
    return minimum_equity(layers, stop, atr, rule)


def martingale(price: D, atr: D, width_atr: D, rule: TradingRule) -> dict:
    """Long side of a frozen range [L, U] of ``width_atr`` ATRs; layers at
    L + 0.25W / 0.15W / 0.05W with 1/2/4 lots, stop at L - 1 ATR. The first
    layer's known fill reference is taken at its trigger line."""
    width = width_atr * atr
    lower = price - D("0.25") * width
    triggers = tuple(lower + fraction * width for fraction in (D("0.25"), D("0.15"), D("0.05")))
    layers = tuple(LayerSpec(index=i, trigger_price=t, reference_entry_price=t, lots=lots)
                   for i, (t, lots) in enumerate(zip(triggers, (1, 2, 4))))
    return minimum_equity(layers, lower - atr, atr, rule)


def _fmt(value: D) -> str:
    return f"{int(value.to_integral_value(rounding='ROUND_CEILING')):,}"


def report(name: str, price: D, atr: D, label: str, rule: TradingRule, width_atr: D) -> None:
    one = directional(price, atr, rule)
    seven = martingale(price, atr, width_atr, rule)
    print(f"{name:>3} {label:<6} price={price} ATR={atr} | 1 lot: min equity {_fmt(one['minimum_equity'])} CNY "
          f"(binding: {one['binding']}; planned {_fmt(one['planned'])}, stress {_fmt(one['stress'])}, "
          f"stressed margin {_fmt(one['margin'])}) | MARTINGALE 1+2+4: min equity "
          f"{_fmt(seven['minimum_equity'])} CNY (binding: {seven['binding']})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name")
    for option in ("--price", "--atr", "--multiplier", "--tick", "--margin", "--fee-rate",
                   "--fee-per-lot"):
        parser.add_argument(option, type=D)
    parser.add_argument("--range-width-atr", type=D, default=D("4"),
                        help="frozen range width in ATRs for the MARTINGALE row (default 4)")
    args = parser.parse_args()
    research = m1_policy.research_policy()
    print("Frozen caps: round loss <= {} of equity, stress loss <= {}, stressed strategy margin <= {}"
          .format(COMMON_PARAMETER_DEFAULTS["round_risk_ratio"],
                  research["maximum_stress_loss_to_equity"],
                  research["maximum_strategy_margin_ratio"]))
    if args.price is not None:
        missing = [o for o in ("atr", "multiplier", "tick", "margin") if getattr(args, o) is None]
        if missing:
            parser.error("with --price also give: " + ", ".join("--" + m for m in missing))
        rule = _rule(args.multiplier, args.tick, args.margin, args.fee_rate or D("0"),
                     args.fee_per_lot or D("0"))
        report(args.name or "X", args.price, args.atr, "input", rule, args.range_width_atr)
        return 0
    print("ILLUSTRATIVE ASSUMPTIONS (round numbers, not market data, not verified exchange rules):")
    for name, price, daily_atr, minute_atr, multiplier, tick, margin, fee_rate, per_lot in ILLUSTRATIVE:
        rule = _rule(D(multiplier), D(tick), D(margin), D(fee_rate), D(per_lot))
        report(name, D(price), D(daily_atr), "DAILY", rule, args.range_width_atr)
        report(name, D(price), D(minute_atr), "MINUTE", rule, args.range_width_atr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
