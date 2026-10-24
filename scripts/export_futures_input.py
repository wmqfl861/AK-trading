"""Thin read-only export CLI for the futures research input bundle.

The default DSN source is the process environment. An explicit ``--env-file``
opts into reading only ``AK_DATABASE_URL`` with interpolation disabled. A
process value takes precedence, including an explicitly empty value. Nothing
is loaded into the environment; no DSN, user or password is emitted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 3


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only bounded export of futures daily bars into a futures_bundle_v1 input bundle."
    )
    parser.add_argument("--products", required=True,
                        help="comma-separated exchange.product tokens, e.g. SHFE.rb,INE.sc")
    parser.add_argument("--start", required=True,
                        help="span start, timezone-aware ISO8601")
    parser.add_argument("--end", required=True,
                        help="span end (exclusive), timezone-aware ISO8601")
    parser.add_argument("--duration", type=int, default=86400,
                        help="bar duration seconds (default 86400)")
    parser.add_argument("--universe-cache", required=True,
                        help="path to backfill_contract_universe.json")
    parser.add_argument("--catalog", required=True,
                        help="path to china_futures_catalog.json")
    parser.add_argument("--output", required=True,
                        help="output directory (must not exist)")
    parser.add_argument("--env-file", type=Path,
                        help="explicit dotenv file for AK_DATABASE_URL only; process value wins "
                             "(even empty); no interpolation or environment mutation")
    parser.add_argument("--local-metadata", action="append", default=[],
                        help="optional local metadata path (repeatable)")
    parser.add_argument("--include-session-probe", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--include-member-ranks", action="store_true",
                        help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # Unsupported probing options are rejected explicitly, never accepted and
    # silently ignored (§六.5).
    if args.include_session_probe or args.include_member_ranks:
        print(json.dumps({
            "status": "rejected",
            "reason": "unsupported_option",
            "options": [name for name, flag in (
                ("--include-session-probe", args.include_session_probe),
                ("--include-member-ranks", args.include_member_ranks),
            ) if flag],
        }))
        return EXIT_USAGE

    try:
        span_start = datetime.fromisoformat(args.start)
        span_end = datetime.fromisoformat(args.end)
    except ValueError:
        print(json.dumps({"status": "rejected", "reason": "invalid_iso_datetime"}))
        return EXIT_USAGE
    if span_start.tzinfo is None or span_end.tzinfo is None:
        print(json.dumps({"status": "rejected", "reason": "timezone_aware_datetimes_required"}))
        return EXIT_USAGE
    if span_start >= span_end:
        print(json.dumps({"status": "rejected", "reason": "start_must_precede_end"}))
        return EXIT_USAGE

    from ak_trading.futures.data import UniversePolicy
    from ak_trading.futures.spec import parse_product_token

    products = []
    for token in args.products.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            products.append(parse_product_token(token))
        except ValueError:
            print(json.dumps({"status": "rejected", "reason": "invalid_product_token"}))
            return EXIT_USAGE
    if not products or UniversePolicy().review(products)[1]:
        print(json.dumps({"status": "rejected", "reason": "invalid_or_unsupported_products"}))
        return EXIT_USAGE
    if args.duration <= 0:
        print(json.dumps({"status": "rejected", "reason": "duration_must_be_positive_integer"}))
        return EXIT_USAGE

    output = Path(args.output)
    if output.exists():
        print(json.dumps({"status": "rejected", "reason": "output_directory_exists", "output": str(output)}))
        return EXIT_USAGE

    url = os.environ.get("AK_DATABASE_URL")
    if args.env_file is not None:
        from dotenv import dotenv_values

        try:
            # Open explicitly: dotenv otherwise treats a missing path as an
            # empty mapping. Never search parents or interpolate other keys.
            with args.env_file.open(encoding="utf-8-sig") as stream:
                file_url = dotenv_values(stream=stream, interpolate=False).get("AK_DATABASE_URL")
        except (OSError, UnicodeError, ValueError):
            print(json.dumps({"status": "rejected", "reason": "env_file_unreadable"}))
            return EXIT_USAGE
        if url is None:
            url = file_url
    url = (url or "").strip()
    if not url:
        print(json.dumps({
            "status": "export_blocked",
            "reason": ("AK_DATABASE_URL_not_present_in_process_environment"
                       if args.env_file is None else "AK_DATABASE_URL_missing_or_empty"),
            "note": "process value wins including empty; dotenv is read only with --env-file",
        }))
        return EXIT_BLOCKED

    try:
        from ak_trading.persistence.config import DatabaseSettings
        from ak_trading.persistence.database import create_database_engine
        from ak_trading.persistence.futures_research import (
            ExportRequest, export_futures_bundle, validate_export_request,
        )
    except ImportError:
        print(json.dumps({"status": "export_blocked", "reason": "database_dependencies_unavailable"}))
        return EXIT_BLOCKED

    request = ExportRequest(
        products=tuple(products),
        span_start=span_start,
        span_end=span_end,
        as_of=span_end,
        universe_cache_path=Path(args.universe_cache),
        catalog_path=Path(args.catalog),
        duration_seconds=args.duration,
        local_metadata_paths=tuple(Path(p) for p in args.local_metadata),
    )

    failure = validate_export_request(request, output)
    if failure is not None:
        print(json.dumps({"status": failure.status, "reasons": list(failure.reasons)}))
        return EXIT_BLOCKED
    try:
        settings = DatabaseSettings(url=url)
    except Exception:
        print(json.dumps({"status": "export_blocked", "reason": "invalid_database_url_format"}))
        return EXIT_BLOCKED

    engine = None
    error = None
    try:
        engine = create_database_engine(settings)
        outcome = export_futures_bundle(engine, request, output)
    except Exception:
        error = "database_engine_failed" if engine is None else "export_operation_failed"
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                error = error or "database_cleanup_failed"
    if error:
        print(json.dumps({"status": "export_blocked", "reason": error}))
        return EXIT_BLOCKED

    summary = {
        "status": outcome.status,
        "reasons": list(outcome.reasons),
        "row_counts": dict(outcome.row_counts),
        "output": outcome.output_path,
        "read_only_transaction": (
            {
                "mechanism": outcome.read_only_evidence.mechanism,
                "driver": outcome.read_only_evidence.driver,
                "observed_isolation": outcome.read_only_evidence.observed_isolation,
                "observed_read_only": outcome.read_only_evidence.observed_read_only,
                "verified": outcome.read_only_evidence.verified,
            }
            if outcome.read_only_evidence is not None
            else {"executed": False, "reason": "not_reached"}
        ),
    }
    print(json.dumps(summary, ensure_ascii=False))
    if outcome.status == "published":
        return EXIT_OK
    return EXIT_BLOCKED


if __name__ == "__main__":
    sys.exit(main())
