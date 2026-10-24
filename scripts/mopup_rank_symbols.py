"""Force-refetch rankings for specific symbols (cursor-ledger bypass).

Re-walks each symbol's full 2020-07-20..now window for all three ranking
metrics and upserts everything found; existing rows are harmless idempotent
updates. Used to recover blocks the main driver marked failed.
"""

from __future__ import annotations

import argparse
import re
import sys
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.config import load_settings
from ak_trading.member_rank import MemberRankRow
from ak_trading.persistence.config import load_database_settings
from ak_trading.persistence.database import create_database_engine
from ak_trading.persistence.member_rank import import_member_rank_rows

_PREFIX_RX = re.compile(r"^[A-Z](?=[\u4e00-\u9fff])")
_METRICS = {
    "VOLUME": ("volume", "volume_ranking", "volume", "volume_change"),
    "LONG": ("long", "long_ranking", "long_oi", "long_change"),
    "SHORT": ("short", "short_ranking", "short_oi", "short_change"),
}


def build_rows(df, symbol: str) -> list[MemberRankRow]:
    exchange = symbol.split(".")[0]
    instrument = symbol.split(".", 1)[1]
    product = "".join(ch for ch in instrument if ch.isalpha())
    out: list[MemberRankRow] = []
    metric_name, rank_col, qty_col, chg_col = _METRICS[df.attrs.get("metric")]
    for _, row in df.iterrows():
        rank = row.get(rank_col)
        qty = row.get(qty_col)
        if rank is None or qty is None or qty != qty:  # NaN guard
            continue
        member = _PREFIX_RX.sub("", str(row.get("broker") or ""))
        if not member:
            continue
        try:
            day = date(int(str(row["datetime"])[:4]), int(str(row["datetime"])[4:6]),
                       int(str(row["datetime"])[6:8]))
        except (ValueError, TypeError):
            continue
        chg = row.get(chg_col)
        out.append(MemberRankRow(
            exchange=exchange, trading_day=day, product_id=product.upper(),
            instrument_id=instrument, metric=metric_name, rank_no=int(rank),
            member_name=member, quantity=int(qty),
            change_qty=(int(chg) if chg == chg and chg is not None else None),
            source_url="tqsdk:query_symbol_ranking",
        ))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols-file", type=Path,
                        default=ROOT / "data" / "downloads" / "tq_rank_mopup_symbols.txt")
    args = parser.parse_args()
    symbols = [
        s.strip() for s in args.symbols_file.read_text().split(",") if s.strip()
    ]
    from tqsdk import TqApi, TqAuth

    settings = load_settings()
    engine = create_database_engine(load_database_settings(required=True))
    start = date(2020, 7, 20)
    end = date(2026, 9, 3)
    total_rows = 0
    with closing(TqApi(auth=TqAuth(settings.tq_user, settings.tq_pass))) as api:
        for symbol in symbols:
            for metric in ("VOLUME", "LONG", "SHORT"):
                cursor = start
                imported = 0
                guard = 0
                while cursor < end and guard < 200:
                    guard += 1
                    try:
                        df = api.query_symbol_ranking(
                            symbol, metric, days=20, start_dt=cursor)
                    except Exception as exc:  # noqa: BLE001
                        print(f"MOP_FAIL {symbol} {metric} @{cursor}: "
                              f"{str(exc)[:80]}", flush=True)
                        cursor += timedelta(days=25)
                        continue
                    df.attrs["metric"] = metric
                    if len(df):
                        rows = build_rows(df, symbol)
                        if rows:
                            res = import_member_rank_rows(engine, rows)
                            imported += res.rows_written
                            last = max(r.trading_day for r in rows)
                            cursor = last + timedelta(days=1)
                            continue
                    cursor += timedelta(days=25)
                total_rows += imported
                print(f"MOP_OK {symbol} {metric} imported={imported}", flush=True)
    engine.dispose()
    print(f"MOP_SUMMARY symbols={len(symbols)} total_rows={total_rows}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
