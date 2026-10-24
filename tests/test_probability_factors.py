from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math

import pytest

from ak_trading.futures.spec import ContractBar
from ak_trading.probability.factors import build_factor_rows, factor_registry


SYMBOL = "SHFE.rb2610"
CONTRACT = "SHFE.rb2610@2026"


def bar(index: int, close: str | int = 100, **changes) -> ContractBar:
    price = Decimal(str(close))
    return replace(
        ContractBar(
            symbol=SYMBOL,
            duration_seconds=86400,
            datetime_nano=1_700_000_000_000_000_000 + index * 86_400_000_000_000,
            ts=datetime(2023, 11, 14, tzinfo=timezone.utc) + timedelta(days=index),
            source="manual",
            open=price,
            high=price + 1,
            low=price - 1,
            close=price,
            volume=Decimal(100),
            close_oi=Decimal(1000),
        ),
        **changes,
    )


def test_registry_declares_versioned_causal_windows():
    registry = factor_registry()
    assert isinstance(registry, tuple)
    assert [(item.name, item.version, item.lookback) for item in registry] == [
        ("close", "1", 1),
        ("prior_high20", "1", 21),
        ("prior_low20", "1", 21),
        ("atr14", "1", 15),
        ("er20", "1", 21),
        ("vol20", "1", 21),
        ("ma20", "1", 20),
        ("ma60", "1", 60),
        ("ma_spread_atr", "1", 60),
        ("zscore20", "1", 21),
        ("distance_high_atr", "1", 21),
        ("distance_low_atr", "1", 21),
        ("volume_ratio20", "1", 21),
        ("oi_change5", "1", 6),
    ]
    assert all(item.unit and item.definition for item in registry)
    assert "heuristic" in next(item.definition for item in registry if item.name == "er20")


def test_first_observation_retains_identity_and_individual_warmup_values():
    first = bar(0)
    rows = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", (first,))
    assert isinstance(rows, tuple) and len(rows) == 1
    row = rows[0]
    assert (row.event_id, row.contract_id, row.symbol, row.product, row.index, row.timestamp_nano) == (
        f"{CONTRACT}|1700000000000000000", CONTRACT, SYMBOL, "SHFE.RB", 0, first.datetime_nano,
    )
    assert row.values == {item.name: 100.0 if item.name == "close" else None for item in factor_registry()}
    assert row.status == "warmup" and "ma60" in row.reason
    assert (row.context, row.regime) == ("unavailable", "unavailable")
    assert build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", ()) == ()


def test_sixty_closes_produce_manual_lagged_factors_and_become_ready():
    bars = tuple(bar(i, 100 + i, volume=Decimal(100 + i), close_oi=Decimal(1000 + 10 * i)) for i in range(60))
    rows = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", bars)
    assert rows[58].status == "warmup"
    row = rows[59]
    assert row.status == "ready" and row.reason
    assert row.values == pytest.approx({
        "close": 159, "prior_high20": 159, "prior_low20": 138,
        "atr14": 2, "er20": 1, "vol20": 0.0002691650930989979,
        "ma20": 149.5, "ma60": 129.5, "ma_spread_atr": 10,
        "zscore20": 1.7748239349298849,
        "distance_high_atr": 0, "distance_low_atr": 10.5,
        "volume_ratio20": 1.0707070707070707, "oi_change5": 0.032467532467532534,
    })
    assert (row.context, row.regime) == ("near_high", "uptrend")


def test_invalid_price_stays_in_place_and_each_window_recovers_independently():
    bars = [bar(i, 100 + i) for i in range(130)]
    bars[65] = replace(bars[65], close=None)
    rows = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", bars)
    assert len(rows) == 130 and [row.index for row in rows] == list(range(130))
    assert rows[65].values["close"] is None
    assert rows[65].values["prior_high20"] == 165
    assert rows[65].status == "unavailable" and rows[65].reason
    assert rows[79].values["atr14"] is None
    assert rows[80].values["atr14"] == 2
    assert rows[84].values["ma20"] is None
    assert rows[85].values["ma20"] == 175.5
    assert rows[85].values["zscore20"] is None
    assert rows[86].values["zscore20"] == pytest.approx(1.7748239349298849)
    assert rows[124].values["ma60"] is None and rows[124].status == "unavailable"
    assert rows[125].values["ma60"] == 195.5 and rows[125].status == "ready"


@pytest.mark.parametrize("close,high,low", [
    (Decimal("NaN"), Decimal(170), Decimal(160)),
    (Decimal("Infinity"), Decimal(170), Decimal(160)),
    (Decimal(165), Decimal(160), Decimal(170)),
    (Decimal(0), Decimal(170), Decimal(160)),
    (Decimal("1e999"), Decimal("1e999"), Decimal("1e999")),
])
def test_invalid_ohlc_never_leaks_nonfinite_numbers(close, high, low):
    bars = [bar(i, 100 + i) for i in range(66)]
    bars[-1] = replace(bars[-1], open=close, close=close, high=high, low=low)
    row = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", bars)[-1]
    assert row.status == "unavailable"
    assert row.values["close"] is None and row.values["atr14"] is None
    assert row.values["prior_high20"] == 165
    assert all(value is None or math.isfinite(value) for value in row.values.values())


def test_missing_invalid_and_zero_volume_oi_are_optional_and_recover():
    bars = [bar(i, 100 + i) for i in range(90)]
    bars[65] = replace(bars[65], volume=Decimal("NaN"), close_oi=Decimal(-1))
    rows = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", bars)
    assert all(row.status == "ready" for row in rows[59:])
    assert rows[65].values["volume_ratio20"] is None and rows[65].values["oi_change5"] is None
    assert rows[70].values["oi_change5"] is None
    assert rows[71].values["oi_change5"] == 0
    assert rows[85].values["volume_ratio20"] is None
    assert rows[86].values["volume_ratio20"] == 1
    missing = tuple(replace(item, volume=None, close_oi=None) for item in bars[:60])
    row = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", missing)[-1]
    assert row.status == "ready" and row.values["volume_ratio20"] is None and row.values["oi_change5"] is None
    zero_now = list(bars[:60])
    zero_now[-1] = replace(zero_now[-1], volume=Decimal(0), close_oi=Decimal(0))
    row = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", zero_now)[-1]
    assert row.status == "ready" and row.values["volume_ratio20"] == 0 and row.values["oi_change5"] == -1
    all_zero = tuple(replace(item, volume=Decimal(0), close_oi=Decimal(0)) for item in bars[:60])
    row = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", all_zero)[-1]
    assert row.status == "ready" and row.values["volume_ratio20"] is None and row.values["oi_change5"] is None


def test_context_uses_nearest_absolute_boundary_with_high_tie_and_breakouts():
    # A wide current range makes both prior boundaries close in ATR units.
    cases = [("100", "near_high"), ("99.9", "near_low"),
             ("100.1", "near_high"), ("101.2", "near_high"),
             ("98.8", "near_low"), ("120", "middle")]
    for close, expected in cases:
        bars = [bar(i, 100, high=Decimal(101), low=Decimal(99)) for i in range(60)]
        bars[-1] = bar(59, close, high=Decimal(128), low=Decimal(72))
        row = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", bars)[-1]
        assert row.context == expected, close
    boundary = [bar(i, 100) for i in range(60)]
    assert build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", boundary)[-1].context == "near_high"


@pytest.mark.parametrize("change", ["symbol", "duplicate", "reversed"])
def test_factor_input_rejects_mixed_contracts_and_nonascending_raw_timestamps(change):
    bars = [bar(0), bar(1)]
    if change == "symbol":
        bars[1] = replace(bars[1], symbol="SHFE.rb2701")
    elif change == "duplicate":
        bars[1] = replace(bars[1], datetime_nano=bars[0].datetime_nano)
    else:
        bars.reverse()
    with pytest.raises(ValueError, match="symbol|ascending"):
        build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", bars)


def test_zero_atr_or_undefined_statistics_block_ready_without_erasing_other_values():
    bars = [bar(i, 100 + i) for i in range(45)]
    bars.extend(bar(i, 200, high=Decimal(200), low=Decimal(200)) for i in range(45, 60))
    row = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", bars)[-1]
    assert row.values["atr14"] == 0 and row.values["er20"] == 1
    assert row.values["ma60"] is not None and row.values["zscore20"] is not None
    assert row.status == "unavailable" and "atr14" in row.reason
    assert row.values["distance_high_atr"] is None and row.values["ma_spread_atr"] is None
    assert row.regime == "uptrend"  # Direction uses raw MA sign, even with zero ATR.
    flat = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", [bar(i) for i in range(60)])[-1]
    assert flat.values["er20"] is None and flat.values["zscore20"] is None and flat.values["vol20"] is None
    assert flat.status == "unavailable" and flat.values["ma60"] == 100
    huge = [bar(i, "1e308", high=Decimal("1e308"), low=Decimal("1e308")) for i in range(60)]
    row = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", huge)[-1]
    assert row.status == "unavailable" and row.values["close"] == 1e308
    assert row.values["ma20"] == 1e308 and row.values["ma60"] == 1e308
    assert all(value is None or math.isfinite(value) for value in row.values.values())


def test_atr_uses_prior_close_gap_and_channels_exclude_current_extremes():
    bars = [bar(i) for i in range(21)]
    bars[-1] = bar(20, 110, high=Decimal(130), low=Decimal(109))
    rows = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", bars)
    assert rows[13].values["atr14"] is None and rows[14].values["atr14"] == 2
    assert rows[20].values["atr14"] == 4  # (13*2 + max(21,30,9))/14
    assert rows[20].values["prior_high20"] == 101 and rows[20].values["prior_low20"] == 99
    assert rows[20].values["distance_high_atr"] == -2.25
    assert rows[20].values["distance_low_atr"] == 2.75


def test_factor_rows_are_prefix_causal_in_values_status_and_metadata():
    bars = tuple(bar(i, 100 + i) for i in range(150))
    rows = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", bars)
    for stop in (1, 15, 21, 59, 60, 75, 150):
        assert build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", bars[:stop]) == rows[:stop]
    revised = bars[:75] + tuple(replace(item, close=None, volume=None) for item in bars[75:])
    assert build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", revised)[:75] == rows[:75]
    # Contract identity has no effect on the causal numeric values.
    other = build_factor_rows("another-instance", SYMBOL, "SHFE.RB", bars[:75])[-1]
    assert other.values == rows[74].values and other.event_id != rows[74].event_id


def test_current_regime_is_an_er_and_ma_heuristic_with_inclusive_thresholds():
    cases = [(0, "range", 0.0), (3, "range", 0.3), (4, "transition", 0.4),
             (5, "uptrend", 0.5), (10, "uptrend", 1.0)]
    for final_up_moves, expected, er in cases:
        # 10 unit down/up pairs end at 100; overwrite the last pairs with
        # consecutive unit gains to set ER to 0, .3, .4, .5, or 1 exactly.
        moves = [-1, 1] * (10 - final_up_moves) + [1] * (2 * final_up_moves)
        closes = [100] * 40
        for move in moves:
            closes.append(closes[-1] + move)
        row = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", [bar(i, price) for i, price in enumerate(closes)])[-1]
        assert row.values["er20"] == er and row.regime == expected
    down = [bar(i, 200 - i) for i in range(60)]
    row = build_factor_rows(CONTRACT, SYMBOL, "SHFE.RB", down)[-1]
    assert row.values["er20"] == 1 and row.regime == "downtrend"
