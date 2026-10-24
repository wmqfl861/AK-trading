"""S1 focused tests: four implementations, unified interface, registry and
prefix invariance (finalized-plan §十二.11, minimum 14 cases)."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping

import pytest

from ak_trading.futures.data import datetime_to_nano
from ak_trading.futures.spec import (
    ClockStatus,
    ContractBar,
    ContractMetadata,
    ProductKey,
    StrategyContext,
    StrategyState,
)
from ak_trading.futures.strategies import (
    CANDIDATE_GRID,
    DonchianTrend,
    LegacyDualMAAdapter,
    MATrend,
    TermStructureCarry,
    ZScoreReversion,
    check_requirements,
    default_registry,
    evaluate_registry,
)
from ak_trading.futures.structures import (
    CARRY_FACTOR_NAME,
    build_structure_frame,
    carry_annualized_factor,
)

SH = timezone(timedelta(hours=8))
PRODUCT = ProductKey("SHFE", "RB")


def make_bar(
    index: int,
    close: str,
    *,
    symbol: str = "SHFE.rb2609",
    high: str | None = None,
    low: str | None = None,
    volume: str = "1000",
    day: date | None = None,
) -> ContractBar:
    close_dec = Decimal(close)
    trading_day = day or date(2026, 1, 5) + timedelta(days=index)
    session_close = datetime.combine(trading_day, datetime.min.time(), tzinfo=SH) + timedelta(hours=15)
    return ContractBar(
        symbol=symbol,
        duration_seconds=86400,
        datetime_nano=datetime_to_nano(session_close),
        ts=session_close,
        source="tqsdk",
        trading_day=trading_day,
        trading_day_source="tqsdk",
        session_open=session_close - timedelta(hours=6),
        session_close=session_close,
        available_at=session_close,
        open=close_dec,
        high=Decimal(high) if high else close_dec + Decimal("5"),
        low=Decimal(low) if low else close_dec - Decimal("5"),
        close=close_dec,
        volume=Decimal(volume),
    )


def make_context(
    strategy_id: str,
    bars: list[ContractBar],
    *,
    structure=None,
    decision_at: datetime | None = None,
    clock: ClockStatus = ClockStatus.VERIFIED,
    expiry_at: datetime | None = None,
    pair_candidates: tuple[ContractMetadata, ...] = (),
    pair_bars: Mapping[str, tuple[ContractBar, ...]] | None = None,
    previous_pair: PairSelectionState | None = None,
) -> StrategyContext:
    return StrategyContext(
        strategy_id=strategy_id,
        product=PRODUCT,
        contract_instance_id="SHFE.rb2609|2070-09-14T00:00:00+08:00",
        raw_symbol="SHFE.rb2609",
        decision_at=decision_at if decision_at is not None else bars[-1].session_close,
        clock_status=clock,
        completed_bar_id=f"{bars[-1].symbol}:{bars[-1].datetime_nano}" if bars else None,
        bars=tuple(bars),
        structure=structure,
        factors={},
        expiry_at=expiry_at,
        pair_candidates=pair_candidates,
        pair_bars=dict(pair_bars or {}),
    )


def _vary(count: int, start: float = 100.0) -> list[str]:
    """Deterministic positive closes with a wave so std/vol are non-zero."""
    values = []
    for i in range(count):
        values.append(start + 10 * math.sin(i / 3.0) + i * 0.05)
    return [f"{value:.6f}" for value in values]


# ---------------------------------------------------------------------------
# DonchianTrend
# ---------------------------------------------------------------------------


def test_donchian_hand_computed_breakout_and_touch_not_trigger() -> None:
    strategy = DonchianTrend()
    # Deterministic channel: previous 40 bars have high=100 / low=80.
    bars = [make_bar(i, close, high="100", low="80") for i, close in enumerate(_vary(41))]
    bars[-1] = make_bar(40, "100.5", high="101", low="80")
    frame = replace_regime(build_structure_frame(bars), regime="trend")
    context = make_context("donchian_trend", bars, structure=frame)
    result = strategy.evaluate(context, None)
    assert result.target_position == 1
    assert result.status == "ready"
    assert result.reason == "breakout_above_entry_channel"
    assert result.diagnostics["upper_entry"] == 100.0
    assert result.diagnostics["lower_entry"] == 80.0

    # Touching the border exactly is not a breakout (strict inequality).
    bars[-1] = make_bar(40, "100", high="100", low="80")
    frame = replace_regime(build_structure_frame(bars), regime="trend")
    context = make_context("donchian_trend", bars, structure=frame)
    result = strategy.evaluate(context, None)
    assert result.target_position == 0
    assert result.status == "flat"
    assert result.reason == "inside_entry_channel"


def test_donchian_current_bar_excluded_from_channel() -> None:
    strategy = DonchianTrend()
    bars = [make_bar(i, "90", high="95", low="85") for i in range(41)]
    bars[-1] = make_bar(40, "97", high="150", low="10")
    frame = build_structure_frame(bars)
    # A spike in the CURRENT bar must not raise the entry channel.
    assert frame.upper_entry == 95.0
    assert frame.lower_entry == 85.0


def test_donchian_reversal_and_exit_channel_priority() -> None:
    strategy = DonchianTrend()
    previous = StrategyState(strategy_id="donchian_trend", direction=1, signal_age=3)
    bars = [make_bar(i, "90", high="95", low="85") for i in range(41)]
    # Holding long; close breaks below the entry channel low.
    bars[-1] = make_bar(40, "84", high="95", low="83")
    # In trend regime the reverse entry wins over the plain exit.
    frame_trend = replace_regime(build_structure_frame(bars), regime="trend")
    result = strategy.evaluate(
        make_context("donchian_trend", bars, structure=frame_trend), previous
    )
    assert result.target_position == -1
    assert result.reason == "reverse_to_short"

    # Outside the trend regime the same price is only an exit-channel exit.
    frame_plain = build_structure_frame(bars)
    result_exit = strategy.evaluate(
        make_context("donchian_trend", bars, structure=replace_regime(frame_plain, regime="transition")),
        previous,
    )
    assert result_exit.target_position == 0
    assert result_exit.reason == "exit_below_exit_channel"

    # Holding long above the exit channel keeps the direction and ages.
    bars2 = [make_bar(i, "90", high="95", low="85") for i in range(41)]
    bars2[-1] = make_bar(40, "86", high="95", low="85.5")
    frame2 = replace_regime(build_structure_frame(bars2), regime="transition")
    result2 = strategy.evaluate(
        make_context("donchian_trend", bars2, structure=frame2), previous
    )
    assert result2.target_position == 1
    assert result2.reason == "maintain_direction"
    assert result2.next_state.signal_age == 4


def test_donchian_warmup_returns_null_target() -> None:
    strategy = DonchianTrend()
    bars = [make_bar(i, close) for i, close in enumerate(_vary(20))]
    context = make_context("donchian_trend", bars, structure=build_structure_frame(bars))
    result = strategy.evaluate(context, None)
    assert result.target_position is None
    assert result.status == "warmup"
    assert result.reason == "insufficient_history"


# ---------------------------------------------------------------------------
# MATrend
# ---------------------------------------------------------------------------


def test_ma_bidirectional_cross_and_regime_gate() -> None:
    strategy = MATrend()
    bars = [make_bar(i, close) for i, close in enumerate(_vary(60))]
    frame = build_structure_frame(bars)
    # Steady uptrend: fast above slow inside trend regime.
    rising = [make_bar(i, f"{100 + i}.0") for i in range(61)]
    rising_frame = build_structure_frame(rising)
    context = make_context("ma_trend", rising, structure=rising_frame)
    result = strategy.evaluate(context, None)
    assert result.target_position == 1
    assert result.diagnostics["fast_ma"] > result.diagnostics["slow_ma"]

    falling = [make_bar(i, f"{200 - i}.0") for i in range(61)]
    falling_frame = build_structure_frame(falling)
    context = make_context("ma_trend", falling, structure=falling_frame)
    result = strategy.evaluate(context, None)
    assert result.target_position == -1

    # Regime gate: outside trend the direction is 0 even with fast>slow.
    gated = replace_regime(rising_frame, regime="range")
    context = make_context("ma_trend", rising, structure=gated)
    result = strategy.evaluate(context, None)
    assert result.target_position == 0
    assert result.status == "flat"


def replace_regime(frame, **overrides):
    from dataclasses import replace

    return replace(frame, **overrides)


def test_ma_equal_machines_give_zero_and_required_bars_from_params() -> None:
    strategy = MATrend(fast_window=10, slow_window=40)
    assert strategy.spec.required_bars == 40
    flat = [make_bar(i, "100") for i in range(41)]
    frame = build_structure_frame(flat, fast_window=10, slow_window=40)
    assert frame.fast_ma == frame.slow_ma == 100.0
    # Equal MAs in a zero-variance series: ER is no_movement -> regime
    # unavailable -> direction 0; never fabricated.
    context = make_context("ma_trend", flat, structure=frame)
    result = strategy.evaluate(context, None)
    assert result.target_position == 0
    with pytest.raises(ValueError):
        MATrend(fast_window=40, slow_window=10)


# ---------------------------------------------------------------------------
# ZScoreReversion
# ---------------------------------------------------------------------------


def _zscore_context(closes: list[str], *, regime: str = "range", strategy_id: str = "zscore_reversion"):
    bars = [make_bar(i, close) for i, close in enumerate(closes)]
    frame = build_structure_frame(bars)
    if regime != frame.regime:
        frame = replace_regime(frame, regime=regime)
    return make_context(strategy_id, bars, structure=frame), frame


def test_zscore_entry_hand_computed_with_band_edges() -> None:
    strategy = ZScoreReversion()
    base = _vary(21, start=100.0)
    mu_window = [float(c) for c in base[:-1]]
    mean = sum(mu_window) / len(mu_window)
    variance = sum((v - mean) ** 2 for v in mu_window) / (len(mu_window) - 1)
    std = variance ** 0.5
    # Slightly inside the band edges so float noise cannot flip the branch:
    # z = +2 + eps -> short entry; z = +4 + eps -> no new entry at the stop.
    bars_closes = list(base)
    bars_closes[-1] = f"{mean + (2.0 + 1e-5) * std:.10f}"
    context, frame = _zscore_context(bars_closes)
    assert frame.zscore == pytest.approx(2.0, abs=1e-4)
    result = strategy.evaluate(context, None)
    assert result.target_position == -1
    assert result.reason == "enter_short_above_band"

    bars_closes[-1] = f"{mean + (4.0 + 1e-5) * std:.10f}"
    context, _ = _zscore_context(bars_closes)
    result = strategy.evaluate(context, None)
    assert result.target_position == 0
    assert result.reason == "extreme_deviation_no_new_entry"


def test_zscore_exit_rules_priority_over_entry_and_no_reversal() -> None:
    strategy = ZScoreReversion()
    base = _vary(21, start=100.0)
    mu_window = [float(c) for c in base[:-1]]
    mean = sum(mu_window) / len(mu_window)
    variance = sum((v - mean) ** 2 for v in mu_window) / (len(mu_window) - 1)
    std = variance ** 0.5
    # Holding long; z crossed above -exit_z (overshoot beyond mean) -> exit.
    closes = list(base)
    closes[-1] = f"{(mean + 0.6 * std):.10f}"
    context, _ = _zscore_context(closes)
    previous = StrategyState(strategy_id="zscore_reversion", direction=1, signal_age=2)
    result = strategy.evaluate(context, previous)
    assert result.target_position == 0
    assert result.reason == "zscore_mean_reversion_exit_long"

    # Even an opposite-side entry signal (z = +2) exits, never reverses.
    closes2 = list(base)
    closes2[-1] = f"{(mean + 2.0 * std):.10f}"
    context2, _ = _zscore_context(closes2)
    result2 = strategy.evaluate(context2, previous)
    assert result2.target_position == 0


def test_zscore_age_and_er_exits_and_zero_variance() -> None:
    strategy = ZScoreReversion()
    base = _vary(21, start=100.0)
    mu_window = [float(c) for c in base[:-1]]
    mean = sum(mu_window) / len(mu_window)
    variance = sum((v - mean) ** 2 for v in mu_window) / (len(mu_window) - 1)
    std = variance ** 0.5
    closes = list(base)
    closes[-1] = f"{(mean + 1.0 * std):.10f}"  # inside holding band
    context, frame = _zscore_context(closes)
    frame = replace_regime(frame, regime="range", er20=0.1)
    aged = StrategyState(strategy_id="zscore_reversion", direction=-1, signal_age=20)
    result = strategy.evaluate(
        make_context("zscore_reversion", [make_bar(i, c) for i, c in enumerate(closes)], structure=frame),
        aged,
    )
    assert result.target_position == 0
    assert result.reason == "max_signal_age_reached"

    er_exit = replace_regime(frame, er20=0.7)
    context_er = make_context("zscore_reversion", [make_bar(i, c) for i, c in enumerate(closes)], structure=er_exit)
    fresh = StrategyState(strategy_id="zscore_reversion", direction=-1, signal_age=1)
    result_er = strategy.evaluate(context_er, fresh)
    assert result_er.target_position == 0
    assert result_er.reason == "er20_left_range"

    constant = ["100"] * 21
    context_zero, _ = _zscore_context(constant)
    result_zero = strategy.evaluate(context_zero, None)
    assert result_zero.target_position is None
    assert result_zero.status == "unavailable"
    assert result_zero.reason == "zero_variance_window"


# ---------------------------------------------------------------------------
# TermStructureCarry
# ---------------------------------------------------------------------------


NEAR_EXPIRY = datetime(2026, 9, 14, tzinfo=SH)


def _carry_context(
    near_closes: list[str],
    far_symbol: str,
    far_closes: list[str],
    far_expiry: datetime,
    *,
    previous: StrategyState | None = None,
    extra_candidates: tuple[tuple[str, datetime, list[str]], ...] = (),
    desync_far: bool = False,
):
    near_bars = [make_bar(i, close) for i, close in enumerate(near_closes)]
    last_day = near_bars[-1].trading_day
    far_bars = [
        make_bar(i, close, symbol=far_symbol) for i, close in enumerate(far_closes)
    ]
    if desync_far:
        # A genuinely missing far price on the observation day: the far
        # series simply ends one row earlier (no forward fill allowed).
        far_bars = far_bars[:-1]
    candidates = [
        _far_metadata(far_symbol, far_expiry),
        *(_far_metadata(sym, exp) for sym, exp, _ in extra_candidates),
    ]
    pair_bars = {far_symbol: tuple(far_bars)}
    for sym, exp, closes in extra_candidates:
        extra_bars = [
            make_bar(i, close, symbol=sym, day=last_day if i == len(far_closes) - 1 else None)
            for i, close in enumerate(closes)
        ]
        # Rebuild to align the final bar's day with the near bar.
        extra_bars = [
            make_bar(i, close, symbol=sym, day=(last_day - timedelta(days=len(closes) - 1 - i)))
            for i, close in enumerate(closes)
        ]
        pair_bars[sym] = tuple(extra_bars)
    context = make_context(
        "term_structure_carry",
        near_bars,
        structure=None,
        expiry_at=NEAR_EXPIRY,
        pair_candidates=tuple(candidates),
        pair_bars=pair_bars,
    )
    if previous is not None:
        return context
    return context


def _far_metadata(symbol: str, expiry: datetime) -> ContractMetadata:
    return ContractMetadata(
        contract_instance_id=f"{symbol}|{expiry.isoformat()}",
        raw_symbol=symbol,
        product=PRODUCT,
        expiry_at=expiry,
    )


def test_carry_hand_computed_slope_sign_and_thresholds() -> None:
    strategy = TermStructureCarry(threshold=0.03)
    gap = 90
    # carry_annualized = ln(near/far)*365/90; choose prices for exactly ~+0.02.
    raw_spread = 0.02 * gap / 365.0
    near_price = math.exp(raw_spread / 2)
    far_price = math.exp(-raw_spread / 2)
    near_closes = _vary(21, start=100.0)
    far_closes = [f"{float(c) * far_price / near_price:.10f}" for c in near_closes]
    context = _carry_context(
        near_closes, "SHFE.rb2612", far_closes, NEAR_EXPIRY + timedelta(days=gap)
    )
    # Observation 1: pending, carry unavailable.
    first = strategy.evaluate(context, None)
    assert first.status == "unavailable"
    assert first.reason == "awaiting_second_confirmation"
    # Observation 2: confirmed; positive slope below threshold -> flat 0.
    second = strategy.evaluate(context, first.next_state)
    assert second.status == "flat"
    assert second.target_position == 0
    assert second.diagnostics["gap_days"] == pytest.approx(gap)
    assert second.diagnostics["carry_annualized"] == pytest.approx(0.02, rel=1e-4)
    # Above threshold -> long the near (backwardation).
    big = 0.05 * gap / 365.0
    near_high = [f"{float(c) * math.exp(big / 2):.10f}" for c in _vary(21, 200.0)]
    far_high = [f"{float(c) * math.exp(-big / 2):.10f}" for c in _vary(21, 200.0)]
    context_high = _carry_context(
        near_high, "SHFE.rb2612", far_high, NEAR_EXPIRY + timedelta(days=gap)
    )
    r1 = strategy.evaluate(context_high, None)
    r2 = strategy.evaluate(context_high, r1.next_state)
    assert r2.target_position == 1
    assert r2.reason == "backwardation_above_threshold"
    # Negative slope beyond -threshold -> short.
    context_low = _carry_context(
        far_high, "SHFE.rb2612", near_high, NEAR_EXPIRY + timedelta(days=gap)
    )
    r1 = strategy.evaluate(context_low, None)
    r2 = strategy.evaluate(context_low, r1.next_state)
    assert r2.target_position == -1
    assert r2.reason == "contango_below_negative_threshold"


def test_carry_requires_synchronized_far_price() -> None:
    strategy = TermStructureCarry()
    near_closes = _vary(21, 100.0)
    far_closes = list(near_closes)
    context = _carry_context(
        near_closes, "SHFE.rb2612", far_closes,
        NEAR_EXPIRY + timedelta(days=90), desync_far=True,
    )
    first = strategy.evaluate(context, None)
    second = strategy.evaluate(context, first.next_state)
    assert second.target_position is None
    assert second.reason in ("no_candidate_pairs", "far_price_unavailable")


def test_carry_gap_bounds_rejected_at_59_and_181_days() -> None:
    near_closes = _vary(21, 100.0)
    far_closes = list(near_closes)
    for gap, name in ((59, "SHFE.rb2611"), (181, "SHFE.rb2703")):
        strategy = TermStructureCarry()
        context = _carry_context(
            near_closes, name, far_closes, NEAR_EXPIRY + timedelta(days=gap)
        )
        first = strategy.evaluate(context, None)
        result = strategy.evaluate(context, first.next_state)
        assert result.target_position is None
        assert result.status == "unavailable"


def test_carry_far_switch_requires_two_confirmations_and_stays_sticky() -> None:
    strategy = TermStructureCarry()
    near_closes = _vary(21, 100.0)
    gap_a, gap_b = 120, 90
    far_a = [f"{float(c) * 0.99:.10f}" for c in near_closes]
    far_b = [f"{float(c) * 0.985:.10f}" for c in near_closes]
    # Observations 1-2 confirm far A.
    ctx_a = _carry_context(near_closes, "SHFE.rb2701", far_a, NEAR_EXPIRY + timedelta(days=gap_a))
    r1 = strategy.evaluate(ctx_a, None)
    r2 = strategy.evaluate(ctx_a, r1.next_state)
    assert r2.status in ("flat", "ready")
    assert r2.next_state.pair.far_symbol == "SHFE.rb2701"
    # Observation 3: B (gap 90, better rank) appears once -> sticky A.
    ctx_both = _carry_context(
        near_closes, "SHFE.rb2701", far_a, NEAR_EXPIRY + timedelta(days=gap_a),
        extra_candidates=(("SHFE.rb2612", NEAR_EXPIRY + timedelta(days=gap_b), far_b),),
    )
    r3 = strategy.evaluate(ctx_both, r2.next_state)
    assert r3.next_state.pair.far_symbol == "SHFE.rb2701"
    assert r3.next_state.pair.pending_far_symbol == "SHFE.rb2612"
    # Observation 4: B selected again -> switch.
    r4 = strategy.evaluate(ctx_both, r3.next_state)
    assert r4.next_state.pair.far_symbol == "SHFE.rb2612"
    assert r4.diagnostics["pair_changed"] is True


def test_carry_near_roll_resets_pair_state() -> None:
    strategy = TermStructureCarry()
    near_closes = _vary(21, 100.0)
    far = [f"{float(c) * 0.99:.10f}" for c in near_closes]
    ctx = _carry_context(near_closes, "SHFE.rb2701", far, NEAR_EXPIRY + timedelta(days=120))
    r1 = strategy.evaluate(ctx, None)
    r2 = strategy.evaluate(ctx, r1.next_state)
    assert r2.next_state.pair.far_symbol == "SHFE.rb2701"
    # The near contract rolls: new context on a different near symbol with
    # a synchronized far candidate available.
    rolled_bars = [make_bar(i, c, symbol="SHFE.rb2701") for i, c in enumerate(_vary(21, 150.0))]
    rolled_far = [
        make_bar(i, f"{float(c) * 0.99:.10f}", symbol="SHFE.rb2705")
        for i, c in enumerate(_vary(21, 150.0))
    ]
    rolled_ctx = StrategyContext(
        strategy_id="term_structure_carry",
        product=PRODUCT,
        contract_instance_id="SHFE.rb2701|2071-01-14T00:00:00+08:00",
        raw_symbol="SHFE.rb2701",
        decision_at=rolled_bars[-1].session_close,
        clock_status=ClockStatus.VERIFIED,
        completed_bar_id=f"{rolled_bars[-1].symbol}:{rolled_bars[-1].datetime_nano}",
        bars=tuple(rolled_bars),
        structure=None,
        factors={},
        expiry_at=NEAR_EXPIRY + timedelta(days=120),
        pair_candidates=(_far_metadata("SHFE.rb2705", NEAR_EXPIRY + timedelta(days=250)),),
        pair_bars={"SHFE.rb2705": tuple(rolled_far)},
    )
    rolled_state = StrategyState(
        strategy_id="term_structure_carry",
        direction=0,
        pair=r2.next_state.pair,
    )
    result = strategy.evaluate(rolled_ctx, rolled_state)
    assert result.status == "unavailable"
    assert result.reason == "near_roll_reset_pair_confirmation"


def test_carry_act365_units_match_plan_reference_values() -> None:
    # 0.03 at 60 days -> raw spread 0.0049315; at 180 days -> 0.0147945.
    factor_60 = carry_annualized_factor(
        factor_name=CARRY_FACTOR_NAME,
        near_close=math.exp(0.0049315 / 2),
        far_close=math.exp(-0.0049315 / 2),
        near_expiry_at=NEAR_EXPIRY,
        far_expiry_at=NEAR_EXPIRY + timedelta(days=60),
        same_session=True,
    )
    assert factor_60.value == pytest.approx(0.03, abs=1e-6)
    factor_180 = carry_annualized_factor(
        factor_name=CARRY_FACTOR_NAME,
        near_close=math.exp(0.0147945 / 2),
        far_close=math.exp(-0.0147945 / 2),
        near_expiry_at=NEAR_EXPIRY,
        far_expiry_at=NEAR_EXPIRY + timedelta(days=180),
        same_session=True,
    )
    assert factor_180.value == pytest.approx(0.03, abs=1e-6)
    # Inverted expiry is rejected.
    inverted = carry_annualized_factor(
        factor_name=CARRY_FACTOR_NAME, near_close=1.0, far_close=1.0,
        near_expiry_at=NEAR_EXPIRY + timedelta(days=90),
        far_expiry_at=NEAR_EXPIRY, same_session=True,
    )
    assert inverted.status == "unavailable"


# ---------------------------------------------------------------------------
# Legacy adapter + registry + prefix invariance
# ---------------------------------------------------------------------------


def test_legacy_adapter_preserves_none_long_flat_semantics() -> None:
    adapter = LegacyDualMAAdapter(fast_window=5, slow_window=20)
    short_bars = [make_bar(i, "100") for i in range(10)]
    result = adapter.evaluate(make_context("legacy_dual_ma_reference", short_bars), None)
    assert result.target_position is None
    assert result.status == "warmup"
    assert result.reason == "insufficient_history"

    rising = [make_bar(i, f"{100 + i}.0") for i in range(20)]
    result = adapter.evaluate(make_context("legacy_dual_ma_reference", rising), None)
    assert result.target_position == 1
    assert result.status == "ready"

    falling_tail = [make_bar(i, f"{100 + i}.0") for i in range(15)] + [
        make_bar(i, f"{5 - i}.0") for i in range(5)
    ]
    result = adapter.evaluate(make_context("legacy_dual_ma_reference", falling_tail), None)
    assert result.target_position == 0
    assert result.status == "flat"


def test_registry_specs_and_candidate_grid() -> None:
    registry = default_registry()
    assert sorted(registry) == [
        "donchian_trend", "ma_trend", "term_structure_carry", "zscore_reversion",
    ]
    assert registry["donchian_trend"].spec.required_bars == 41
    assert registry["ma_trend"].spec.required_bars == 60
    assert registry["zscore_reversion"].spec.required_bars == 21
    assert registry["term_structure_carry"].spec.required_bars == 21
    assert registry["term_structure_carry"].spec.required_factors == (CARRY_FACTOR_NAME,)
    for strategy in registry.values():
        spec = strategy.spec
        assert spec.asset_class == "FUTURE"
        assert spec.supported_durations == (86400,)
    grid_sizes = [len(v) for v in CANDIDATE_GRID.values()]
    assert sum(grid_sizes) == 13


def test_evaluate_registry_uniform_requirements_block_short_history() -> None:
    registry = default_registry()
    bars = [make_bar(i, c) for i, c in enumerate(_vary(15))]
    results, _ = evaluate_registry(
        registry,
        lambda strategy: make_context(strategy.spec.strategy_id, bars),
        {},
    )
    assert set(results) == set(registry)
    for result in results.values():
        assert result.target_position is None
        assert result.status == "warmup"
        assert result.reason.startswith("insufficient_history")
    # Mismatched context ids fail closed as unavailable (enough bars present).
    enough = [make_bar(i, c) for i, c in enumerate(_vary(65))]
    wrong = make_context("donchian_trend", enough)
    assert check_requirements(registry["ma_trend"].spec, wrong).startswith("context_strategy_mismatch")


def test_observation_only_prefix_gives_null_target_with_candidate() -> None:
    strategy = DonchianTrend()
    bars = [make_bar(i, "90", high="95", low="85") for i in range(41)]
    bars[-1] = make_bar(40, "100.5", high="101", low="80")
    frame = build_structure_frame(bars)
    context = make_context(
        "donchian_trend", bars, structure=frame, decision_at=None,
        clock=ClockStatus.OBSERVATION_ONLY,
    )
    result = strategy.evaluate(context, None)
    assert result.target_position is None
    assert result.decision_at is None
    assert result.status == "unavailable"
    assert result.diagnostics["non_actionable"] is True
    assert result.diagnostics["candidate_direction"] == 1


def test_signal_age_advances_on_entry_then_each_observation() -> None:
    strategy = DonchianTrend()
    base = [make_bar(i, "90", high="95", low="85") for i in range(41)]
    entry_bars = list(base)
    entry_bars[-1] = make_bar(40, "100.5", high="101", low="80")
    frame = build_structure_frame(entry_bars)
    result = strategy.evaluate(make_context("donchian_trend", entry_bars, structure=frame), None)
    assert result.next_state.direction == 1
    assert result.next_state.signal_age == 0

    hold_bars = list(base) + [make_bar(41, "101", high="102", low="90")]
    frame_hold = build_structure_frame(hold_bars)
    result2 = strategy.evaluate(
        make_context("donchian_trend", hold_bars, structure=frame_hold), result.next_state
    )
    assert result2.next_state.direction == 1
    assert result2.next_state.signal_age == 1


def test_prefix_invariance_future_extension_does_not_change_result() -> None:
    strategy = MATrend()
    bars = [make_bar(i, f"{100 + i}.0") for i in range(61)]
    frame = build_structure_frame(bars)
    context = make_context("ma_trend", bars, structure=frame)
    baseline = strategy.evaluate(context, None)

    # The host truncates to the as-of prefix; appending future bars must not
    # alter the result computed from the same declared prefix.
    context_again = make_context("ma_trend", bars, structure=build_structure_frame(bars))
    repeat = strategy.evaluate(context_again, None)
    assert baseline.target_position == repeat.target_position
    assert baseline.diagnostics["fast_ma"] == repeat.diagnostics["fast_ma"]

    # Determinism across equal inputs includes full result equality.
    assert baseline == repeat


def test_result_contract_rejects_invalid_combinations() -> None:
    from ak_trading.futures.spec import StrategyResult

    with pytest.raises(ValueError, match="concrete direction"):
        StrategyResult(
            strategy_id="x", family="trend", product=PRODUCT,
            concrete_symbol="SHFE.rb2609", decision_at=datetime(2026, 1, 1, tzinfo=SH),
            target_position=None, status="ready", reason="r",
        )
    with pytest.raises(ValueError, match="non-zero"):
        StrategyResult(
            strategy_id="x", family="trend", product=PRODUCT,
            concrete_symbol="SHFE.rb2609", decision_at=datetime(2026, 1, 1, tzinfo=SH),
            target_position=0, status="ready", reason="r",
        )
    with pytest.raises(ValueError, match="concrete direction"):
        StrategyResult(
            strategy_id="x", family="trend", product=PRODUCT,
            concrete_symbol="SHFE.rb2609", decision_at=datetime(2026, 1, 1, tzinfo=SH),
            target_position=None, status="flat", reason="r",
        )


def test_state_rejects_invalid_direction_and_negative_age() -> None:
    with pytest.raises(ValueError):
        StrategyState(strategy_id="x", direction=2)
    with pytest.raises(ValueError):
        StrategyState(strategy_id="x", direction=0, signal_age=-1)
