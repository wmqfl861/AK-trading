import argparse
import math

from ak_trading.config import load_settings
from ak_trading.runtime import wait_until
from ak_trading.session import create_api


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal live quote / 60s kline sample")
    parser.add_argument("--timeout", type=float, default=60.0, help="wall-clock seconds")
    parser.add_argument("--updates", type=int, default=5, help="quote updates to collect")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("BAD_TIMEOUT", args.timeout)
        return 2
    if args.updates <= 0:
        print("BAD_UPDATES", args.updates)
        return 2

    settings = load_settings()
    symbol = settings.default_symbol
    api = create_api(settings)
    try:
        quote = api.get_quote(symbol)
        klines = api.get_kline_serial(symbol, 60, data_length=50)
        print("subscribed", symbol)
        updates = 0

        def done() -> bool:
            return updates >= args.updates

        def on_update() -> None:
            nonlocal updates
            if api.is_changing(quote, ["last_price", "ask_price1", "bid_price1"]):
                print(
                    "quote",
                    quote.datetime,
                    "last",
                    quote.last_price,
                    "bid",
                    quote.bid_price1,
                    "ask",
                    quote.ask_price1,
                    "oi",
                    quote.open_interest,
                )
                updates += 1
            if api.is_changing(klines.iloc[-1], "datetime"):
                bar = klines.iloc[-1]
                print("kline60", bar.datetime, "close", bar.close, "volume", bar.volume)

        if not wait_until(api, done, timeout=args.timeout, on_update=on_update):
            print("QUOTE_TIMEOUT", "updates", updates)
            return 2
        return 0
    finally:
        api.close()


if __name__ == "__main__":
    raise SystemExit(main())
