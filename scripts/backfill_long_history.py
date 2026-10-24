"""Backfill long 1-minute K-line history (2013..2023) for all main-continuity symbols.

This driver is incremental and resumable:

* Symbols are taken from ``SELECT DISTINCT symbol FROM market_bars``.
* A cheap probe (single TqApi session, 16-day June window per candidate year,
  binary search) determines the earliest year with downloadable 1-minute data
  per symbol. Probe results are cached in
  ``data/downloads/backfill_long_probe.json`` so probes never repeat.
* Blocks (symbol, calendar year) are executed oldest-year-first by invoking the
  existing, untouched ``scripts/download_market_data.py`` (idempotent upsert).
* Progress is appended to ``data/downloads/backfill_long_progress.jsonl``.
  On restart, blocks recorded as ``ok``/``skipped_db`` are skipped, and blocks
  for which ``market_bars`` already has >= 1 row are also treated as done.
* Each block is retried up to 2 extra times on failure; permanent failures are
  logged and the run continues. After several consecutive failed blocks the run
  aborts (circuit breaker) so a dead session/trial does not burn hours.
* One JSON line per block goes to ``data/downloads/backfill_long.log``.

The driver never prints credentials and never touches .env; it only loads
settings through the project helpers, exactly like download_market_data.py.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.config import load_settings  # noqa: E402

DATA_DIR = ROOT / "data" / "downloads"
PROBE_DIR = DATA_DIR / "probe"
PROBE_CACHE = DATA_DIR / "backfill_long_probe.json"
PROGRESS_FILE = DATA_DIR / "backfill_long_progress.jsonl"
LOG_FILE = DATA_DIR / "backfill_long.log"

FIRST_YEAR = 2013
LAST_YEAR = 2023
DURATION_SECONDS = 60
DONE_STATUSES = frozenset({"ok", "skipped_db"})
ATTEMPTS_PER_BLOCK = 3  # initial try + 2 retries
ATTEMPT_TIMEOUT_S = 3600
PROBE_WINDOWS = (
    (6, 1, 6, 16),  # June 1 .. June 16
    (11, 1, 12, 31),  # fallback: catches products listed mid-year after June
)
PROBE_DEADLINE_S = 180

IMPORT_RE = re.compile(
    r"DOWNLOAD_IMPORTED batch (\S+) rows (\d+) null_trading_days (\d+)"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _log_event(record: dict) -> None:
    record = {"ts": _now_iso(), **record}
    line = json.dumps(record, ensure_ascii=True)
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _connect_db():
    import psycopg

    from ak_trading.persistence.config import load_database_settings

    settings = load_database_settings(required=True)
    assert settings is not None
    parsed = urlsplit(settings.url)
    return psycopg.connect(
        host=parsed.hostname,
        port=parsed.port,
        dbname=settings.database_name,
        user=parsed.username,
        password=parsed.password,
        options="-c statement_timeout=120000",
    )


def load_symbols() -> list[str]:
    with closing(_connect_db()) as conn:
        rows = conn.execute("SELECT DISTINCT symbol FROM market_bars").fetchall()
    return sorted(row[0] for row in rows)


def load_db_year_coverage() -> set[tuple[str, int]]:
    with closing(_connect_db()) as conn:
        rows = conn.execute(
            "SELECT symbol, EXTRACT(YEAR FROM ts)::int FROM market_bars "
            "GROUP BY 1, 2"
        ).fetchall()
    return {(symbol, int(year)) for symbol, year in rows}


def read_progress() -> dict[tuple[str, int], dict]:
    done: dict[tuple[str, int], dict] = {}
    if not PROGRESS_FILE.exists():
        return done
    with PROGRESS_FILE.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate a torn final line
            key = (record.get("symbol"), int(record.get("year", 0)))
            if key[0] is None:
                continue
            if record.get("status") in DONE_STATUSES:
                done[key] = record
    return done


class ProbeSession:
    """One TqApi session reused across all probe windows, recreated on errors."""

    def __init__(self) -> None:
        self._api = None
        self.recreations = 0

    def api(self):
        if self._api is None:
            from tqsdk import TqApi, TqAuth

            settings = load_settings()
            self._api = TqApi(auth=TqAuth(settings.tq_user, settings.tq_pass))
        return self._api

    def reset(self) -> None:
        if self._api is not None:
            try:
                self._api.close()
            except Exception:
                pass
            self._api = None
            self.recreations += 1

    def close(self) -> None:
        self.reset()


def probe_year_has_data(session: ProbeSession, symbol: str, year: int) -> bool:
    """Download small probe windows of 1-minute bars; True if any row exists.

    The June window is tried first; only when it is empty is the Nov-Dec
    fallback window tried, so products listed mid-year after June are still
    detected without doubling the cost for years that simply have no data.
    """
    from tqsdk.tools import DataDownloader

    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    for month_start, day_start, month_end, day_end in PROBE_WINDOWS:
        handle, raw_path = tempfile.mkstemp(
            prefix="probe_", suffix=".csv", dir=PROBE_DIR
        )
        os.close(handle)
        csv_path = Path(raw_path)
        csv_path.unlink(missing_ok=True)
        try:
            api = session.api()
            downloader = DataDownloader(
                api,
                symbol_list=symbol,
                dur_sec=DURATION_SECONDS,
                start_dt=date(year, month_start, day_start),
                end_dt=date(year, month_end, day_end),
                csv_file_name=str(csv_path),
            )
            deadline = time.time() + PROBE_DEADLINE_S
            while not downloader.is_finished():
                api.wait_update(deadline=deadline)
            del downloader
            if csv_path.exists():
                with csv_path.open(
                    "r", encoding="utf-8", errors="replace"
                ) as csv_handle:
                    line_count = sum(1 for _ in csv_handle)
                if line_count > 1:  # header only => no data in this window
                    return True
        finally:
            csv_path.unlink(missing_ok=True)
    return False


def probe_with_retry(
    session: ProbeSession, symbol: str, year: int, attempts: int = 3
) -> bool:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return probe_year_has_data(session, symbol, year)
        except Exception as exc:  # session hiccup, timeout, network: recreate
            last_error = exc
            session.reset()
            time.sleep(3 * attempt)
    raise RuntimeError(
        f"probe failed for {symbol} {year} after {attempts} attempts: {last_error}"
    )


def earliest_year_for_symbol(session: ProbeSession, symbol: str) -> int | None:
    """Binary search the first year in FIRST_YEAR..LAST_YEAR with data."""
    years = list(range(FIRST_YEAR, LAST_YEAR + 1))
    if not probe_with_retry(session, symbol, years[-1]):
        return None
    lo, hi = 0, len(years) - 1  # invariant: years[hi] has data
    while lo < hi:
        mid = (lo + hi) // 2
        if probe_with_retry(session, symbol, years[mid]):
            hi = mid
        else:
            lo = mid + 1
    return years[lo]


def load_probe_cache() -> dict:
    if PROBE_CACHE.exists():
        try:
            with PROBE_CACHE.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, OSError):
            pass
    return {"earliest_year": {}, "no_data": [], "probe_failed": []}


def save_probe_cache(cache: dict) -> None:
    temp = PROBE_CACHE.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(cache, handle, ensure_ascii=True, indent=1, sort_keys=True)
    os.replace(temp, PROBE_CACHE)


def run_probe_phase(symbols: list[str], log_every: int = 10) -> dict:
    cache = load_probe_cache()
    pending = [
        symbol
        for symbol in symbols
        if symbol not in cache["earliest_year"]
        and symbol not in cache["no_data"]
        and symbol not in cache["probe_failed"]
    ]
    if not pending:
        return cache
    _log_event({"event": "probe_start", "symbols_to_probe": len(pending)})
    session = ProbeSession()
    consecutive_failures = 0
    started = time.monotonic()
    try:
        for index, symbol in enumerate(pending, start=1):
            try:
                earliest = earliest_year_for_symbol(session, symbol)
            except Exception as exc:
                consecutive_failures += 1
                cache.setdefault("probe_failed", []).append(symbol)
                save_probe_cache(cache)
                _log_event(
                    {
                        "event": "probe_symbol_failed",
                        "symbol": symbol,
                        "error": str(exc)[:200],
                        "consecutive_failures": consecutive_failures,
                    }
                )
                if consecutive_failures >= 5:
                    raise RuntimeError(
                        "probe circuit breaker: 5 consecutive symbol failures"
                    )
                session.reset()
                continue
            consecutive_failures = 0
            if earliest is None:
                cache["no_data"].append(symbol)
            else:
                cache["earliest_year"][symbol] = earliest
            save_probe_cache(cache)  # incremental: never lose probe work
            if index % log_every == 0 or index == len(pending):
                elapsed = time.monotonic() - started
                _log_event(
                    {
                        "event": "probe_progress",
                        "probed": index,
                        "total": len(pending),
                        "symbol": symbol,
                        "earliest_year": earliest,
                        "elapsed_s": round(elapsed, 1),
                    }
                )
    finally:
        session.close()
    _log_event(
        {
            "event": "probe_done",
            "earliest_known": len(cache["earliest_year"]),
            "no_data": len(cache["no_data"]),
            "probe_failed": len(cache.get("probe_failed", [])),
        }
    )
    return cache


def build_blocks(cache: dict) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    for symbol, earliest in cache["earliest_year"].items():
        for year in range(max(FIRST_YEAR, int(earliest)), LAST_YEAR + 1):
            blocks.append((year, symbol))
    blocks.sort()  # oldest year first, then symbol
    return blocks


def run_block(symbol: str, year: int) -> dict:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "download_market_data.py"),
        "--symbol",
        symbol,
        "--start",
        f"{year}-01-01",
        "--end",
        f"{year}-12-31",
        "--duration",
        str(DURATION_SECONDS),
        "--acknowledge-data-cost",
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=ATTEMPT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "rows": -1,
            "batch_id": None,
            "detail": f"timeout after {ATTEMPT_TIMEOUT_S}s",
        }
    elapsed = time.monotonic() - started
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    match = None
    for line in stdout.splitlines():
        match = IMPORT_RE.search(line)
        if match:
            break
    if completed.returncode == 0 and match:
        return {
            "ok": True,
            "rows": int(match.group(2)),
            "batch_id": match.group(1),
            "detail": "",
            "elapsed_s": round(elapsed, 1),
            "tail": "",
        }
    tail = (stdout + stderr).strip().splitlines()[-3:]
    return {
        "ok": False,
        "rows": -1,
        "batch_id": None,
        "detail": " | ".join(piece.strip() for piece in tail if piece.strip())[:400],
        "elapsed_s": round(elapsed, 1),
    }


def append_progress(record: dict) -> None:
    with PROGRESS_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True) + "\n")
        handle.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--symbols", help="comma-separated subset filter")
    parser.add_argument("--max-blocks", type=int, default=0, help="0 = unlimited")
    parser.add_argument("--abort-after-failures", type=int, default=6)
    parser.add_argument("--sleep-between-blocks", type=float, default=2.0)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    symbols = load_symbols()
    if args.symbols:
        wanted = {piece.strip() for piece in args.symbols.split(",") if piece.strip()}
        symbols = [symbol for symbol in symbols if symbol in wanted]
    if not symbols:
        print("no symbols to process")
        return 2

    try:
        cache = run_probe_phase(symbols)
    except Exception as exc:  # probe circuit breaker or fatal session loss
        cache = load_probe_cache()
        _log_event({"event": "probe_aborted", "error": str(exc)[:300]})
        if not cache["earliest_year"]:
            print("probe phase produced no usable results")
            return 2
    if args.probe_only:
        print(
            json.dumps(
                {
                    "earliest_year": cache["earliest_year"],
                    "no_data": cache["no_data"],
                    "probe_failed": cache.get("probe_failed", []),
                },
                indent=1,
                sort_keys=True,
            )
        )
        return 0

    blocks = build_blocks(cache)
    if args.symbols:
        wanted = {piece.strip() for piece in args.symbols.split(",") if piece.strip()}
        blocks = [(year, symbol) for year, symbol in blocks if symbol in wanted]
    if args.shard_count > 1:
        blocks = [
            block
            for index, block in enumerate(blocks)
            if index % args.shard_count == args.shard_index
        ]

    done_progress = read_progress()
    db_coverage = load_db_year_coverage()

    pending: list[tuple[int, str]] = []
    for year, symbol in blocks:
        key = (symbol, year)
        if key in done_progress:
            continue
        if key in db_coverage:
            append_progress(
                {"ts": _now_iso(), "symbol": symbol, "year": year, "status": "skipped_db", "rows": -1}
            )
            _log_event(
                {"event": "block", "symbol": symbol, "year": year, "status": "skipped_db", "rows": -1}
            )
            continue
        pending.append((year, symbol))

    _log_event(
        {
            "event": "run_start",
            "pid": os.getpid(),
            "blocks_total": len(blocks),
            "blocks_pending": len(pending),
            "already_done": len(blocks) - len(pending),
            "shard": f"{args.shard_index}/{args.shard_count}",
        }
    )

    if args.dry_run:
        print(
            json.dumps(
                {
                    "blocks_total": len(blocks),
                    "blocks_pending": len(pending),
                    "first_pending": [
                        {"year": year, "symbol": symbol} for year, symbol in pending[:20]
                    ],
                },
                indent=1,
            )
        )
        return 0

    executed = 0
    failed = 0
    consecutive_failures = 0
    rows_total = 0
    run_started = time.monotonic()
    abort_reason = None
    for year, symbol in pending:
        if args.max_blocks and executed >= args.max_blocks:
            abort_reason = f"max_blocks={args.max_blocks} reached"
            break
        block_started = time.monotonic()
        result: dict = {}
        attempts_used = 0
        for attempt in range(1, ATTEMPTS_PER_BLOCK + 1):
            attempts_used = attempt
            result = run_block(symbol, year)
            if result["ok"]:
                break
            if attempt < ATTEMPTS_PER_BLOCK:
                time.sleep(5 * attempt)
        elapsed = round(time.monotonic() - block_started, 1)
        executed += 1
        if result.get("ok"):
            consecutive_failures = 0
            rows_total += max(result.get("rows", 0), 0)
            status = "ok"
        else:
            consecutive_failures += 1
            failed += 1
            status = "failed"
        record = {
            "symbol": symbol,
            "year": year,
            "status": status,
            "rows": result.get("rows", -1),
            "attempts": attempts_used,
            "elapsed_s": elapsed,
            "batch_id": result.get("batch_id"),
            "detail": result.get("detail", ""),
        }
        append_progress({"ts": _now_iso(), **record})
        _log_event({"event": "block", **record})
        if consecutive_failures >= args.abort_after_failures:
            abort_reason = (
                f"circuit breaker: {consecutive_failures} consecutive failed blocks"
            )
            break
        if args.sleep_between_blocks > 0:
            time.sleep(args.sleep_between_blocks)

    run_elapsed = round(time.monotonic() - run_started, 1)
    summary = {
        "event": "run_finish" if abort_reason is None else "run_abort",
        "reason": abort_reason,
        "executed": executed,
        "failed": failed,
        "rows_imported_this_run": rows_total,
        "run_elapsed_s": run_elapsed,
        "avg_block_s": round(run_elapsed / executed, 1) if executed else 0,
        "blocks_remaining_after_run": max(len(pending) - executed, 0),
        "eta_hours_for_remaining": (
            round((run_elapsed / executed) * (len(pending) - executed) / 3600, 2)
            if executed
            else None
        ),
    }
    _log_event(summary)
    print(json.dumps(summary, indent=1), flush=True)
    return 0 if failed == 0 and abort_reason is None else (1 if failed else 3)


if __name__ == "__main__":
    raise SystemExit(main())
