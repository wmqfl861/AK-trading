import argparse
import math

from tqsdk import TargetPosTask

from ak_trading.config import load_settings
from ak_trading.orders import dump_orders_for_symbol
from ak_trading.runtime import is_valid_price, wait_until
from ak_trading.session import create_api
from ak_trading.symbols import resolve_trade_symbol


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal local TqSim open/close sample")
    parser.add_argument("--timeout", type=float, default=60.0, help="per-stage wall-clock seconds")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("BAD_TIMEOUT", args.timeout)
        return 2

    settings = load_settings()
    api = create_api(settings, sim_balance=1_000_000.0)
    target = None
    position = None
    symbol = ""
    try:
        seed_quote = api.get_quote(settings.default_symbol)

        def seed_ready() -> bool:
            ins_class = str(getattr(seed_quote, "ins_class", "") or "").upper()
            if not seed_quote.datetime or not ins_class:
                return False
            is_main = settings.default_symbol.startswith("KQ.m@") or ins_class in {
                "CONT",
                "CONTINUOUS",
            }
            if is_main and not str(
                getattr(seed_quote, "underlying_symbol", "") or ""
            ).strip():
                return False
            resolve_trade_symbol(seed_quote, settings.default_symbol)
            return True

        try:
            if not wait_until(api, seed_ready, timeout=args.timeout):
                print("RESOLVE_TIMEOUT", settings.default_symbol, seed_quote.datetime)
                return 2
            symbol = resolve_trade_symbol(seed_quote, settings.default_symbol)
        except ValueError as exc:
            print("RESOLVE_FAIL", exc)
            return 2

        quote = api.get_quote(symbol)
        account = api.get_account()
        position = api.get_position(symbol)
        target = TargetPosTask(api, symbol)

        if not wait_until(api, lambda: is_valid_price(quote.last_price), timeout=args.timeout):
            print("QUOTE_TIMEOUT", symbol)
            return 2

        print("sim_symbol", symbol, "last", quote.last_price)
        target.set_target_volume(1)

        if not wait_until(api, lambda: position.pos == 1, timeout=args.timeout):
            print("OPEN_TIMEOUT", "pos", position.pos)
            dump_orders_for_symbol(api, symbol)
            return 3

        print(
            "filled",
            "balance",
            account.balance,
            "available",
            account.available,
            "margin",
            account.margin,
        )
        target.set_target_volume(0)

        if not wait_until(api, lambda: position.pos == 0, timeout=args.timeout):
            print("CLOSE_TIMEOUT", "pos", position.pos)
            dump_orders_for_symbol(api, symbol)
            return 4

        print("flat_done", "balance", account.balance)
        return 0
    finally:
        try:
            if (
                target is not None
                and position is not None
                and position.pos != 0
                and target.is_finished()
                and symbol
            ):
                target = TargetPosTask(api, symbol)
            if target is not None and position is not None and position.pos != 0:
                try:
                    target.set_target_volume(0)
                    flat_timeout = min(args.timeout, 15.0)
                    if not wait_until(
                        api, lambda: position.pos == 0, timeout=flat_timeout
                    ):
                        print("FLATTEN_FAIL", "pos", position.pos, "symbol", symbol)
                        if symbol:
                            dump_orders_for_symbol(api, symbol)
                except Exception as exc:
                    print("FLATTEN_WARN", type(exc).__name__, str(exc)[:200])
            if target is not None and not target.is_finished():
                try:
                    target.cancel()
                    cancel_timeout = min(args.timeout, 15.0)
                    if not wait_until(api, target.is_finished, timeout=cancel_timeout):
                        print("TARGET_CANCEL_TIMEOUT", symbol)
                except Exception as exc:
                    print("TARGET_CANCEL_WARN", type(exc).__name__, str(exc)[:200])
        finally:
            api.close()


if __name__ == "__main__":
    raise SystemExit(main())
