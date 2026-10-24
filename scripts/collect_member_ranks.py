"""Collect daily member position rank disclosures into PostgreSQL.

Implements the five exchanges whose official endpoints are verified
(SHFE/INE/CFFEX/GFEX/CZCE; see ``ak_trading.member_rank`` for the endpoint
map, the INE sc/ec disclosure gap, and the not-yet-implemented DCE API).

Every fetch first stores the raw official payload under the evidence
directory, then upserts metric rows into ``member_position_ranks``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from ak_trading.member_rank import (
    FETCHERS,
    MemberRankError,
    MemberRankFetchResult,
    collect_day,
    fetch_czce_rank_bulk,
)
from ak_trading.persistence.config import (
    DatabaseConfigurationError,
    load_database_settings,
)
from ak_trading.persistence.database import create_database_engine
from ak_trading.persistence.member_rank import (
    MemberRankImportError,
    import_member_rank_rows,
)

ROOT = Path(__file__).resolve().parents[1]
_EVIDENCE_SUFFIXES = {".dat": ".json", ".xml": ".xml", ".htm": ".htm", ".html": ".html"}


def _evidence_path(evidence_dir: Path, exchange: str, day: date, url: str, payload: bytes) -> Path:
    directory = evidence_dir / exchange / f"{day:%Y%m%d}"
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(payload).hexdigest()[:12]
    lowered = url.split("#")[0].lower()
    suffix = ".json"
    for marker, ext in _EVIDENCE_SUFFIXES.items():
        if marker in lowered:
            suffix = ext
            break
    return directory / f"payload_{digest}{suffix}"


def _save_evidence(
    evidence_dir: Path,
    exchange: str,
    day: date,
    result,
) -> list[str]:
    files: list[str] = []
    directory = evidence_dir / exchange / f"{day:%Y%m%d}"
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    for url, payload in result.evidence.items():
        path = _evidence_path(evidence_dir, exchange, day, url, payload)
        path.write_bytes(payload)
        files.append(str(path))
    manifest = {
        "exchange": exchange,
        "trading_day": f"{day:%Y-%m-%d}",
        "status": result.status,
        "source_urls": list(result.source_urls),
        "rows": len(result.rows),
        "note": result.note,
        "files": files,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return files


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exchange",
        action="append",
        choices=sorted(FETCHERS),
        help="exchange to collect (repeatable; default: all implemented)",
    )
    parser.add_argument("--start", type=_parse_day, help="first calendar day (YYYY-MM-DD)")
    parser.add_argument("--end", type=_parse_day, help="last calendar day (YYYY-MM-DD)")
    parser.add_argument(
        "--lookback", type=int,
        help="collect from (today - N days) through today; used by the daily task",
    )
    parser.add_argument(
        "--evidence-dir", type=Path, default=ROOT / "data" / "member_ranks"
    )
    parser.add_argument(
        "--no-czce-bulk", action="store_true",
        help="fetch CZCE one day per browser session instead of one session per range",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="fetch and store evidence only; do not write to PostgreSQL",
    )
    args = parser.parse_args(argv)

    exchanges = args.exchange or sorted(FETCHERS)
    if args.lookback is not None:
        if args.lookback < 0:
            parser.error("--lookback must not be negative")
        end = date.today()
        start = end - timedelta(days=args.lookback)
    else:
        end = args.end or date.today() - timedelta(days=1)
        start = args.start or end - timedelta(days=2)
    if start > end:
        parser.error("--start must not be after --end")

    engine = None
    if not args.dry_run:
        try:
            settings = load_database_settings()
        except DatabaseConfigurationError as exc:
            print(f"database configuration error: {exc}", file=sys.stderr)
            return 2
        engine = create_database_engine(settings)

    day = start
    ok_days = unavailable_days = error_days = 0
    imported_rows = 0

    def _record_and_import(result, day_value: date) -> None:
        nonlocal ok_days, unavailable_days, error_days, imported_rows
        _save_evidence(args.evidence_dir, result.exchange, day_value, result)
        record = {
            "exchange": result.exchange,
            "day": f"{day_value:%Y-%m-%d}",
            "status": result.status,
            "rows": len(result.rows),
        }
        if result.note:
            record["note"] = result.note
        if result.status == "error":
            error_days += 1
        elif result.status == "unavailable":
            unavailable_days += 1
        else:
            ok_days += 1
            if engine is not None and result.rows:
                try:
                    outcome = import_member_rank_rows(engine, result.rows)
                    imported_rows += outcome.rows_written
                    record["imported"] = outcome.rows_written
                except MemberRankImportError as exc:
                    record["status"] = "error"
                    record["note"] = f"import failed: {exc}"
                    error_days += 1
                    ok_days -= 1
        print(json.dumps(record, ensure_ascii=False), flush=True)

    czce_bulk_results: dict | None = None
    use_czce_bulk = (
        "CZCE" in exchanges and not args.no_czce_bulk and (end - start).days >= 1
    )
    # CZCE is handled only by its dedicated daily or bulk path below.
    direct_exchanges = [name for name in exchanges if name != "CZCE"]

    try:
        # Pass 1: direct-endpoint exchanges stream into the database and log
        # immediately, before the slower browser-driven CZCE bulk pass.
        while day <= end:
            if direct_exchanges:
                results = collect_day(day, exchanges=direct_exchanges)
                for exchange in sorted(results):
                    _record_and_import(results[exchange], day)
            if not use_czce_bulk and "CZCE" in exchanges:
                results = collect_day(day, exchanges=("CZCE",))
                _record_and_import(results["CZCE"], day)
            day += timedelta(days=1)

        # Pass 2: one browser session per chunk for the whole CZCE range.
        if use_czce_bulk:
            czce_days = []
            cursor = start
            while cursor <= end:
                czce_days.append(cursor)
                cursor += timedelta(days=1)
            try:
                czce_bulk_results = fetch_czce_rank_bulk(czce_days)
                czce_bulk_note = "czce bulk run missing this day"
            except MemberRankError as exc:
                czce_bulk_results = {}
                czce_bulk_note = str(exc)
            for czce_day in czce_days:
                result = czce_bulk_results.get(czce_day)
                if result is None:
                    result = MemberRankFetchResult(
                        exchange="CZCE", trading_day=czce_day, status="error",
                        source_urls=(), note=czce_bulk_note,
                    )
                _record_and_import(result, czce_day)
    finally:
        if engine is not None:
            engine.dispose()

    summary = {
        "start": f"{start:%Y-%m-%d}",
        "end": f"{end:%Y-%m-%d}",
        "exchanges": exchanges,
        "ok": ok_days,
        "unavailable": unavailable_days,
        "error": error_days,
        "imported_rows": imported_rows,
        "dry_run": args.dry_run,
    }
    print("summary", json.dumps(summary, ensure_ascii=False))
    return 1 if error_days else 0


if __name__ == "__main__":
    sys.exit(main())
