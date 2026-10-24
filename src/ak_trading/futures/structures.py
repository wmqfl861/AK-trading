"""Causal structure computations over contract-local completed observations.

Pure functions over an as-of truncated bar prefix (finalized-plan §八). No
clock reads, no environment, no randomness; identical prefixes produce
identical frames. ``None`` plus a reason encodes every unknown.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Sequence

from ak_trading.futures.spec import ContractBar, FactorValue, StructureFrame

__all__ = [
    "CARRY_FACTOR_NAME",
    "sample_std",
    "simple_returns",
    "daily_vol20",
    "efficiency_ratio20",
    "classify_regime",
    "rolling_liquidity20",
    "build_structure_frame",
    "carry_annualized_factor",
    "REGIME_LABEL",
]

CARRY_FACTOR_NAME = "near_far_curve_ACT365"
REGIME_LABEL = "uncalibrated_heuristic"

_ER_TREND = 0.50
_ER_RANGE = 0.30


def _finite_floats(bars: Sequence[ContractBar]) -> list[float] | None:
    closes: list[float] = []
    for bar in bars:
        if bar.close is None:
            return None
        value = float(bar.close)
        if not math.isfinite(value) or value <= 0:
            return None
        closes.append(value)
    return closes


def sample_std(values: Sequence[float]) -> float | None:
    """Sample standard deviation (ddof=1); None when fewer than 2 values."""
    if len(values) < 2:
        return None
    return statistics.stdev(values)


def simple_returns(closes: Sequence[float]) -> list[float]:
    """r_t = C_t / C_{t-1} - 1 for consecutive finite positive closes."""
    returns: list[float] = []
    for previous, current in zip(closes, closes[1:]):
        returns.append(current / previous - 1.0)
    return returns


def daily_vol20(closes: Sequence[float]) -> float | None:
    """Sample std of the last 20 simple returns; needs 21 closes (§八)."""
    if len(closes) < 21:
        return None
    returns = simple_returns(closes[-21:])
    if len(returns) < 20:
        return None
    std = sample_std(returns)
    if std is None or std <= 0:
        return None
    return std


def efficiency_ratio20(closes: Sequence[float]) -> tuple[float | None, str | None]:
    """ER20 = |C_t - C_{t-20}| / sum of the last 20 absolute moves."""
    if len(closes) < 21:
        return None, "insufficient_history"
    numerator = abs(closes[-1] - closes[-21])
    denominator = sum(
        abs(current - previous)
        for previous, current in zip(closes[-21:], closes[-20:])
    )
    if denominator <= 0:
        return None, "no_movement"
    return numerator / denominator, None


def classify_regime(er20: float | None, er_status: str | None) -> str:
    """trend / range / transition from ER20; heuristic, not a probability."""
    if er20 is None:
        return "unavailable"
    if er20 >= _ER_TREND:
        return "trend"
    if er20 <= _ER_RANGE:
        return "range"
    return "transition"


def rolling_liquidity20(
    bars: Sequence[ContractBar],
) -> tuple[float | None, float | None, int]:
    """Median volume / close OI over the past <=20 completed bars incl. t."""
    window = bars[-20:]
    volumes = sorted(
        float(bar.volume)
        for bar in window
        if bar.volume is not None and float(bar.volume) >= 0
    )
    opens = sorted(
        float(bar.close_oi)
        for bar in window
        if bar.close_oi is not None and float(bar.close_oi) >= 0
    )

    def _median(values: list[float]) -> float | None:
        if not values:
            return None
        middle = len(values) // 2
        if len(values) % 2 == 1:
            return values[middle]
        return (values[middle - 1] + values[middle]) / 2.0

    return _median(volumes), _median(opens), len(window)


def build_structure_frame(
    bars: Sequence[ContractBar],
    *,
    entry_window: int = 40,
    exit_window: int = 20,
    fast_window: int = 20,
    slow_window: int = 60,
    zscore_lookback: int = 20,
) -> StructureFrame:
    """Compute one StructureFrame over a completed-bar prefix.

    Channels and the z-score reference window exclude the current bar; the
    moving averages include the current close (§八.A/B/C).
    """
    closes = _finite_floats(bars)
    if closes is None or not closes:
        return StructureFrame(status="unavailable", reason="invalid_or_empty_closes")

    highs_lows: list[tuple[float, float]] = []
    for bar in bars:
        if (
            bar.high is None
            or bar.low is None
            or bar.close is None
        ):
            highs_lows = []
            break
        try:
            high, low = float(bar.high), float(bar.low)
        except (TypeError, ValueError, OverflowError):
            highs_lows = []
            break
        if not (math.isfinite(high) and math.isfinite(low) and high >= low and low > 0):
            highs_lows = []
            break
        highs_lows.append((high, low))

    def channel(window: int) -> tuple[float | None, float | None]:
        if not highs_lows or len(highs_lows) < window + 1:
            return None, None
        reference = highs_lows[-(window + 1) : -1]  # excludes the current bar
        upper = max(high for high, _ in reference)
        lower = min(low for _, low in reference)
        return upper, lower

    upper_entry, lower_entry = channel(entry_window)
    upper_exit, lower_exit = channel(exit_window)

    fast_ma: float | None = None
    if len(closes) >= fast_window:
        fast_ma = math.fsum(closes[-fast_window:]) / fast_window
    slow_ma: float | None = None
    if len(closes) >= slow_window:
        slow_ma = math.fsum(closes[-slow_window:]) / slow_window

    lagged_mean: float | None = None
    lagged_std: float | None = None
    zscore: float | None = None
    if len(closes) >= zscore_lookback + 1:
        reference = closes[-(zscore_lookback + 1) : -1]
        lagged_mean = math.fsum(reference) / len(reference)
        lagged_std = sample_std(reference)
        if lagged_std is not None and lagged_std > 0:
            zscore = (closes[-1] - lagged_mean) / lagged_std
        else:
            lagged_std = None if lagged_std == 0 else lagged_std

    er20, er_status = efficiency_ratio20(closes)
    regime = classify_regime(er20, er_status)
    vol20 = daily_vol20(closes)

    reason: str | None = None
    status = "ready"
    if er20 is None:
        status = "partial"
        reason = er_status
    elif vol20 is None:
        status = "partial"
        reason = "vol20_unavailable"

    return StructureFrame(
        close=closes[-1],
        upper_entry=upper_entry,
        lower_entry=lower_entry,
        upper_exit=upper_exit,
        lower_exit=lower_exit,
        fast_ma=fast_ma,
        slow_ma=slow_ma,
        lagged_mean=lagged_mean,
        lagged_std=lagged_std,
        zscore=zscore,
        er20=er20,
        er_status=er_status,
        regime=regime,
        daily_vol20=vol20,
        window_length=len(closes),
        status=status,
        reason=reason,
    )


def carry_annualized_factor(
    *,
    factor_name: str,
    near_close: float,
    far_close: float,
    near_expiry_at: datetime,
    far_expiry_at: datetime,
    same_session: bool,
) -> FactorValue:
    """raw_log_spread = ln(close_near/close_far); annualized ACT/365 (§八.D).

    ``carry_annualized = raw_log_spread * 365 / gap_days``. Positive means
    backwardation. Inverted expiry, non-positive prices, non-synchronized
    sessions or a gap outside 60..180 days yield an unavailable factor.
    """
    if not same_session:
        return FactorValue(
            name=factor_name,
            value=None,
            status="unavailable",
            reason="sessions_not_synchronized",
        )
    if near_close <= 0 or far_close <= 0:
        return FactorValue(
            name=factor_name,
            value=None,
            status="unavailable",
            reason="non_positive_price",
        )
    if far_expiry_at <= near_expiry_at:
        return FactorValue(
            name=factor_name,
            value=None,
            status="unavailable",
            reason="expiry_not_later",
        )
    gap_days = (far_expiry_at - near_expiry_at).total_seconds() / 86400.0
    if not (60.0 <= gap_days <= 180.0):
        return FactorValue(
            name=factor_name,
            value=None,
            status="unavailable",
            reason=f"gap_days_out_of_range:{gap_days:.4f}",
        )
    raw_log_spread = math.log(near_close / far_close)
    annualized = raw_log_spread * 365.0 / gap_days
    return FactorValue(
        name=factor_name,
        value=annualized,
        status="ready",
        reason=None,
    )
