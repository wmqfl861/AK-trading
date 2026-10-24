"""Backfill member ranking history via TqApi.query_symbol_ranking (trial window).

Scope (2026-09 task, trial expires 2026-09-05 13:49):

* DCE: full history 2020-07-20 .. 2026-08-31 (TQ official data start).
* SHFE / INE / CFFEX / GFEX / CZCE: only 2020-07-20 .. 2023-12-31. These five
  exchanges have official-exchange data from 2024-01-01 as the canonical
  history, so TQ rows for them are HARD-GUARDED against trading_day >=
  2024-01-01 (assertions in build_rows plus a final pre-import check).

Verified query behavior (probed 2026-09-01):

* ``query_symbol_ranking(symbol, ranking_type, days, start_dt)`` returns one
  DataFrame with 20 rows per day that has data inside the window
  ``[start_dt, start_dt + days trading days)``. Days without disclosure for
  that contract simply do not appear; an empty frame means "no data in this
  window" (before listing, after data end, gaps, or non-trading).
* Each ranking_type call contributes ONLY its own three columns
  (VOLUME -> volume/volume_change/volume_ranking, LONG -> long_*, SHORT ->
  short_*); cross-metric columns are incomplete in that call and are dropped.
* ``broker`` values carry a single ASCII uppercase prefix letter before the
  Chinese name (e.g. 'D东证期货（代客）'); the prefix is stripped before
  import with a strict ^[A-Z](?=CJK) rule.
* Large ``days`` (>= ~100) degrade per-day throughput badly and can hang, so
  blocks use days=20 (~0.5 s per contract-day-metric on 2020 DCE data, the
  measured sweet spot; days=60 was 3x worse per day, days=250 hangs).

Design:

* One coordinator process spawns N child workers (subprocess, same script
  ``--worker-mode child``). Workers statically partition the product list
  (idx % N) so each product -- and its per-contract observed-first-data
  chain -- stays inside one worker.
* Resume: one JSON line per block in
  ``data/downloads/backfill_tq_rank_progress.jsonl`` keyed by
  (phase, symbol, metric) with a forward cursor. Blocks whose window already
  has >= 1 row in ``member_position_ranks`` are skipped (``skipped_db``).
* The walk per (contract, metric) starts at
  max(phase_start, expire - 420d, prev_contract_first_data - 60d) so
  pre-listing probing is bounded, and slides 25 calendar days on empty windows
  (20 trading days always span >= 28 calendar days, so empty windows overlap
  and no data day can ever be skipped) until the cursor passes
  min(phase_end, expire + 7d).
* Every block retries twice more on errors with 30/60 s backoff (the TQ
  account is shared with two other backfill processes); a failed block is
  recorded and the walk abandons that symbol-metric for a later run. A
  circuit breaker aborts the worker after 15 consecutive failures (exit code
  42; the coordinator waits two minutes before respawning it).
* The coordinator kills and respawns a worker whose shared log has been
  silent for 15 minutes (hung-query protection) and enforces the global
  stop-before deadline.
* Imports go through ``ak_trading.member_rank.MemberRankRow`` +
  ``ak_trading.persistence.member_rank.import_member_rank_rows`` (idempotent
  upsert into ``member_position_ranks``). No DELETE/TRUNCATE, no schema
  changes.
* The driver never prints credentials, never touches .env, never trades, and
  creates no TqKq/TqAccount objects (market-data TqAuth session only).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DATA_DIR = ROOT / "data" / "downloads"
PROGRESS_FILE = DATA_DIR / "backfill_tq_rank_progress.jsonl"
LOG_FILE = DATA_DIR / "backfill_tq_rank.log"
UNIVERSE_SRC = DATA_DIR / "backfill_contract_universe.json"  # read-only reuse
UNIVERSE_OWN = DATA_DIR / "backfill_tq_rank_universe.json"

SOURCE_URL = "tqsdk:query_symbol_ranking"

GUARDED_FIVE = frozenset({"SHFE", "INE", "CFFEX", "GFEX", "CZCE"})
FIVE_CUTOFF = date(2024, 1, 1)  # TQ must never write five-ex rows on/after this
GLOBAL_MAX_DAY = date(2026, 8, 31)  # absolute data ceiling for this backfill

PHASES: dict[str, dict] = {
    "dce": {
        "exchanges": ("DCE",),
        "start": date(2020, 7, 20),
        "end": date(2026, 8, 31),
    },
    "five_2020_2022": {
        "exchanges": ("SHFE", "INE", "CFFEX", "GFEX", "CZCE"),
        "start": date(2020, 7, 20),
        "end": date(2022, 12, 31),
    },
    "five_2023": {
        "exchanges": ("SHFE", "INE", "CFFEX", "GFEX", "CZCE"),
        "start": date(2023, 1, 1),
        "end": date(2023, 12, 31),
    },
}
PHASE_ORDER = ("dce", "five_2020_2022", "five_2023")

DAYS_PER_CALL = 20
EMPTY_SLIDE_DAYS = 25          # < 28 = min calendar span of 20 trading days
FETCH_WINDOW_CAL_DAYS = 28     # db-skip window mirror of one fetch
LISTING_LEAD_DAYS = 420        # contracts list <= ~14 months before expiry
CHAIN_MARGIN_DAYS = 60         # prev contract first-data minus this margin
EXPIRE_TAIL_BUFFER_DAYS = 7
ATTEMPTS_PER_BLOCK = 3
BACKOFF_S = (30, 60)
CIRCUIT_BREAKER = 15
CIRCUIT_BREAKER_EXIT = 42
STALL_KILL_S = 900             # coordinator kills a silent worker after 15 min
COORDINATOR_POLL_S = 30
MAX_CHILD_RESTARTS = 60
STOP_BEFORE_DEFAULT = "2026-09-05T13:30:00+08:00"
BEIJING = timezone(timedelta(hours=8))

METRIC_SPECS = (  # ranking_type -> (metric, qty col, change col, rank col)
    ("VOLUME", "volume", "volume", "volume_change", "volume_ranking"),
    ("LONG", "long", "long_oi", "long_change", "long_ranking"),
    ("SHORT", "short", "short_oi", "short_change", "short_ranking"),
)

_BROKER_PREFIX_RE = re.compile(r"^[A-Z](?=[\u4e00-\u9fff])")
_PRODUCT_RE = re.compile(r"^[A-Za-z]+")

DONE_STATUSES = frozenset({"ok", "empty", "skipped_db"})


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
        from ak_trading.config import load_settings
        from ak_trading.journal import redact_text

        settings = load_settings()
        if settings is not None:
            text = redact_text(text, secrets=(settings.tq_user, settings.tq_pass))
    except Exception:
        pass
    return text[:250].replace("\n", " ")


def parse_day(value: object) -> date | None:
    text = str(value)[:8]
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except (ValueError, IndexError):
        return None


def strip_broker_prefix(name: str) -> str:
    return _BROKER_PREFIX_RE.sub("", name, count=1)


# ---------------------------------------------------------------------------
# Database helpers (psycopg direct for probes; SQLAlchemy for imports)
# ---------------------------------------------------------------------------


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
        options="-c statement_timeout=60000",
    )


def _load_main_products() -> list[tuple[str, str]]:
    with closing(_connect_db()) as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM market_bars WHERE symbol LIKE 'KQ.m@%'"
        ).fetchall()
    pairs = []
    for (symbol,) in rows:
        rest = symbol[len("KQ.m@"):]
        exchange_id, product_id = rest.split(".", 1)
        pairs.append((exchange_id, product_id))
    return sorted(pairs)


# ---------------------------------------------------------------------------
# Universe (contract list per product with expire timestamps)
# ---------------------------------------------------------------------------


def load_universe() -> dict:
    source = UNIVERSE_SRC if UNIVERSE_SRC.exists() else UNIVERSE_OWN
    if source.exists():
        with source.open("r", encoding="utf-8") as handle:
            universe = json.load(handle)
        _log_event({"event": "universe_loaded", "path": source.name,
                    "products": len(universe["products"]),
                    "contracts": sum(len(v) for v in universe["products"].values())})
        return universe
    return _build_universe()


def _build_universe() -> dict:
    """Build our own universe cache (only when the shared one is absent)."""

    pairs = _load_main_products()
    _log_event({"event": "universe_build_start", "products": len(pairs)})
    session = ApiSession()
    per_product: dict[str, list[str]] = {}
    try:
        for exchange_id, product_id in pairs:
            api = session.api()
            found = set(
                api.query_quotes(
                    ins_class="FUTURE", exchange_id=exchange_id,
                    product_id=product_id, expired=True,
                )
            )
            found |= set(
                api.query_quotes(
                    ins_class="FUTURE", exchange_id=exchange_id,
                    product_id=product_id, expired=False,
                )
            )
            per_product[f"{exchange_id}.{product_id}"] = sorted(found)
    finally:
        session.close()
    all_contracts = [s for items in per_product.values() for s in items]
    expire_info: dict[str, dict] = {}
    session = ApiSession()
    try:
        for offset in range(0, len(all_contracts), 500):
            frame = session.api().query_symbol_info(
                all_contracts[offset: offset + 500]
            )
            for row in frame.itertuples(index=False):
                expire_raw = row.expire_datetime
                expire_info[row.instrument_id] = {
                    "expire_datetime": (
                        int(expire_raw) if expire_raw == expire_raw else None
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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = UNIVERSE_OWN.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(universe, handle, ensure_ascii=True, indent=1, sort_keys=True)
    os.replace(temp, UNIVERSE_OWN)
    _log_event({"event": "universe_build_done", "contracts": len(all_contracts)})
    return universe


# ---------------------------------------------------------------------------
# Progress resume
# ---------------------------------------------------------------------------


def _progress_key(phase: str, symbol: str, metric: str) -> str:
    return f"{phase}|{symbol}|{metric}"


def read_progress() -> dict[str, dict]:
    """key -> {"cursor": date, "done": bool, "first": date|None} (max cursor wins)."""

    state: dict[str, dict] = {}
    if not PROGRESS_FILE.exists():
        return state
    with PROGRESS_FILE.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn final line
            symbol = record.get("symbol")
            if not record.get("phase") or not symbol or not record.get("metric"):
                continue
            key = _progress_key(record["phase"], symbol, record["metric"])
            try:
                cursor = date.fromisoformat(str(record.get("cursor_to"))[:10])
            except (TypeError, ValueError):
                cursor = None
            entry = state.setdefault(
                key, {"cursor": None, "done": False, "first": None}
            )
            if cursor is not None and (
                entry["cursor"] is None or cursor > entry["cursor"]
            ):
                entry["cursor"] = cursor
            if record.get("status") == "done":
                entry["done"] = True
            first = record.get("first")
            if first:
                try:
                    first_day = date.fromisoformat(str(first)[:10])
                except (TypeError, ValueError):
                    first_day = None
                if first_day is not None and (
                    entry["first"] is None or first_day < entry["first"]
                ):
                    entry["first"] = first_day
    return state


def record_progress(record: dict) -> None:
    """One JSON line per block: progress file (resume) + shared log (monitor)."""

    record = {"ts": _now_iso(), **record}
    line = json.dumps(record, ensure_ascii=True)
    with PROGRESS_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


# ---------------------------------------------------------------------------
# TqApi session reuse
# ---------------------------------------------------------------------------


class ApiSession:
    """One TqApi market-data session, recreated on errors (login backoff)."""

    def __init__(self, worker: str = "w0") -> None:
        self._api = None
        self.recreations = 0
        self.worker = worker

    def api(self):
        if self._api is None:
            from tqsdk import TqApi, TqAuth

            from ak_trading.config import load_settings

            settings = load_settings()
            last_detail = ""
            for attempt in range(1, 11):
                try:
                    self._api = TqApi(auth=TqAuth(settings.tq_user, settings.tq_pass))
                    if attempt > 1:
                        _log_event({"event": "login_ok_after_retry",
                                    "worker": self.worker, "attempt": attempt})
                    return self._api
                except Exception as exc:  # noqa: BLE001
                    last_detail = _safe_detail(exc)
                    self._api = None
                    time.sleep(45)  # account shared with other backfills
            raise RuntimeError(f"tq login failed after retries: {last_detail}")
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
# Row construction with the five-exchange hard guard
# ---------------------------------------------------------------------------


def _num(value: object) -> float | None:
    """NaN/pd.NA-safe float conversion."""

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


def build_rows(df, *, ranking_type: str, win_start: date, win_end: date) -> list:
    """DataFrame -> clamped, guarded rows for ONE ranking_type only.

    Only the calling ranking_type's own three columns are extracted; the
    cross-metric columns are incomplete in that call and must never be mixed
    in (verified behavior: each call returns its own full top-20 only).

    Hard guard: GUARDED_FIVE rows must never carry trading_day >= 2024-01-01,
    and no row may exceed the global 2026-08-31 ceiling. Violations raise
    AssertionError (the block fails loudly instead of writing bad data).
    """

    from ak_trading.member_rank import MemberRankRow

    specs = [s for s in METRIC_SPECS if s[0] == ranking_type]
    assert len(specs) == 1, f"unknown ranking_type {ranking_type!r}"
    _ranking_type, metric, qty_col, chg_col, rank_col = specs[0]
    if df is None or len(df) == 0:
        return []
    rows: list[MemberRankRow] = []
    try:
        sub = df[df[rank_col].notna()]
    except KeyError:
        return []
    for record in sub.to_dict(orient="records"):
        day = parse_day(record.get("datetime"))
        if day is None or not (win_start <= day <= win_end):
            continue  # clamp to the phase/override window
        exchange = str(record.get("exchange_id") or "").strip()
        instrument = str(record.get("instrument_id") or "").strip()
        if not exchange or not instrument:
            continue
        # HARD GUARD (assertion, per task contract)
        assert not (exchange in GUARDED_FIVE and day >= FIVE_CUTOFF), (
            f"TQ five-exchange guard violated: {exchange} {instrument} {day}"
        )
        assert day <= GLOBAL_MAX_DAY, (
            f"TQ global ceiling violated: {exchange} {instrument} {day}"
        )
        rank_no = _num(record.get(rank_col))
        if rank_no is None or not (1 <= rank_no <= 20):
            continue
        rank_no = int(round(rank_no))
        qty = _num(record.get(qty_col))
        if qty is None:
            continue
        change = _num(record.get(chg_col))
        name = strip_broker_prefix(str(record.get("broker") or "").strip())
        if not name:
            continue
        product_match = _PRODUCT_RE.match(instrument)
        product_id = product_match.group(0) if product_match else instrument
        rows.append(MemberRankRow(
            exchange=exchange,
            trading_day=day,
            product_id=product_id,
            instrument_id=instrument,
            metric=metric,
            rank_no=rank_no,
            member_name=name,
            quantity=int(round(qty)),
            change_qty=None if change is None else int(round(change)),
            member_id=None,
            basis=None,
            source_url=SOURCE_URL,
        ))
    # Same-key rows (duplicate stripped names inside one day) would break the
    # upsert ("cannot affect row a second time"); keep the larger quantity.
    deduped: dict[tuple, object] = {}
    dropped = 0
    for row in rows:
        key = (row.exchange, row.trading_day, row.instrument_id, row.metric,
               row.rank_no, row.member_name)
        if key in deduped:
            dropped += 1
            if row.quantity > deduped[key].quantity:
                deduped[key] = row
        else:
            deduped[key] = row
    if dropped:
        _log_event({"event": "duplicate_rows_dropped", "dropped": dropped})
    final_rows = list(deduped.values())
    # Final belt-and-braces guard before the rows leave this function.
    for row in final_rows:
        assert not (row.exchange in GUARDED_FIVE and row.trading_day >= FIVE_CUTOFF), (
            f"pre-import guard violated: {row.exchange} {row.instrument_id} "
            f"{row.trading_day}"
        )
    return final_rows


# ---------------------------------------------------------------------------
# Worker: walk one (phase, symbol, metric) at a time
# ---------------------------------------------------------------------------


class Worker:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.worker = f"w{args.partition}"
        self.stop_before = _parse_stop_before(args.stop_before)
        self.engine = None
        self.dbconn = None
        self.session = None
        self.blocks_executed = 0
        self.consecutive_failures = 0
        self.stats = {"ok": 0, "empty": 0, "skipped_db": 0, "failed": 0, "rows": 0}
        self.circuit_broken = False

    # -- infrastructure -----------------------------------------------------

    def _engine(self):
        if self.engine is None:
            from ak_trading.persistence.config import load_database_settings
            from ak_trading.persistence.database import create_database_engine

            settings = load_database_settings(required=True)
            assert settings is not None
            self.engine = create_database_engine(settings)
        return self.engine

    def _db(self):
        if self.dbconn is None or self.dbconn.closed:
            self.dbconn = _connect_db()
        return self.dbconn

    def _deadline_hit(self) -> bool:
        return (
            self.stop_before is not None
            and datetime.now(timezone.utc) >= self.stop_before
        )

    def _limits_hit(self, reason_out: dict) -> bool:
        if self._deadline_hit():
            reason_out["reason"] = "deadline"
            return True
        if self.consecutive_failures >= CIRCUIT_BREAKER:
            self.circuit_broken = True
            reason_out["reason"] = "circuit_breaker"
            return True
        if (
            self.args.limit_blocks is not None
            and self.blocks_executed >= self.args.limit_blocks
        ):
            reason_out["reason"] = "limit_blocks"
            return True
        return False

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
        if self.dbconn is not None:
            try:
                self.dbconn.close()
            except Exception:
                pass
        if self.engine is not None:
            self.engine.dispose()

    # -- data access ---------------------------------------------------------

    def db_has_rows_between(self, exchange: str, instrument: str, metric: str,
                            lo: date, hi: date) -> bool:
        if self.args.no_db_skip:
            return False
        if hi < lo:
            return False
        query = (
            "SELECT 1 FROM member_position_ranks WHERE exchange = %s "
            "AND instrument_id = %s AND metric = %s AND trading_day >= %s "
            "AND trading_day <= %s LIMIT 1"
        )
        params = [exchange, instrument, metric, lo, hi]
        for attempt in (1, 2):
            try:
                return (
                    self._db().execute(query, params).fetchone() is not None
                )
            except Exception:  # noqa: BLE001 - reconnect once
                if self.dbconn is not None:
                    try:
                        self.dbconn.close()
                    except Exception:
                        pass
                    self.dbconn = None
                if attempt == 2:
                    raise
        return False

    def fetch_ranking(self, symbol: str, ranking_type: str, start: date):
        assert self.session is not None
        last_detail = ""
        for attempt in range(1, ATTEMPTS_PER_BLOCK + 1):
            try:
                return self.session.api().query_symbol_ranking(
                    symbol, ranking_type=ranking_type,
                    days=DAYS_PER_CALL, start_dt=start,
                )
            except Exception as exc:  # noqa: BLE001
                last_detail = _safe_detail(exc)
                self.session.reset()
                _log_event({"event": "fetch_retry", "worker": self.worker,
                            "symbol": symbol, "metric": ranking_type,
                            "attempt": attempt, "detail": last_detail})
                if attempt < ATTEMPTS_PER_BLOCK:
                    time.sleep(BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)])
        raise RuntimeError(
            f"fetch failed after {ATTEMPTS_PER_BLOCK} attempts: {last_detail}"
        )

    # -- main loop ------------------------------------------------------------

    def run(self) -> None:
        from ak_trading.persistence.member_rank import import_member_rank_rows

        self.session = ApiSession(worker=self.worker)
        state = read_progress()
        universe = load_universe()
        symbols_filter = (
            {s.strip() for s in self.args.symbols.split(",") if s.strip()}
            if self.args.symbols else None
        )
        phases = (
            [self.args.phase] if self.args.phase != "auto" else list(PHASE_ORDER)
        )
        reason: dict = {}
        for phase in phases:
            spec = PHASES[phase]
            # phase-level guard: five-exchange phases must end before 2024
            if set(spec["exchanges"]) & GUARDED_FIVE:
                assert spec["end"] < FIVE_CUTOFF, (
                    f"phase {phase} window breaches five-ex cutoff"
                )
            win_start, win_end = spec["start"], spec["end"]
            if self.args.start:
                win_start = max(win_start, date.fromisoformat(self.args.start))
            if self.args.end:
                win_end = min(win_end, date.fromisoformat(self.args.end))
            products = sorted(
                key for key in universe["products"]
                if key.split(".", 1)[0] in spec["exchanges"]
            )
            assigned = [
                key for index, key in enumerate(products)
                if index % self.args.partitions == self.args.partition
            ]
            _log_event({"event": "phase_start", "worker": self.worker,
                        "phase": phase, "products_assigned": len(assigned),
                        "of": len(products),
                        "window": [str(win_start), str(win_end)]})
            for product in assigned:
                reason.clear()
                if self._limits_hit(reason):
                    break
                contracts = universe["products"][product]
                prefix = product + "."
                # observed first-data chain for this product (all phases)
                chain_first: dict[str, date] = {}
                for key, entry in state.items():
                    _, symbol, _ = key.split("|", 2)
                    if symbol.startswith(prefix) and entry.get("first"):
                        prior = chain_first.get(symbol)
                        if prior is None or entry["first"] < prior:
                            chain_first[symbol] = entry["first"]
                expire_by_symbol = {
                    c["symbol"]: c.get("expire_datetime") for c in contracts
                }
                _log_event({"event": "product_start", "worker": self.worker,
                            "phase": phase, "product": product,
                            "contracts": len(contracts)})
                for contract in contracts:
                    reason.clear()
                    if self._limits_hit(reason):
                        break
                    symbol = contract["symbol"]
                    if symbols_filter is not None and symbol not in symbols_filter:
                        continue
                    exchange, instrument = symbol.split(".", 1)
                    expire_ts = contract.get("expire_datetime")
                    expire = (
                        datetime.fromtimestamp(expire_ts, tz=BEIJING).date()
                        if expire_ts else None
                    )
                    if expire is not None and expire < win_start - timedelta(days=30):
                        continue  # cannot have data inside this phase window
                    hard_end = win_end if expire is None else min(
                        win_end, expire + timedelta(days=EXPIRE_TAIL_BUFFER_DAYS)
                    )
                    init = win_start
                    if expire is not None:
                        init = max(
                            init, expire - timedelta(days=LISTING_LEAD_DAYS)
                        )
                    prev_first = None
                    for other_symbol, other_first in chain_first.items():
                        other_ts = expire_by_symbol.get(other_symbol)
                        if other_ts is not None and other_ts < (expire_ts or 2**31):
                            if prev_first is None or other_first > prev_first:
                                prev_first = other_first
                    if prev_first is not None:
                        init = max(
                            init, prev_first - timedelta(days=CHAIN_MARGIN_DAYS)
                        )
                    for ranking_type, metric, _q, _c, _r in METRIC_SPECS:
                        reason.clear()
                        if self._limits_hit(reason):
                            break
                        key = _progress_key(phase, symbol, metric)
                        entry = state.get(key) or {}
                        if entry.get("done"):
                            continue
                        cursor = entry.get("cursor") or init
                        if cursor < init:
                            cursor = init
                        first_day = self._walk(
                            phase=phase, symbol=symbol, exchange=exchange,
                            instrument=instrument, metric=metric,
                            ranking_type=ranking_type, cursor=cursor,
                            hard_end=hard_end, win_start=win_start,
                            win_end=win_end, key=key,
                            import_member_rank_rows=import_member_rank_rows,
                        )
                        # update local state + listing chain (no global reread)
                        merged_first = entry.get("first")
                        if first_day is not None and (
                            merged_first is None or first_day < merged_first
                        ):
                            merged_first = first_day
                        state[key] = {"cursor": hard_end, "done": True,
                                      "first": merged_first}
                        if merged_first is not None:
                            prior = chain_first.get(symbol)
                            if prior is None or merged_first < prior:
                                chain_first[symbol] = merged_first
                if reason:
                    break
                _log_event({"event": "product_done", "worker": self.worker,
                            "phase": phase, "product": product,
                            "stats": self.stats})
            if reason:
                _log_event({"event": "phase_stopped", "worker": self.worker,
                            "phase": phase, **reason, "stats": self.stats})
                break
            _log_event({"event": "phase_done", "worker": self.worker,
                        "phase": phase, "stats": self.stats})
        _log_event({"event": "worker_complete", "worker": self.worker,
                    "circuit_broken": self.circuit_broken, "stats": self.stats})

    # -- one walk -------------------------------------------------------------

    def _walk(self, *, phase, symbol, exchange, instrument, metric,
              ranking_type, cursor, hard_end, win_start, win_end, key,
              import_member_rank_rows) -> date | None:
        first_seen: date | None = None
        while cursor <= hard_end:
            reason: dict = {}
            if self._limits_hit(reason):
                _log_event({"event": "walk_stopped", "worker": self.worker,
                            "symbol": symbol, "metric": metric,
                            "cursor": str(cursor), **reason})
                return first_seen
            started = time.monotonic()
            window_hi = min(cursor + timedelta(days=FETCH_WINDOW_CAL_DAYS), hard_end)
            base_record = {
                "event": "block", "worker": self.worker, "phase": phase,
                "symbol": symbol, "exchange": exchange,
                "instrument": instrument, "metric": metric,
                "cursor_from": str(cursor),
            }
            try:
                if self.db_has_rows_between(
                    exchange, instrument, metric, cursor, window_hi
                ):
                    next_cursor = window_hi + timedelta(days=1)
                    record_progress({**base_record,
                                     "cursor_to": str(next_cursor),
                                     "status": "skipped_db", "rows": 0,
                                     "elapsed_s": round(
                                         time.monotonic() - started, 2)})
                    self.stats["skipped_db"] += 1
                    self.consecutive_failures = 0
                    cursor = next_cursor
                    continue
                df = self.fetch_ranking(symbol, ranking_type, cursor)
                self.blocks_executed += 1
                rows = build_rows(
                    df, ranking_type=ranking_type,
                    win_start=win_start, win_end=win_end,
                )
                written = (
                    import_member_rank_rows(self._engine(), rows).rows_written
                    if rows else 0
                )
            except AssertionError:
                raise  # guard violations must abort loudly, never retry
            except Exception as exc:  # noqa: BLE001
                self.stats["failed"] += 1
                self.consecutive_failures += 1
                record_progress({**base_record, "cursor_to": str(cursor),
                                 "status": "failed", "rows": -1,
                                 "detail": _safe_detail(exc),
                                 "elapsed_s": round(
                                     time.monotonic() - started, 2)})
                _log_event({"event": "block_failed", "worker": self.worker,
                            "symbol": symbol, "metric": metric,
                            "consecutive_failures": self.consecutive_failures})
                return first_seen  # leave this walk for a later run
            self.consecutive_failures = 0
            if df is None or len(df) == 0:
                next_cursor = cursor + timedelta(days=EMPTY_SLIDE_DAYS)
                record_progress({**base_record, "cursor_to": str(next_cursor),
                                 "status": "empty", "rows": 0,
                                 "elapsed_s": round(
                                     time.monotonic() - started, 2)})
                self.stats["empty"] += 1
                cursor = next_cursor
                continue
            day_values = sorted({str(v)[:8] for v in df["datetime"]})
            block_first = parse_day(day_values[0])
            last_any = parse_day(day_values[-1])
            if block_first is not None and (
                first_seen is None or block_first < first_seen
            ):
                first_seen = block_first
            if rows:
                last_data = max(row.trading_day for row in rows)
                next_cursor = last_data + timedelta(days=1)
            else:
                # every returned day fell outside the clamp window
                next_cursor = min(last_any or hard_end, hard_end) + timedelta(days=1)
            record_progress({**base_record, "cursor_to": str(next_cursor),
                             "status": "ok", "rows": written,
                             "first": str(block_first) if block_first else None,
                             "last": str(last_any) if last_any else None,
                             "elapsed_s": round(
                                 time.monotonic() - started, 2)})
            self.stats["ok"] += 1
            self.stats["rows"] += written
            cursor = next_cursor
        record_progress({"event": "block", "worker": self.worker,
                         "phase": phase, "symbol": symbol, "exchange": exchange,
                         "instrument": instrument, "metric": metric,
                         "cursor_from": str(hard_end), "cursor_to": str(hard_end),
                         "status": "done", "rows": 0, "elapsed_s": 0.0})
        return first_seen


# ---------------------------------------------------------------------------
# Coordinator: spawn/supervise workers, stall kill, deadline stop
# ---------------------------------------------------------------------------


def _parse_stop_before(value: str) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.astimezone(timezone.utc)


def _last_worker_activity(worker: str, tail_bytes: int = 1_000_000) -> float | None:
    """Epoch of the most recent shared-log line from this worker (tail scan)."""

    if not LOG_FILE.exists():
        return None
    latest = None
    try:
        with LOG_FILE.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            chunk = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in chunk.splitlines():
        if f'"worker": "{worker}"' not in line:
            continue
        try:
            record = json.loads(line)
            ts = datetime.fromisoformat(record["ts"]).timestamp()
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def _spawn_child(args: argparse.Namespace, slot: int) -> dict:
    command = [
        sys.executable, "-u", str(Path(__file__).resolve()),
        "--worker-mode", "child", "--partition", str(slot),
        "--partitions", str(args.workers), "--phase", args.phase,
        "--stop-before", args.stop_before,
    ]
    if args.symbols:
        command += ["--symbols", args.symbols]
    if args.start:
        command += ["--start", args.start]
    if args.end:
        command += ["--end", args.end]
    if args.limit_blocks is not None:
        command += ["--limit-blocks", str(args.limit_blocks)]
    if args.no_db_skip:
        command += ["--no-db-skip"]
    out_path = DATA_DIR / f"backfill_tq_rank_worker{slot}.out.log"
    err_path = DATA_DIR / f"backfill_tq_rank_worker{slot}.err.log"
    handle_out = out_path.open("a", encoding="utf-8")
    handle_err = err_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        command, cwd=str(ROOT), stdout=handle_out, stderr=handle_err,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    worker = f"w{slot}"
    _log_event({"event": "child_spawned", "worker": worker, "slot": slot,
                "pid": proc.pid})
    return {"proc": proc, "worker": worker, "handles": (handle_out, handle_err)}


def _close_handles(handles) -> None:
    for handle in handles:
        try:
            handle.close()
        except Exception:
            pass


def run_coordinator(args: argparse.Namespace) -> int:
    stop_before = _parse_stop_before(args.stop_before)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    children = {slot: _spawn_child(args, slot) for slot in range(args.workers)}
    restarts = {slot: 0 for slot in children}
    _log_event({"event": "coordinator_start", "pid": os.getpid(),
                "workers": args.workers, "phase": args.phase,
                "stop_before": args.stop_before})
    try:
        while True:
            time.sleep(COORDINATOR_POLL_S)
            if stop_before is not None and datetime.now(timezone.utc) >= stop_before:
                _log_event({"event": "coordinator_deadline_stop"})
                break
            all_done = True
            for slot, info in list(children.items()):
                proc = info["proc"]
                if proc.poll() is None:
                    all_done = False
                    activity = _last_worker_activity(info["worker"])
                    if (
                        args.watch_stalls
                        and activity is not None
                        and time.time() - activity > STALL_KILL_S
                    ):
                        _log_event({"event": "worker_stall_kill",
                                    "worker": info["worker"], "slot": slot,
                                    "silent_s": int(time.time() - activity),
                                    "restarts": restarts[slot]})
                        proc.kill()
                        try:
                            proc.wait(timeout=30)
                        except Exception:
                            pass
                        _close_handles(info["handles"])
                        children[slot] = {"proc": proc, "worker": info["worker"],
                                          "handles": info["handles"]}
                    continue
                _close_handles(info["handles"])
                if proc.returncode != 0:
                    _log_event({"event": "worker_exited_bad",
                                "worker": info["worker"], "slot": slot,
                                "returncode": proc.returncode})
                    if restarts[slot] < MAX_CHILD_RESTARTS:
                        restarts[slot] += 1
                        if proc.returncode == CIRCUIT_BREAKER_EXIT:
                            time.sleep(120)  # let the shared account breathe
                        children[slot] = _spawn_child(args, slot)
                        all_done = False
                else:
                    # clean exit = terminal (deadline, limit, or all done)
                    _log_event({"event": "worker_exited_ok",
                                "worker": info["worker"], "slot": slot})
            if all_done:
                _log_event({"event": "coordinator_all_children_done"})
                break
    finally:
        for info in children.values():
            if info["proc"].poll() is None:
                try:
                    info["proc"].kill()
                    info["proc"].wait(timeout=30)
                except Exception:
                    pass
            _close_handles(info["handles"])
    _log_event({"event": "coordinator_end", "restarts": restarts})
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill member rankings via TqApi.query_symbol_ranking (resumable)"
        )
    )
    parser.add_argument("--worker-mode", choices=("coordinator", "child"),
                        default="coordinator")
    parser.add_argument("--workers", type=int, default=2,
                        help="parallel child workers (default 2)")
    parser.add_argument("--partition", type=int, default=0)
    parser.add_argument("--partitions", type=int, default=1)
    parser.add_argument("--phase", choices=(*PHASE_ORDER, "auto"), default="auto",
                        help="dce | five_2020_2022 | five_2023 | auto (priority order)")
    parser.add_argument("--symbols",
                        help="comma-separated contract filter, e.g. DCE.m2609")
    parser.add_argument("--start", help="override window start (YYYY-MM-DD)")
    parser.add_argument("--end",
                        help="override window end (YYYY-MM-DD, clamped by guards)")
    parser.add_argument("--limit-blocks", type=int,
                        help="stop after N fetched blocks (smoke tests)")
    parser.add_argument("--no-db-skip", action="store_true",
                        help="disable the db-already-has-rows block skip "
                             "(repair runs completing partial imports)")
    parser.add_argument("--stop-before", default=STOP_BEFORE_DEFAULT,
                        help="do not start new blocks after this ISO datetime")
    parser.add_argument("--watch-stalls", dest="watch_stalls",
                        action="store_true", default=True,
                        help="kill+respawn silent workers (default on)")
    parser.add_argument("--no-watch-stalls", dest="watch_stalls",
                        action="store_false")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.worker_mode == "child":
        worker = Worker(args)
        _log_event({"event": "child_start", "worker": worker.worker,
                    "pid": os.getpid(), "partition": args.partition,
                    "of": args.partitions})
        exit_code = 0
        try:
            worker.run()
        except AssertionError as exc:
            _log_event({"event": "guard_violation", "worker": worker.worker,
                        "detail": str(exc)[:250]})
            exit_code = CIRCUIT_BREAKER_EXIT
        finally:
            worker.close()
        if worker.circuit_broken:
            exit_code = CIRCUIT_BREAKER_EXIT
        _log_event({"event": "child_end", "worker": worker.worker,
                    "stats": worker.stats,
                    "session_recreations": (
                        worker.session.recreations if worker.session else 0)})
        return exit_code
    return run_coordinator(args)


if __name__ == "__main__":
    raise SystemExit(main())
