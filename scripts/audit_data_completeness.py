"""Audit market-bar and member-rank completeness for backtest readiness.

Produces a JSON summary (and prints a markdown report) covering:
- KQ.m@ main-continuous minute coverage vs the KQ.i@ product-index calendar
- contract-level daily vs minute day coverage per contract
- member-rank trading-day coverage per exchange and year
- a registry of known unfillable gaps that backtests must respect

Usage:
    AK_DATABASE_STATEMENT_TIMEOUT_MS=600000 python scripts/audit_data_completeness.py \
        [--out data/DATA_COMPLETENESS_YYYYMMDD.json] [--skip-contracts]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from urllib.parse import urlsplit  # noqa: E402

import psycopg  # noqa: E402

from ak_trading.persistence.config import load_database_settings  # noqa: E402


def connect(timeout_ms: int = 0):
    settings = load_database_settings()
    parts = urlsplit(settings.url)
    return psycopg.connect(
        host=parts.hostname, port=parts.port, dbname=parts.path.lstrip("/"),
        user=parts.username, password=parts.password,
        options=f"-c statement_timeout={timeout_ms} -c work_mem=512MB",
    )


# Gaps that cannot be filled from any available source. Backtests must
# explicitly account for each entry.
UNFILLABLE_GAPS = [
    {
        "id": "BAD_SOURCE_DAY_20180806",
        "scope": "market_bars KQ.m@ minute",
        "detail": "DCE.p / SHFE.zn main-continuous minute bars on 2018-08-06 "
                  "are partial: the TQ source contains negative-volume rows "
                  "rejected by validation; the valid prefix of that day was "
                  "imported.",
        "action": "exclude 2018-08-06 for these two symbols in backtests",
    },
    {
        "id": "SC_RANKING_NOT_DISCLOSED",
        "scope": "member_position_ranks",
        "detail": "INE crude oil (sc) member ranking is not published in any "
                  "daily channel: absent from INE/SHFE pm files (verified "
                  "2018-2026) and from TQ query_symbol_ranking.",
        "action": "net-position factor cannot cover sc",
    },
    {
        "id": "EC_RANKING_2026_05_PLUS",
        "scope": "member_position_ranks",
        "detail": "INE ec ( container shipping index) ranking disappears from "
                  "pm files after 2026-04; covered 2024-01..2026-04 via "
                  "exchange endpoints and 2020-07..2023-12 via TQ.",
        "action": "net-position factor for ec ends 2026-04",
    },
    {
        "id": "TRADING_DAY_NULL_PRE_2026",
        "scope": "market_bars.trading_day",
        "detail": "the legacy reference calendar covers 2026 only, so "
                  "trading_day is NULL for bars before 2026-01-01; ts "
                  "(timestamptz) is authoritative.",
        "action": "derive trading day from ts in backtests for pre-2026 data",
    },
    {
        "id": "CFFEX_VOLUME_DOUBLE_COUNTED",
        "scope": "member_position_ranks metric=volume",
        "detail": "CFFEX ranking volumes are double counted (basis='double'); "
                  "other exchanges' basis is NULL/unknown.",
        "action": "never sum volume across exchanges; halve CFFEX if needed",
    },
    {
        "id": "RANK_HISTORY_STARTS_2020_07",
        "scope": "member_position_ranks",
        "detail": "TQ ranking history starts 2020-07-20; exchange-endpoint "
                  "history in this DB starts 2024-01-02. No ranking source "
                  "before 2020-07-20.",
        "action": "net-position factor backtests start 2020-07-20",
    },
]


def audit_main_continuous(cur) -> dict:
    cur.execute("""
        WITH idx AS (
            SELECT symbol, (ts AT TIME ZONE 'Asia/Shanghai')::date AS day
            FROM market_bars
            WHERE symbol LIKE 'KQ.i@%' AND duration_seconds = 86400
            GROUP BY 1, 2
        ), mi AS (
            SELECT replace(symbol, 'KQ.m@', 'KQ.i@') AS symbol,
                   (ts AT TIME ZONE 'Asia/Shanghai')::date AS day
            FROM market_bars
            WHERE symbol LIKE 'KQ.m@%' AND duration_seconds = 60
            GROUP BY 1, 2
        )
        SELECT i.symbol, count(*) AS idx_days, count(m.day) AS minute_days
        FROM idx i
        LEFT JOIN mi m ON m.symbol = i.symbol AND m.day = i.day
        GROUP BY i.symbol
    """)
    rows = cur.fetchall()
    result = {}
    missing_total = 0
    for symbol, idx_days, minute_days in rows:
        missing = idx_days - minute_days
        missing_total += missing
        result[symbol] = {
            "index_days": idx_days, "minute_days": minute_days, "missing_days": missing,
        }
    return {"products": len(rows), "missing_days_total": missing_total,
            "worst": sorted(result.items(), key=lambda kv: -kv[1]["missing_days"])[:10],
            "per_product": result}


def audit_contracts(cur, skip: bool) -> dict:
    if skip:
        return {"skipped": True}
    cur.execute("""
        CREATE TEMP TABLE _daily_days AS
        SELECT symbol, (ts AT TIME ZONE 'Asia/Shanghai')::date AS day
        FROM market_bars
        WHERE symbol NOT LIKE 'KQ.%' AND duration_seconds = 86400
        GROUP BY 1, 2
    """)
    cur.execute("CREATE INDEX ON _daily_days (symbol, day)")
    cur.execute("""
        CREATE TEMP TABLE _minute_days AS
        SELECT symbol, (ts AT TIME ZONE 'Asia/Shanghai')::date AS day
        FROM market_bars
        WHERE symbol NOT LIKE 'KQ.%' AND duration_seconds = 60
        GROUP BY 1, 2
    """)
    cur.execute("CREATE INDEX ON _minute_days (symbol, day)")
    cur.execute("ANALYZE _daily_days")
    cur.execute("ANALYZE _minute_days")
    cur.execute("""
        SELECT d.symbol, count(*) AS daily_days, count(m.day) AS minute_days
        FROM _daily_days d
        LEFT JOIN _minute_days m ON m.symbol = d.symbol AND m.day = d.day
        GROUP BY d.symbol
    """)
    rows = cur.fetchall()
    stats = {"contracts": len(rows), "contracts_full": 0, "contracts_partial": 0,
             "contracts_no_minute": 0, "missing_days_total": 0}
    partial_examples = []
    for symbol, daily_days, minute_days in rows:
        if minute_days >= daily_days:
            stats["contracts_full"] += 1
        elif minute_days == 0:
            stats["contracts_no_minute"] += 1
            stats["missing_days_total"] += daily_days
        else:
            stats["contracts_partial"] += 1
            stats["missing_days_total"] += daily_days - minute_days
            if len(partial_examples) < 800:
                partial_examples.append(
                    {"symbol": symbol, "daily": daily_days, "minute": minute_days})
    stats["partial_examples"] = partial_examples
    return stats


def audit_ranks(cur) -> dict:
    cur.execute("""
        SELECT exchange,
               extract(year FROM trading_day)::int AS yr,
               count(DISTINCT trading_day) AS days,
               count(*) AS rows,
               count(DISTINCT product_id) AS products
        FROM member_position_ranks
        GROUP BY 1, 2 ORDER BY 1, 2
    """)
    per_ex = {}
    for exch, yr, days, rows, products in cur.fetchall():
        per_ex.setdefault(exch, {})[int(yr)] = {
            "days": days, "rows": rows, "products": products}
    return {"per_exchange_year": per_ex}


def audit_nulls(cur) -> dict:
    cur.execute("""
        SELECT
          count(*) FILTER (WHERE open IS NULL OR high IS NULL
                           OR low IS NULL OR close IS NULL) AS null_ohlc,
          count(*) FILTER (WHERE volume < 0) AS neg_volume,
          count(*) AS total
        FROM market_bars
    """)
    null_ohlc, neg_volume, total = cur.fetchone()
    return {"bars_total": total, "null_ohlc": null_ohlc, "negative_volume": neg_volume}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--skip-contracts", action="store_true")
    args = parser.parse_args()
    if args.out is None:
        args.out = ROOT / "data" / f"DATA_COMPLETENESS_{date.today():%Y%m%d}.json"

    conn = connect(0)  # read-only audit session: no statement timeout
    with conn, conn.cursor() as cur:
        report = {
            "generated_at": date.today().isoformat(),
            "main_continuous_vs_index": audit_main_continuous(cur),
            "contracts_daily_vs_minute": audit_contracts(cur, args.skip_contracts),
            "member_ranks": audit_ranks(cur),
            "bar_sanity": audit_nulls(cur),
            "unfillable_gaps": UNFILLABLE_GAPS,
        }
    conn.close()
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    mc = report["main_continuous_vs_index"]
    cx = report["contracts_daily_vs_minute"]
    print(f"=== DATA COMPLETENESS {report['generated_at']} ===")
    print(f"main-continuous: {mc['products']} products, "
          f"missing minute days vs index calendar: {mc['missing_days_total']}")
    if not cx.get("skipped"):
        print(f"contracts: {cx['contracts']} total | full {cx['contracts_full']} | "
              f"partial {cx['contracts_partial']} | no-minute {cx['contracts_no_minute']} | "
              f"missing days {cx['missing_days_total']}")
    print(f"rank coverage by exchange/year: "
          f"{ {k: sum(v['days'] for v in y.values()) for k, y in report['member_ranks']['per_exchange_year'].items()} }")
    bs = report["bar_sanity"]
    print(f"bars: {bs['bars_total']:,} | null OHLC {bs['null_ohlc']:,} | "
          f"negative volume {bs['negative_volume']:,}")
    print(f"unfillable gaps registered: {len(UNFILLABLE_GAPS)}")
    print(f"report saved: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
