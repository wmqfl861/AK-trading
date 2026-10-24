"""Append-only, credential-safe JSONL audit trail for MCP tool calls.

Only fixed tool/parameter names and validated primitive values reach JSON.
Rejected or failed calls retain type/length metadata, never argument values.
"""
from __future__ import annotations

import ipaddress
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ak_trading.mcp_server import queries as q

ROOT = Path(__file__).resolve().parents[3]
AUDIT_DIR = ROOT / "data" / "mcp_audit"
_LOCK = threading.Lock()
MAX_RECORD_BYTES = 16 * 1024

_TOOL_FIELDS = {
    "search_symbols": ("pattern", "limit"),
    "get_daily_bars": ("symbol", "start", "end", "limit"),
    "get_minute_bars": ("symbol", "start", "end", "limit"),
    "get_coverage": ("symbols",),
    "get_member_ranks": ("trading_day", "exchange", "product_id", "instrument_id", "limit"),
    "get_settlement_reference": ("day", "symbol"),
    "get_trading_calendar": ("start", "end"),
    "list_products": (),
}
_SENSITIVE_KEYS = frozenset({
    "password", "pass", "passwd", "pwd", "token", "secret", "authorization",
    "api_key", "apikey", "api-key", "access_token", "refresh_token",
    "client_secret", "x-api-key", "cookie", "set-cookie",
})
_TYPES = {str: "str", bool: "bool", int: "int", float: "float", list: "list",
          tuple: "tuple", dict: "dict", bytes: "bytes", type(None): "null"}


def _metadata(tool: str | None, args: Any, reason: str) -> dict[str, Any]:
    fields = _TOOL_FIELDS.get(tool, ())
    parameters = {}
    unknown_count = 0
    if type(args) is dict:
        for key, value in args.items():
            if type(key) is not str or key not in fields:
                unknown_count += 1
                continue
            kind = type(value)
            item = {"type": _TYPES.get(kind, "object")}
            if kind in (str, bytes, list, tuple, dict):
                item["length"] = len(value)
            parameters[key] = item
    return {"reason": reason, "parameters": parameters,
            "unknown_parameter_count": unknown_count}


def _validated_args(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Apply the query validators without opening connections or loading files."""
    fields = _TOOL_FIELDS[tool]
    safe: dict[str, Any] = {}
    for key in fields:
        if key not in args:
            continue
        value = args[key]
        if key == "symbols":
            if type(value) not in (list, tuple) or not 1 <= len(value) <= q.MAX_COVERAGE_SYMBOLS:
                raise q.MarketMcpQueryError("invalid symbols")
            if any(type(symbol) is not str for symbol in value):
                raise q.MarketMcpQueryError("invalid symbols")
            safe[key] = [q.check_symbol(symbol) for symbol in value]
        elif key == "limit":
            if type(value) is not int:
                raise q.MarketMcpQueryError("invalid limit")
            cap = {"search_symbols": q.MAX_SEARCH_LIMIT,
                   "get_daily_bars": q.MAX_DAILY_LIMIT,
                   "get_minute_bars": q.MAX_MINUTE_LIMIT,
                   "get_member_ranks": q.MAX_RANK_LIMIT}[tool]
            safe[key] = q.check_limit(value, cap=cap)
        else:
            if type(value) is not str and value is not None:
                raise q.MarketMcpQueryError("invalid argument type")
            if key == "symbol":
                safe[key] = q.check_symbol(value)
            elif key == "pattern":
                safe[key] = q.check_search_prefix(value)
            elif key == "exchange":
                safe[key] = q.check_exchange(value)
            elif key in ("product_id", "instrument_id"):
                # The tool surface's empty optional strings mean no filter.
                safe[key] = q.check_rank_filter(None if value == "" else value, name=key)
            elif key in ("day", "trading_day", "start", "end"):
                if type(value) is not str or len(value) > 10:
                    raise q.MarketMcpQueryError("invalid date")
                safe[key] = q.check_iso_day(value, name=key).isoformat()
    if "start" in safe and "end" in safe:
        start, end = q.check_iso_day(safe["start"]), q.check_iso_day(safe["end"])
        cap = q.MAX_MINUTE_WINDOW_DAYS if tool == "get_minute_bars" else q.MAX_DAILY_WINDOW_DAYS
        q.check_window(start, end, max_days=cap, label=tool)
        if tool in ("get_minute_bars", "get_daily_bars"):
            q._day_bounds(start, end)
    unknown_count = 0
    for key in args:
        if type(key) is str and key in fields:
            continue
        if type(key) is str and len(key) <= 32 and key.lower() in _SENSITIVE_KEYS:
            safe[key.lower()] = "REDACTED"
        else:
            unknown_count += 1
    if unknown_count:
        safe["unknown_parameter_count"] = unknown_count
    return safe


def _safe_client(value: Any) -> str | None:
    # IP literals only: no hostname resolution, scope-id text, ports or headers.
    if type(value) is not str or len(value) > 45:
        return None
    if any(char not in "0123456789abcdefABCDEF:." for char in value):
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _safe_error(error: Any) -> str | None:
    if error is None:
        return None
    if type(error) is str and error.startswith("rejected:"):
        return "rejected:INVALID_ARGUMENT"
    return "internal:ERROR"


def write_audit(
    tool: str,
    args: dict[str, Any],
    *,
    rows: int | None = None,
    error: str | None = None,
    client: str | None = None,
    audit_dir: Path | None = None,
) -> None:
    tool = tool if type(tool) is str and tool in _TOOL_FIELDS else None
    error = _safe_error(error)
    if error is not None:
        safe_args = _metadata(tool, args, "INVALID_ARGUMENT" if error.startswith("rejected:") else "ERROR")
    elif tool is None:
        safe_args = "REDACTED"
    elif type(args) is not dict:
        safe_args = _metadata(tool, args, "INVALID_ARGUMENT")
    else:
        try:
            safe_args = _validated_args(tool, args)
        except q.MarketMcpQueryError:
            safe_args = _metadata(tool, args, "INVALID_ARGUMENT")
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool or "REDACTED",
        "args": safe_args,
        "rows": rows if type(rows) is int and rows >= 0 and error is None else None,
        "error": error,
        "client": _safe_client(client),
    }
    # Bound enormous integers before serialization (including Python's digit cap).
    oversized = type(record["rows"]) is int and record["rows"].bit_length() > MAX_RECORD_BYTES * 3
    line = ""
    if not oversized:
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            oversized = len(line.encode("utf-8")) + 1 > MAX_RECORD_BYTES
        except (ValueError, OverflowError):
            oversized = True
    if oversized:
        record.update(args={"reason": "TRUNCATED"}, rows=None)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    directory = audit_dir or AUDIT_DIR
    with _LOCK:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"audit-{datetime.now():%Y-%m}.jsonl"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
