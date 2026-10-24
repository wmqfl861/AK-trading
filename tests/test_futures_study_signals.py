"""Offline regression tests for original-row, observation-only study events."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ak_trading.futures import study_signals
from ak_trading.futures.data import datetime_to_nano
from ak_trading.futures.spec import ClockStatus, ContractBar, StrategyState
from ak_trading.futures.strategies import default_registry
from ak_trading.futures.study_signals import generate_study_signals
from ak_trading.futures.study_spec import StudyContract, StudySignal

START = datetime(2025, 1, 1, 15, tzinfo=timezone.utc)
EXPIRY = datetime(2026, 9, 15, tzinfo=timezone.utc)
IMPORT_CLOCK = datetime(2026, 9, 8, tzinfo=timezone.utc)
STRATEGIES = tuple(sorted(default_registry()))
CARRY = "term_structure_carry"


def contract(
    symbol: str = "SHFE.rb2609", *, count: int = 75,
    expiry: datetime = EXPIRY, product: str = "SHFE.RB",
    scale: str = "1", volume: str = "1000", first: int = 0,
) -> StudyContract:
    bars = []
    for index in range(first, count):
        timestamp = START + timedelta(days=index)
        close = Decimal(100 + index) * Decimal(scale)
        bars.append(ContractBar(
            symbol=symbol,
            duration_seconds=86400,
            datetime_nano=datetime_to_nano(timestamp),
            ts=timestamp,
            source=f"frozen_fixture:{symbol}",
            available_at=IMPORT_CLOCK,
            # Real study rows may lack trading day and session evidence.
            trading_day=None,
            open=close,
            high=close + Decimal("0.25"),
            low=close - Decimal("0.25"),
            close=close,
            volume=Decimal(volume),
        ))
    return StudyContract(
        contract_instance_id=f"{symbol}|{expiry.isoformat()}",
        raw_symbol=symbol,
        product=product,
        expiry_at=expiry,
        bars=tuple(bars),
    )


def far_contract(**kwargs) -> StudyContract:
    options = {
        "symbol": "SHFE.rb2612", "expiry": EXPIRY + timedelta(days=90),
        "scale": "0.98",
    }
    options.update(kwargs)
    return contract(**options)


def event(signals, strategy: str, index: int, near: StudyContract | None = None):
    return next(
        signal for signal in signals
        if signal.strategy_id == strategy and signal.index == index
        and (near is None or signal.contract_instance_id == near.contract_instance_id)
    )


def mutate_suffix(item: StudyContract, after: int, *, scale: str, volume: str):
    return replace(item, bars=tuple(
        row if index <= after else replace(
            row, open=row.open * Decimal(scale), high=row.high * Decimal(scale),
            low=row.low * Decimal(scale), close=row.close * Decimal(scale),
            volume=Decimal(volume),
        ) for index, row in enumerate(item.bars)
    ))


def test_four_events_at_every_original_index_in_deterministic_order():
    first = contract("SHFE.rb2608", count=65)
    second = contract(count=75)
    far = far_contract()
    signals = generate_study_signals((second, first), (far, second, first))
    assert isinstance(signals, tuple)
    assert len(signals) == 4 * (65 + 75)
    assert all(isinstance(signal, StudySignal) for signal in signals)
    keys = [(s.contract_instance_id, s.index, s.strategy_id) for s in signals]
    assert keys == sorted(keys)
    counts = Counter((s.contract_instance_id, s.index) for s in signals)
    assert set(counts.values()) == {4}
    for near in (first, second):
        for index, bar in enumerate(near.bars):
            rows = [s for s in signals if s.contract_instance_id == near.contract_instance_id and s.index == index]
            assert tuple(s.strategy_id for s in rows) == STRATEGIES
            assert all(s.datetime_nano == bar.datetime_nano for s in rows)
    assert signals == generate_study_signals((first, second), (first, second, far))


def test_empty_inputs_have_no_invented_observations():
    assert generate_study_signals((), ()) == ()
    assert generate_study_signals((contract(count=0),), ()) == ()


def test_default_parameters_and_candidate_readiness_are_preserved():
    near = contract()
    signals = generate_study_signals((near,), (far_contract(),))
    for signal in signals:
        assert signal.diagnostics["strategy_parameters"] == dict(default_registry()[signal.strategy_id].spec.parameters)
        assert (signal.feature_status == "ready") == (signal.candidate_direction is not None)
        if signal.candidate_direction is not None:
            assert type(signal.candidate_direction) is int
            assert signal.candidate_direction in (-1, 0, 1)
        else:
            assert signal.feature_status in ("warmup", "unavailable")
        assert signal.diagnostics["target_position"] is None
        assert signal.diagnostics["decision_at"] is None
        assert signal.diagnostics["non_actionable"] is True
        assert signal.diagnostics["scope"] == "independent_candidate_events"
        assert signal.diagnostics["historical_execution_established"] is False
    assert event(signals, "donchian_trend", 39).feature_status == "warmup"
    assert event(signals, "donchian_trend", 40).candidate_direction == 1
    assert event(signals, "ma_trend", 58).feature_status == "warmup"
    assert event(signals, "ma_trend", 59).candidate_direction == 1
    assert event(signals, "zscore_reversion", 60).candidate_direction == 0
    assert event(signals, "zscore_reversion", 60).feature_status == "ready"


@pytest.mark.parametrize("invalid", [None, Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("0"), True])
def test_invalid_original_slot_is_retained_and_ages_out(invalid):
    near = contract(count=172)
    rows = list(near.bars)
    rows[50] = replace(rows[50], close=invalid)
    near = replace(near, bars=tuple(rows))
    signals = generate_study_signals((near,), (far_contract(count=172),))
    assert len(signals) == 4 * 172
    assert event(signals, "donchian_trend", 49).candidate_direction == 1
    for index in (50, 51, 119, 169):
        for strategy in STRATEGIES:
            signal = event(signals, strategy, index)
            assert signal.candidate_direction is None
            assert signal.reason == "invalid_close_window"
            assert signal.datetime_nano == near.bars[index].datetime_nano
    assert event(signals, "ma_trend", 169).diagnostics["window_start_index"] == 50
    recovered = event(signals, "ma_trend", 170)
    assert recovered.diagnostics["window_start_index"] == 51
    assert recovered.diagnostics["window_rows"] == 120
    assert recovered.candidate_direction == 1
    json.dumps([asdict(signal) for signal in signals], allow_nan=False)


def test_invalid_high_low_retains_row_and_disables_channel_candidate():
    near = contract()
    rows = list(near.bars)
    rows[55] = replace(rows[55], high=None)
    signals = generate_study_signals((replace(near, bars=tuple(rows)),), ())
    assert len(signals) == len(rows) * 4
    assert event(signals, "donchian_trend", 55).candidate_direction is None
    assert event(signals, "donchian_trend", 55).reason == "channels_unavailable_invalid_high_low"
    assert event(signals, "ma_trend", 60).candidate_direction == 1


@pytest.mark.parametrize("field", ["high", "low"])
def test_signaling_nan_channels_keep_original_event_slots(field):
    near = contract()
    rows = list(near.bars)
    rows[55] = replace(rows[55], **{field: Decimal("sNaN")})
    result = generate_study_signals((replace(near, bars=tuple(rows)),), ())
    assert len(result) == 4 * len(rows)
    assert event(result, "donchian_trend", 55).candidate_direction is None
    assert event(result, "ma_trend", 60).candidate_direction == 1
    json.dumps([asdict(signal) for signal in result], allow_nan=False)


def test_carry_ranking_uses_arithmetic_median_of_even_volume_window():
    near = contract(count=30)
    far_a = far_contract(expiry=EXPIRY + timedelta(days=80), count=30, scale="0.98")
    far_a = replace(far_a, bars=tuple(
        replace(row, volume=Decimal("1" if index % 2 == 0 else "101"))
        for index, row in enumerate(far_a.bars)
    ))
    far_b = far_contract(symbol="SHFE.rb2701", expiry=EXPIRY + timedelta(days=100),
                         count=30, scale="1.02", volume="60")
    result = generate_study_signals((near,), (far_a, far_b))
    last = event(result, CARRY, 29)
    assert last.diagnostics["pair_state"]["far_symbol"] == far_b.raw_symbol
    assert last.candidate_direction == -1


def test_donchian_events_do_not_maintain_a_prior_position():
    near = contract(count=43)
    rows = list(near.bars)
    for index, close in ((40, "145"), (41, "144")):
        price = Decimal(close)
        rows[index] = replace(rows[index], open=price, close=price, high=price + Decimal("0.25"), low=price - Decimal("0.25"))
    signals = generate_study_signals((replace(near, bars=tuple(rows)),), ())
    assert event(signals, "donchian_trend", 40).candidate_direction == 1
    assert event(signals, "donchian_trend", 41).candidate_direction == 0


def test_reversion_events_do_not_maintain_a_prior_position():
    near = contract(count=42)
    rows = []
    for index, row in enumerate(near.bars):
        price = Decimal(99 if index % 2 == 0 else 101)
        if index == 40:
            price = Decimal("102.5")
        elif index == 41:
            price = Decimal("101.5")
        rows.append(replace(row, open=price, close=price, high=price + Decimal("0.25"), low=price - Decimal("0.25")))
    signals = generate_study_signals((replace(near, bars=tuple(rows)),), ())
    assert event(signals, "zscore_reversion", 40).candidate_direction == -1
    assert event(signals, "zscore_reversion", 41).candidate_direction == 0


def test_later_near_and_far_price_volume_mutations_leave_all_prefix_events_identical():
    near = contract(count=150)
    # Equal distance to 90-day target makes trailing volume decide the pair.
    far_a = far_contract(expiry=EXPIRY + timedelta(days=80), count=150, volume="2000")
    far_b = far_contract(symbol="SHFE.rb2701", expiry=EXPIRY + timedelta(days=100), count=150, scale="1.02", volume="1000")
    cutoff = 85
    baseline = generate_study_signals((near,), (far_a, far_b))
    changed = generate_study_signals(
        (mutate_suffix(near, cutoff, scale="0.8", volume="1"),),
        (mutate_suffix(far_a, cutoff, scale="2", volume="1"),
         mutate_suffix(far_b, cutoff, scale="0.5", volume="999999999")),
    )
    truncated = generate_study_signals(
        (replace(near, bars=near.bars[:cutoff + 1]),),
        (replace(far_a, bars=far_a.bars[:cutoff + 1]), replace(far_b, bars=far_b.bars[:cutoff + 1])),
    )
    prefix = tuple(signal for signal in baseline if signal.index <= cutoff)
    assert prefix == tuple(signal for signal in changed if signal.index <= cutoff)
    assert prefix == truncated
    assert event(baseline, CARRY, cutoff).candidate_direction == 1
    assert event(baseline, CARRY, cutoff).diagnostics["pair_state"]["far_symbol"] == far_a.raw_symbol
    assert event(changed, CARRY, 149).diagnostics["pair_state"]["far_symbol"] == far_b.raw_symbol


def test_pair_confirmation_is_independent_for_each_near_instance():
    near_a = contract("SHFE.rb2608", count=25)
    near_b = contract("SHFE.rb2609", count=25)
    far = far_contract(count=25, first=20)
    signals = generate_study_signals((near_b, near_a), (far,))
    for near in (near_a, near_b):
        first = event(signals, CARRY, 20, near)
        confirmed = event(signals, CARRY, 21, near)
        assert first.candidate_direction is None
        assert first.reason == "awaiting_second_confirmation"
        assert first.diagnostics["pair_state"]["confirmation_count"] == 1
        assert confirmed.candidate_direction == 1
        assert confirmed.diagnostics["pair_state"]["far_symbol"] == far.raw_symbol
        assert tuple(s for s in signals if s.contract_instance_id == near.contract_instance_id) == generate_study_signals((near,), (far,))


@pytest.mark.parametrize("invalid_close", [False, True])
def test_context_windows_preserve_clocks_and_only_carry_receives_pair_state(monkeypatch, invalid_close):
    near = contract(count=130)
    if invalid_close:
        rows = list(near.bars)
        rows[110] = replace(rows[110], close=None)
        near = replace(near, bars=tuple(rows))
    far = far_contract(count=140)
    seen = Counter()
    registry = default_registry()

    class RecordingStrategy:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.spec = wrapped.spec

        def evaluate(self, context, previous):
            index = (context.bars[-1].ts - START).days
            seen[self.spec.strategy_id] += 1
            assert context.decision_at is None
            assert context.clock_status is ClockStatus.OBSERVATION_ONLY
            assert context.bars == near.bars[max(0, index - 119):index + 1]
            assert all(row.available_at == IMPORT_CLOCK for row in context.bars)
            assert all(row.trading_day is None for row in context.bars)
            if self.spec.strategy_id != CARRY:
                assert previous is None
                assert context.pair_bars == {}
            else:
                if index == 0:
                    assert previous is None
                else:
                    assert isinstance(previous, StrategyState)
                    assert previous.direction == 0
                    assert previous.signal_age is None
                prefix = context.pair_bars[far.raw_symbol]
                assert prefix == far.bars[max(0, index - 119):index + 1]
                assert prefix[-1].datetime_nano == context.bars[-1].datetime_nano
            result = self.wrapped.evaluate(context, previous)
            assert result.target_position is None
            return result

    monkeypatch.setattr(study_signals, "default_registry", lambda: {
        key: RecordingStrategy(strategy) for key, strategy in registry.items()
    })
    generate_study_signals((near,), (far,))
    assert seen == Counter({strategy: 130 for strategy in STRATEGIES})


@pytest.mark.parametrize("missing_leg", ["near", "far"])
def test_missing_availability_is_disclosed_and_never_fabricated(missing_leg):
    near, far = contract(), far_contract()
    if missing_leg == "near":
        near = replace(near, bars=tuple(replace(row, available_at=None) for row in near.bars))
    else:
        far = replace(far, bars=tuple(replace(row, available_at=None) for row in far.bars))
    signals = generate_study_signals((near,), (far,))
    signal = event(signals, CARRY, 60)
    assert signal.candidate_direction is None
    assert signal.feature_status == "unavailable"
    reference = (
        signal.diagnostics["observation_reference"] if missing_leg == "near"
        else signal.diagnostics["far_prefixes"][far.raw_symbol]["last_observation"]
    )
    assert reference["available_at"] is None
    assert "available_at" in reference["missing_timing_fields"]
    assert reference["availability_quality"] == "missing"
    assert event(signals, "ma_trend", 60).candidate_direction == 1


def test_raw_timestamp_pairing_is_only_an_observation_reference():
    near, far = contract(), far_contract()
    far = replace(far, bars=tuple(replace(row, available_at=IMPORT_CLOCK + timedelta(days=1)) for row in far.bars))
    signal = event(generate_study_signals((near,), (far,)), CARRY, 60)
    assert signal.candidate_direction == 1
    assert signal.diagnostics["pair_alignment"] == "raw_timestamp_reference_only_not_verified_session"
    assert signal.diagnostics["expiry_quality"] == "reference_only"
    assert signal.diagnostics["reference_expiry_at"] == EXPIRY.isoformat()
    reference = signal.diagnostics["observation_reference"]
    assert reference["available_at"] == IMPORT_CLOCK.isoformat()
    assert reference["trading_day"] is None
    assert reference["trading_day_quality"] == "missing"
    assert set(reference["missing_timing_fields"]) == {"trading_day", "trading_day_source", "session_open", "session_close"}
    assert signal.diagnostics["far_prefixes"][far.raw_symbol]["last_observation"]["available_at"] == (IMPORT_CLOCK + timedelta(days=1)).isoformat()
    json.dumps(asdict(signal), allow_nan=False)


def test_far_missing_current_timestamp_is_not_filled_from_past_or_future():
    near = contract(count=70)
    far = far_contract(count=70)
    far = replace(far, bars=far.bars[:60] + far.bars[61:])
    signals = generate_study_signals((near,), (far,))
    signal = event(signals, CARRY, 60)
    assert signal.candidate_direction is None
    assert signal.feature_status == "unavailable"
    prefix = signal.diagnostics["far_prefixes"][far.raw_symbol]
    assert prefix["raw_timestamp_matches"] is False
    assert prefix["last_observation"]["datetime_nano"] == near.bars[59].datetime_nano
    assert event(signals, CARRY, 61).candidate_direction == 1


def test_far_starting_after_near_history_cannot_supply_prices_or_volume():
    near = contract(count=65)
    future_far = far_contract(count=80, first=65, volume="999999999")
    signals = generate_study_signals((near,), (future_far,))
    for signal in signals:
        if signal.strategy_id == CARRY:
            assert signal.candidate_direction is None
            assert signal.diagnostics["far_prefixes"] == {}
            assert signal.diagnostics["pair_state"]["far_symbol"] is None


@pytest.mark.parametrize("gap,ready", [(59, False), (60, True), (180, True), (181, False), (-90, False)])
def test_far_expiry_gap_eligibility(gap, ready):
    near = contract(count=25)
    far = far_contract(count=25, expiry=EXPIRY + timedelta(days=gap))
    signal = event(generate_study_signals((near,), (far,)), CARRY, 24)
    assert (signal.feature_status == "ready") is ready
    assert (signal.candidate_direction == 1) is ready


def test_far_pair_eligibility_excludes_other_products_and_same_symbol():
    near = contract()
    other_product = far_contract(symbol="DCE.m2612", product="DCE.M")
    same_symbol = far_contract(symbol=near.raw_symbol)
    signal = event(generate_study_signals((near,), (other_product, same_symbol)), CARRY, 60)
    assert signal.candidate_direction is None
    assert signal.reason == "no_candidate_pairs"


def test_expiry_excludes_observation_at_boundary_and_later_without_dropping_rows():
    near = contract(count=65, expiry=START + timedelta(days=60))
    far = far_contract(count=65, expiry=near.expiry_at + timedelta(days=90))
    signals = generate_study_signals((near,), (far,))
    assert len(signals) == 4 * 65
    assert event(signals, "ma_trend", 59).candidate_direction == 1
    for signal in signals:
        if signal.index >= 60:
            assert signal.candidate_direction is None
            assert signal.feature_status == "unavailable"
            assert signal.reason == "observation_at_or_after_reference_expiry"
            assert signal.diagnostics["observation_eligible"] is False
            assert signal.diagnostics["target_position"] is None


@pytest.mark.parametrize("candidate", [True, False, 2, -2, "1", 1.0, Decimal(1), float("nan"), None])
def test_non_integer_or_out_of_range_diagnostic_candidates_are_unavailable(monkeypatch, candidate):
    registry = default_registry()

    class CandidateOverride:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.spec = wrapped.spec

        def evaluate(self, context, previous):
            result = self.wrapped.evaluate(context, previous)
            return replace(result, diagnostics={"candidate_direction": candidate})

    monkeypatch.setattr(study_signals, "default_registry", lambda: {
        key: CandidateOverride(strategy) for key, strategy in registry.items()
    })
    signals = generate_study_signals((contract(count=65),), ())
    assert all(signal.candidate_direction is None for signal in signals)
    assert all(signal.feature_status in ("warmup", "unavailable") for signal in signals)
    json.dumps([asdict(signal) for signal in signals], allow_nan=False)
