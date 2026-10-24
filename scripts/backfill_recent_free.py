"""Backfill recent minute bars for one symbol via the TqSdk free real-time channel.

The paid DataDownloader path (scripts/download_market_data.py) is deliberately NOT
used: the account's desk rejects historical downloads with "账户不支持下载历史数据
功能". Instead a plain real-time TqApi(auth=...) session is opened and
api.get_kline_serial(symbol, duration, data_length=8192) lets the websocket
backfill the most recent ~8192 bars of the requested duration.

Only rows whose reference trading day lies in [MAX(trading_day)+1, today] are
imported, and only intervals that have already closed (bar start + duration <=
now). Import goes through ak_trading.persistence.market_data.import_market_bars,
whose upsert on (symbol, duration_seconds, datetime_nano) makes reruns
idempotent. Night-session bars opening after 20:00 label to the NEXT trading day
and are therefore naturally excluded from an earlier day's window.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from numbers import Real
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.config import load_settings
from ak_trading.journal import redact_text
from ak_trading.market_reference import ReferenceDataError, load_reference_book
from ak_trading.persistence.config import (
    DatabaseConfigurationError,
    load_database_settings,
    redact_database_text,
)
from ak_trading.persistence.database import (
    DatabaseOperationError,
    create_database_engine,
    database_status,
)
from ak_trading.persistence.market_data import (
    MarketDataImportError,
    datetime_from_nano,
    import_market_bars,
    trading_day_for_timestamp,
    write_recent_kline_frame_csv,
)
from ak_trading.persistence.storage import store_reference_snapshot
from ak_trading.tqsdk_errors import (
    TqSdkOperationError,
    expected_tqsdk_error,
)

SHANGHAI = timezone(timedelta(hours=8))
NANOSECOND = 1_000_000_000
MAX_BIGINT = 9_223_372_036_854_775_807
BAR_FIELDS = ("open", "high", "low", "close", "volume", "open_oi", "close_oi")


def _safe_error(exc: BaseException, tq_settings, db_settings) -> str:
    text = redact_database_text(exc, db_settings)
    if tq_settings is not None:
        text = redact_text(text, secrets=(tq_settings.tq_user, tq_settings.tq_pass))
    return text[:300]


def _shanghai_nano(value: int) -> str:
    return datetime_from_nano(int(value)).astimezone(SHANGHAI).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def latest_stored_bar(engine, symbol: str, duration: int):
    """Index-backed reverse scan for the newest stored bar (symbol, duration)."""
    from sqlalchemy import select

    from ak_trading.persistence.schema import market_bars

    statement = (
        select(
            market_bars.c.trading_day,
            market_bars.c.ts,
            market_bars.c.datetime_nano,
        )
        .where(
            market_bars.c.symbol == symbol,
            market_bars.c.duration_seconds == duration,
        )
        .order_by(market_bars.c.ts.desc())
        .limit(1)
    )
    with engine.connect() as connection:
        return connection.execute(statement).mappings().first()


def fetch_recent_frame(api, symbol: str, duration: int, rows: int, wait_seconds: float):
    """Subscribe via a public SDK task, wait for the backfill, return a stable copy.

    Mirrors scripts/fill_recent_main_minute.py: the synchronous get_kline_serial
    carries an 85-second builtin wait outside a task, so the call is pumped from
    api.create_task while wait_update drives the websocket backfill. Readiness is
    api.is_serial_ready; afterwards one no-update wait_update cycle confirms the
    serial is idle (market closed or backfill complete) before the deep copy.
    """
    stop = time.monotonic() + wait_seconds

    async def subscribe():
        return api.get_kline_serial(symbol, duration, data_length=rows)

    task = api.create_task(subscribe())
    try:
        while not task.done():
            remaining = stop - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("subscription_not_ready")
            api.wait_update(deadline=time.time() + min(remaining, 1.0))
        frame = task.result()
        while not api.is_serial_ready(frame):
            remaining = stop - time.monotonic()
            if remaining <= 0 or not api.wait_update(
                deadline=time.time() + max(0.0, min(remaining, 5.0))
            ):
                if not api.is_serial_ready(frame):
                    raise TimeoutError("serial_not_ready")
                break
        # Stability: with the session idle (no packet within the short deadline)
        # and the serial ready, the backfill cannot still be growing.
        api.wait_update(deadline=time.time() + 5.0)
        return frame.copy(deep=True)
    finally:
        if not task.done():
            task.cancel()


def select_valid_rows(frame, *, duration: int):
    """Drop SDK padding rows, validate values, and return (frame, rows_nanos)."""
    required = ["datetime", *BAR_FIELDS]
    if not frame.columns.is_unique or not set(required).issubset(frame.columns):
        raise ValueError("invalid_columns")
    positions, nanos = [], []
    for position, value in enumerate(frame["datetime"]):
        if value != value:  # NaN padding
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not math.isfinite(value)
            or not 0 <= value <= MAX_BIGINT
            or value != int(value)
        ):
            raise ValueError("invalid_datetime")
        if value > 0:
            positions.append(position)
            nanos.append(int(value))
    valid = frame.iloc[positions].loc[:, required].copy(deep=True)
    valid["datetime"] = [int(nano) for nano in nanos]
    last_nano = None
    for nano, *values in valid.itertuples(index=False, name=None):
        if last_nano is not None and nano <= last_nano:
            raise ValueError("unordered_datetime")
        last_nano = nano
        if any(
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not math.isfinite(value)
            or value < 0
            for value in values
        ):
            raise ValueError("invalid_values")
        open_, high, low, close = values[:4]
        if min(open_, high, low, close) <= 0 or not (
            low <= min(open_, close) <= max(open_, close) <= high
        ):
            raise ValueError("invalid_ohlc")
    return valid


def label_rows(frame, calendar):
    """Return (labels, sources) aligned to the valid frame rows."""
    return zip(*(
        trading_day_for_timestamp(datetime_from_nano(int(nano)), calendar)
        for nano in frame["datetime"]
    ))


def group_by_day(frame, labels):
    grouped: dict[date, dict[str, object]] = {}
    for nano, day in zip(frame["datetime"], labels):
        entry = grouped.setdefault(
            day, {"rows": 0, "first_nano": nano, "last_nano": nano}
        )
        entry["rows"] += 1
        entry["last_nano"] = nano
    return grouped


def write_coverage_csv(path: Path, grouped, label_sources: Counter) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(
            "trading_day,rows,first_ts_shanghai,last_ts_shanghai,label_source\n"
        )
        for day in sorted(grouped, key=lambda value: (value is None, value)):
            entry = grouped[day]
            handle.write(
                f"{day.isoformat() if day is not None else 'none'},{entry['rows']},"
                f"{_shanghai_nano(entry['first_nano'])},"
                f"{_shanghai_nano(entry['last_nano'])},"
                f"{label_sources.get(day, '')}\n"
            )


def day_counts_in_window(engine, symbol: str, duration: int, first_day, last_day):
    from sqlalchemy import func, select

    from ak_trading.persistence.schema import market_bars

    statement = (
        select(
            market_bars.c.trading_day,
            func.count().label("rows"),
            func.min(market_bars.c.ts).label("first_ts"),
            func.max(market_bars.c.ts).label("last_ts"),
        )
        .where(
            market_bars.c.symbol == symbol,
            market_bars.c.duration_seconds == duration,
            market_bars.c.trading_day.is_not(None),
            market_bars.c.trading_day >= first_day,
            market_bars.c.trading_day <= last_day,
        )
        .group_by(market_bars.c.trading_day)
        .order_by(market_bars.c.trading_day)
    )
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(statement).mappings().all()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill recent bars via the TqSdk free real-time session channel"
    )
    parser.add_argument("--symbol", default="KQ.m@SHFE.rb")
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--data-length", type=int, default=8192)
    parser.add_argument(
        "--wait-seconds", type=float, default=180.0,
        help="total budget for subscription plus backfill readiness",
    )
    parser.add_argument(
        "--reference-config", type=Path,
        default=ROOT / "config" / "market_reference.json",
    )
    parser.add_argument(
        "--artifact-dir", type=Path,
        default=ROOT / "data" / "maintenance_artifacts" / "tq-backfill-20260918",
    )
    args = parser.parse_args(argv)
    if args.duration <= 0 or args.data_length <= 1 or args.wait_seconds <= 0:
        print("BACKFILL_SETUP_FAIL invalid arguments")
        return 2

    tq_settings = None
    db_settings = None
    engine = None
    api = None
    context = "settings"
    summary: dict[str, object] = {
        "symbol": args.symbol,
        "duration_seconds": args.duration,
        "data_length": args.data_length,
        "channel": "tqsdk_free_realtime_session",
    }
    try:
        from tqsdk import TqApi, TqAuth

        tq_settings = load_settings()
        db_settings = load_database_settings(required=True)
        assert db_settings is not None
        context = "database_setup"
        status = database_status(db_settings)
        if not status["alembic_revision"]:
            raise DatabaseOperationError(
                "database is not migrated; run scripts/manage_database.py migrate"
            )
        book = load_reference_book(args.reference_config)
        engine = create_database_engine(db_settings)
        snapshot = store_reference_snapshot(
            engine, args.reference_config, settings=db_settings
        )

        context = "coverage_before"
        before = latest_stored_bar(engine, args.symbol, args.duration)
        max_td_before = before["trading_day"] if before is not None else None
        needed_from = (
            max_td_before + timedelta(days=1) if max_td_before is not None else None
        )
        today = datetime.now(SHANGHAI).date()
        print(
            "BACKFILL_COVERAGE_BEFORE",
            f"max_trading_day={max_td_before.isoformat() if max_td_before else 'none'}",
            f"last_ts={before['ts'].astimezone(SHANGHAI).isoformat() if before else 'none'}",
            f"needed_from={needed_from.isoformat() if needed_from else 'any'}",
            f"target_through={today.isoformat()}",
        )
        summary["before"] = {
            "max_trading_day": max_td_before.isoformat() if max_td_before else None,
            "last_ts_shanghai": (
                before["ts"].astimezone(SHANGHAI).isoformat() if before else None
            ),
        }

        context = "session"
        try:
            api = TqApi(auth=TqAuth(tq_settings.tq_user, tq_settings.tq_pass))
        except Exception as exc:
            expected = expected_tqsdk_error(exc, operation="session")
            if expected is None:
                raise exc
            raise expected from None

        context = "fetch"
        frame = fetch_recent_frame(
            api, args.symbol, args.duration, args.data_length, args.wait_seconds
        )
        with closing(api):
            context = "validation"
            valid = select_valid_rows(frame, duration=args.duration)
            if valid.empty:
                raise MarketDataImportError("serial contains no valid bars")
            labels, sources = label_rows(valid, book.calendar)
            fetched_grouped = group_by_day(valid, labels)
            label_source_by_day: Counter = Counter()
            for day, source in zip(labels, sources):
                label_source_by_day[(day, source)] += 1
            fetched_days = sorted(day for day in fetched_grouped if day is not None)
            earliest_fetched = fetched_days[0] if fetched_days else None
            now_nano = int(time.time() * NANOSECOND)
            cutoff = now_nano - args.duration * NANOSECOND
            keep_positions = [
                position
                for position, (day, nano) in enumerate(zip(labels, valid["datetime"]))
                if day is not None
                and (needed_from is None or day >= needed_from)
                and day <= today
                and nano <= cutoff
            ]
        api = None

        fetched_earliest_ts = _shanghai_nano(valid["datetime"].iloc[0])
        fetched_latest_ts = _shanghai_nano(valid["datetime"].iloc[-1])
        print(
            "BACKFILL_FETCHED",
            f"bars={len(valid)}",
            f"earliest={fetched_earliest_ts}",
            f"latest={fetched_latest_ts}",
            f"trading_days={len(fetched_days)}",
        )
        summary["fetched"] = {
            "bars": len(valid),
            "earliest_ts_shanghai": fetched_earliest_ts,
            "latest_ts_shanghai": fetched_latest_ts,
            "distinct_trading_days": len(fetched_days),
            "earliest_trading_day": (
                earliest_fetched.isoformat() if earliest_fetched else None
            ),
        }
        args.artifact_dir.mkdir(parents=True, exist_ok=True)
        safe_symbol = args.symbol.replace("@", "_at_").replace(".", "_")
        write_coverage_csv(
            args.artifact_dir / f"{safe_symbol}-fetched-coverage.csv",
            fetched_grouped,
            Counter(
                {day: source for (day, source), _ in label_source_by_day.items()}
            ),
        )

        shortfall = (
            needed_from is not None
            and earliest_fetched is not None
            and earliest_fetched > needed_from
        )
        if shortfall:
            print(
                "BACKFILL_SHORTFALL",
                f"earliest_fetched={earliest_fetched.isoformat()}",
                f"needed_from={needed_from.isoformat()}",
                "history_depth_insufficient_for_full_gap",
            )
            summary["shortfall_from"] = needed_from.isoformat()
            summary["shortfall_earliest_fetched"] = earliest_fetched.isoformat()

        selected = valid.iloc[keep_positions].copy(deep=True)
        keep_labels = [labels[position] for position in keep_positions]
        if selected.empty:
            print(
                "BACKFILL_NO_GAP",
                "selected_rows=0",
                f"db_max_trading_day={max_td_before.isoformat() if max_td_before else 'none'}",
                f"fetch_latest={fetched_latest_ts}",
            )
            summary["imported"] = {"rows": 0, "trading_days": 0, "batch_id": None}
            result = None
        else:
            selected_grouped = group_by_day(selected, keep_labels)
            for day in sorted(selected_grouped):
                entry = selected_grouped[day]
                print(
                    "BACKFILL_DAY",
                    day.isoformat(),
                    f"rows={entry['rows']}",
                    f"first={_shanghai_nano(entry['first_nano'])}",
                    f"last={_shanghai_nano(entry['last_nano'])}",
                )
            context = "import"
            csv_path = args.artifact_dir / f"{safe_symbol}-window.csv"
            write_recent_kline_frame_csv(
                selected, csv_path, symbol=args.symbol
            )
            latest_selected = max(selected_grouped)
            result = import_market_bars(
                engine,
                csv_path,
                symbol=args.symbol,
                duration_seconds=args.duration,
                calendar=book.calendar,
                reference_sha256=snapshot.content_sha256,
                source_label=(
                    f"tqsdk_recent_free_backfill:{args.symbol}:{args.duration}"
                    f":{needed_from.isoformat()}:{latest_selected.isoformat()}"
                ),
                settings=db_settings,
            )
            print(
                "BACKFILL_IMPORTED",
                "batch",
                result.batch_id,
                "rows_read",
                result.rows_read,
                "rows_written",
                result.rows_written,
                "null_trading_days",
                result.null_trading_days,
            )
            summary["imported"] = {
                "rows": result.rows_written,
                "trading_days": len(selected_grouped),
                "batch_id": result.batch_id,
                "days": {
                    day.isoformat(): {
                        "rows": entry["rows"],
                        "first_ts_shanghai": _shanghai_nano(entry["first_nano"]),
                        "last_ts_shanghai": _shanghai_nano(entry["last_nano"]),
                    }
                    for day, entry in sorted(selected_grouped.items())
                },
            }

        context = "coverage_after"
        after = latest_stored_bar(engine, args.symbol, args.duration)
        max_td_after = after["trading_day"] if after is not None else None
        print(
            "BACKFILL_COVERAGE_AFTER",
            f"max_trading_day={max_td_after.isoformat() if max_td_after else 'none'}",
            f"last_ts={after['ts'].astimezone(SHANGHAI).isoformat() if after else 'none'}",
        )
        summary["after"] = {
            "max_trading_day": max_td_after.isoformat() if max_td_after else None,
            "last_ts_shanghai": (
                after["ts"].astimezone(SHANGHAI).isoformat() if after else None
            ),
        }
        verify_from = (
            max(needed_from, today - timedelta(days=10))
            if needed_from is not None
            else today - timedelta(days=10)
        )
        for row in day_counts_in_window(
            engine, args.symbol, args.duration, verify_from, today
        ):
            print(
                "BACKFILL_VERIFY_DAY",
                row["trading_day"].isoformat(),
                f"rows={row['rows']}",
                f"first={row['first_ts'].astimezone(SHANGHAI).isoformat()}",
                f"last={row['last_ts'].astimezone(SHANGHAI).isoformat()}",
            )
        (args.artifact_dir / f"{safe_symbol}-summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
        print("BACKFILL_SUMMARY", "ok", f"rows={summary['imported']['rows']}")
        return 0
    except (
        DatabaseConfigurationError,
        DatabaseOperationError,
        MarketDataImportError,
        OSError,
        ReferenceDataError,
        TqSdkOperationError,
        TimeoutError,
        ValueError,
    ) as exc:
        print(
            "BACKFILL_FAIL",
            context,
            type(exc).__name__,
            _safe_error(exc, tq_settings, db_settings),
        )
        return 2
    finally:
        if api is not None:
            try:
                api.close()
            except Exception:
                pass
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
