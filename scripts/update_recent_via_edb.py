"""Backfill EDB daily/minute bars through the native idempotent CSV importer.

Dates are inclusive raw timestamp dates in UTC+08:00, not inferred trading days.
Defaults are today minus three calendar days through yesterday (UTC+08:00).
Use --expected-through for a known last session date on weekends/holidays.
Default scope is cached FUTURE candidates with expiry >= start; a stale cache
cannot certify current listings. --symbols-file explicitly overrides that scope.
Requests are sequential with --timeout <= 30 seconds and no automatic retries.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import dotenv_values
import httpx

from ak_trading.market_reference import load_reference_book
from ak_trading.persistence.config import load_database_settings
from ak_trading.persistence.database import create_database_engine
from ak_trading.persistence.market_data import expected_kline_columns, import_market_bars
from ak_trading.persistence.storage import store_reference_snapshot

BAR_FIELDS = ("open", "high", "low", "close", "volume", "open_oi", "close_oi")
CST = timezone(timedelta(hours=8))


def edb_token(env: dict, timeout: float = 15) -> str:
    r = httpx.post("https://edb.shinnytech.com/token",
                   json={"username": env["TQ_USER"], "password": env["TQ_PASS"]},
                   timeout=timeout)
    r.raise_for_status()
    return r.json()["token"]


def fetch_edb(symbol: str, period: int, start: str, end: str, headers: dict,
              timeout: float = 15) -> list[str]:
    params = {"symbol": symbol, "period": period,
              "start_time": f"{start} 00:00:00", "end_time": f"{end} 23:59:59"}
    r = httpx.get("https://edb.shinnytech.com/md/kline", params=params,
                  headers=headers, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"EDB HTTP status {int(r.status_code)}")
    lines = r.text.splitlines()
    header = next(csv.reader([lines[0].lstrip("\ufeff")], strict=True)) if lines else []
    # EDB uses datetime_nano; retain the legacy datetime header explicitly.
    if header not in (["datetime_nano", *BAR_FIELDS], ["datetime", *BAR_FIELDS]):
        raise ValueError("invalid EDB CSV header")
    return lines[1:]


def to_bar_rows(lines: list[str]) -> list[tuple]:
    """Validate every source row before importing any part of the response."""
    rows = []
    previous = -1
    for parts in csv.reader(lines, strict=True):
        if len(parts) != 8 or not re.fullmatch(r"[0-9]+", parts[0]):
            raise ValueError("invalid EDB CSV row")
        nano = int(parts[0])
        if not previous < nano <= 9_223_372_036_854_775_807 or nano == 0:
            raise ValueError("invalid or unordered EDB timestamp")
        seconds, nanos = divmod(nano, 1_000_000_000)
        ts = datetime.fromtimestamp(seconds, tz=CST) + timedelta(microseconds=nanos // 1000)
        vals = [Decimal(value) for value in parts[1:]]
        if any(not value.is_finite() or value < 0 for value in vals):
            raise ValueError("EDB bar values must be finite and non-negative")
        opening, high, low, close = vals[:4]
        if high < max(opening, low, close) or low > min(opening, close):
            raise ValueError("invalid EDB OHLC bounds")
        rows.append((ts, nano, *vals))
        previous = nano
    return rows


def write_and_import(engine, symbol: str, duration: int, rows: list[tuple],
                     db_settings, snapshot, calendar) -> int:
    if not rows:
        return 0
    fd, name = tempfile.mkstemp(prefix="ak_edb_", suffix=".csv",
                                dir=ROOT / "data" / "downloads")
    os.close(fd)
    try:
        with open(name, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(expected_kline_columns(symbol))
            for ts, nano, *vals in rows:
                writer.writerow([ts.isoformat(), nano] + vals)
        result = import_market_bars(
            engine, name, symbol=symbol, duration_seconds=duration,
            calendar=calendar, reference_sha256=snapshot.content_sha256,
            source_label=f"edb_free:{symbol}:{duration}", settings=db_settings,
        )
        return result.rows_written
    finally:
        Path(name).unlink(missing_ok=True)


def select_scope(args) -> dict:
    """Use the cached FUTURE discovery; never infer an active scope from bars."""
    if args.symbols_file:
        symbols = sorted({line.strip() for line in args.symbols_file.read_text(
            encoding="utf-8-sig").splitlines() if line.strip()})
        if any(not re.fullmatch(r"(?:[A-Z]+\.[A-Za-z0-9_-]+|KQ\.[mi]@[A-Z]+\.[A-Za-z0-9]+)", symbol)
               for symbol in symbols):
            raise ValueError("symbols-file requires literal market symbols, one per line")
        scope = {"source": "symbols_file", "generated_at": None,
                 "cache_stale": None, "note": "Explicit market symbols; expiry filter bypassed."}
    else:
        cache = ROOT / "data" / "downloads" / "backfill_contract_universe.json"
        universe = json.loads(cache.read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(universe["generated_at"])
        if generated.tzinfo is None:
            raise ValueError("cache generated_at must have a timezone")
        symbols = set()
        for contracts in universe["products"].values():
            if not isinstance(contracts, list):
                raise ValueError("cache product entries must be lists")
            for item in contracts:
                symbol = item["symbol"]
                if (not re.fullmatch(r"(?:CFFEX|SHFE|DCE|CZCE|INE|GFEX)\.[A-Za-z]{1,3}[0-9]{3,4}", symbol)
                        or item.get("ins_class", "FUTURE") != "FUTURE"):
                    continue
                expiry = item["expire_datetime"]
                # The discovery cache stores Unix SECONDS, never milliseconds/nanos.
                if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not 0 < expiry < 100_000_000_000:
                    raise ValueError("cache expiry must be numeric Unix seconds")
                if datetime.fromtimestamp(expiry, CST).date() >= date.fromisoformat(args.start):
                    symbols.add(symbol)
        symbols = sorted(symbols)
        scope = {"source": "cache", "generated_at": universe["generated_at"],
                 "cache_stale": generated.astimezone(CST).date() < datetime.now(CST).date(),
                 "note": "Cached FUTURE candidates by expiry >= start; expired is a snapshot flag, "
                         "not current eligibility. Stale generated_at may omit new listings; no live discovery."}
    scope["candidates"] = len(symbols)
    scope["symbols"] = symbols[:args.limit] if args.limit else symbols
    return scope


def iso_day(value: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("date must be a valid YYYY-MM-DD") from None


def process_symbol(args, symbol, headers, engine, db_settings, snapshot, calendar) -> dict:
    result = {"symbol": symbol, "status": "failed", "rows": 0,
              "latest_raw_date": None, "latest_imported_raw_date": None,
              "freshness": "unavailable", "reason": None,
              "rows_received": 0, "rows_outside_range": 0}
    try:
        lines = fetch_edb(symbol, args.duration, args.start, args.end, headers, timeout=args.timeout)
        source_rows = to_bar_rows(lines)
        result["rows_received"] = len(source_rows)
        result["latest_raw_date"] = max((r[0].date().isoformat() for r in source_rows), default=None)
        rows = [r for r in source_rows if args.start <= r[0].date().isoformat() <= args.end]
        result["rows_outside_range"] = len(source_rows) - len(rows)
        if not rows:
            result.update(status="empty", reason="no_rows_in_requested_range" if source_rows else "empty_response")
        else:
            written = write_and_import(engine, symbol, args.duration, rows, db_settings, snapshot, calendar)
            latest = max(r[0].date().isoformat() for r in rows)
            stale = latest < args.expected_through
            result.update(rows=written, latest_imported_raw_date=latest,
                          status="stale" if stale else "ok", freshness="stale" if stale else "fresh",
                          reason="latest_raw_date_before_expected_through" if stale else None)
    except Exception as exc:
        # Never include provider/DB messages: they can carry passwords or tokens.
        kind = type(exc).__name__
        result.update(status="failed", freshness="unavailable", reason=kind, error=kind, detail=kind)
    return result


class RunLockUnavailable(RuntimeError):
    """The duration already has a coordinator, or its lock cannot be acquired."""


@contextmanager
def run_lock(path: Path):
    # Same OS locking pattern as the option updater, kept local for standalone use.
    # Leave the inode in place on release: unlinking it can permit two owners.
    handle = None
    acquired = False
    try:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+b")
            os.set_inheritable(handle.fileno(), False)
            if os.name == "nt":
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise RunLockUnavailable() from None
        yield
    finally:
        if handle is not None:
            try:
                if acquired:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def failed_result(symbol: str, exc: Exception) -> dict:
    kind = type(exc).__name__
    return {"symbol": symbol, "status": "failed", "rows": 0, "latest_raw_date": None,
            "latest_imported_raw_date": None, "freshness": "unavailable", "reason": kind,
            "error": kind, "detail": kind}


def update_symbols(args, scope) -> list[dict]:
    results, engine = [], None
    try:
        env = dotenv_values(ROOT / ".env")
        token = edb_token(env, timeout=args.timeout)
        headers = {"Authorization": f"Bearer {token}"}
        db_settings = load_database_settings(required=True)
        engine = create_database_engine(db_settings)
        book = load_reference_book(ROOT / "config" / "market_reference.json")
        snapshot = store_reference_snapshot(engine, ROOT / "config" / "market_reference.json",
                                            settings=db_settings)
        for symbol in scope["symbols"]:
            result = process_symbol(args, symbol, headers, engine, db_settings, snapshot, book.calendar)
            results.append(result)
            print(json.dumps(result), flush=True)
            time.sleep(0.1)
    except Exception as exc:
        for symbol in scope["symbols"][len(results):]:
            result = failed_result(symbol, exc)
            results.append(result)
            print(json.dumps(result), flush=True)
    finally:
        if engine is not None:
            engine.dispose()
    return results


def finish(args, scope, results, *, status=None, manifest=None) -> int:
    summary = {"symbols": len(scope["symbols"]), "done": sum(r["status"] == "ok" for r in results),
               "failed": sum(r["status"] == "failed" for r in results),
               "rows": sum(r["rows"] for r in results), "duration": args.duration,
               "range": f"{args.start}..{args.end}"}
    complete = bool(scope["symbols"]) and summary["done"] == len(scope["symbols"])
    status = status or ("complete" if complete else "incomplete")
    report = {"status": status, "scope": scope, "results": results, "SUMMARY": summary,
              "freshness_basis": "raw_timestamp_UTC+08:00; no inferred trading day or closed-bar certification"}
    if manifest is not None:
        try:
            json.dump(report, manifest, indent=2, allow_nan=False)
            manifest.write("\n")
            manifest.flush()
        except Exception as exc:
            status = "manifest_failed"
            kind = type(exc).__name__
            print(json.dumps({"status": status, "reason": kind, "error": kind, "detail": kind}), flush=True)
        finally:
            try:
                manifest.close()
            except Exception as exc:
                status = "manifest_failed"
                kind = type(exc).__name__
                print(json.dumps({"status": status, "reason": kind, "error": kind, "detail": kind}), flush=True)
    print(json.dumps({"status": status, "empty": sum(r["status"] == "empty" for r in results),
                      "stale": sum(r["status"] == "stale" for r in results)}), flush=True)
    print(json.dumps({"SUMMARY": summary}), flush=True)
    return 0 if status in ("complete", "dry_run") and scope["symbols"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=iso_day, help="inclusive raw date; default today minus 3 days")
    parser.add_argument("--end", type=iso_day, help="inclusive raw date; default yesterday")
    parser.add_argument("--expected-through", type=iso_day, help="freshness raw date; default end")
    parser.add_argument("--timeout", type=float, default=15, help="HTTP timeout seconds, 0 < timeout <= 30")
    parser.add_argument("--duration", type=int, choices=[60, 86400], required=True)
    parser.add_argument("--symbols-file", type=Path,
                        help="one explicit market symbol per line; default = cached futures by expiry")
    parser.add_argument("--limit", type=int, default=0, help="max symbols (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="list selected scope without credentials, HTTP or DB")
    parser.add_argument("--manifest-output", type=Path, help="write a JSON report to a new file; never overwrite")
    args = parser.parse_args(argv)
    today = datetime.now(CST).date()
    args.start = args.start or today - timedelta(days=3)
    args.end = args.end or today - timedelta(days=1)
    args.expected_through = args.expected_through or args.end
    if args.start > args.end or args.end > today:
        parser.error("require start <= end <= today (UTC+08:00)")
    if not args.start <= args.expected_through <= args.end:
        parser.error("expected-through must be between start and end")
    if args.limit < 0 or not 0 < args.timeout <= 30:
        parser.error("limit must be non-negative; timeout must be > 0 and <= 30")
    args.start, args.end = args.start.isoformat(), args.end.isoformat()
    args.expected_through = args.expected_through.isoformat()

    manifest = None
    if args.manifest_output:
        try:
            # Reserve before credentials or any import, atomically refusing existing paths.
            manifest = args.manifest_output.open("x", encoding="utf-8")
        except OSError as exc:
            parser.error(f"manifest-output requires a writable new file ({type(exc).__name__})")
    scope = {"symbols": [], "source": "symbols_file" if args.symbols_file else "cache",
             "start": args.start, "end": args.end, "expected_through": args.expected_through,
             "date_basis": "raw_timestamp_UTC+08:00"}
    results = []
    try:
        scope.update(select_scope(args))
        print(json.dumps({"SCOPE": scope}), flush=True)
        if not scope["symbols"]:
            return finish(args, scope, results, status="empty_scope", manifest=manifest)
        if args.dry_run:
            results = [{"symbol": symbol, "status": "selected", "rows": 0, "latest_raw_date": None,
                        "freshness": "unavailable", "reason": "dry_run"} for symbol in scope["symbols"]]
            return finish(args, scope, results, status="dry_run", manifest=manifest)
        # Daily and minute work have disjoint native keys; serialize within a duration.
        lock_name = "edb_daily_update.lock" if args.duration == 86400 else "edb_minute_update.lock"
        with run_lock(ROOT / "data" / "downloads" / lock_name):
            results = update_symbols(args, scope)
            return finish(args, scope, results, manifest=manifest)
    except RunLockUnavailable:
        finish(args, scope, results, status="blocked_existing_run_or_lock_unavailable", manifest=manifest)
        return 2
    except Exception as exc:
        kind = type(exc).__name__
        print(json.dumps({"status": "failed", "error": kind, "detail": kind}), flush=True)
        return finish(args, scope, results, status="failed", manifest=manifest)
    finally:
        if manifest is not None:
            try:
                manifest.close()
            except Exception:
                pass  # finish reports flush/close failures using exception types only.


if __name__ == "__main__":
    sys.exit(main())
