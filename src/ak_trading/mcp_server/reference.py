"""File-based reference data for the MCP server.

Trading calendar and product directory come from the frozen catalog
(config/china_futures_catalog.json); settlement parameters come from the
daily snapshots under data/settlement_params. Neither touches PostgreSQL.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from ak_trading.mcp_server.queries import (
    MarketMcpQueryError, check_iso_day, check_window,
)

ROOT = Path(__file__).resolve().parents[3]
CATALOG_PATH = ROOT / "config" / "china_futures_catalog.json"
SETTLEMENT_ROOT = ROOT / "data" / "settlement_params"

MAX_CALENDAR_WINDOW_DAYS = 800
CALENDAR_EXCHANGE = "SHFE"  # six calendars agree on Chinese trading days


@lru_cache(maxsize=4)
def _load_catalog(path: str):
    from ak_trading.futures_catalog import FuturesCatalogError, load_futures_catalog

    try:
        return load_futures_catalog(Path(path), require_all_exchanges=False)
    except (FuturesCatalogError, OSError, ValueError) as exc:
        raise MarketMcpQueryError(f"catalog unavailable: {exc}") from None


def trading_calendar(start: str, end: str) -> dict[str, Any]:
    begin = check_iso_day(start, name="start")
    stop = check_iso_day(end, name="end")
    check_window(begin, stop, max_days=MAX_CALENDAR_WINDOW_DAYS, label="calendar")
    calendar = _load_catalog(str(CATALOG_PATH)).calendar_for(CALENDAR_EXCHANGE)
    days: list[dict[str, Any]] = []
    trading: list[str] = []
    cursor = begin
    while cursor <= stop:
        if cursor < calendar.valid_from or cursor > calendar.valid_through:
            raise MarketMcpQueryError(
                f"calendar covers {calendar.valid_from}..{calendar.valid_through}; "
                "narrow the requested window"
            )
        is_trading = calendar.is_trading_day(cursor)
        days.append({"date": cursor.isoformat(), "trading": is_trading})
        if is_trading:
            trading.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return {"exchange": CALENDAR_EXCHANGE, "start": begin.isoformat(),
            "end": stop.isoformat(), "trading_days": trading, "days": days}


def products() -> dict[str, Any]:
    catalog = _load_catalog(str(CATALOG_PATH))
    rows = [
        {"exchange": product.exchange, "code": product.code,
         "name_zh": product.name_zh}
        for product in catalog.products
    ]
    return {"rows": len(rows), "products": rows}


def _decimal_to_float(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _decimal_to_float(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimal_to_float(v) for v in value]
    return value


def settlement_reference(day: str, symbol: str) -> dict[str, Any]:
    parsed_day = check_iso_day(day, name="day")
    from ak_trading.settlement_reference import load_settlement_reference

    try:
        result = load_settlement_reference(
            SETTLEMENT_ROOT, day=parsed_day, symbol=symbol
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise MarketMcpQueryError(f"settlement reference rejected: {exc}") from None
    return _decimal_to_float(result)
