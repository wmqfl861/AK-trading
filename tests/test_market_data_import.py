from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ak_trading.market_reference import SHANGHAI_TZ, load_reference_book
from ak_trading.persistence.market_data import (
    MarketDataImportError,
    datetime_from_nano,
    expected_kline_columns,
    iter_tqsdk_kline_csv,
)


ROOT = Path(__file__).resolve().parents[1]
SYMBOL = "KQ.m@SHFE.rb"


def _nano(local_text: str, remainder: int = 123_456_789) -> int:
    local = datetime.fromisoformat(local_text).replace(tzinfo=SHANGHAI_TZ)
    return int(local.timestamp()) * 1_000_000_000 + remainder


def _write_csv(path: Path, rows: list[tuple[str, int]], *, symbol: str = SYMBOL) -> None:
    columns = expected_kline_columns(symbol)
    lines = [",".join(columns)]
    for display, nano in rows:
        lines.append(
            ",".join(
                [
                    display,
                    str(nano),
                    "3100.125",
                    "3102.25",
                    "3099.75",
                    "3101.5",
                    "12",
                    "123.5",
                    "124.5",
                ]
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parser_preserves_nanoseconds_decimals_and_calendar_semantics(tmp_path: Path):
    book = load_reference_book(ROOT / "config" / "market_reference.json")
    path = tmp_path / "bars.csv"
    rows = [
        ("2026-07-28 21:01:00.123456", _nano("2026-07-28 21:01:00")),
        ("2026-07-29 09:01:00.123456", _nano("2026-07-29 09:01:00")),
        ("2027-01-04 09:01:00.123456", _nano("2027-01-04 09:01:00")),
    ]
    _write_csv(path, rows)

    bars = list(
        iter_tqsdk_kline_csv(
            path,
            symbol=SYMBOL,
            duration_seconds=60,
            calendar=book.calendar,
        )
    )

    assert [bar.datetime_nano for bar in bars] == [row[1] for row in rows]
    assert bars[0].open == Decimal("3100.125")
    assert not isinstance(bars[0].open, float)
    assert bars[0].trading_day.isoformat() == "2026-07-29"
    assert bars[0].trading_day_source == "reference_calendar_night"
    assert bars[1].trading_day.isoformat() == "2026-07-29"
    assert bars[2].trading_day is None
    assert bars[2].trading_day_source == "outside_reference_calendar"
    assert datetime_from_nano(rows[0][1]).microsecond == 123_456


def test_parser_keeps_sub_microsecond_datetime_nano_distinct(tmp_path: Path):
    first = _nano("2026-07-29 09:01:00", 123_456_100)
    second = _nano("2026-07-29 09:01:00", 123_456_900)
    path = tmp_path / "bars.csv"
    _write_csv(
        path,
        [
            ("2026-07-29 09:01:00.123456", first),
            ("2026-07-29 09:01:00.123456", second),
        ],
    )

    bars = list(
        iter_tqsdk_kline_csv(path, symbol=SYMBOL, duration_seconds=60)
    )

    assert bars[0].datetime_nano != bars[1].datetime_nano
    assert bars[0].ts == bars[1].ts


def test_parser_does_not_guess_a_configured_closed_day(tmp_path: Path):
    book = load_reference_book(ROOT / "config" / "market_reference.json")
    path = tmp_path / "closed.csv"
    _write_csv(
        path,
        [
            (
                "2026-10-01 09:01:00.123456",
                _nano("2026-10-01 09:01:00"),
            )
        ],
    )

    bar = next(
        iter_tqsdk_kline_csv(
            path,
            symbol=SYMBOL,
            duration_seconds=60,
            calendar=book.calendar,
        )
    )

    assert bar.trading_day is None
    assert bar.trading_day_source == "non_trading_calendar_date"


def test_parser_requires_exact_symbol_prefixed_schema(tmp_path: Path):
    path = tmp_path / "bad.csv"
    path.write_text(
        "datetime,datetime_nano,open,high,low,close,volume,open_oi,close_oi\n",
        encoding="utf-8",
    )

    with pytest.raises(MarketDataImportError, match="exact single-symbol"):
        list(iter_tqsdk_kline_csv(path, symbol=SYMBOL, duration_seconds=60))


def test_parser_rejects_display_time_that_disagrees_with_nano(tmp_path: Path):
    path = tmp_path / "bad-time.csv"
    _write_csv(
        path,
        [("2026-07-29 09:02:00.123456", _nano("2026-07-29 09:01:00"))],
    )

    with pytest.raises(MarketDataImportError, match="authoritative"):
        list(iter_tqsdk_kline_csv(path, symbol=SYMBOL, duration_seconds=60))
