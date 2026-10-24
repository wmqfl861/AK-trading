"""Collect official exchange rules into local auditable artifacts."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from ak_trading.exchange_rule_artifacts import (
    CollectionLock,
    CollectionLockedError,
    ExchangeRuleArtifactError,
    ExchangeRuleArtifactStore,
)
from ak_trading.exchange_rule_collection import (
    ExchangeRuleCollectionError,
    bootstrap_legacy_reference,
    collect_exchange_rules,
    collect_exchange_rules_from_catalog,
)
from ak_trading.market_reference import SHANGHAI_TZ


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_ROOT = ROOT / "data" / "exchange_rules"
DEFAULT_REFERENCE = ROOT / "config" / "market_reference.json"
DEFAULT_REGISTRY = ROOT / "config" / "exchange_rule_sources.json"
DEFAULT_CATALOG = ROOT / "config" / "china_futures_catalog.json"


def _aware_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI_TZ)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("--now must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError("--now must include a timezone offset")
    return parsed.astimezone(SHANGHAI_TZ)


def _fixture_values(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for raw in values:
        exchange, separator, path = raw.partition("=")
        exchange = exchange.strip().upper()
        if not separator or not exchange or not path.strip():
            raise ValueError("--fixture must use EXCHANGE=PATH")
        if exchange in result:
            raise ValueError(f"duplicate --fixture for {exchange}")
        result[exchange] = Path(path.strip())
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect official mainland China futures exchange rules"
    )
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=DEFAULT_CATALOG,
        help="official six-exchange futures catalog; options remain excluded",
    )
    parser.add_argument(
        "--legacy-three-products",
        action="store_true",
        help="use the legacy RB/TA/M source registry and reference calendar",
    )
    parser.add_argument(
        "--trading-day",
        type=date.fromisoformat,
        help="explicit YYYY-MM-DD trading day; otherwise derive in Asia/Shanghai",
    )
    parser.add_argument("--now", help=argparse.SUPPRESS)
    parser.add_argument(
        "--fixture",
        action="append",
        default=[],
        metavar="EXCHANGE=PATH",
        help="offline source fixture; repeat for the catalog exchanges being tested",
    )
    parser.add_argument("--search-timeout", type=float, default=45.0)
    parser.add_argument("--fetch-timeout", type=float, default=120.0)
    parser.add_argument("--websearch", type=Path, default=None)
    parser.add_argument(
        "--no-humanlike",
        action="store_true",
        help="disable the optional Scrapling human-like browser fallback",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="store the legacy reference as a non-approved baseline",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="required with --bootstrap; makes the non-approved import explicit",
    )
    return parser


def _print_result(result) -> None:
    snapshot = result.snapshot
    print(
        "EXCHANGE_RULES_RUN",
        json.dumps(
            {
                "run_id": result.run["run_id"],
                "status": result.run["status"],
                "target_trading_day": result.run["target_trading_day"],
                "snapshot_id": snapshot.snapshot_id if snapshot else None,
                "previous_snapshot_id": (
                    result.previous_snapshot.snapshot_id if result.previous_snapshot else None
                ),
                "changes": len(result.changes),
                "documents_written": len(result.documents_written),
                "run_path": str(result.run_path),
                "snapshot_path": str(result.snapshot_path) if result.snapshot_path else None,
                "catalog_run_id": (
                    result.catalog_run["run_id"] if result.catalog_run is not None else None
                ),
                "catalog_run_path": (
                    str(result.catalog_run_path) if result.catalog_run_path else None
                ),
                "future_product_count": (
                    len(result.catalog_run.get("expected_futures", []))
                    if result.catalog_run is not None
                    else None
                ),
                "excluded_option_count": (
                    len(result.catalog_run.get("excluded_options", []))
                    if result.catalog_run is not None
                    else None
                ),
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
    )
    for change in result.changes:
        print(
            "EXCHANGE_RULES_CHANGE",
            json.dumps(change.as_dict(), ensure_ascii=True, sort_keys=True),
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        now = _aware_now(args.now)
        if args.search_timeout <= 0 or args.fetch_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if args.bootstrap and not args.confirm:
            raise ValueError("--bootstrap requires --confirm")
        if args.confirm and not args.bootstrap:
            raise ValueError("--confirm is only valid with --bootstrap")
        if args.bootstrap and args.legacy_three_products:
            raise ValueError("--bootstrap does not use --legacy-three-products")
        fixture_paths = _fixture_values(args.fixture)
        store = ExchangeRuleArtifactStore(args.artifact_root)
        with CollectionLock(store.lock_path):
            if args.bootstrap:
                result = bootstrap_legacy_reference(
                    store=store,
                    reference_path=args.reference,
                    now=now,
                    trading_day=args.trading_day,
                )
            elif args.legacy_three_products:
                result = collect_exchange_rules(
                    store=store,
                    source_registry_path=args.registry,
                    reference_path=args.reference,
                    now=now,
                    trading_day=args.trading_day,
                    fixture_paths=fixture_paths,
                    search_timeout=args.search_timeout,
                    fetch_timeout=args.fetch_timeout,
                    websearch_executable=args.websearch,
                    humanlike_enabled=not args.no_humanlike,
                )
            else:
                result = collect_exchange_rules_from_catalog(
                    store=store,
                    catalog_path=args.catalog,
                    now=now,
                    trading_day=args.trading_day,
                    fixture_paths=fixture_paths,
                    search_timeout=args.search_timeout,
                    fetch_timeout=args.fetch_timeout,
                    websearch_executable=args.websearch,
                    humanlike_enabled=not args.no_humanlike,
                )
    except (CollectionLockedError, ExchangeRuleArtifactError, ExchangeRuleCollectionError, OSError, ValueError) as exc:
        print("EXCHANGE_RULES_FAIL", type(exc).__name__, str(exc)[:500])
        return 2
    _print_result(result)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
