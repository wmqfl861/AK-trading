import argparse
import json
from dataclasses import replace
from pathlib import Path

from ak_trading.artifacts import report_path_for
from ak_trading.market_reference import load_reference_book
from ak_trading.persistence.config import (
    DatabaseConfigurationError,
    load_database_settings,
    redact_database_text,
)
from ak_trading.persistence.database import (
    DatabaseOperationError,
    create_database_engine,
    database_status,
    upgrade_database,
)
from ak_trading.persistence.exchange_rules import import_exchange_rule_artifacts
from ak_trading.persistence.inspection import InspectionRequest, inspect_database
from ak_trading.persistence.market_data import (
    MarketDataImportError,
    import_market_bars,
)
from ak_trading.persistence.storage import (
    PersistenceError,
    import_run_artifacts,
    result_dict,
    store_preopen_report,
    store_reference_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]


def _print_result(kind: str, value: object) -> None:
    print(kind, json.dumps(value, ensure_ascii=True, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate and import AK-trading PostgreSQL data"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser("migrate", help="apply Alembic migrations")
    migrate.add_argument("--revision", default="head")

    subparsers.add_parser("status", help="show credential-free database status")

    inspection = subparsers.add_parser(
        "inspect", help="read-only catalog inventory with approximate table row estimates",
    )
    inspection.add_argument("--symbol", help="coverage symbol; requires --duration")
    inspection.add_argument("--duration", type=int, help="coverage K-line seconds; requires --symbol")
    inspection.add_argument("--start", help="inclusive timezone-aware ISO datetime; requires coverage")
    inspection.add_argument("--end", help="exclusive timezone-aware ISO datetime; requires coverage")
    inspection.add_argument(
        "--limit", type=int,
        help="earliest matching coverage rows (default 1000, maximum 10000); reads one extra row to detect truncation",
    )

    reference = subparsers.add_parser(
        "import-reference", help="store one immutable market-reference snapshot"
    )
    reference.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "market_reference.json",
    )

    existing = subparsers.add_parser(
        "import-existing", help="import existing journals, summaries, and pre-open reports"
    )
    existing.add_argument(
        "--run-root",
        action="append",
        type=Path,
        help="journal root; repeatable (defaults to data/runs and data/backtests)",
    )
    existing.add_argument(
        "--preopen-root",
        type=Path,
        default=ROOT / "data" / "market_reference",
    )
    existing.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "market_reference.json",
    )

    bars = subparsers.add_parser(
        "import-bars", help="import a strict single-symbol TqSdk K-line CSV"
    )
    bars.add_argument("--csv", type=Path, required=True)
    bars.add_argument("--symbol", required=True)
    bars.add_argument("--duration", type=int, required=True, help="K-line seconds")
    bars.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "market_reference.json",
    )

    exchange_rules = subparsers.add_parser(
        "import-exchange-rules",
        help="import immutable local exchange-rule artifacts",
    )
    exchange_rules.add_argument(
        "--artifact-root",
        type=Path,
        default=ROOT / "data" / "exchange_rules",
    )
    return parser


def _import_existing(engine, settings, args) -> dict[str, int]:
    snapshot = store_reference_snapshot(engine, args.config, settings=settings)
    run_roots = args.run_root or [
        ROOT / "data" / "runs",
        ROOT / "data" / "backtests",
    ]
    journals = sorted(
        {
            path.resolve()
            for root in run_roots
            if root.exists()
            for path in root.rglob("*.jsonl")
        }
    )
    events_read = 0
    events_inserted = 0
    run_reports = 0
    for journal_path in journals:
        journal_result, report_result = import_run_artifacts(
            engine,
            journal_path,
            report_path_for(journal_path),
            settings=settings,
        )
        events_read += journal_result.events_read
        events_inserted += journal_result.events_inserted
        run_reports += int(report_result is not None and report_result.inserted)

    preopen_reports = 0
    if args.preopen_root.exists():
        for json_path in sorted(args.preopen_root.rglob("preopen.json")):
            markdown_path = json_path.with_name("preopen.md")
            if not markdown_path.exists():
                raise PersistenceError(
                    f"pre-open JSON has no matching Markdown artifact: {json_path}"
                )
            report = store_preopen_report(
                engine,
                json_path,
                markdown_path,
                reference_snapshot_id=snapshot.snapshot_id,
                settings=settings,
            )
            preopen_reports += int(report.inserted)
    return {
        "journals": len(journals),
        "events_read": events_read,
        "events_inserted": events_inserted,
        "run_reports_inserted": run_reports,
        "preopen_reports_inserted": preopen_reports,
        "reference_snapshot_id": snapshot.snapshot_id,
    }


def _inspect(args) -> int:
    try:
        request = InspectionRequest(
            symbol=args.symbol, duration_seconds=args.duration,
            start=args.start, end=args.end, limit=args.limit,
        )
    except ValueError as exc:
        print("DATABASE_FAIL", "InvalidInspectionRequest", str(exc))
        return 2
    try:
        settings = load_database_settings(required=True)
        assert settings is not None
        settings = replace(
            settings, connect_timeout=min(settings.connect_timeout, 5),
            statement_timeout_ms=min(settings.statement_timeout_ms, 5000),
        )
        engine = create_database_engine(settings)
        try:
            result = inspect_database(engine, settings=settings, request=request)
        finally:
            engine.dispose()
        _print_result("DATABASE_INSPECTION", result)
        return 0
    except Exception:
        # Configuration and driver errors can contain credentials even before
        # a DatabaseSettings instance exists. Do not serialize their text.
        print("DATABASE_FAIL", "DatabaseOperationError", "database inspection failed")
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "inspect":
        return _inspect(args)
    settings = None
    engine = None
    try:
        settings = load_database_settings(required=True)
        assert settings is not None
        if args.command == "migrate":
            upgrade_database(settings, args.revision)
            _print_result(
                "DATABASE_MIGRATED",
                {"location": settings.safe_location, "revision": args.revision},
            )
            return 0
        if args.command == "status":
            _print_result("DATABASE_STATUS", database_status(settings))
            return 0

        engine = create_database_engine(settings)
        if args.command == "import-exchange-rules":
            result = import_exchange_rule_artifacts(
                engine,
                args.artifact_root,
                settings=settings,
            )
            _print_result("EXCHANGE_RULES_IMPORTED", result.as_dict())
        elif args.command == "import-reference":
            result = store_reference_snapshot(engine, args.config, settings=settings)
            _print_result("REFERENCE_IMPORTED", result_dict(result))
        elif args.command == "import-existing":
            _print_result(
                "EXISTING_IMPORTED", _import_existing(engine, settings, args)
            )
        elif args.command == "import-bars":
            book = load_reference_book(args.config)
            snapshot = store_reference_snapshot(engine, args.config, settings=settings)
            result = import_market_bars(
                engine,
                args.csv,
                symbol=args.symbol,
                duration_seconds=args.duration,
                calendar=book.calendar,
                reference_sha256=snapshot.content_sha256,
                settings=settings,
            )
            _print_result("BARS_IMPORTED", result.as_dict())
        else:
            parser.error(f"unsupported command {args.command}")
        return 0
    except (
        DatabaseConfigurationError,
        DatabaseOperationError,
        MarketDataImportError,
        OSError,
        PersistenceError,
        ValueError,
    ) as exc:
        print(
            "DATABASE_FAIL",
            type(exc).__name__,
            redact_database_text(exc, settings)[:300],
        )
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
