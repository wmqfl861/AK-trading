"""Four futures strategy implementations plus the legacy reference adapter.

Unified entry point (finalized-plan §四.2):
``evaluate(context, previous) -> StrategyResult`` — deterministic, offline,
no strategy-specific runner glue. Lots and money never appear here; only
direction risk units in {-1, 0, +1, None}.
"""
from __future__ import annotations

import math
from statistics import median
from dataclasses import dataclass, replace
from typing import Callable, Mapping, Protocol

from ak_trading.futures.spec import (
    ClockStatus,
    ContractBar,
    ResearchPolicy,
    StrategyContext,
    StrategyResult,
    StrategySpec,
    StrategyState,
)
from ak_trading.futures.structures import (
    CARRY_FACTOR_NAME,
    carry_annualized_factor,
    daily_vol20,
)
from ak_trading.futures.data import (
    PairCandidate,
    pair_gap_days,
    rank_pair_candidates,
    synchronized_far_bar,
    update_pair_selection,
)
from ak_trading.strategy import DualMAStrategy, SignalDecision

__all__ = [
    "FuturesStrategy",
    "DonchianTrend",
    "MATrend",
    "ZScoreReversion",
    "TermStructureCarry",
    "LegacyDualMAAdapter",
    "STRATEGY_REGISTRY_IDS",
    "CANDIDATE_GRID",
    "default_registry",
    "check_requirements",
    "evaluate_registry",
]


class FuturesStrategy(Protocol):
    """Structural protocol shared by all futures implementations."""

    @property
    def spec(self) -> StrategySpec: ...

    def evaluate(
        self, context: StrategyContext, previous: StrategyState | None
    ) -> StrategyResult: ...


#: Parameter grid: 13 candidates, train-time sensitivity only (§八).
CANDIDATE_GRID: Mapping[str, tuple[tuple[object, ...], ...]] = {
    "donchian_trend": ((20, 10), (40, 20), (60, 20)),
    "ma_trend": ((10, 40), (20, 60), (40, 120)),
    "zscore_reversion": ((20, 1.5), (20, 2.0), (40, 1.5), (40, 2.0)),
    "term_structure_carry": ((0.01,), (0.03,), (0.05,)),
}

STRATEGY_REGISTRY_IDS = (
    "donchian_trend",
    "ma_trend",
    "zscore_reversion",
    "term_structure_carry",
)


def _validate_positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _bars_closes(bars: tuple[ContractBar, ...]) -> list[float] | None:
    closes: list[float] = []
    for bar in bars:
        if bar.close is None:
            return None
        value = float(bar.close)
        if not math.isfinite(value) or value <= 0:
            return None
        closes.append(value)
    return closes


def _observation_only_result(
    context: StrategyContext, family: str, candidate_direction: int | None, reason: str
) -> StrategyResult:
    """Descriptive observation output: null target, non-actionable (§四.4)."""
    return StrategyResult(
        strategy_id=context.strategy_id,
        family=family,
        product=context.product,
        concrete_symbol=context.raw_symbol,
        decision_at=None,
        target_position=None,
        status="unavailable",
        reason="observation_only",
        diagnostics={
            "candidate_direction": candidate_direction,
            "non_actionable": True,
            "underlying_reason": reason,
        },
    )


def _warmup_result(context: StrategyContext, family: str, reason: str) -> StrategyResult:
    return StrategyResult(
        strategy_id=context.strategy_id,
        family=family,
        product=context.product,
        concrete_symbol=context.raw_symbol,
        decision_at=context.decision_at,
        target_position=None,
        status="warmup",
        reason=reason,
    )


def _unavailable_result(
    context: StrategyContext, family: str, reason: str, diagnostics: Mapping[str, object] | None = None
) -> StrategyResult:
    return StrategyResult(
        strategy_id=context.strategy_id,
        family=family,
        product=context.product,
        concrete_symbol=context.raw_symbol,
        decision_at=context.decision_at,
        target_position=None,
        status="unavailable",
        reason=reason,
        diagnostics=dict(diagnostics or {}),
    )


@dataclass(frozen=True)
class DonchianTrend:
    """Channel breakout with separate exit channel (§八.A).

    Entry/exit channels exclude the current bar; touching a border does not
    trigger (strict inequalities). Reversal to the opposite entry is allowed
    while regime=trend; exits follow the exit channel otherwise.
    """

    entry_window: int = 40
    exit_window: int = 20

    def __post_init__(self) -> None:
        _validate_positive_int("entry_window", self.entry_window)
        _validate_positive_int("exit_window", self.exit_window)

    @property
    def spec(self) -> StrategySpec:
        required = max(self.entry_window + 1, self.exit_window + 1, 21)
        return StrategySpec(
            strategy_id="donchian_trend",
            family="trend",
            version="1",
            parameters={"entry": self.entry_window, "exit": self.exit_window},
            required_bars=required,
            required_features=(
                "entry_channel",
                "exit_channel",
                "er20",
                "daily_vol20",
                "roll_selector_state",
                "liquidity20",
            ),
            required_factors=(),
        )

    def evaluate(
        self, context: StrategyContext, previous: StrategyState | None
    ) -> StrategyResult:
        frame = context.structure
        closes = _bars_closes(context.bars)
        required = self.spec.required_bars
        if frame is None or closes is None or len(closes) < required:
            reason = "insufficient_history"
        elif (
            frame.upper_entry is None
            or frame.lower_entry is None
            or frame.upper_exit is None
            or frame.lower_exit is None
            or frame.close is None
        ):
            reason = "channels_unavailable_invalid_high_low"
        elif context.clock_status is ClockStatus.OBSERVATION_ONLY or context.decision_at is None:
            return _observation_only_result(
                context,
                "trend",
                _donchian_candidate(frame, previous),
                "observation_prefix",
            )
        else:
            return _donchian_decision(context, frame, previous)
        return _warmup_result(context, "trend", reason)


def _donchian_candidate(frame, previous: StrategyState | None) -> int | None:
    """Descriptive-only direction from channel logic (no state changes)."""
    close = frame.close
    if previous is not None and previous.direction != 0:
        if frame.regime == "trend":
            if previous.direction > 0 and close < frame.lower_entry:
                return -1
            if previous.direction < 0 and close > frame.upper_entry:
                return 1
        if previous.direction > 0 and close < frame.lower_exit:
            return 0
        if previous.direction < 0 and close > frame.upper_exit:
            return 0
        return previous.direction
    if frame.regime == "trend":
        if close > frame.upper_entry:
            return 1
        if close < frame.lower_entry:
            return -1
    return 0


def _donchian_decision(
    context: StrategyContext, frame, previous: StrategyState | None
) -> StrategyResult:
    close = frame.close
    current_direction = previous.direction if previous is not None else 0
    age = previous.signal_age if previous is not None else None
    if current_direction == 0:
        target = 0
        reason = "no_entry_outside_trend_regime"
        if frame.regime == "trend":
            if close > frame.upper_entry:
                target, reason = 1, "breakout_above_entry_channel"
            elif close < frame.lower_entry:
                target, reason = -1, "breakdown_below_entry_channel"
            else:
                reason = "inside_entry_channel"
        next_age = None
        if target != 0:
            next_age = 0
        status = "ready" if target != 0 else "flat"
    else:
        target = current_direction
        reason = "maintain_direction"
        next_age = 0 if age is None else age + 1
        if frame.regime == "trend":
            if current_direction > 0 and close < frame.lower_entry:
                target, reason, next_age = -1, "reverse_to_short", 0
            elif current_direction < 0 and close > frame.upper_entry:
                target, reason, next_age = 1, "reverse_to_long", 0
        if target == current_direction:
            if current_direction > 0 and close < frame.lower_exit:
                target, reason, next_age = 0, "exit_below_exit_channel", None
            elif current_direction < 0 and close > frame.upper_exit:
                target, reason, next_age = 0, "exit_above_exit_channel", None
        status = "ready" if target != 0 else "flat"

    next_state = StrategyState(
        strategy_id=context.strategy_id,
        direction=target,
        signal_age=next_age,
        contract_instance_id=context.contract_instance_id,
        episode_id=previous.episode_id if previous is not None else None,
    )
    return StrategyResult(
        strategy_id=context.strategy_id,
        family="trend",
        product=context.product,
        concrete_symbol=context.raw_symbol,
        decision_at=context.decision_at,
        target_position=target,
        status=status,
        reason=reason,
        diagnostics={
            "er20": frame.er20,
            "er_status": frame.er_status,
            "regime": frame.regime,
            "regime_label": "uncalibrated_heuristic",
            "daily_vol20": frame.daily_vol20,
            "upper_entry": frame.upper_entry,
            "lower_entry": frame.lower_entry,
        },
        next_state=next_state,
    )


@dataclass(frozen=True)
class MATrend:
    """Two moving averages including the current close, regime-gated (§八.B)."""

    fast_window: int = 20
    slow_window: int = 60

    def __post_init__(self) -> None:
        _validate_positive_int("fast_window", self.fast_window)
        _validate_positive_int("slow_window", self.slow_window)
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be smaller than slow_window")

    @property
    def spec(self) -> StrategySpec:
        return StrategySpec(
            strategy_id="ma_trend",
            family="trend",
            version="1",
            parameters={"fast": self.fast_window, "slow": self.slow_window},
            required_bars=max(self.slow_window, 21),
            required_features=(
                "fast_ma",
                "slow_ma",
                "er20",
                "daily_vol20",
                "roll_selector_state",
                "liquidity20",
            ),
            required_factors=(),
        )

    def evaluate(
        self, context: StrategyContext, previous: StrategyState | None
    ) -> StrategyResult:
        frame = context.structure
        closes = _bars_closes(context.bars)
        if (
            frame is None
            or closes is None
            or len(closes) < self.spec.required_bars
            or frame.fast_ma is None
            or frame.slow_ma is None
            or frame.close is None
        ):
            reason = (
                "insufficient_history"
                if frame is None or frame.window_length < self.spec.required_bars
                else "invalid_closes"
            )
            return _warmup_result(context, "trend", reason)
        if context.clock_status is ClockStatus.OBSERVATION_ONLY or context.decision_at is None:
            return _observation_only_result(
                context, "trend", _ma_direction(frame), "observation_prefix"
            )
        return _ma_decision(context, frame, previous)


def _ma_direction(frame) -> int:
    if frame.regime != "trend":
        return 0
    if frame.fast_ma > frame.slow_ma:
        return 1
    if frame.fast_ma < frame.slow_ma:
        return -1
    return 0


def _ma_decision(
    context: StrategyContext, frame, previous: StrategyState | None
) -> StrategyResult:
    direction = _ma_direction(frame)
    previous_direction = previous.direction if previous is not None else 0
    previous_age = previous.signal_age if previous is not None else None
    if direction != 0 and previous_direction == direction:
        next_age = 0 if previous_age is None else previous_age + 1
    elif direction != 0:
        next_age = 0
    else:
        next_age = None
    reason = {
        1: "fast_above_slow_in_trend",
        -1: "fast_below_slow_in_trend",
        0: "no_directional_state",
    }[direction]
    if direction == 0 and previous_direction != 0:
        reason = "signal_left_or_gate_closed"
    status = "ready" if direction != 0 else "flat"
    next_state = StrategyState(
        strategy_id=context.strategy_id,
        direction=direction,
        signal_age=next_age,
        contract_instance_id=context.contract_instance_id,
        episode_id=previous.episode_id if previous is not None else None,
    )
    return StrategyResult(
        strategy_id=context.strategy_id,
        family="trend",
        product=context.product,
        concrete_symbol=context.raw_symbol,
        decision_at=context.decision_at,
        target_position=direction,
        status=status,
        reason=reason,
        diagnostics={
            "fast_ma": frame.fast_ma,
            "slow_ma": frame.slow_ma,
            "er20": frame.er20,
            "regime": frame.regime,
            "regime_label": "uncalibrated_heuristic",
            "daily_vol20": frame.daily_vol20,
        },
        next_state=next_state,
    )


@dataclass(frozen=True)
class ZScoreReversion:
    """Mean reversion with fixed exit/stop/age rules (§八.C)."""

    lookback: int = 20
    entry_z: float = 2.0
    exit_z: float = 0.5
    stop_z: float = 4.0
    max_signal_age: int = 20

    def __post_init__(self) -> None:
        _validate_positive_int("lookback", self.lookback)
        if self.lookback < 2:
            raise ValueError("lookback must be at least 2")
        for name in ("entry_z", "exit_z", "stop_z"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{name} must be a positive number")
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        if not (self.entry_z < self.stop_z):
            raise ValueError("entry_z must be smaller than stop_z")
        if not (self.exit_z < self.entry_z):
            raise ValueError("exit_z must be smaller than entry_z")

    @property
    def spec(self) -> StrategySpec:
        return StrategySpec(
            strategy_id="zscore_reversion",
            family="mean_reversion",
            version="1",
            parameters={"lookback": self.lookback, "entry_z": self.entry_z},
            required_bars=max(self.lookback + 1, 21),
            required_features=(
                "lagged_mean_std",
                "zscore",
                "er20",
                "daily_vol20",
                "signal_age",
                "roll_selector_state",
                "liquidity20",
            ),
            required_factors=(),
        )

    def evaluate(
        self, context: StrategyContext, previous: StrategyState | None
    ) -> StrategyResult:
        frame = context.structure
        closes = _bars_closes(context.bars)
        if frame is None or closes is None:
            return _warmup_result(context, "mean_reversion", "insufficient_history")
        if len(closes) < self.spec.required_bars or frame.zscore is None:
            if frame.lagged_mean is not None and frame.lagged_std is None:
                return _unavailable_result(
                    context, "mean_reversion", "zero_variance_window"
                )
            return _warmup_result(context, "mean_reversion", "insufficient_history")
        if context.clock_status is ClockStatus.OBSERVATION_ONLY or context.decision_at is None:
            candidate = self._candidate(frame, previous)
            return _observation_only_result(
                context, "mean_reversion", candidate, "observation_prefix"
            )
        return self._decision(context, frame, previous)

    def _candidate(self, frame, previous: StrategyState | None) -> int | None:
        z = frame.zscore
        direction = previous.direction if previous is not None else 0
        if direction == 0:
            if frame.regime != "range":
                return 0
            if self.entry_z <= z < self.stop_z:
                return -1
            if -self.stop_z < z <= -self.entry_z:
                return 1
            return 0
        if abs(z) >= self.stop_z or (frame.er20 is not None and frame.er20 > 0.50):
            return 0
        if direction > 0 and z >= -self.exit_z:
            return 0
        if direction < 0 and z <= self.exit_z:
            return 0
        return direction

    def _decision(
        self, context: StrategyContext, frame, previous: StrategyState | None
    ) -> StrategyResult:
        z = frame.zscore
        direction = previous.direction if previous is not None else 0
        age = previous.signal_age if previous is not None else None
        er_leaves_range = frame.er20 is not None and frame.er20 > 0.50
        age_expired = age is not None and age >= self.max_signal_age

        if direction == 0:
            target, reason = 0, "no_entry_outside_range_or_band"
            if frame.regime == "range":
                if self.entry_z <= z < self.stop_z:
                    target, reason = -1, "enter_short_above_band"
                elif -self.stop_z < z <= -self.entry_z:
                    target, reason = 1, "enter_long_below_band"
                elif abs(z) >= self.stop_z:
                    reason = "extreme_deviation_no_new_entry"
            next_age = 0 if target != 0 else None
        else:
            # Exit checks first; no same-decision reversal (§八.C.2).
            exit_reason: str | None = None
            if direction > 0 and z >= -self.exit_z:
                exit_reason = "zscore_mean_reversion_exit_long"
            elif direction < 0 and z <= self.exit_z:
                exit_reason = "zscore_mean_reversion_exit_short"
            elif abs(z) >= self.stop_z:
                exit_reason = "stop_z_reached"
            elif er_leaves_range:
                exit_reason = "er20_left_range"
            elif age_expired:
                exit_reason = "max_signal_age_reached"
            if exit_reason is not None:
                target, reason, next_age = 0, exit_reason, None
            else:
                target, reason = direction, "maintain_direction"
                next_age = 0 if age is None else age + 1

        status = "ready" if target != 0 else "flat"
        next_state = StrategyState(
            strategy_id=context.strategy_id,
            direction=target,
            signal_age=next_age,
            contract_instance_id=context.contract_instance_id,
            episode_id=previous.episode_id if previous is not None else None,
        )
        return StrategyResult(
            strategy_id=context.strategy_id,
            family="mean_reversion",
            product=context.product,
            concrete_symbol=context.raw_symbol,
            decision_at=context.decision_at,
            target_position=target,
            status=status,
            reason=reason,
            diagnostics={
                "zscore": z,
                "lagged_mean": frame.lagged_mean,
                "lagged_std": frame.lagged_std,
                "er20": frame.er20,
                "regime": frame.regime,
                "signal_age": next_age,
            },
            next_state=next_state,
        )


@dataclass(frozen=True)
class TermStructureCarry:
    """Near-contract direction from the ACT/365 term-structure slope (§八.D).

    Trades only the near leg; the far leg is observation/selection input.
    ``carry_annualized = ln(close_near/close_far) * 365 / gap_days``.
    """

    threshold: float = 0.03
    policy: ResearchPolicy = ResearchPolicy()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.threshold, (int, float))
            or isinstance(self.threshold, bool)
            or not math.isfinite(float(self.threshold))
            or float(self.threshold) <= 0
        ):
            raise ValueError("threshold must be a positive finite number")

    @property
    def spec(self) -> StrategySpec:
        return StrategySpec(
            strategy_id="term_structure_carry",
            family="term_structure_carry",
            version="1",
            parameters={"threshold": self.threshold},
            required_bars=21,
            required_features=(
                "near_daily_vol20",
                "near_liquidity20",
                "far_liquidity20",
                "expiry_pair",
                "synchronized_pair_prices",
                "pair_selection_state",
            ),
            required_factors=(CARRY_FACTOR_NAME,),
        )

    def evaluate(
        self, context: StrategyContext, previous: StrategyState | None
    ) -> StrategyResult:
        closes = _bars_closes(context.bars)
        near_vol_ready = closes is not None and len(closes) >= 21
        pair_state = previous.pair if previous is not None else None

        # Pair confirmation may begin on the first observation with far
        # liquidity history even while near volatility warms up (§八 table);
        # the selection state therefore always moves forward.
        candidates = self._ranked_candidates(context)
        next_pair_state, selection_status, selection_reason = update_pair_selection(
            pair_state, candidates, self.policy
        )
        carry_next_state = StrategyState(
            strategy_id=context.strategy_id,
            direction=previous.direction if previous is not None else 0,
            signal_age=previous.signal_age if previous is not None else None,
            contract_instance_id=context.contract_instance_id,
            episode_id=previous.episode_id if previous is not None else None,
            pair=next_pair_state,
        )

        if closes is None or not near_vol_ready:
            result = _warmup_result(context, "term_structure_carry", "near_history_insufficient")
            return replace(result, next_state=carry_next_state)
        if daily_vol20(closes) is None:
            result = _unavailable_result(
                context, "term_structure_carry", "near_vol20_unavailable"
            )
            return replace(result, next_state=carry_next_state)
        if selection_status != "ready":
            result = _unavailable_result(
                context,
                "term_structure_carry",
                selection_reason or "pair_not_confirmed",
                {
                    "pair_selection_status": selection_status,
                    "confirmation_count": next_pair_state.confirmation_count,
                },
            )
            return replace(result, next_state=carry_next_state)

        selected = next_pair_state
        far_symbol = selected.far_symbol or ""
        far_bars = context.pair_bars.get(far_symbol, ())
        near_bar = context.bars[-1]
        far_bar = synchronized_far_bar(near_bar, far_bars)
        if far_bar is None or selected.far_expiry_at is None or context.expiry_at is None:
            result = _unavailable_result(
                context,
                "term_structure_carry",
                "far_price_unavailable",
                {"pair_selection_status": "ready", "far_symbol": far_symbol},
            )
            return replace(result, next_state=carry_next_state)

        near_close = float(near_bar.close) if near_bar.close is not None else 0.0
        far_close = float(far_bar.close) if far_bar.close is not None else 0.0
        factor = carry_annualized_factor(
            factor_name=CARRY_FACTOR_NAME,
            near_close=near_close,
            far_close=far_close,
            near_expiry_at=context.expiry_at,
            far_expiry_at=selected.far_expiry_at,
            same_session=True,
        )
        if factor.status != "ready" or factor.value is None:
            result = _unavailable_result(
                context,
                "term_structure_carry",
                factor.reason or "carry_factor_unavailable",
                {"far_symbol": far_symbol},
            )
            return replace(result, next_state=carry_next_state)

        carry = factor.value
        diagnostics = {
            "pair_id": f"{near_bar.symbol}|{far_symbol}",
            "expiry_near": context.expiry_at.isoformat(),
            "expiry_far": selected.far_expiry_at.isoformat(),
            "gap_days": pair_gap_days(context.expiry_at, selected.far_expiry_at),
            "raw_log_spread": math.log(near_close / far_close) if far_close > 0 and near_close > 0 else None,
            "carry_annualized": carry,
            "confirmation_count": selected.confirmation_count,
            "pair_changed": (
                pair_state is not None
                and pair_state.far_symbol is not None
                and pair_state.far_symbol != far_symbol
            ),
            "selection_reason": selection_reason,
        }

        if context.clock_status is ClockStatus.OBSERVATION_ONLY or context.decision_at is None:
            candidate = 1 if carry > self.threshold else (-1 if carry < -self.threshold else 0)
            observation_result = _observation_only_result(
                context, "term_structure_carry", candidate, "observation_prefix"
            )
            # Pair confirmation advances even on observation-only prefixes
            # (§五.12), so the selection state must carry forward.
            return replace(observation_result, next_state=carry_next_state)

        if carry > self.threshold:
            target, reason = 1, "backwardation_above_threshold"
        elif carry < -self.threshold:
            target, reason = -1, "contango_below_negative_threshold"
        else:
            target, reason = 0, "slope_within_threshold_band"

        previous_direction = previous.direction if previous is not None else 0
        previous_age = previous.signal_age if previous is not None else None
        if target != 0 and previous_direction == target:
            next_age = 0 if previous_age is None else previous_age + 1
        elif target != 0:
            next_age = 0
        else:
            next_age = None
        status = "ready" if target != 0 else "flat"
        next_state = StrategyState(
            strategy_id=context.strategy_id,
            direction=target,
            signal_age=next_age,
            contract_instance_id=context.contract_instance_id,
            episode_id=previous.episode_id if previous is not None else None,
            pair=next_pair_state,
        )
        return StrategyResult(
            strategy_id=context.strategy_id,
            family="term_structure_carry",
            product=context.product,
            concrete_symbol=context.raw_symbol,
            decision_at=context.decision_at,
            target_position=target,
            status=status,
            reason=reason,
            diagnostics=diagnostics,
            next_state=next_state,
        )

    def _ranked_candidates(self, context: StrategyContext) -> tuple[PairCandidate, ...]:
        if context.expiry_at is None or not context.bars:
            return ()
        near_bar = context.bars[-1]
        candidates: list[PairCandidate] = []
        for far in context.pair_candidates:
            if far.raw_symbol == context.raw_symbol or far.expiry_at is None:
                continue
            far_bars = context.pair_bars.get(far.raw_symbol, ())
            far_bar = synchronized_far_bar(near_bar, far_bars)
            if far_bar is None or far_bar.close is None or near_bar.close is None:
                continue
            far_volume_window = [
                float(bar.volume)
                for bar in far_bars[-20:]
                if bar.volume is not None
            ]
            far_median_volume = (
                median(far_volume_window)
                if far_volume_window
                else None
            )
            candidates.append(
                PairCandidate(
                    near_symbol=context.raw_symbol,
                    far_symbol=far.raw_symbol,
                    near_expiry_at=context.expiry_at,
                    far_expiry_at=far.expiry_at,
                    near_bar=near_bar,
                    far_bar=far_bar,
                    far_median_volume20=far_median_volume,
                )
            )
        return rank_pair_candidates(candidates, self.policy)


@dataclass(frozen=True)
class LegacyDualMAAdapter:
    """Compatibility wrapper over the original DualMAStrategy (§四.9).

    Preserves the legacy None/long/flat semantics verbatim; it never counts
    as a fifth strategy and has no MATrend regime semantics.
    """

    fast_window: int = 5
    slow_window: int = 20

    def __post_init__(self) -> None:
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be smaller than slow_window")

    @property
    def spec(self) -> StrategySpec:
        return StrategySpec(
            strategy_id="legacy_dual_ma_reference",
            family="trend",
            version="legacy",
            parameters={"fast": self.fast_window, "slow": self.slow_window},
            required_bars=self.slow_window,
            required_features=("fast_ma", "slow_ma"),
            required_factors=(),
        )

    def evaluate(
        self, context: StrategyContext, previous: StrategyState | None  # noqa: ARG002
    ) -> StrategyResult:
        legacy = DualMAStrategy(
            fast_window=self.fast_window, slow_window=self.slow_window
        )
        decision: SignalDecision = legacy.evaluate(
            [bar.close for bar in context.bars]
        )
        if decision.target_position is None:
            return StrategyResult(
                strategy_id=self.spec.strategy_id,
                family="trend",
                product=context.product,
                concrete_symbol=context.raw_symbol,
                decision_at=context.decision_at,
                target_position=None,
                status="unavailable" if decision.reason == "invalid_close" else "warmup",
                reason=decision.reason,
                diagnostics={"legacy_reason": decision.reason},
            )
        status = "ready" if decision.target_position == 1 else "flat"
        return StrategyResult(
            strategy_id=self.spec.strategy_id,
            family="trend",
            product=context.product,
            concrete_symbol=context.raw_symbol,
            decision_at=context.decision_at,
            target_position=decision.target_position,
            status=status,
            reason=decision.reason,
            diagnostics={
                "legacy_reason": decision.reason,
                "fast_ma": decision.fast_ma,
                "slow_ma": decision.slow_ma,
            },
        )


def default_registry(
    policy: ResearchPolicy | None = None,
) -> dict[str, FuturesStrategy]:
    """Static registry of the four implementations (legacy excluded)."""
    return {
        "donchian_trend": DonchianTrend(),
        "ma_trend": MATrend(),
        "zscore_reversion": ZScoreReversion(),
        "term_structure_carry": TermStructureCarry(policy=policy or ResearchPolicy()),
    }


def check_requirements(
    spec: StrategySpec, context: StrategyContext
) -> str | None:
    """Uniform host-side requirement check; None when satisfiable.

    Enforces the parts the host can verify generically: bar-count sufficiency
    against the parameter-derived ``required_bars`` and context identity.
    Feature/factor readiness is enforced by each implementation through its
    unavailable/warmup semantics (verified in tests) — the declarations tell
    hosts what inputs to supply, without strategy-id special cases.
    """
    if len(context.bars) < spec.required_bars:
        return f"insufficient_history:{len(context.bars)}/{spec.required_bars}"
    if context.strategy_id != spec.strategy_id:
        return f"context_strategy_mismatch:{context.strategy_id}!={spec.strategy_id}"
    return None


def evaluate_registry(
    registry: Mapping[str, FuturesStrategy],
    context_provider: Callable[[FuturesStrategy], StrategyContext],
    previous_states: Mapping[str, StrategyState | None],
) -> tuple[dict[str, StrategyResult], dict[str, StrategyState | None]]:
    """Run every registry strategy under uniform requirement checks."""
    results: dict[str, StrategyResult] = {}
    next_states: dict[str, StrategyState | None] = {}
    for strategy_id in sorted(registry):
        strategy = registry[strategy_id]
        context = context_provider(strategy)
        failure = check_requirements(strategy.spec, context)
        if failure is not None:
            results[strategy_id] = StrategyResult(
                strategy_id=strategy_id,
                family=strategy.spec.family,
                product=context.product,
                concrete_symbol=context.raw_symbol,
                decision_at=context.decision_at,
                target_position=None,
                status="warmup" if failure.startswith("insufficient_history") else "unavailable",
                reason=failure,
            )
            next_states[strategy_id] = previous_states.get(strategy_id)
            continue
        result = strategy.evaluate(context, previous_states.get(strategy_id))
        results[strategy_id] = result
        next_states[strategy_id] = result.next_state
    return results, next_states
