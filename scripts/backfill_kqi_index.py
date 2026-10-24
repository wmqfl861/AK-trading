"""Backfill product-index (KQ.i@) daily bars via get_kline_serial.

DataDownloader rejects KQ.i@ symbols in the instrument service, but
``get_kline_serial`` serves them fine; daily granularity fits comfortably
inside the free 10000-bar window, so this path is also trial-independent.
Rows are written through the standard idempotent ``import_market_bars``.
"""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.config import load_settings
from ak_trading.market_reference import load_reference_book
from ak_trading.persistence.config import load_database_settings
from ak_trading.persistence.database import create_database_engine
from ak_trading.persistence.market_data import (
    expected_kline_columns,
    import_market_bars,
)
from ak_trading.persistence.storage import store_reference_snapshot

_BAR_FIELDS = ("open", "high", "low", "close", "volume", "open_oi", "close_oi")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols-file", type=Path,
                        default=ROOT / "data" / "downloads" / "kqi_symbols.txt")
    parser.add_argument("--rows", type=int, default=4000)
    args = parser.parse_args()

    from tqsdk import TqApi, TqAuth

    settings = load_settings()
    db_settings = load_database_settings(required=True)
    engine = create_database_engine(db_settings)
    book = load_reference_book(ROOT / "config" / "market_reference.json")
    snapshot = store_reference_snapshot(
        engine, ROOT / "config" / "market_reference.json", settings=db_settings
    )

    symbols = [
        line.strip() for line in args.symbols_file.read_text().splitlines()
        if line.strip()
    ]
    done = failed = 0
    total_rows = 0
    with closing(TqApi(auth=TqAuth(settings.tq_user, settings.tq_pass))) as api:
        for symbol in symbols:
            try:
                frame = api.get_kline_serial(symbol, 86400, args.rows)
                api.wait_update(deadline=30)
                valid = frame.dropna(subset=["close", "datetime"])
                valid = valid[valid["datetime"] > 0]
                if valid.empty:
                    print(f"KQI_EMPTY {symbol}", flush=True)
                    continue
                prefix = f"{symbol}."
                fd, temp_name = tempfile.mkstemp(
                    prefix="ak_kqi_", suffix=".csv",
                    dir=ROOT / "data" / "downloads",
                )
                import os

                os.close(fd)
                out_path = Path(temp_name)
                with out_path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(expected_kline_columns(symbol))
                    import datetime as dt

                    for _, row in valid.iterrows():
                        nano = int(row["datetime"])
                        ts = dt.datetime.fromtimestamp(
                            nano / 1e9, tz=dt.timezone(dt.timedelta(hours=8))
                        )
                        writer.writerow(
                            [ts.isoformat(), nano]
                            + [row[field] for field in _BAR_FIELDS]
                        )
                result = import_market_bars(
                    engine,
                    out_path,
                    symbol=symbol,
                    duration_seconds=86400,
                    calendar=book.calendar,
                    reference_sha256=snapshot.content_sha256,
                    source_label=f"tqsdk_kqi_index:{symbol}:86400",
                    settings=db_settings,
                )
                total_rows += result.rows_written
                done += 1
                print(f"KQI_OK {symbol} rows={result.rows_written}", flush=True)
                out_path.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001 - per-symbol isolation
                failed += 1
                print(f"KQI_FAIL {symbol} {type(exc).__name__}: {exc}", flush=True)
    engine.dispose()
    print(f"KQI_SUMMARY done={done} failed={failed} rows={total_rows}", flush=True)
    return 1 if failed and not done else 0


if __name__ == "__main__":
    sys.exit(main())
