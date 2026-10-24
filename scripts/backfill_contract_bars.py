"""Backfill contract-level (non main-continuity) K-line history for all products.

Scope (per the 2026-09 backfill task):

* Task A (priority): daily bars (duration 86400) for EVERY futures contract
  (including expired ones, e.g. SHFE.rb2505 / DCE.m2609 / CFFEX.IF2406) of all
  products listed in ``market_bars`` as ``KQ.m@EX.product`` main-continuity
  symbols, across SHFE/INE/DCE/CZCE/CFFEX/GFEX.
* Task B (opportunistic): 1-minute bars (duration 60) for the same contracts,
  chunked per calendar year, executed only after Task A is complete.

Design (mirrors the proven ``scripts/backfill_long_history.py`` driver, which
stays untouched):

* Contract universe discovery uses the free ``query_quotes(..., expired=True)``
  API plus ``query_symbol_info`` expire timestamps, cached in
  ``data/downloads/backfill_contract_universe.json``.
* Downloads run IN-PROCESS through one reusable TqApi session (recreated on
  errors) via ``tqsdk.tools.DataDownloader`` and are imported with the exact
  project pipeline ``import_market_bars`` (idempotent upsert), so the database
  semantics are identical to ``scripts/download_market_data.py``.
* Resume: one JSON line per block goes to
  ``data/downloads/backfill_contract_progress.jsonl``; blocks recorded as
  ``ok`` / ``empty`` / ``skipped_db`` are skipped on restart. A block is also
  skipped when ``market_bars`` already has >= 1 row for it (daily: per
  symbol+duration; minute: per symbol+duration+calendar year, because a single
  banked year must not mark the whole contract done).
* Each block is retried up to 2 extra times; permanent failures are logged and
  the run continues. A circuit breaker aborts after many consecutive failures
  (e.g. trial expired) so a dead session does not burn hours.
* JSON event log: ``data/downloads/backfill_contract.log``.
* Order: product by product, contracts by expiry from oldest to newest.
* The driver never prints credentials, never touches .env, never trades, and
  only performs idempotent upserts.
"""

from __future__ import annotations

import argparse
import json
import os
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
from ak_trading.journal import redact_text  # noqa: E402

DATA_DIR = ROOT / "data" / "downloads"
UNIVERSE_FILE = DATA_DIR / "backfill_contract_universe.json"
PROGRESS_FILE = DATA_DIR / "backfill_contract_progress.jsonl"
LOG_FILE = DATA_DIR / "backfill_contract.log"
TMP_DIR = DATA_DIR / "contract_tmp"
MAIN_PROBE_CACHE = DATA_DIR / "backfill_long_probe.json"  # read-only reuse

REFERENCE_CONFIG = ROOT / "config" / "market_reference.json"

START_DATE = date(2010, 1, 1)
END_DATE = date(2026, 8, 31)
DAILY = 86400
MINUTE = 60
DEFAULT_MINUTE_FLOOR_YEAR = 2016  # contract 1m history on the server starts ~2016

DONE_STATUSES = frozenset({"ok", "empty", "skipped_db"})
ATTEMPTS_PER_BLOCK = 3  # initial try + 2 retries
DAILY_DEADLINE_S = 300
MINUTE_DEADLINE_S = 3600
CIRCUIT_BREAKER_LIMIT = 15
STOP_BEFORE_DEFAULT = "2026-09-05T13:30:00+08:00"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _log_event(record: dict) -> None:
    record = {"ts": _now_iso(), **record}
    line = json.dumps(record, ensure_ascii=True)
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _safe_detail(exc: BaseException) -> str:
    text = str(exc)
    try:
        settings = load_settings()
    except Exception:
        settings = None
    if settings is not None:
        text = redact_text(text, secrets=(settings.tq_user, settings.tq_pass))
    return text[:250].replace("\n", " ")


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


def load_main_products() -> list[tuple[str, str]]:
    """(exchange_id, product_id) pairs from the KQ.m@ main-continuity symbols."""
    with closing(_connect_db()) as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM market_bars WHERE symbol LIKE 'KQ.m@%'"
        ).fetchall()
    pairs = []
    for (symbol,) in rows:
        rest = symbol[len("KQ.m@") :]
        exchange_id, product_id = rest.split(".", 1)
        pairs.append((exchange_id, product_id))
    return sorted(pairs)


# ---------------------------------------------------------------------------
# Universe discovery (free query_quotes / query_symbol_info APIs)
# ---------------------------------------------------------------------------


def build_universe(force: bool = False) -> dict:
    if UNIVERSE_FILE.exists() and not force:
        with UNIVERSE_FILE.open("r", encoding="utf-8") as handle:
            universe = json.load(handle)
        _log_event(
            {
                "event": "universe_cache_loaded",
                "contracts": sum(
                    len(v) for v in universe["products"].values()
                ),
            }
        )
        return universe

    pairs = load_main_products()
    _log_event({"event": "universe_start", "products": len(pairs)})

    session = ApiSession()
    all_contracts: list[str] = []
    per_product: dict[str, list[str]] = {}
    try:
        for index, (exchange_id, product_id) in enumerate(pairs, start=1):
            api = session.api()
            found = set(
                api.query_quotes(
                    ins_class="FUTURE",
                    exchange_id=exchange_id,
                    product_id=product_id,
                    expired=True,
                )
            )
            found |= set(
                api.query_quotes(
                    ins_class="FUTURE",
                    exchange_id=exchange_id,
                    product_id=product_id,
                    expired=False,
                )
            )
            key = f"{exchange_id}.{product_id}"
            per_product[key] = sorted(found)
            all_contracts.extend(sorted(found))
            if index % 20 == 0 or index == len(pairs):
                _log_event(
                    {
                        "event": "universe_progress",
                        "products_done": index,
                        "products_total": len(pairs),
                        "contracts": len(all_contracts),
                    }
                )
    finally:
        session.close()

    expire_info: dict[str, dict] = {}
    session = ApiSession()
    try:
        for offset in range(0, len(all_contracts), 500):
            api = session.api()
            frame = api.query_symbol_info(
                all_contracts[offset : offset + 500]
            )
            for row in frame.itertuples(index=False):
                expire_raw = row.expire_datetime
                expire_info[row.instrument_id] = {
                    "expire_datetime": (
                        int(expire_raw)
                        if expire_raw == expire_raw  # not NaN
                        else None
                    ),
                    "expired": bool(row.expired),
                }
    finally:
        session.close()

    universe = {
        "generated_at": _now_iso(),
        "products": {
            key: sorted(
                (
                    {
                        "symbol": symbol,
                        "expire_datetime": expire_info.get(symbol, {}).get(
                            "expire_datetime"
                        ),
                        "expired": expire_info.get(symbol, {}).get("expired"),
                    }
                    for symbol in symbols
                ),
                key=lambda item: (
                    item["expire_datetime"] is None,
                    item["expire_datetime"] or 0,
                    item["symbol"],
                ),
            )
            for key, symbols in per_product.items()
        },
    }
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    temp = UNIVERSE_FILE.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(universe, handle, ensure_ascii=True, indent=1, sort_keys=True)
    os.replace(temp, UNIVERSE_FILE)
    _log_event(
        {
            "event": "universe_done",
            "contracts": len(all_contracts),
            "with_expire": len(expire_info),
        }
    )
    return universe


# ---------------------------------------------------------------------------
# Progress resume
# ---------------------------------------------------------------------------


def read_progress() -> set[tuple]:
    done: set[tuple] = set()
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
            if record.get("status") not in DONE_STATUSES:
                continue
            task = record.get("task")
            symbol = record.get("symbol")
            if task == "daily":
                done.add(("daily", symbol))
            elif task == "minute" and record.get("year"):
                done.add(("minute", symbol, int(record["year"])))
    return done


def record_progress(record: dict) -> None:
    record = {"ts": _now_iso(), **record}
    line = json.dumps(record, ensure_ascii=True)
    with PROGRESS_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def db_has_rows(symbol: str, duration_seconds: int, year: int | None) -> bool:
    query = (
        "SELECT 1 FROM market_bars WHERE symbol = %s AND duration_seconds = %s"
    )
    params: list[object] = [symbol, duration_seconds]
    if year is not None:
        query += " AND ts >= make_timestamptz(%s, 1, 1, 0, 0, 0, 'Asia/Shanghai')"
        query += (
            " AND ts < make_timestamptz(%s, 1, 1, 0, 0, 0, 'Asia/Shanghai')"
        )
        params.extend([year, year + 1])
    query += " LIMIT 1"
    with closing(_connect_db()) as conn:
        return conn.execute(query, params).fetchone() is not None


def daily_year_span(symbol: str) -> tuple[int, int] | None:
    """First/last calendar year with daily bars (Task A coverage map)."""
    with closing(_connect_db()) as conn:
        row = conn.execute(
            "SELECT EXTRACT(YEAR FROM min(ts))::int, "
            "EXTRACT(YEAR FROM max(ts))::int "
            "FROM market_bars WHERE symbol = %s AND duration_seconds = %s",
            [symbol, DAILY],
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0]), int(row[1])


def load_minute_floor_years() -> dict[str, int]:
    """Per-product earliest 1m year from the main-continuity probe cache."""
    floors: dict[str, int] = {}
    if not MAIN_PROBE_CACHE.exists():
        return floors
    try:
        with MAIN_PROBE_CACHE.open("r", encoding="utf-8") as handle:
            cache = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return floors
    for symbol, year in cache.get("earliest_year", {}).items():
        if isinstance(year, int):
            floors[symbol] = year
    return floors


# ---------------------------------------------------------------------------
# TqApi session reuse
# ---------------------------------------------------------------------------


class ApiSession:
    """One TqApi session reused across blocks, recreated on errors."""

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


# ---------------------------------------------------------------------------
# Block execution: DataDownloader + import_market_bars (idempotent upsert)
# ---------------------------------------------------------------------------


class ImportContext:
    """Long-lived SQLAlchemy engine + reference snapshot (one per run)."""

    def __init__(self) -> None:
        from ak_trading.market_reference import load_reference_book
        from ak_trading.persistence.config import load_database_settings
        from ak_trading.persistence.database import create_database_engine
        from ak_trading.persistence.storage import store_reference_snapshot

        self.db_settings = load_database_settings(required=True)
        assert self.db_settings is not None
        self.engine = create_database_engine(self.db_settings)
        book = load_reference_book(REFERENCE_CONFIG)
        self.calendar = book.calendar
        snapshot = store_reference_snapshot(
            self.engine, REFERENCE_CONFIG, settings=self.db_settings
        )
        self.reference_sha256 = snapshot.content_sha256

    def close(self) -> None:
        self.engine.dispose()


def _download_csv(
    session: ApiSession,
    symbol: str,
    duration_seconds: int,
    start: date,
    end: date,
    deadline_s: int,
) -> Path:
    from tqsdk.tools import DataDownloader

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix="contract_", suffix=".csv", dir=TMP_DIR
    )
    os.close(descriptor)
    csv_path = Path(raw_path)
    csv_path.unlink(missing_ok=True)

    api = session.api()
    downloader = DataDownloader(
        api,
        symbol_list=symbol,
        dur_sec=duration_seconds,
        start_dt=start,
        end_dt=end,
        csv_file_name=str(csv_path),
    )
    deadline = time.monotonic() + deadline_s
    try:
        while not downloader.is_finished():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"download not finished within {deadline_s}s"
                )
            api.wait_update(deadline=time.time() + min(remaining, 60.0))
    except Exception:
        session.reset()
        raise
    finally:
        del downloader
    if not csv_path.exists():
        raise RuntimeError("downloader produced no CSV file")
    return csv_path


def _csv_data_rows(csv_path: Path) -> int:
    try:
        with csv_path.open("r", encoding="utf-8", errors="replace") as handle:
            return max(sum(1 for _ in handle) - 1, 0)  # minus header
    except OSError:
        return 0


def execute_block(
    session: ApiSession,
    context: ImportContext,
    symbol: str,
    duration_seconds: int,
    start: date,
    end: date,
    deadline_s: int,
) -> dict:
    """Run one block end to end. Returns a result dict, never raises."""
    started = time.monotonic()
    last_detail = ""
    for attempt in range(1, ATTEMPTS_PER_BLOCK + 1):
        csv_path = None
        try:
            csv_path = _download_csv(
                session, symbol, duration_seconds, start, end, deadline_s
            )
            rows = _csv_data_rows(csv_path)
            if rows == 0:
                return {
                    "status": "empty",
                    "rows": 0,
                    "batch_id": None,
                    "detail": "",
                    "attempts": attempt,
                    "elapsed_s": round(time.monotonic() - started, 1),
                }
            from ak_trading.persistence.market_data import import_market_bars

            result = import_market_bars(
                context.engine,
                csv_path,
                symbol=symbol,
                duration_seconds=duration_seconds,
                calendar=context.calendar,
                reference_sha256=context.reference_sha256,
                source_label=(
                    f"tqsdk:{symbol}:{duration_seconds}:"
                    f"{start.isoformat()}:{end.isoformat()}"
                ),
                settings=context.db_settings,
            )
            return {
                "status": "ok",
                "rows": result.rows_written,
                "batch_id": result.batch_id,
                "detail": "",
                "attempts": attempt,
                "elapsed_s": round(time.monotonic() - started, 1),
            }
        except Exception as exc:  # noqa: BLE001 - logged and retried
            last_detail = _safe_detail(exc)
            time.sleep(min(3 * attempt, 10))
        finally:
            if csv_path is not None:
                csv_path.unlink(missing_ok=True)
    return {
        "status": "failed",
        "rows": -1,
        "batch_id": None,
        "detail": last_detail,
        "attempts": ATTEMPTS_PER_BLOCK,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


# ---------------------------------------------------------------------------
# Task runners
# ---------------------------------------------------------------------------


def _deadline_reached(stop_before: datetime | None) -> bool:
    return stop_before is not None and datetime.now(timezone.utc) >= stop_before


def run_daily(
    session: ApiSession,
    context: ImportContext,
    universe: dict,
    done: set[tuple],
    *,
    products_filter: set[str] | None,
    limit: int | None,
    stop_before: datetime | None,
) -> None:
    executed = 0
    consecutive_failures = 0
    for key in sorted(universe["products"]):
        if products_filter is not None and key not in products_filter:
            continue
        contracts = universe["products"][key]
        _log_event(
            {
                "event": "daily_product_start",
                "product": key,
                "contracts": len(contracts),
                "blocks_executed": executed,
            }
        )
        for contract in contracts:
            symbol = contract["symbol"]
            if limit is not None and executed >= limit:
                return
            if _deadline_reached(stop_before):
                _log_event({"event": "daily_stopped_deadline", "symbol": symbol})
                return
            if ("daily", symbol) in done:
                continue
            if db_has_rows(symbol, DAILY, None):
                record_progress(
                    {
                        "task": "daily",
                        "symbol": symbol,
                        "duration_seconds": DAILY,
                        "status": "skipped_db",
                        "rows": 0,
                    }
                )
                done.add(("daily", symbol))
                continue
            result = execute_block(
                session,
                context,
                symbol,
                DAILY,
                START_DATE,
                END_DATE,
                DAILY_DEADLINE_S,
            )
            executed += 1
            record_progress(
                {
                    "task": "daily",
                    "symbol": symbol,
                    "duration_seconds": DAILY,
                    **result,
                }
            )
            if result["status"] == "failed":
                consecutive_failures += 1
                if consecutive_failures >= CIRCUIT_BREAKER_LIMIT:
                    _log_event(
                        {
                            "event": "daily_circuit_breaker",
                            "consecutive_failures": consecutive_failures,
                            "last_detail": result["detail"],
                        }
                    )
                    return
            else:
                consecutive_failures = 0
                done.add(("daily", symbol))
        _log_event(
            {
                "event": "daily_product_done",
                "product": key,
                "blocks_executed": executed,
            }
        )
    _log_event({"event": "daily_task_complete", "blocks_executed": executed})


def run_minute(
    session: ApiSession,
    context: ImportContext,
    universe: dict,
    done: set[tuple],
    *,
    products_filter: set[str] | None,
    limit: int | None,
    stop_before: datetime | None,
) -> None:
    floors = load_minute_floor_years()
    executed = 0
    consecutive_failures = 0
    for key in sorted(universe["products"]):
        if products_filter is not None and key not in products_filter:
            continue
        contracts = universe["products"][key]
        _log_event(
            {
                "event": "minute_product_start",
                "product": key,
                "contracts": len(contracts),
                "blocks_executed": executed,
            }
        )
        floor_year = floors.get(f"KQ.m@{key}", DEFAULT_MINUTE_FLOOR_YEAR)
        for contract in contracts:
            symbol = contract["symbol"]
            span = daily_year_span(symbol)
            if span is None:
                continue
            first_year = max(span[0], floor_year)
            last_year = min(span[1], END_DATE.year)
            for year in range(first_year, last_year + 1):
                if limit is not None and executed >= limit:
                    return
                if _deadline_reached(stop_before):
                    _log_event(
                        {"event": "minute_stopped_deadline", "symbol": symbol}
                    )
                    return
                if ("minute", symbol, year) in done:
                    continue
                if db_has_rows(symbol, MINUTE, year):
                    record_progress(
                        {
                            "task": "minute",
                            "symbol": symbol,
                            "year": year,
                            "duration_seconds": MINUTE,
                            "status": "skipped_db",
                            "rows": 0,
                        }
                    )
                    done.add(("minute", symbol, year))
                    continue
                block_end = min(date(year, 12, 31), END_DATE)
                result = execute_block(
                    session,
                    context,
                    symbol,
                    MINUTE,
                    date(year, 1, 1),
                    block_end,
                    MINUTE_DEADLINE_S,
                )
                executed += 1
                record_progress(
                    {
                        "task": "minute",
                        "symbol": symbol,
                        "year": year,
                        "duration_seconds": MINUTE,
                        **result,
                    }
                )
                if result["status"] == "failed":
                    consecutive_failures += 1
                    if consecutive_failures >= CIRCUIT_BREAKER_LIMIT:
                        _log_event(
                            {
                                "event": "minute_circuit_breaker",
                                "consecutive_failures": consecutive_failures,
                                "last_detail": result["detail"],
                            }
                        )
                        return
                else:
                    consecutive_failures = 0
                    done.add(("minute", symbol, year))
        _log_event(
            {
                "event": "minute_product_done",
                "product": key,
                "blocks_executed": executed,
            }
        )
    _log_event({"event": "minute_task_complete", "blocks_executed": executed})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill contract-level daily/minute K-lines (resumable)"
    )
    parser.add_argument(
        "--task",
        choices=("daily", "minute", "auto"),
        default="auto",
        help="daily first, then minute when 'auto' (default)",
    )
    parser.add_argument(
        "--products",
        help="comma-separated EX.product filter, e.g. SHFE.rb,DCE.m",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="execute at most N blocks (for smoke tests)",
    )
    parser.add_argument(
        "--refresh-universe",
        action="store_true",
        help="rebuild the contract universe cache",
    )
    parser.add_argument(
        "--stop-before",
        default=STOP_BEFORE_DEFAULT,
        help="do not start new blocks after this ISO datetime",
    )
    parser.add_argument(
        "--max-runtime-hours",
        type=float,
        help="soft stop after N hours of runtime",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()

    stop_before: datetime | None = None
    if args.stop_before:
        try:
            parsed = datetime.fromisoformat(args.stop_before)
            stop_before = (
                parsed if parsed.tzinfo else parsed.astimezone(timezone.utc)
            )
        except ValueError:
            stop_before = datetime.fromisoformat(STOP_BEFORE_DEFAULT)

    def _soft_deadline_hit() -> bool:
        if args.max_runtime_hours is not None:
            if (time.monotonic() - started) / 3600 >= args.max_runtime_hours:
                return True
        return _deadline_reached(stop_before)

    products_filter = None
    if args.products:
        products_filter = {
            item.strip() for item in args.products.split(",") if item.strip()
        }

    _log_event(
        {
            "event": "run_start",
            "task": args.task,
            "pid": os.getpid(),
            "products_filter": sorted(products_filter) if products_filter else None,
            "limit": args.limit,
            "stop_before": args.stop_before,
        }
    )

    universe = build_universe(force=args.refresh_universe)
    done = read_progress()
    _log_event(
        {
            "event": "progress_loaded",
            "done_daily": sum(1 for item in done if item[0] == "daily"),
            "done_minute": sum(1 for item in done if item[0] == "minute"),
        }
    )

    session = ApiSession()
    context = ImportContext()
    try:
        if args.task in ("daily", "auto"):
            run_daily(
                session,
                context,
                universe,
                done,
                products_filter=products_filter,
                limit=args.limit,
                stop_before=stop_before,
            )
        if args.task in ("minute", "auto") and not _soft_deadline_hit():
            run_minute(
                session,
                context,
                universe,
                done,
                products_filter=products_filter,
                limit=args.limit,
                stop_before=stop_before,
            )
    finally:
        session.close()
        context.close()
    _log_event(
        {
            "event": "run_end",
            "elapsed_s": round(time.monotonic() - started, 1),
            "session_recreations": session.recreations,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
