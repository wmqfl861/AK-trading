"""Read-only, guard-railed market data queries for the MCP server.

Every function enforces three invariants:

1. Connections open with server-side ``default_transaction_read_only=on``,
   so injected write SQL fails inside PostgreSQL, not just in Python.
2. User input reaches SQL only through psycopg placeholders after
   validation, and only on index-friendly paths: single-symbol +
   duration equality, ts range, small LIMITs.
3. Query shapes known to destroy this database (trading_day x duration
   aggregates over the ~180GB market_bars table, DISTINCT symbol sweeps,
   leading-wildcard LIKE) are rejected before a connection is opened.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from ak_trading.market_reference import SHANGHAI_TZ
from ak_trading.persistence.config import DatabaseSettings

EXCHANGES = frozenset({"CFFEX", "CZCE", "DCE", "GFEX", "INE", "SHFE"})

SYMBOL_RX = re.compile(r"^[A-Z]{2,6}\.[A-Za-z0-9_+=@:.-]{1,40}$")
# Search prefixes are pure prefix matches: no SQL wildcards ever reach the
# server. Anything longer than 60 chars or outside this charset is rejected.
SEARCH_PREFIX_RX = re.compile(r"^[A-Za-z0-9_.@+-]{2,60}$")
PRODUCT_RX = re.compile(r"^[A-Za-z]{1,2}[0-9]{0,2}$|^[A-Z]{1,4}$")
INSTRUMENT_RX = re.compile(r"^[A-Za-z0-9]{1,12}$")

DAILY_SECONDS = 86_400
MINUTE_SECONDS = 60

MAX_DAILY_WINDOW_DAYS = 800
MAX_MINUTE_WINDOW_DAYS = 31
MAX_DAILY_LIMIT = 4_000
MAX_MINUTE_LIMIT = 6_000
MAX_SEARCH_LIMIT = 100
MAX_SEARCH_PREFIX_LEN = 60
MIN_SEARCH_PREFIX_LEN = 6  # must name at least EXCHANGE.product fragment
MAX_COVERAGE_SYMBOLS = 50
MAX_RANK_LIMIT = 2_000


class MarketMcpQueryError(ValueError):
    """A rejected request: bad input, oversized window, or forbidden shape."""


def check_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not SYMBOL_RX.fullmatch(symbol):
        raise MarketMcpQueryError(
            "symbol must look like EXCHANGE.contract, e.g. SHFE.rb2610 or KQ.m@SHFE.rb"
        )
    if re.search(r"^[-.]|[-.]$", symbol.split(".", 1)[1]):
        raise MarketMcpQueryError(
            "symbol contract part must not start or end with '-' or '.'"
        )
    return symbol


def check_search_prefix(prefix: str) -> str:
    if not isinstance(prefix, str):
        raise MarketMcpQueryError("pattern must be a string")
    if len(prefix) < MIN_SEARCH_PREFIX_LEN or len(prefix) > MAX_SEARCH_PREFIX_LEN:
        raise MarketMcpQueryError(
            f"pattern length must be {MIN_SEARCH_PREFIX_LEN}..{MAX_SEARCH_PREFIX_LEN} "
            "(prefix search only; use list_products for exchange-wide listings)"
        )
    if not SEARCH_PREFIX_RX.fullmatch(prefix):
        raise MarketMcpQueryError(
            "pattern may only contain letters, digits and _ . @ + - (no wildcards)"
        )
    return prefix


def check_rank_filter(value: str | None, *, name: str) -> str | None:
    rx = PRODUCT_RX if name == "product_id" else INSTRUMENT_RX
    if value is not None and (
        not isinstance(value, str) or not rx.fullmatch(value)
    ):
        raise MarketMcpQueryError(f"{name} contains unexpected characters")
    return value


def check_iso_day(value: str, *, name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None:
        raise MarketMcpQueryError(f"{name} must be an ISO date YYYY-MM-DD")
    return parsed


def check_window(start: date, end: date, *, max_days: int, label: str) -> tuple[date, date]:
    if end < start:
        raise MarketMcpQueryError(f"{label}: end precedes start")
    if (end - start).days + 1 > max_days:
        raise MarketMcpQueryError(
            f"{label}: window exceeds {max_days} days; split into smaller requests"
        )
    return start, end


def check_limit(limit: int, *, cap: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise MarketMcpQueryError("limit must be a positive integer")
    return min(limit, cap)


def check_exchange(exchange: str | None) -> str | None:
    if exchange is None or exchange == "":
        return None
    if not isinstance(exchange, str) or exchange.upper() not in EXCHANGES:
        raise MarketMcpQueryError(f"exchange must be one of {sorted(EXCHANGES)}")
    return exchange.upper()


def _day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    begin = datetime(start.year, start.month, start.day, tzinfo=SHANGHAI_TZ)
    try:
        stop = begin + timedelta(days=(end - start).days + 1)
    except OverflowError:
        raise MarketMcpQueryError("end is outside the supported date range") from None
    return begin, stop


def _jsonify(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _jsonify_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _jsonify(value) for key, value in row.items()}


# The search alphabet: the only characters a validated prefix may contain.
# Symbols themselves stay ASCII, so a prefix successor over this closed,
# collation-ordered alphabet is an exact upper bound for the family.
_SEARCH_ALPHABET = "-+.0123456789@_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _successor(prefix: str, order: Sequence[str]) -> str:
    """Smallest string over ``order`` strictly above every ``prefix`` extension.

    Standard successor in lexicographic order: bump the last character to
    its next-in-order char, dropping exhausted positions. An all-max prefix
    has no finite successor, so cap with the max character repeated beyond
    the symbol length limit (symbols are <= 47 chars).
    """
    rank = {char: index for index, char in enumerate(order)}
    chars = list(prefix)
    while chars:
        position = rank[chars[-1]]
        if position + 1 < len(order):
            chars[-1] = order[position + 1]
            return "".join(chars)
        chars.pop()
    return order[-1] * 48


class ReadOnlyMarketData:
    """Parameterised, index-path-only reads against the ak_trading database."""

    def __init__(self, settings: DatabaseSettings, *, connect=None) -> None:
        self._settings = settings
        self._connect = connect or self._default_connect
        self._search_order: tuple[str, ...] | None = None

    def _default_connect(self):
        import os

        import psycopg
        from psycopg.rows import dict_row

        # The global AK_DATABASE_STATEMENT_TIMEOUT_MS (5s) is tuned for
        # ingestion tooling; this service owns its own budget, clamped to
        # a hard 1s..60s ceiling.
        try:
            timeout_ms = int(os.getenv("AK_MCP_STATEMENT_TIMEOUT_MS", "15000"))
        except ValueError:
            timeout_ms = 15_000
        timeout_ms = max(1_000, min(timeout_ms, 60_000))
        # Server-side read-only plus hard timeouts: even a fully injected
        # query string cannot write, lock catalog objects, or run forever.
        # Only the driver scheme changes; retain all authority/path/query bytes.
        scheme, separator, rest = self._settings.url.partition("://")
        url = self._settings.url
        if separator and scheme.lower() in {"postgres", "postgresql", "postgresql+psycopg"}:
            url = "postgresql://" + rest
        return psycopg.connect(
            url,
            connect_timeout=self._settings.connect_timeout,
            options=(
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={timeout_ms} "
                "-c idle_in_transaction_session_timeout=30000 "
                "-c application_name=ak_trading_mcp"
            ),
            row_factory=dict_row,
        )

    # -- bars ---------------------------------------------------------------

    def _bars(
        self, *, symbol: str, duration_seconds: int, start: date, end: date,
        limit: int, max_window: int, cap: int, label: str,
    ) -> dict[str, Any]:
        symbol = check_symbol(symbol)
        start = check_iso_day(start, name="start")
        end = check_iso_day(end, name="end")
        check_window(start, end, max_days=max_window, label=label)
        limit = check_limit(limit, cap=cap)
        begin, stop = _day_bounds(start, end)
        sql = (
            "SELECT ts, trading_day, open, high, low, close, volume, open_oi, close_oi "
            "FROM market_bars "
            "WHERE symbol = %s AND duration_seconds = %s AND ts >= %s AND ts < %s "
            "ORDER BY ts LIMIT %s"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (symbol, duration_seconds, begin, stop, limit))
                rows = [_jsonify_row(r) for r in cur.fetchall()]
        return {
            "symbol": symbol,
            "duration_seconds": duration_seconds,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "rows": len(rows),
            "truncated": len(rows) == limit,
            "bars": rows,
        }

    def daily_bars(self, symbol: str, start: str, end: str, *, limit: int = 1500) -> dict[str, Any]:
        return self._bars(
            symbol=symbol, duration_seconds=DAILY_SECONDS, start=start, end=end,
            limit=limit, max_window=MAX_DAILY_WINDOW_DAYS, cap=MAX_DAILY_LIMIT,
            label="daily_bars",
        )

    def minute_bars(self, symbol: str, start: str, end: str, *, limit: int = 3000) -> dict[str, Any]:
        return self._bars(
            symbol=symbol, duration_seconds=MINUTE_SECONDS, start=start, end=end,
            limit=limit, max_window=MAX_MINUTE_WINDOW_DAYS, cap=MAX_MINUTE_LIMIT,
            label="minute_bars",
        )

    # -- freshness ----------------------------------------------------------

    def coverage(self, symbols: Sequence[str]) -> list[dict[str, Any]]:
        cleaned: list[str] = []
        for symbol in symbols:
            cleaned.append(check_symbol(symbol))
        if not cleaned:
            raise MarketMcpQueryError("symbols must be a non-empty list")
        if len(cleaned) > MAX_COVERAGE_SYMBOLS:
            raise MarketMcpQueryError(
                f"at most {MAX_COVERAGE_SYMBOLS} symbols per coverage call"
            )
        # Two (symbol, duration) probes per symbol: the combined FILTER
        # variant scans the symbol's full history and is ~700x slower.
        sql = (
            "SELECT max(ts) FROM market_bars "
            "WHERE symbol = %s AND duration_seconds = %s"
        )
        out: list[dict[str, Any]] = []
        with self._connect() as conn:
            with conn.cursor() as cur:
                for symbol in cleaned:
                    through: dict[str, Any] = {"symbol": symbol}
                    for column, duration in (
                        ("daily_through", DAILY_SECONDS),
                        ("minute_through", MINUTE_SECONDS),
                    ):
                        cur.execute(sql, (symbol, duration))
                        row = cur.fetchone() or {}
                        through[column] = _jsonify(row.get("max"))
                    out.append(through)
        return out

    # -- symbol search ------------------------------------------------------

    def search_symbols(self, prefix: str, *, limit: int = 50) -> dict[str, Any]:
        prefix = check_search_prefix(prefix)
        limit = check_limit(limit, cap=MAX_SEARCH_LIMIT)
        # ICU collation (en-US) cannot push LIKE prefixes into the index, and
        # naive ASCII bounds are wrong under it. Ask the server once for the
        # real ordering of the closed alphabet, then range-scan
        # [prefix, successor(prefix)) - an exact, index-only family scan.
        # Daily dimension only: option prefixes own tens of millions of
        # minute index entries and are unusably slow to deduplicate.
        # Main-continuous KQ.m@ streams have no daily bars - agents construct
        # those directly (KQ.m@EXCHANGE.product).
        with self._connect() as conn:
            if self._search_order is None:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT c FROM unnest(%s::text[]) AS c ORDER BY c",
                        (list(_SEARCH_ALPHABET),),
                    )
                    self._search_order = tuple(r["c"] for r in cur.fetchall())
            upper = _successor(prefix, self._search_order)
            sql = (
                "SELECT DISTINCT symbol FROM market_bars "
                "WHERE symbol >= %s AND symbol < %s AND duration_seconds = 86400 "
                "ORDER BY symbol LIMIT %s"
            )
            with conn.cursor() as cur:
                cur.execute(sql, (prefix, upper, limit))
                rows = [r["symbol"] for r in cur.fetchall()]
        return {"prefix": prefix, "rows": len(rows), "truncated": len(rows) == limit,
                "symbols": rows}

    # -- member ranks -------------------------------------------------------

    def member_ranks(
        self, trading_day: str, *, exchange: str | None = None,
        product_id: str | None = None, instrument_id: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        day = check_iso_day(trading_day, name="trading_day")
        exchange = check_exchange(exchange)
        product_id = check_rank_filter(product_id, name="product_id")
        instrument_id = check_rank_filter(instrument_id, name="instrument_id")
        limit = check_limit(limit, cap=MAX_RANK_LIMIT)
        clauses, params = ["trading_day = %s"], [day]
        for column, value in (
            ("exchange", exchange), ("product_id", product_id),
            ("instrument_id", instrument_id),
        ):
            if value:
                clauses.append(f"{column} = %s")
                params.append(value)
        sql = (
            "SELECT exchange, trading_day, product_id, instrument_id, metric, "
            "rank_no, member_id, member_name, quantity, change_qty "
            "FROM member_position_ranks WHERE "
            + " AND ".join(clauses)
            + " ORDER BY product_id, instrument_id, metric, rank_no LIMIT %s"
        )
        params.append(limit)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = [_jsonify_row(r) for r in cur.fetchall()]
        return {"trading_day": day.isoformat(), "rows": len(rows),
                "truncated": len(rows) == limit, "ranks": rows}
