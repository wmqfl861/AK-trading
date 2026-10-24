"""Print the last fully completed exchange session date.

Used by scheduled tasks to pass ``--expected-through`` on weekends and
holidays, so freshness checks compare against the last session that EDB
can actually have data for, instead of the calendar date (which marks
every symbol stale on non-trading days).

Output: a single YYYY-MM-DD line on stdout. Any failure exits nonzero so
callers can fall back to their previous behaviour.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.futures_catalog import FuturesCatalogError, load_futures_catalog

CST = timezone(timedelta(hours=8))


def last_completed_session(catalog_path: Path, exchange: str, now: datetime) -> date:
    calendar = load_futures_catalog(catalog_path).calendar_for(exchange)
    candidate = now.date()
    while not calendar.is_trading_day(candidate):
        candidate -= timedelta(days=1)
    if candidate == now.date() and now.time() < time(15, 0):
        candidate -= timedelta(days=1)
        while not calendar.is_trading_day(candidate):
            candidate -= timedelta(days=1)
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", type=datetime.fromisoformat,
                        help="override current time (ISO format, for testing)")
    parser.add_argument("--exchange", default="SHFE",
                        help="calendar to consult (default SHFE)")
    parser.add_argument("--catalog", type=Path,
                        default=ROOT / "config" / "china_futures_catalog.json")
    args = parser.parse_args()

    now = args.now or datetime.now(CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    try:
        print(last_completed_session(args.catalog, args.exchange, now).isoformat())
    except (FuturesCatalogError, OSError, ValueError) as exc:
        print(f"compute_last_session: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
