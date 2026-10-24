from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ak_trading.futures.spec import ContractBar
from ak_trading.probability.contracts import FactorRow, PathScenario
from ak_trading.probability.labels import label_paths


SYMBOL = "SHFE.rb2610"
CONTRACT = "SHFE.rb2610@2026"


def bar(index: int, open: str, high: str, low: str, close: str) -> ContractBar:
    return ContractBar(
        symbol=SYMBOL, duration_seconds=86400,
        datetime_nano=1_700_000_000_000_000_000 + index * 86_400_000_000_000,
        ts=datetime(2023, 11, 14, tzinfo=timezone.utc) + timedelta(days=index),
        source="manual", open=Decimal(open), high=Decimal(high),
        low=Decimal(low), close=Decimal(close),
    )


def observation(index: int = 0, **changes) -> FactorRow:
    nano = 1_700_000_000_000_000_000 + index * 86_400_000_000_000
    return replace(FactorRow(
        event_id=f"{CONTRACT}|{nano}", contract_id=CONTRACT, symbol=SYMBOL,
        product="SHFE.RB", index=index, timestamp_nano=nano,
        values={"close": 100.0, "atr14": 2.0}, status="ready", reason="ready",
        context="middle", regime="downtrend",
    ), **changes)


def test_manual_upper_hit_uses_next_open_and_complete_terminal_path():
    bars = (
        bar(0, "90", "120", "80", "100"),
        bar(1, "110", "113", "109", "112"),
        bar(2, "112", "117", "104", "116"),
        bar(3, "116", "118", "115", "117"),
    )
    result = label_paths(observation(), bars, PathScenario(2, 1.5, 2.0))
    assert (result.event_id, result.contract_id, result.index, result.scenario_id, result.horizon) == (
        observation().event_id, CONTRACT, 0, "h2-u1.5-d2", 2,
    )
    assert result.status == "mature" and result.reason
    assert result.outcome == "up_first" and result.first_hit_step == 1
    assert (result.entry_price, result.upper_price, result.lower_price) == (110, 113, 106)
    assert result.terminal_nano == bars[2].datetime_nano
    assert result.terminal_points == 6 and result.terminal_return == pytest.approx(6 / 110)
    assert result.mfe_points == 7 and result.mae_points == 6
    assert result.terminal_regime == "uptrend"


def test_unready_observations_and_unmatured_tails_never_become_neither():
    bars = (bar(0, "100", "101", "99", "100"), bar(1, "100", "105", "99", "104"))
    for status in ("warmup", "unavailable"):
        result = label_paths(observation(status=status), bars, PathScenario(1, 1, 1))
        assert result.status == "unavailable" and result.outcome is None and result.reason
        assert result.terminal_nano is None and result.terminal_return is None
    for available in (bars[:1], bars):
        result = label_paths(observation(), available, PathScenario(2, 1, 1))
        assert result.status == "unmatured" and result.outcome is None and result.reason
        assert result.terminal_nano is None and result.terminal_points is None
        assert result.first_hit_step is None


def test_invalid_future_bar_after_first_hit_invalidates_entire_label():
    bars = (bar(0, "100", "101", "99", "100"), bar(1, "100", "105", "99", "104"),
            bar(2, "104", "106", "102", "103"))
    for bad_index in (1, 2):
        damaged = list(bars)
        damaged[bad_index] = replace(damaged[bad_index], low=None)
        result = label_paths(observation(), damaged, PathScenario(2, 1, 1))
        assert result.status == "invalid" and result.outcome is None and result.reason
        assert result.first_hit_step is None and result.terminal_points is None
        assert result.mfe_points is None and result.mae_points is None
        assert result.terminal_nano == bars[2].datetime_nano


@pytest.mark.parametrize("opening,expected", [("102", "up_first"), ("103", "up_first"),
                                                ("98", "down_first"), ("97", "down_first"),
                                                ("100", "ambiguous")])
def test_opening_barrier_touch_precedes_intrabar_double_touch(opening, expected):
    bars = (bar(0, "100", "101", "99", "100"), bar(1, "100", "101", "99", "100"),
            bar(2, opening, "105", "95", "100"))
    result = label_paths(observation(), bars, PathScenario(2, 1, 1))
    assert result.status == "mature" and result.outcome == expected
    assert result.first_hit_step == 2
    assert (result.mfe_points, result.mae_points, result.terminal_points) == (5, 5, 0)


def test_unusable_decision_atr_returns_unavailable():
    bars = (bar(0, "100", "101", "99", "100"), bar(1, "100", "105", "99", "104"))
    for atr in (None, 0, -2, float("nan"), float("inf"), True):
        result = label_paths(observation(values={"atr14": atr}), bars, PathScenario(1, 1, 1))
        assert result.status == "unavailable" and "atr" in result.reason
        assert result.outcome is None and result.entry_price is None


def test_path_rejects_contract_crossing_and_misaligned_observation_time():
    bars = (bar(0, "100", "101", "99", "100"), bar(1, "100", "105", "99", "104"),
            bar(2, "104", "106", "102", "103"))
    other_contract = list(bars)
    other_contract[2] = replace(other_contract[2], symbol="SHFE.rb2701")
    duplicate_time = list(bars)
    duplicate_time[2] = replace(duplicate_time[2], datetime_nano=bars[1].datetime_nano)
    for inputs in (other_contract, duplicate_time):
        result = label_paths(observation(), inputs, PathScenario(2, 1, 1))
        assert result.status == "invalid" and result.outcome is None and result.reason
    for event in (observation(timestamp_nano=1), observation(index=-1),
                  observation(index=20), observation(event_id="wrong-contract|123"),
                  observation(symbol="SHFE.rb2701")):
        result = label_paths(event, bars, PathScenario(2, 1, 1))
        assert result.status == "invalid" and result.outcome is None and result.reason


def test_fractional_decimal_barrier_equality_is_a_hit():
    bars = (bar(0, "0.1", "0.2", "0.05", "0.1"), bar(1, "0.1", "0.3", "0.05", "0.2"))
    result = label_paths(observation(values={"atr14": 0.2}), bars, PathScenario(1, 1, 1))
    assert result.status == "mature" and result.outcome == "up_first"
    assert result.upper_price == 0.3 and result.terminal_points == 0.1


def test_nonfinite_or_unrepresentable_path_values_produce_invalid_labels():
    bars = (bar(0, "100", "101", "99", "100"), bar(1, "100", "105", "99", "104"))
    for price in (Decimal("NaN"), Decimal("Infinity"), Decimal("1e999"), Decimal("1e-999")):
        damaged = (bars[0], replace(bars[1], open=price, high=price, low=price, close=price))
        result = label_paths(observation(), damaged, PathScenario(1, 1, 1))
        assert result.status == "invalid" and result.outcome is None
    result = label_paths(observation(values={"atr14": 1e308}), bars, PathScenario(1, 10, 10))
    assert result.status == "invalid" and result.outcome is None


def test_label_depends_only_on_aligned_observation_and_its_horizon():
    bars = (bar(0, "100", "101", "99", "100"), bar(1, "100", "101", "99", "100"),
            bar(2, "100", "102", "99", "101"), bar(3, "101", "103", "100", "102"))
    event = observation(1)
    expected = label_paths(event, bars, PathScenario(2, 1, 1))
    # Full-series validation belongs to the factor builder; unrelated suffix
    # identities and prices must neither alter labels nor add per-event scans.
    suffix = replace(bar(0, "100", "101", "99", "100"), symbol="SHFE.rb2701", close=None)
    assert label_paths(event, bars + (suffix,), PathScenario(2, 1, 1)) == expected
    assert expected.terminal_nano == bars[3].datetime_nano and expected.first_hit_step == 1
