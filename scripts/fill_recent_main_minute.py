"""Fill recent closed main-continuous (KQ.m@) minute bars.

--through limits the inclusive Shanghai trading day using the importer's local
reference-calendar mapping, plus an exclusive following-midnight timestamp
bound. Unknown trading-day labels remain explicitly unknown. --expected-through
checks the latest selected raw timestamp's Shanghai date, not session completeness.
Only intervals ending by now (and --closed-before, if supplied) are imported.
"""

from __future__ import annotations

import argparse
import math
import re
import ssl
import sys
import tempfile
import time
from datetime import date, datetime, time as day_time, timedelta, timezone
from numbers import Real
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.config import load_settings
from ak_trading.market_reference import load_reference_book
from ak_trading.persistence.config import load_database_settings
from ak_trading.persistence.database import create_database_engine
from ak_trading.persistence.market_data import (
    datetime_from_nano,
    import_market_bars,
    trading_day_for_timestamp,
    write_recent_kline_frame_csv,
)
from ak_trading.persistence.storage import store_reference_snapshot

_BAR_FIELDS = ("open", "high", "low", "close", "volume", "open_oi", "close_oi")
SHANGHAI = timezone(timedelta(hours=8))
_NANOSECOND = 1_000_000_000
_MINUTE = 60 * _NANOSECOND
_MAX_BIGINT = 9_223_372_036_854_775_807
_MAX_SYMBOLS = 512
_FETCH_RETRIES = 2
_MAIN_SYMBOL = re.compile(r"KQ\.m@(CFFEX|CZCE|DCE|GFEX|INE|SHFE)\.[A-Za-z]{1,8}\Z")


def _rows(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("rows must be an integer in 1..10000") from None
    if not 1 <= result <= 10000:
        raise argparse.ArgumentTypeError("rows must be an integer in 1..10000")
    return result


def _timeout(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be finite and in (0, 120]") from None
    if not math.isfinite(result) or not 0 < result <= 120:
        raise argparse.ArgumentTypeError("timeout must be finite and in (0, 120]")
    return result


def _date(value: str) -> date:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from None


def _aware_timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise argparse.ArgumentTypeError("timestamp must be timezone-aware ISO 8601") from None


def _timestamp_nano(value: datetime) -> int:
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * _NANOSECOND + delta.microseconds * 1000


def read_symbols(path: Path) -> list[str]:
    """Freeze a small, explicit main-continuous scope before loading credentials."""
    try:
        if not path.is_file() or path.stat().st_size > 65536:
            raise ValueError
        with path.open(encoding="utf-8-sig") as handle:
            text = handle.read(65537)
        if len(text) > 65536:
            raise ValueError
    except (OSError, UnicodeError, ValueError):
        raise ValueError("symbols file must be a readable UTF-8 file up to 64 KiB") from None
    symbols = list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))
    if not 1 <= len(symbols) <= _MAX_SYMBOLS:
        raise ValueError("symbols file must contain 1..512 unique symbols")
    if any(_MAIN_SYMBOL.fullmatch(symbol) is None for symbol in symbols):
        raise ValueError("symbols file must contain only six-exchange KQ.m@ main symbols")
    return symbols


def wait_ready(api, frame, timeout: float = 30) -> bool:
    """Wait for serial initialization; the public SDK deadline is epoch seconds.

    See the local wait-update-and-update-loop.md reference and TqApi.wait_update.
    A monotonic budget prevents unrelated updates from extending the timeout.
    """
    stop = time.monotonic() + timeout
    while not api.is_serial_ready(frame):
        remaining = stop - time.monotonic()
        if remaining <= 0:
            return False
        if not api.wait_update(deadline=time.time() + remaining):
            return bool(api.is_serial_ready(frame))
    return True


def fetch_snapshot(api, symbol: str, rows: int, timeout: float):
    """Bound subscription and readiness together using only public SDK methods."""
    stop = time.monotonic() + timeout

    async def subscribe():
        # Synchronous get_kline_serial has its own 85-second wait in SDK 3.10.1.
        # Within a public SDK task it returns the live serial without that wait.
        return api.get_kline_serial(symbol, 60, data_length=rows)

    task = api.create_task(subscribe())
    try:
        while not task.done():
            remaining = stop - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("subscription_not_ready")
            # Pump the subscription task promptly even if a cached serial is
            # ready and no fresh market packet will arrive for the full timeout.
            updated = api.wait_update(deadline=time.time() + min(remaining, 1.0))
            if not updated and not task.done() and time.monotonic() >= stop:
                raise TimeoutError("subscription_not_ready")
        frame = task.result()
        if not wait_ready(api, frame, max(0, stop - time.monotonic())):
            raise TimeoutError("serial_not_ready")
        # SDK frames update in place. Never coerce an iterrows row to recover ns.
        return frame.copy(deep=True)
    finally:
        if not task.done():
            task.cancel()


def _retry_read(operation, *, retry_errors, context: str, symbol: str = ""):
    """Two retries for transport reads only; imports never enter this loop."""
    for attempt in range(_FETCH_RETRIES + 1):
        try:
            return operation()
        except Exception as exc:
            if (not isinstance(exc, retry_errors)
                    or isinstance(exc, ssl.SSLCertVerificationError)
                    or attempt == _FETCH_RETRIES):
                raise
            print(f"FILL_RETRY {symbol} context={context} retry={attempt + 1}", flush=True)


def select_closed_frame(frame, *, calendar, through=None, closed_before=None, end=None):
    """Remove timestamp placeholders and out-of-scope intervals, then validate."""
    import pandas as pd

    required = ["datetime", *_BAR_FIELDS]
    if not frame.columns.is_unique or not set(required).issubset(frame.columns):
        raise ValueError("invalid_columns")
    positions, nanos = [], []
    for position, value in enumerate(frame["datetime"]):
        if pd.isna(value):
            continue  # SDK padding can have NaN datetimes, including at the tail.
        if (isinstance(value, bool) or not isinstance(value, Real)
                or not math.isfinite(value) or not 0 <= value <= _MAX_BIGINT
                or value != int(value)):
            raise ValueError("invalid_datetime")
        if value > 0:
            positions.append(position)
            nanos.append(int(value))
    valid = frame.iloc[positions].loc[:, required].copy(deep=True)
    valid["datetime"] = pd.Series(nanos, index=valid.index, dtype="int64")
    raw_latest = datetime_from_nano(max(nanos)).astimezone(SHANGHAI) if nanos else None
    cutoff = int(time.time() * _NANOSECOND)
    if closed_before is not None:
        cutoff = min(cutoff, _timestamp_nano(closed_before))
    # Subtract the interval length before comparing, avoiding BIGINT overflow.
    keep = valid["datetime"] <= cutoff - _MINUTE
    if end is not None:
        keep &= valid["datetime"] < _timestamp_nano(end)
    if through is not None:
        following_midnight = datetime.combine(through + timedelta(days=1), day_time(), SHANGHAI)
        keep &= valid["datetime"] < _timestamp_nano(following_midnight)
    valid = valid.loc[keep].copy(deep=True)
    labels = [trading_day_for_timestamp(datetime_from_nano(nano), calendar)
              for nano in valid["datetime"]]
    if through is not None:
        keep_positions = [i for i, (day, _) in enumerate(labels) if day is None or day <= through]
        valid = valid.iloc[keep_positions].copy(deep=True)
        labels = [labels[i] for i in keep_positions]
    last_nano = None
    for nano, *values in valid.itertuples(index=False, name=None):
        if last_nano is not None and nano <= last_nano:
            raise ValueError("unordered_datetime")
        last_nano = nano
        if any(isinstance(value, bool) or not isinstance(value, Real)
               or not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("invalid_values")
        open_, high, low, close = values[:4]
        if min(open_, high, low, close) <= 0 or not low <= min(open_, close) <= max(open_, close) <= high:
            raise ValueError("invalid_ohlc")
    latest = datetime_from_nano(last_nano).astimezone(SHANGHAI) if last_nano is not None else None
    details = {
        "raw_latest": raw_latest.isoformat() if raw_latest else "none",
        "latest": latest.isoformat() if latest else "none",
        "latest_date": latest.date() if latest else None,
        "unknown_trading_day_rows": sum(day is None for day, _ in labels),
        "label_sources": ",".join(sorted({source for _, source in labels})) or "none",
    }
    return valid, details


def import_frame(engine, frame, symbol, *, calendar, snapshot, db_settings, temp_dir):
    temp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="ak_fill_", suffix=".csv", dir=temp_dir, delete=False) as handle:
        path = Path(handle.name)
    try:
        write_recent_kline_frame_csv(frame, path, symbol=symbol)
        return import_market_bars(
            engine, path, symbol=symbol, duration_seconds=60, calendar=calendar,
            reference_sha256=snapshot.content_sha256,
            source_label=f"tqsdk_recent_fill:{symbol}:60", settings=db_settings,
        )
    finally:
        path.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols-file", type=Path, default=ROOT / "data/downloads/kqm_symbols.txt")
    parser.add_argument("--rows", type=_rows, default=2000, help="recent bars per symbol, 1..10000")
    parser.add_argument("--timeout", type=_timeout, default=30,
                        help="subscription/readiness seconds per attempt, (0, 120]; SDK connection timeout is separate")
    parser.add_argument("--through", type=_date, default=None, help="inclusive reference trading day, YYYY-MM-DD")
    parser.add_argument("--expected-through", type=_date, default=None,
                        help="require latest selected raw Shanghai date at least YYYY-MM-DD")
    parser.add_argument("--end", type=_aware_timestamp, default=None,
                        help="exclusive bar-start bound, aware ISO timestamp normalized to UTC")
    parser.add_argument("--closed-before", type=_aware_timestamp, default=None,
                        help="require bar start + 60 seconds <= this aware ISO timestamp and now")
    args = parser.parse_args(argv)
    if args.through == date.max:
        parser.error("through must allow a following calendar day")
    if args.through and args.expected_through and args.expected_through > args.through:
        parser.error("expected-through cannot be later than through")
    try:
        symbols = read_symbols(args.symbols_file)
    except ValueError:
        parser.error("invalid symbols file: require readable UTF-8 up to 64 KiB and 1..512 six-exchange KQ.m@ symbols")

    done = failed = empty = stale = total_rows = cleanup_failed = 0
    engine = api = None
    context = "dependencies"
    try:
        # Parse and freeze scope before importing the provider or loading secrets.
        from requests.exceptions import ConnectionError as RequestConnectionError, Timeout as RequestTimeout
        from tqsdk import TqApi, TqAuth, TqTimeoutError

        retry_errors = (TimeoutError, ConnectionError, ssl.SSLError,
                        RequestConnectionError, RequestTimeout, TqTimeoutError)
        context = "settings"
        settings = load_settings()
        db_settings = load_database_settings(required=True)
        context = "reference"
        book = load_reference_book(ROOT / "config/market_reference.json")
        context = "database_setup"
        engine = create_database_engine(db_settings)
        snapshot = store_reference_snapshot(engine, ROOT / "config/market_reference.json", settings=db_settings)
        context = "api_create"
        api = _retry_read(lambda: TqApi(auth=TqAuth(settings.tq_user, settings.tq_pass)),
                          retry_errors=retry_errors, context=context)
        for symbol in symbols:
            context = "fetch"
            try:
                frame = _retry_read(lambda: fetch_snapshot(api, symbol, args.rows, args.timeout),
                                    retry_errors=retry_errors, context=context, symbol=symbol)
                context = "validation"
                valid, details = select_closed_frame(frame, calendar=book.calendar, through=args.through,
                                                     closed_before=args.closed_before, end=args.end)
                info = (f"raw_latest={details['raw_latest']} latest={details['latest']} "
                        f"freshness_basis=selected_raw_timestamp_shanghai_date "
                        f"unknown_trading_day_rows={details['unknown_trading_day_rows']} "
                        f"label_sources={details['label_sources']}")
                if valid.empty:
                    empty += 1
                    print(f"FILL_EMPTY {symbol} {info} calendar_empty=unknown", flush=True)
                    continue
                is_stale = args.expected_through is not None and details["latest_date"] < args.expected_through
                context = "import"
                result = import_frame(engine, valid, symbol, calendar=book.calendar, snapshot=snapshot,
                                      db_settings=db_settings, temp_dir=ROOT / "data/downloads")
                if type(result.rows_written) is not int or result.rows_written != len(valid):
                    raise ValueError("incomplete_import_acknowledgement")
                total_rows += result.rows_written
                if is_stale:
                    stale += 1
                    print(f"FILL_STALE {symbol} rows={result.rows_written} expected_through={args.expected_through} {info}", flush=True)
                else:
                    done += 1
                    print(f"FILL_OK {symbol} rows={result.rows_written} {info}", flush=True)
            except Exception:  # Per-symbol isolation, never print provider/DB exception text.
                failed += 1
                print(f"FILL_FAIL {symbol} context={context}", flush=True)
    except Exception:
        failed = len(symbols) - done - empty - stale
        print(f"FILL_FAIL context={context}", flush=True)
    finally:
        for resource, method, label in ((api, "close", "api_close"), (engine, "dispose", "engine_dispose")):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception:
                    cleanup_failed += 1
                    print(f"FILL_FAIL context={label}", flush=True)
    print(f"FILL_SUMMARY done={done} failed={failed} rows={total_rows} "
          f"empty={empty} stale={stale} cleanup_failed={cleanup_failed}", flush=True)
    return 0 if done == len(symbols) and not (failed or empty or stale or cleanup_failed) else 1


if __name__ == "__main__":
    sys.exit(main())
