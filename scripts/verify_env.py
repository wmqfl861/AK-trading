import argparse
import math

from ak_trading.config import load_settings
from ak_trading.runtime import is_valid_price, wait_until
from ak_trading.session import create_api


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify TqSdk auth and quote connectivity")
    parser.add_argument("--timeout", type=float, default=20.0, help="wall-clock seconds")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("BAD_TIMEOUT", args.timeout)
        return 2

    settings = load_settings()
    api = create_api(settings)
    try:
        quote = api.get_quote(settings.default_symbol)

        def ready() -> bool:
            return bool(quote.datetime) and is_valid_price(quote.last_price)

        if not wait_until(api, ready, timeout=args.timeout):
            print("CONNECT_TIMEOUT_NO_QUOTE")
            print("datetime", quote.datetime, "last_price", quote.last_price)
            return 2

        print("CONNECT_OK")
        print("symbol", settings.default_symbol)
        print("underlying", getattr(quote, "underlying_symbol", ""))
        print("datetime", quote.datetime)
        print("last_price", quote.last_price)
        print("bid", quote.bid_price1, "ask", quote.ask_price1)
        return 0
    finally:
        api.close()


if __name__ == "__main__":
    raise SystemExit(main())
