"""OPT-01 behavior probes: SYNTHETIC inputs, no DB/SDK/network or real audit."""
from __future__ import annotations

import json
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from ak_trading.market_reference import SHANGHAI_TZ
from ak_trading.mcp_server import audit, queries as q, reference, server
from ak_trading.persistence.config import DatabaseSettings

SYNTHETIC_SECRET = "SYNTHETIC_ONLY_OPT01_CREDENTIAL_271828"
TOP_LEVEL = {"ts", "tool", "args", "rows", "error", "client"}


class FakeCursor:
    def __init__(self, engine):
        self.engine = engine
        self.rows = []

    def execute(self, sql, params=None):
        self.engine.statements.append((sql, tuple(params or ())))
        self.rows = (
            [{"c": c} for c in sorted(params[0])]
            if sql.startswith("SELECT c FROM unnest") else []
        )

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeEngine:
    """Only records the query boundary; never opens any database."""
    def __init__(self):
        self.connections = 0
        self.statements = []

    def connect(self):
        self.connections += 1
        return self

    def cursor(self):
        return FakeCursor(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Opaque:
    conversions = 0

    def __str__(self):
        type(self).conversions += 1
        return SYNTHETIC_SECRET

    __repr__ = __str__


@pytest.fixture
def market():
    engine = FakeEngine()
    settings = DatabaseSettings(url="postgresql://synthetic:unused@127.0.0.1/synthetic_test")
    return q.ReadOnlyMarketData(settings, connect=engine.connect), engine


@pytest.fixture
def audit_sink(tmp_path, monkeypatch):
    directory = tmp_path / "SYNTHETIC-audit"
    monkeypatch.setattr(audit, "AUDIT_DIR", directory)

    def read():
        paths = list(directory.glob("audit-*.jsonl"))
        assert len(paths) == 1
        raw = paths[0].read_bytes()
        lines = raw.splitlines()
        assert len(lines) == 1 and raw.endswith(b"\n")
        assert len(raw) <= 16 * 1024
        record = json.loads(raw)
        assert set(record) == TOP_LEVEL
        return record, raw.decode("utf-8")

    return read


def query_field(market, field, value):
    if field == "symbol":
        return market.daily_bars(value, "2026-09-01", "2026-09-01")
    if field == "prefix":
        return market.search_symbols(value)
    return market.member_ranks("2026-09-01", **{field: value})


def test_candidate_imports_are_from_reserved_main_root():
    root = Path("D:/Projects/AK-trading").resolve()
    for module in (q, audit, server, reference):
        assert Path(module.__file__).resolve().is_relative_to(root / "src")
    print("OPT-01 candidate import root:", root)


@pytest.mark.parametrize("field,base", [
    ("symbol", "SHFE.rb2610"), ("prefix", "SHFE.rb"),
    ("product_id", "rb"), ("instrument_id", "rb2610"),
])
@pytest.mark.parametrize("suffix", ["\n", "\r", "\r\n", "\x00", "\t", "\x1f", "\x7f", "\u2028"])
def test_control_suffix_rejected_before_connection(market, field, base, suffix):
    api, engine = market
    with pytest.raises(q.MarketMcpQueryError):
        query_field(api, field, base + suffix)
    assert engine.connections == 0 and engine.statements == []


@pytest.mark.parametrize("field", ["symbol", "prefix", "product_id", "instrument_id"])
@pytest.mark.parametrize("value", [1, True, [], {}, b"rb2610"])
def test_wrong_scalar_type_is_a_query_rejection(market, field, value):
    api, engine = market
    with pytest.raises(q.MarketMcpQueryError):
        query_field(api, field, value)
    assert engine.connections == 0


@pytest.mark.parametrize("symbol", [
    "KQ.m@SHFE.rb", "CFFEX.HO2609-C-2500", "DCE.m2609C3000", "SHFE.rb2610", "CZCE.MA609",
])
def test_legal_symbol_language_and_case_preserved(market, symbol):
    api, engine = market
    assert api.daily_bars(symbol, "2026-09-01", "2026-09-01")["symbol"] == symbol
    assert engine.statements[-1][1][0] == symbol
    assert "symbol = %s" in engine.statements[-1][0]


@pytest.mark.parametrize("product,instrument", [("rb", "rb2610"), ("RB", "RB2610"), ("IF", "IF2609"), ("AB12", "AB12"), (None, None)])
def test_legal_rank_filters_keep_existing_language(market, product, instrument):
    api, engine = market
    api.member_ranks("2026-09-01", product_id=product, instrument_id=instrument)
    assert engine.connections == 1
    if product is not None:
        assert engine.statements[-1][1][1:3] == (product, instrument)


@pytest.mark.parametrize("method,days,allowed", [
    ("minute_bars", 31, True), ("minute_bars", 32, False),
    ("daily_bars", 800, True), ("daily_bars", 801, False),
    ("minute_bars", 1, True), ("daily_bars", 1, True),
])
def test_bars_window_counts_both_endpoints(market, method, days, allowed):
    api, engine = market
    start = date(2024, 1, 1)
    end = start + timedelta(days=days - 1)
    call = lambda: getattr(api, method)("SHFE.rb2610", start.isoformat(), end.isoformat())
    if not allowed:
        with pytest.raises(q.MarketMcpQueryError):
            call()
        assert engine.connections == 0
    else:
        call()
        _, params = engine.statements[-1]
        assert params[2] == datetime(2024, 1, 1, tzinfo=SHANGHAI_TZ)
        assert params[3] == datetime(end.year, end.month, end.day, tzinfo=SHANGHAI_TZ) + timedelta(days=1)
        assert (params[3] - params[2]).days == days


@pytest.mark.parametrize("method", ["daily_bars", "minute_bars"])
@pytest.mark.parametrize("start,end", [("2026-09-02", "2026-09-01"), ("9999-12-31", "9999-12-31")])
def test_reversed_or_overflowing_bars_reject_safely(market, method, start, end):
    api, engine = market
    with pytest.raises(q.MarketMcpQueryError) as caught:
        getattr(api, method)("SHFE.rb2610", start, end)
    rendered = "".join(traceback.format_exception(caught.value))
    assert "OverflowError:" not in rendered
    assert engine.connections == 0


@pytest.mark.parametrize("days,allowed", [(1, True), (800, True), (801, False), (0, False)])
def test_reference_calendar_uses_inclusive_shared_window(monkeypatch, days, allowed):
    loads = []
    calendar = SimpleNamespace(valid_from=date(2020, 1, 1), valid_through=date(2030, 12, 31),
                               is_trading_day=lambda day: day.weekday() < 5)

    def load(_):
        loads.append(True)
        return SimpleNamespace(calendar_for=lambda _: calendar)

    monkeypatch.setattr(reference, "_load_catalog", load)
    end = (date(2024, 1, 1) + timedelta(days=days - 1)).isoformat()
    if allowed:
        result = reference.trading_calendar("2024-01-01", end)
        assert len(result["days"]) == days
        assert result["days"][0]["date"] == "2024-01-01"
        assert result["days"][-1]["date"] == end
        assert loads == [True]
    else:
        with pytest.raises(q.MarketMcpQueryError):
            reference.trading_calendar("2024-01-01", end)
        assert loads == []


@pytest.mark.parametrize("scheme", ["postgresql", "postgres", "postgresql+psycopg", "POSTGRESQL+PSYCOPG"])
def test_synthetic_dsn_scheme_only_normalized_locally(monkeypatch, scheme):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from psycopg.rows import dict_row

    # Reserved documentation IPv6; this is parsed only, never connected.
    suffix = "synthetic%40user:SYNTHETIC%25%2f%3Apass@[2001:db8::17]:6543/synthetic%2Ddb?sslmode=require&application_name=SYNTHETIC%20probe"
    canonical = "postgresql://" + suffix
    captured = []

    def parse_only(url, **kwargs):
        captured.append((url, kwargs))
        make_conninfo(url, **{k: v for k, v in kwargs.items() if k != "row_factory"})
        return FakeEngine()

    monkeypatch.setattr(psycopg, "connect", parse_only)
    monkeypatch.delenv("AK_MCP_STATEMENT_TIMEOUT_MS", raising=False)
    api = q.ReadOnlyMarketData(DatabaseSettings(url=scheme + "://" + suffix, connect_timeout=3))
    api.daily_bars("SHFE.rb2610", "2026-09-01", "2026-09-01")
    assert len(captured) == 1
    url, options = captured[0]
    assert url == canonical
    assert options["row_factory"] is dict_row
    expected_options = {
        "connect_timeout": 3,
        "options": "-c default_transaction_read_only=on -c statement_timeout=15000 "
                   "-c idle_in_transaction_session_timeout=30000 -c application_name=ak_trading_mcp",
    }
    assert {k: v for k, v in options.items() if k != "row_factory"} == expected_options
    assert conninfo_to_dict(make_conninfo(url, **expected_options)) == conninfo_to_dict(make_conninfo(canonical, **expected_options))


def test_rejected_audit_has_metadata_not_argument_values(market, audit_sink):
    api, engine = market
    value = "Authorization: Bearer " + SYNTHETIC_SECRET
    custom_name = "SYNTHETIC_ONLY_PRIVATE_PARAMETER_NAME"
    args = {"pattern": value, "limit": 10, custom_name: {"password": SYNTHETIC_SECRET}}
    result = server._tool_call("search_symbols", args, lambda: api.search_symbols(value))
    record, text = audit_sink()
    assert engine.connections == 0
    assert result == {"error": "rejected:INVALID_ARGUMENT"}
    assert record["error"] == "rejected:INVALID_ARGUMENT"
    assert record["args"]["reason"] == "INVALID_ARGUMENT"
    assert record["args"]["parameters"]["pattern"] == {"type": "str", "length": len(value)}
    assert record["args"]["parameters"]["limit"] == {"type": "int"}
    assert record["args"]["unknown_parameter_count"] == 1
    assert SYNTHETIC_SECRET not in text and custom_name not in text


def test_internal_audit_omits_exception_class_text_and_all_values(audit_sink):
    error_type = type(SYNTHETIC_SECRET, (RuntimeError,), {})

    def fail():
        raise error_type("postgresql://synthetic:" + SYNTHETIC_SECRET + "@127.0.0.1/test")

    result = server._tool_call("search_symbols", {"pattern": "SHFE.rb", "token": SYNTHETIC_SECRET}, fail)
    record, text = audit_sink()
    assert result == {"error": "internal:ERROR"}
    assert record["error"] == "internal:ERROR"
    assert record["args"]["reason"] == "ERROR"
    assert record["args"]["parameters"]["pattern"] == {"type": "str", "length": 7}
    assert record["args"]["unknown_parameter_count"] == 1
    assert "SHFE.rb" not in text and SYNTHETIC_SECRET not in text


@pytest.mark.parametrize("error_type", [q.MarketMcpQueryError, RuntimeError])
def test_audit_write_failure_cannot_reexpose_original_traceback(monkeypatch, error_type):
    def fail_query():
        raise error_type(SYNTHETIC_SECRET)

    def fail_audit(*_, **__):
        raise OSError(SYNTHETIC_SECRET)

    monkeypatch.setattr(server, "write_audit", fail_audit)
    with pytest.raises(Exception) as caught:
        server._tool_call("search_symbols", {"pattern": "SHFE.rb"}, fail_query)
    rendered = "".join(traceback.format_exception(caught.value))
    assert SYNTHETIC_SECRET not in str(caught.value)
    assert SYNTHETIC_SECRET not in rendered


@pytest.mark.parametrize("tool,args,expected", [
    ("search_symbols", {"pattern": "KQ.m@SHFE.rb", "limit": 500}, {"pattern": "KQ.m@SHFE.rb", "limit": 100}),
    ("get_daily_bars", {"symbol": "SHFE.rb2610", "start": "2026-09-01", "end": "2026-09-01", "limit": 99999}, {"symbol": "SHFE.rb2610", "start": "2026-09-01", "end": "2026-09-01", "limit": 4000}),
    ("get_minute_bars", {"symbol": "CFFEX.HO2609-C-2500", "start": "2026-09-01", "end": "2026-09-01", "limit": 99999}, {"symbol": "CFFEX.HO2609-C-2500", "start": "2026-09-01", "end": "2026-09-01", "limit": 6000}),
    ("get_coverage", {"symbols": ["SHFE.rb2610", "KQ.m@SHFE.rb"]}, {"symbols": ["SHFE.rb2610", "KQ.m@SHFE.rb"]}),
    ("get_member_ranks", {"trading_day": "2026-09-01", "exchange": "shfe", "product_id": "rb", "instrument_id": "rb2610", "limit": 99999}, {"trading_day": "2026-09-01", "exchange": "SHFE", "product_id": "rb", "instrument_id": "rb2610", "limit": 2000}),
    ("get_settlement_reference", {"day": "2026-09-01", "symbol": "SHFE.rb2610"}, {"day": "2026-09-01", "symbol": "SHFE.rb2610"}),
    ("get_trading_calendar", {"start": "2026-09-01", "end": "2026-09-01"}, {"start": "2026-09-01", "end": "2026-09-01"}),
    ("list_products", {}, {}),
])
def test_success_audit_uses_tool_whitelist_and_query_validation(audit_sink, tool, args, expected):
    result = server._tool_call(tool, args, lambda: {"rows": 7})
    record, _ = audit_sink()
    assert result == {"rows": 7}
    assert record["tool"] == tool and record["args"] == expected
    assert record["rows"] == 7 and record["error"] is None


@pytest.mark.parametrize("key", ["password", "pass", "token", "secret", "authorization", "api_key", "PASSWORD", "Authorization"])
def test_success_audit_redacts_sensitive_keys(audit_sink, key):
    args = {"pattern": "SHFE.rb", key: {"nested": SYNTHETIC_SECRET}}
    audit.write_audit("search_symbols", args)
    record, text = audit_sink()
    assert record["args"][key.lower()] == "REDACTED"
    assert record["args"]["pattern"] == "SHFE.rb"
    assert SYNTHETIC_SECRET not in text


def test_unknown_tool_and_custom_parameter_name_are_not_echoed(audit_sink):
    audit.write_audit(SYNTHETIC_SECRET, {SYNTHETIC_SECRET: {"password": SYNTHETIC_SECRET}})
    record, text = audit_sink()
    assert record["tool"] == "REDACTED" and record["args"] == "REDACTED"
    assert SYNTHETIC_SECRET not in text


def test_unknown_success_parameters_are_counted_without_names(audit_sink):
    audit.write_audit("search_symbols", {"pattern": "SHFE.rb", SYNTHETIC_SECRET: "anything"})
    record, text = audit_sink()
    assert record["args"]["pattern"] == "SHFE.rb"
    assert record["args"]["unknown_parameter_count"] == 1
    assert SYNTHETIC_SECRET not in text


def registered_tools(market, monkeypatch):
    """Capture the actual decorated wrappers; no server, transport or SDK."""
    tools = {}

    class FakeMCP:
        def __init__(self, **_):
            pass

        def tool(self, fn):
            tools[fn.__name__] = fn
            return fn

        def http_app(self, **_):
            return SimpleNamespace(add_middleware=lambda *args, **kwargs: None, routes=[])

    module = ModuleType("fastmcp")
    module.FastMCP = FakeMCP
    monkeypatch.setitem(sys.modules, "fastmcp", module)
    server.build_mcp_app(market, token="SYNTHETIC_UNUSED_TOKEN")
    return tools


@pytest.mark.parametrize("field", ["product_id", "instrument_id"])
@pytest.mark.parametrize("value", [False, 0, [], {}])
def test_actual_rank_tool_rejects_falsy_non_string_filters(market, monkeypatch, audit_sink, field, value):
    api, engine = market
    tool = registered_tools(api, monkeypatch)["get_member_ranks"]
    result = tool("2026-09-01", **{field: value})
    assert result == {"error": "rejected:INVALID_ARGUMENT"}
    assert engine.connections == 0
    record, _ = audit_sink()
    assert record["error"] == "rejected:INVALID_ARGUMENT"


def test_actual_rank_tool_preserves_empty_default_filters(market, monkeypatch, audit_sink):
    api, engine = market
    result = registered_tools(api, monkeypatch)["get_member_ranks"]("2026-09-01")
    assert result["rows"] == 0 and engine.connections == 1
    record, _ = audit_sink()
    assert record["args"]["product_id"] is None
    assert record["args"]["instrument_id"] is None


@pytest.mark.parametrize("tool,args", [
    ("search_symbols", {"pattern": "SHFE.rb\n" + SYNTHETIC_SECRET}),
    ("search_symbols", {"pattern": "SHFE." + "r" * 56}),
    ("get_daily_bars", {"symbol": "SHFE." + "r" * 41}),
    ("get_member_ranks", {"product_id": "r" * 5}),
    ("get_member_ranks", {"instrument_id": "r" * 13}),
    ("search_symbols", {"pattern": SYNTHETIC_SECRET * 10000}),
    ("search_symbols", {"pattern": {"secret": SYNTHETIC_SECRET}}),
    ("get_coverage", {"symbols": ["SHFE.rb2610"] * 51}),
    ("get_coverage", {"symbols": [{"token": SYNTHETIC_SECRET}]}),
    ("get_coverage", {"symbols": "SHFE.rb2610"}),
    ("get_minute_bars", {"symbol": "SHFE.rb2610", "start": "2026-08-01", "end": "2026-09-01"}),
    ("get_trading_calendar", {"start": "2024-01-01", "end": "2026-03-11"}),
    ("get_member_ranks", {"trading_day": "2026-09-01", "product_id": False}),
])
def test_success_path_never_serializes_unvalidated_arguments(audit_sink, tool, args):
    audit.write_audit(tool, args)
    record, text = audit_sink()
    assert record["args"]["reason"] == "INVALID_ARGUMENT"
    assert SYNTHETIC_SECRET not in text
    assert "SHFE.rb2610" not in text


@pytest.mark.parametrize("error", [None, "rejected:INVALID_ARGUMENT", "internal:ERROR"])
def test_nested_and_opaque_objects_are_never_stringified(audit_sink, error):
    Opaque.conversions = 0
    value = Opaque()
    audit.write_audit("search_symbols", {"pattern": value, "token": [value]}, error=error, rows=value, client=value)
    record, text = audit_sink()
    assert Opaque.conversions == 0
    assert record["client"] is None and record["rows"] is None
    assert SYNTHETIC_SECRET not in text


@pytest.mark.parametrize("client,expected", [
    ("127.0.0.1", "127.0.0.1"), ("2001:0db8::1", "2001:db8::1"), ("::1", "::1"),
    ("Authorization: Bearer " + SYNTHETIC_SECRET, None), ("127.0.0.1\n", None),
    ("fe80::1%" + SYNTHETIC_SECRET, None), ("localhost", None), ("127.0.0.1/32", None),
    (2130706433, None), (b"127.0.0.1", None), (None, None),
])
def test_client_is_only_a_verified_numeric_address(audit_sink, client, expected):
    audit.write_audit("list_products", {}, client=client)
    record, text = audit_sink()
    assert record["client"] == expected
    assert SYNTHETIC_SECRET not in text


def test_fifty_max_length_symbols_remain_diagnostic_and_bounded(audit_sink, market):
    symbols = ["SHFE." + "A" * 38 + f"{i:02d}" for i in range(50)]
    api, engine = market
    assert len(api.coverage(symbols)) == 50
    assert len(engine.statements) == 100
    audit.write_audit("get_coverage", {"symbols": symbols}, rows=50)
    record, _ = audit_sink()
    assert record["args"] == {"symbols": symbols}


def test_large_unicode_nested_input_stays_one_bounded_json_line(audit_sink):
    value = {"secret": (SYNTHETIC_SECRET + "\n\r\u754c") * 10000}
    value["cycle"] = value
    audit.write_audit("search_symbols", {"pattern": value, "password": value}, client=value)
    record, text = audit_sink()
    assert record["args"]["reason"] == "INVALID_ARGUMENT"
    assert SYNTHETIC_SECRET not in text


def test_oversize_encoded_record_uses_fixed_truncated_metadata(audit_sink):
    # A huge integer is JSON-compatible but exceeds the entire record budget.
    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(0)
        audit.write_audit("list_products", {}, rows=10 ** 17000)
    finally:
        sys.set_int_max_str_digits(previous)
    record, _ = audit_sink()
    assert record["args"] == {"reason": "TRUNCATED"}
    assert record["rows"] is None
