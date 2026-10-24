import argparse
import json
from pathlib import Path

from ak_trading.persistence.config import (
    load_database_settings,
    redact_database_text,
)
from ak_trading.persistence.database import create_database_engine
from ak_trading.persistence.query import (
    MarketDataQueryError,
    export_market_bars_csv,
    parse_aware_datetime_nano,
    query_market_bars,
    summarize_market_data_quality,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read AK-trading PostgreSQL market data")
    subparsers = parser.add_subparsers(dest="command", required=True)

    quality = subparsers.add_parser("quality", help="summarize provable data quality")
    quality.add_argument("--symbol")
    quality.add_argument("--duration", type=int)

    listing = subparsers.add_parser("list", help="list a bounded bar range as JSON")
    listing.add_argument("--symbol", required=True)
    listing.add_argument("--duration", type=int, required=True)
    listing.add_argument("--start", help="inclusive timezone-aware ISO datetime")
    listing.add_argument("--end", help="exclusive timezone-aware ISO datetime")
    listing.add_argument("--limit", type=int, default=100)

    export = subparsers.add_parser("export", help="atomically export strict K-line CSV")
    export.add_argument("--symbol", required=True)
    export.add_argument("--duration", type=int, required=True)
    export.add_argument("--start", help="inclusive timezone-aware ISO datetime")
    export.add_argument("--end", help="exclusive timezone-aware ISO datetime")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--force", action="store_true")
    return parser


def _bounds(args) -> tuple[int | None, int | None]:
    start = parse_aware_datetime_nano(args.start, "--start") if args.start else None
    end = parse_aware_datetime_nano(args.end, "--end") if args.end else None
    if start is not None and end is not None and start >= end:
        raise MarketDataQueryError("--start must be before --end")
    return start, end


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = None
    engine = None
    try:
        start = end = None
        if args.command in {"list", "export"}:
            start, end = _bounds(args)
        if args.command == "quality":
            if args.duration is not None and args.duration <= 0:
                raise MarketDataQueryError("--duration must be positive")
            if args.symbol is not None and not args.symbol.strip():
                raise MarketDataQueryError("--symbol must not be blank")
        else:
            if args.duration <= 0:
                raise MarketDataQueryError("--duration must be positive")
            if not args.symbol.strip():
                raise MarketDataQueryError("--symbol must not be blank")
            if args.command == "list" and not 1 <= args.limit <= 10_000:
                raise MarketDataQueryError("--limit must be between 1 and 10000")
            if args.command == "export" and args.output.exists() and not args.force:
                raise MarketDataQueryError(
                    f"export target already exists: {args.output}"
                )

        settings = load_database_settings(required=True)
        assert settings is not None
        engine = create_database_engine(settings)
        if args.command == "quality":
            results = summarize_market_data_quality(
                engine,
                symbol=args.symbol,
                duration_seconds=args.duration,
                settings=settings,
            )
            print(
                "MARKET_DATA_QUALITY",
                json.dumps(
                    [result.as_dict() for result in results],
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            )
        elif args.command == "list":
            rows = query_market_bars(
                engine,
                symbol=args.symbol,
                duration_seconds=args.duration,
                start_nano=start,
                end_nano=end,
                limit=args.limit,
                settings=settings,
            )
            print(
                "MARKET_BARS",
                json.dumps(
                    [row.as_dict() for row in rows],
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            )
        else:
            result = export_market_bars_csv(
                engine,
                args.output,
                symbol=args.symbol,
                duration_seconds=args.duration,
                start_nano=start,
                end_nano=end,
                force=args.force,
                settings=settings,
            )
            print(
                "MARKET_BARS_EXPORTED",
                json.dumps(result.as_dict(), ensure_ascii=True, sort_keys=True),
            )
        return 0
    except Exception as exc:
        print(
            "MARKET_DATA_QUERY_FAIL",
            type(exc).__name__,
            redact_database_text(exc, settings)[:300],
        )
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
