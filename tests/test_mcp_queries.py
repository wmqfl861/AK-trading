"""Guard-rail tests for the MCP query layer (fake DB boundary)."""
from __future__ import annotations

import sys
import types
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ak_trading.market_reference import SHANGHAI_TZ
from ak_trading.mcp_server import queries as q
from ak_trading.persistence.config import DatabaseSettings


def make_settings() -> DatabaseSettings:
    return DatabaseSettings(
        url="postgresql://user:secret@127.0.0.1:5432/ak_trading",
        connect_timeout=3, statement_timeout_ms=60000,
    )


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql: str | None = None
        self.params: tuple | None = None
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.sql, self.params = sql, tuple(params or ())
        self.calls.append((sql, self.params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.cursor_obj = FakeCursor(rows)

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_market(rows, settings=None):
    conn = FakeConn(rows)
    return q.ReadOnlyMarketData(settings or make_settings(), connect=lambda: conn), conn


def test_daily_bars_parameterised_and_shaped():
    row = {"ts": datetime(2026, 9, 11, 15, tzinfo=SHANGHAI_TZ), "trading_day": date(2026, 9, 11),
           "open": Decimal("3137"), "high": Decimal("3150"), "low": Decimal("3120"),
           "close": Decimal("3140"), "volume": 12345, "open_oi": 100, "close_oi": 110}
    market, conn = make_market([row])
    result = market.daily_bars("SHFE.rb2610", "2026-09-01", "2026-09-11")
    sql, params = conn.cursor_obj.calls[0]
    assert "symbol = %s" in sql and "duration_seconds = %s" in sql
    assert params[0] == "SHFE.rb2610" and params[1] == 86_400
    assert result["rows"] == 1 and result["bars"][0]["open"] == 3137.0
    assert result["bars"][0]["ts"].startswith("2026-09-11")


def test_minute_window_cap():
    market, conn = make_market([])
    with pytest.raises(q.MarketMcpQueryError, match="31 days"):
        market.minute_bars("SHFE.rb2610", "2026-08-01", "2026-09-11")
    assert not conn.cursor_obj.calls


def test_daily_window_cap():
    market, _ = make_market([])
    with pytest.raises(q.MarketMcpQueryError, match="800 days"):
        market.daily_bars("SHFE.rb2610", "2022-01-01", "2026-09-11")


@pytest.mark.parametrize("limit", [0, -1])
def test_bad_limits_rejected(limit):
    market, _ = make_market([])
    with pytest.raises(q.MarketMcpQueryError):
        market.daily_bars("SHFE.rb2610", "2026-09-01", "2026-09-11", limit=limit)


def test_limit_clamped_to_cap():
    market, conn = make_market([])
    market.daily_bars("SHFE.rb2610", "2026-09-01", "2026-09-11", limit=999_999)
    assert conn.cursor_obj.params[-1] == q.MAX_DAILY_LIMIT


@pytest.mark.parametrize("symbol", [
    "SHFE.rb2610; DROP TABLE market_bars",
    "SHFE.rb2610' OR '1'='1",
    "rb2610", "shfe.rb2610", "", "SHFE.rb2610--",
])
def test_bad_symbols_rejected(symbol):
    market, conn = make_market([])
    with pytest.raises(q.MarketMcpQueryError):
        market.daily_bars(symbol, "2026-09-01", "2026-09-11")
    assert not conn.cursor_obj.calls


@pytest.mark.parametrize("start,end", [("2026/09/01", "2026-09-11"),
                                       ("2026-09-01", "11-09-2026"),
                                       ("2026-13-01", "2026-09-11")])
def test_bad_dates_rejected(start, end):
    market, _ = make_market([])
    with pytest.raises(q.MarketMcpQueryError):
        market.daily_bars("SHFE.rb2610", start, end)


def test_end_before_start_rejected():
    market, _ = make_market([])
    with pytest.raises(q.MarketMcpQueryError):
        market.daily_bars("SHFE.rb2610", "2026-09-11", "2026-09-01")


def test_coverage_caps_and_shape():
    rows = [{"max": datetime(2026, 9, 11, tzinfo=SHANGHAI_TZ)}]
    market, conn = make_market(rows)
    result = market.coverage(["SHFE.rb2610"])
    assert result[0]["symbol"] == "SHFE.rb2610"
    assert result[0]["daily_through"].startswith("2026-09-11")
    assert result[0]["minute_through"].startswith("2026-09-11")
    sqls = [sql for sql, _ in conn.cursor_obj.calls]
    assert all("duration_seconds = %s" in sql for sql in sqls)
    assert all(params == ("SHFE.rb2610", 86_400) or params == ("SHFE.rb2610", 60)
               for _, params in conn.cursor_obj.calls)


def test_coverage_too_many_symbols():
    market, _ = make_market([])
    symbols = [f"SHFE.rb26{i:02d}" for i in range(51)]
    with pytest.raises(q.MarketMcpQueryError, match="50"):
        market.coverage(symbols)


@pytest.mark.parametrize("prefix", [
    "%rb", "SHFE", "SHFE.rb%;", "SHFE.rb2610'--", "SHFE.rb2610 CANCEL",
])
def test_bad_search_prefixes_rejected(prefix):
    market, conn = make_market([])
    with pytest.raises(q.MarketMcpQueryError):
        market.search_symbols(prefix)
    assert not conn.cursor_obj.calls


def test_search_symbols_range_scan_with_collation_successor():
    market, conn = make_market([{"symbol": "SHFE.rb2610"}])
    market._search_order = tuple(sorted(set("-+.0123456789@_"
                                           "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                                           "abcdefghijklmnopqrstuvwxyz")))
    market.search_symbols("SHFE.rb26")
    sql, params = conn.cursor_obj.calls[-1]
    assert "symbol >= %s" in sql and "symbol < %s" in sql
    assert "duration_seconds = 86400" in sql
    assert params[0] == "SHFE.rb26"
    assert params[1] == "SHFE.rb27"  # '6' bumps to '7' in ASCII order
    assert params[2] == 50


def test_search_successor_exhausted_column_drops_position():
    from ak_trading.mcp_server.queries import _successor

    order = tuple("abcz")
    assert _successor("a", order) == "b"
    assert _successor("az", order) == "b"
    assert _successor("zz", order) == "z" * 48  # all-max prefix hits the cap


def test_member_ranks_parameterised():
    market, conn = make_market([])
    market.member_ranks("2026-09-11", exchange="SHFE", product_id="rb", limit=10)
    sql, params = conn.cursor_obj.calls[0]
    assert "trading_day = %s" in sql and "exchange = %s" in sql
    assert "product_id = %s" in sql
    assert params == (date(2026, 9, 11), "SHFE", "rb", 10)


def test_member_ranks_bad_inputs():
    market, _ = make_market([])
    with pytest.raises(q.MarketMcpQueryError):
        market.member_ranks("2026-09-11", exchange="NYSE")
    with pytest.raises(q.MarketMcpQueryError):
        market.member_ranks("2026-09-11", product_id="rb OR 1=1")
    with pytest.raises(q.MarketMcpQueryError):
        market.member_ranks("not-a-date")


def test_connection_options_force_server_side_readonly(monkeypatch):
    captured = {}

    def fake_connect(url, **kwargs):
        captured.update(kwargs, url=url)
        return FakeConn([])

    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.connect = fake_connect
    fake_rows = types.ModuleType("psycopg.rows")
    fake_rows.dict_row = None
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_rows)
    monkeypatch.delenv("AK_MCP_STATEMENT_TIMEOUT_MS", raising=False)

    market = q.ReadOnlyMarketData(make_settings())
    market.daily_bars("SHFE.rb2610", "2026-09-01", "2026-09-11")
    options = captured["options"]
    assert "default_transaction_read_only=on" in options
    assert "statement_timeout=15000" in options
    assert "idle_in_transaction_session_timeout=30000" in options
    assert "secret" not in options  # credentials stay in the URL only


def test_statement_timeout_env_clamped(monkeypatch):
    captured = {}

    def fake_connect(url, **kwargs):
        captured.update(kwargs)
        return FakeConn([])

    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.connect = fake_connect
    fake_rows = types.ModuleType("psycopg.rows")
    fake_rows.dict_row = None
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_rows)

    for raw, expected in (("999999", 60000), ("10", 1000), ("bogus", 15000)):
        captured.clear()
        monkeypatch.setenv("AK_MCP_STATEMENT_TIMEOUT_MS", raw)
        market = q.ReadOnlyMarketData(make_settings())
        market.daily_bars("SHFE.rb2610", "2026-09-01", "2026-09-11")
        assert f"statement_timeout={expected}" in captured["options"]
