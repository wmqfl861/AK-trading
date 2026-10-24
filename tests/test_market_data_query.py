from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from ak_trading.market_reference import SHANGHAI_TZ
from ak_trading.persistence.market_data import (
    MarketDataImportError,
    iter_tqsdk_kline_csv,
    write_recent_kline_frame_csv,
)
from ak_trading.persistence.query import (
    MarketDataQueryError,
    parse_aware_datetime_nano,
)


SYMBOL = "KQ.m@SHFE.rb"


def _nano(local_text: str, remainder: int = 0) -> int:
    local = datetime.fromisoformat(local_text).replace(tzinfo=SHANGHAI_TZ)
    return int(local.timestamp()) * 1_000_000_000 + remainder


def _frame(nanos: list[int]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": pd.Series(nanos, dtype="int64"),
            "open": [3100.1] * len(nanos),
            "high": [3101.25] * len(nanos),
            "low": [3099.5] * len(nanos),
            "close": [3100.125] * len(nanos),
            "volume": [12.0] * len(nanos),
            "open_oi": [123.5] * len(nanos),
            "close_oi": [124.5] * len(nanos),
        }
    )


def test_parse_aware_datetime_nano_is_integer_exact_and_timezone_normalized():
    local = parse_aware_datetime_nano(
        "2026-07-29T09:01:00.123456+08:00", "--start"
    )
    utc = parse_aware_datetime_nano(
        "2026-07-29T01:01:00.123456+00:00", "--start"
    )

    assert local == utc == _nano("2026-07-29 09:01:00", 123_456_000)


@pytest.mark.parametrize(
    "value, message",
    [
        ("2026-07-29T09:01:00", "timezone"),
        ("2026-07-29T09:01:00.1234567+08:00", "sub-microsecond"),
        ("not-a-date", "ISO datetime"),
    ],
)
def test_parse_aware_datetime_nano_rejects_ambiguous_input(value: str, message: str):
    with pytest.raises(MarketDataQueryError, match=message):
        parse_aware_datetime_nano(value, "--start")


def test_recent_frame_writer_preserves_nano_and_strict_decimal_schema(tmp_path: Path):
    first = _nano("2026-07-29 09:01:00", 123_456_100)
    second = _nano("2026-07-29 09:01:00", 123_456_900)
    frame = _frame([first, second])
    frame.loc[1, "volume"] = float("nan")
    path = tmp_path / "recent.csv"

    result = write_recent_kline_frame_csv(frame, path, symbol=SYMBOL)
    bars = list(iter_tqsdk_kline_csv(path, symbol=SYMBOL, duration_seconds=60))

    assert result.rows == 2
    assert result.first_datetime_nano == first
    assert result.last_datetime_nano == second
    assert [bar.datetime_nano for bar in bars] == [first, second]
    assert bars[0].open == Decimal("3100.1000000000")
    assert bars[0].close == Decimal("3100.1250000000")
    assert bars[1].volume is None


def test_recent_frame_writer_skips_tqsdk_placeholder_rows(tmp_path: Path):
    valid_nano = _nano("2026-07-29 09:01:00")
    frame = _frame([0, valid_nano])
    frame.loc[0, ["open", "high", "low", "close", "volume", "open_oi", "close_oi"]] = float("nan")
    path = tmp_path / "recent.csv"

    result = write_recent_kline_frame_csv(frame, path, symbol=SYMBOL)

    assert result.rows == 1
    assert result.first_datetime_nano == valid_nano


def test_recent_frame_writer_rejects_non_increasing_identity_and_cleans_file(
    tmp_path: Path,
):
    nano = _nano("2026-07-29 09:01:00")
    path = tmp_path / "bad.csv"

    with pytest.raises(MarketDataImportError, match="strictly increasing"):
        write_recent_kline_frame_csv(_frame([nano, nano]), path, symbol=SYMBOL)

    assert not path.exists()
