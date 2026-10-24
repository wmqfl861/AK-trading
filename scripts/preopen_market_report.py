import argparse
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ak_trading.market_notices import check_official_notices
from ak_trading.market_reference import (
    ReferenceDataError,
    SHANGHAI_TZ,
    build_preopen_report,
    load_reference_book,
    parse_price,
    write_report_artifacts,
)
from ak_trading.persistence.config import load_database_settings, redact_database_text
from ak_trading.persistence.database import create_database_engine
from ak_trading.persistence.storage import (
    store_preopen_report,
    store_reference_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]


def _price_assignments(values: list[str], option_name: str) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for raw in values:
        code, separator, price_text = raw.partition("=")
        code = code.strip().upper()
        if not separator or not code or not price_text.strip():
            raise ReferenceDataError(f"{option_name} must use PRODUCT=PRICE")
        if code in result:
            raise ReferenceDataError(f"duplicate {option_name} for {code}")
        result[code] = parse_price(price_text.strip(), f"{option_name}.{code}")
    return result


def _aware_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI_TZ)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReferenceDataError("--now must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise ReferenceDataError("--now must include a timezone offset")
    return parsed.astimezone(SHANGHAI_TZ)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the RB/TA/M pre-open exchange-parameter report"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "market_reference.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "market_reference",
    )
    parser.add_argument(
        "--trading-day",
        type=date.fromisoformat,
        help="explicit YYYY-MM-DD trading day; otherwise derive in Asia/Shanghai",
    )
    parser.add_argument(
        "--trade-price",
        action="append",
        default=[],
        metavar="PRODUCT=PRICE",
        help="trade price used for ad-valorem fee and cost-bps estimates",
    )
    parser.add_argument(
        "--settlement-price",
        action="append",
        default=[],
        metavar="PRODUCT=PRICE",
        help="settlement price used for exchange-margin estimates",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="skip the official-domain indexed notice check",
    )
    parser.add_argument(
        "--notice-timeout",
        type=float,
        default=45.0,
        help="per-exchange websearch timeout in seconds",
    )
    parser.add_argument(
        "--now",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    try:
        now = _aware_now(args.now)
        if args.notice_timeout <= 0:
            raise ReferenceDataError("--notice-timeout must be positive")
        book = load_reference_book(args.config)
        trading_day = args.trading_day or book.calendar.target_trading_day(now)
        trade_prices = _price_assignments(args.trade_price, "--trade-price")
        settlement_prices = _price_assignments(
            args.settlement_price, "--settlement-price"
        )
        if args.offline:
            notice_check = {
                "status": "offline_not_checked",
                "checked_at": now.isoformat(),
                "caveat": "Official-domain notice discovery was explicitly skipped.",
            }
        else:
            notice_check = check_official_notices(
                book,
                checked_at=now,
                timeout=args.notice_timeout,
            )
        report = build_preopen_report(
            book,
            trading_day,
            now,
            trade_prices=trade_prices,
            settlement_prices=settlement_prices,
            notice_check=notice_check,
        )
        json_path, markdown_path = write_report_artifacts(report, args.output_dir)
    except (OSError, ReferenceDataError, ValueError) as exc:
        print("MARKET_REFERENCE_FAIL", type(exc).__name__, str(exc)[:300])
        return 2

    database_exit_code = 0
    database_settings = None
    database_engine = None
    try:
        database_settings = load_database_settings()
        if database_settings is not None:
            database_engine = create_database_engine(database_settings)
            snapshot = store_reference_snapshot(
                database_engine, args.config, settings=database_settings
            )
            stored_report = store_preopen_report(
                database_engine,
                json_path,
                markdown_path,
                reference_snapshot_id=snapshot.snapshot_id,
                settings=database_settings,
            )
            print(
                "MARKET_REFERENCE_DATABASE",
                "snapshot",
                snapshot.snapshot_id,
                "report",
                stored_report.identity,
            )
    except Exception as exc:
        print(
            "MARKET_REFERENCE_DATABASE_FAIL",
            type(exc).__name__,
            redact_database_text(exc, database_settings)[:300],
        )
        database_exit_code = 2
    finally:
        if database_engine is not None:
            database_engine.dispose()

    print(
        "MARKET_REFERENCE_REPORT",
        "status",
        report["status"],
        "trading_day",
        report["trading_day"],
    )
    print("MARKET_REFERENCE_JSON", json_path)
    print("MARKET_REFERENCE_MARKDOWN", markdown_path)
    print("MARKET_NOTICE_CHECK", report["notice_check"]["status"])
    if database_exit_code:
        return database_exit_code
    if report["status"] == "error":
        return 2
    if report["status"] == "review_required":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
