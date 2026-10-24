"""Batch download daily K-lines for ALL option contracts (free tier).

Uses get_kline_serial (free, 10000-bar window) to fetch full-life daily bars
for every option contract across six exchanges, including expired ones.
Imports into market_bars via the standard idempotent CSV pipeline.

Supports sharding for parallel execution: launch N instances with
--shard-count N and --shard-index 0..N-1.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.config import load_settings
from ak_trading.market_reference import load_reference_book
from ak_trading.persistence.config import load_database_settings
from ak_trading.persistence.database import create_database_engine
from ak_trading.persistence.market_data import expected_kline_columns, import_market_bars
from ak_trading.persistence.storage import store_reference_snapshot

EXCHANGES = ("SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX")
DAILY = 86400
MAX_BARS = 2000
CST = timezone(timedelta(hours=8))
BAR_FIELDS = ("open", "high", "low", "close", "volume", "open_oi", "close_oi")
DATA_DIR = ROOT / "data" / "downloads"
UNIVERSE_FILE = DATA_DIR / "option_universe.json"


def get_paths(shard: int):
    """Per-shard file paths to avoid write conflicts."""
    suffix = f"_s{shard}" if shard >= 0 else ""
    return {
        "progress": DATA_DIR / f"option_progress{suffix}.jsonl",
        "log": DATA_DIR / f"option_daily{suffix}.log",
    }


def load_all_progress(shard: int) -> set[str]:
    """Load done symbols from this shard's progress file AND all other shards."""
    done = set()
    # scan all progress files (option_progress*.jsonl)
    for f in DATA_DIR.glob("option_progress*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("status") in ("ok", "empty"):
                    done.add(rec["symbol"])
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def log(shard: int, msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] s{shard} {msg}"
    print(line, flush=True)
    paths = get_paths(shard)
    with paths["log"].open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def discover_universe(force: bool = False) -> dict[str, list[str]]:
    if UNIVERSE_FILE.exists() and not force:
        return json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))

    from tqsdk import TqApi, TqAuth
    from contextlib import closing
    settings = load_settings()
    universe: dict[str, list[str]] = {}
    with closing(TqApi(auth=TqAuth(settings.tq_user, settings.tq_pass))) as api:
        for ex in EXCHANGES:
            try:
                all_syms = sorted(api.query_quotes(ins_class="OPTION", exchange_id=ex, expired=True))
                live = set(api.query_quotes(ins_class="OPTION", exchange_id=ex, expired=False))
                universe[ex] = sorted(set(all_syms) | live)
            except Exception:
                universe[ex] = []

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UNIVERSE_FILE.write_text(json.dumps(universe, ensure_ascii=False, indent=1), encoding="utf-8")
    return universe


def record_progress(shard: int, symbol: str, status: str, rows: int, elapsed: float) -> None:
    rec = {"symbol": symbol, "status": status, "rows": rows,
           "elapsed_s": round(elapsed, 2),
           "ts": datetime.now(CST).isoformat(timespec="seconds")}
    paths = get_paths(shard)
    with paths["progress"].open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def fetch_and_import(api, engine, symbol: str, db_settings, snapshot, calendar) -> tuple[str, int]:
    try:
        frame = api.get_kline_serial(symbol, DAILY, MAX_BARS)
        api.wait_update(deadline=15)
        valid = frame.dropna(subset=["close"])
        valid = valid[valid["datetime"] > 0]
        if valid.empty:
            return "empty", 0

        fd, tmp = tempfile.mkstemp(prefix="ak_opt_", suffix=".csv", dir=DATA_DIR)
        os.close(fd)
        try:
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(expected_kline_columns(symbol))
                for _, row in valid.iterrows():
                    ts = datetime.fromtimestamp(row["datetime"] / 1e9, tz=CST)
                    writer.writerow([ts.isoformat(), int(row["datetime"])] +
                                    [row[fd_] for fd_ in BAR_FIELDS])
            result = import_market_bars(
                engine, tmp, symbol=symbol, duration_seconds=DAILY,
                calendar=calendar, reference_sha256=snapshot.content_sha256,
                source_label=f"tqsdk_option:{symbol}:{DAILY}",
                settings=db_settings,
            )
            return "ok", result.rows_written
        finally:
            Path(tmp).unlink(missing_ok=True)
    except Exception as exc:
        return f"error:{type(exc).__name__}:{str(exc)[:80]}", 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-count", type=int, default=1,
                        help="total number of parallel shards")
    parser.add_argument("--shard-index", type=int, default=0,
                        help="this process's shard index (0-based)")
    parser.add_argument("--exchange", action="append")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    shard = args.shard_index if args.shard_count > 1 else -1
    from tqsdk import TqApi, TqAuth
    from contextlib import closing

    settings = load_settings()
    db_settings = load_database_settings(required=True)
    engine = create_database_engine(db_settings)
    book = load_reference_book(ROOT / "config" / "market_reference.json")
    snapshot = store_reference_snapshot(
        engine, ROOT / "config" / "market_reference.json", settings=db_settings)

    universe = discover_universe()
    exchanges = args.exchange or list(universe.keys())
    all_symbols = []
    for ex in exchanges:
        syms = universe.get(ex, [])
        if args.limit:
            syms = syms[:args.limit]
        all_symbols.extend(syms)

    # apply shard filter: round-robin across all symbols
    if args.shard_count > 1:
        all_symbols = [s for i, s in enumerate(all_symbols) if i % args.shard_count == args.shard_index]

    done = load_all_progress(shard)
    pending = [s for s in all_symbols if s not in done]
    log(shard, f"shard {args.shard_index}/{args.shard_count}: "
               f"total={len(all_symbols)} done={len(done)} pending={len(pending)}")

    total_rows = 0
    ok = empty = error = 0
    t_start = time.time()

    with closing(TqApi(auth=TqAuth(settings.tq_user, settings.tq_pass))) as api:
        for i, symbol in enumerate(pending):
            t0 = time.time()
            status, rows = fetch_and_import(api, engine, symbol,
                                            db_settings, snapshot, book.calendar)
            total_rows += rows
            if status == "ok":
                ok += 1
            elif status == "empty":
                empty += 1
            else:
                error += 1
                if error <= 5:
                    log(shard, f"  ERR {symbol}: {status}")

            record_progress(shard, symbol,
                           "ok" if status == "ok" else ("empty" if status == "empty" else "error"),
                           rows, time.time() - t0)

            if (i + 1) % 50 == 0 or i == len(pending) - 1:
                elapsed = time.time() - t_start
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                log(shard, f"  {i+1}/{len(pending)} ok={ok} empty={empty} "
                          f"err={error} rows={total_rows:,} ({rate:.1f}/s)")

    engine.dispose()
    log(shard, f"DONE: total={len(pending)} ok={ok} empty={empty} error={error} "
              f"rows={total_rows:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
