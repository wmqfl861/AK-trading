"""Versioned, contract-local factors over completed daily observations.

Lookbacks count input bars including the observation, even when the reference
window excludes it. Current context/regime are descriptive heuristics, never
predictions of the future path.
"""
from __future__ import annotations

import math
import statistics
from decimal import Decimal
from typing import Sequence

from ak_trading.futures.spec import ContractBar, StructureFrame
from ak_trading.futures.structures import build_structure_frame
from ak_trading.probability.contracts import FactorRow, FactorSpec, validate_bar


_REGISTRY = (
    FactorSpec("close", "1", 1, "price_points", "Current completed close."),
    FactorSpec("prior_high20", "1", 21, "price_points", "Maximum high of the prior 20 bars, excluding current."),
    FactorSpec("prior_low20", "1", 21, "price_points", "Minimum low of the prior 20 bars, excluding current."),
    FactorSpec("atr14", "1", 15, "price_points", "Arithmetic mean of 14 true ranges max(high-low, abs(high-prior_close), abs(low-prior_close)); needs 15 bars."),
    FactorSpec("er20", "1", 21, "ratio", "Absolute 20-bar close displacement / sum of 20 absolute close moves; zero movement unavailable. Current regime heuristic: ER>=0.5 uses MA20-MA60 sign; ER<=0.3 range; otherwise transition. Not a prediction."),
    FactorSpec("vol20", "1", 21, "return_std", "Sample standard deviation (ddof=1) of 20 simple close returns; zero variance unavailable."),
    FactorSpec("ma20", "1", 20, "price_points", "Arithmetic mean of the last 20 closes including current."),
    FactorSpec("ma60", "1", 60, "price_points", "Arithmetic mean of the last 60 closes including current."),
    FactorSpec("ma_spread_atr", "1", 60, "atr", "(MA20-MA60) / positive ATR14."),
    FactorSpec("zscore20", "1", 21, "standard_deviations", "(Current close - mean of prior 20 closes) / prior 20 close sample std (ddof=1); reference excludes current; zero variance unavailable."),
    FactorSpec("distance_high_atr", "1", 21, "atr", "(Prior high20 - current close) / positive ATR14. Near high when absolute distance <=0.5; nearest qualifying boundary wins, high on a tie."),
    FactorSpec("distance_low_atr", "1", 21, "atr", "(Current close - prior low20) / positive ATR14. Near low when absolute distance <=0.5; nearest qualifying boundary wins, high on a tie; otherwise middle."),
    FactorSpec("volume_ratio20", "1", 21, "ratio", "Current volume / mean of prior 20 volumes excluding current; all 21 volumes finite nonnegative, denominator positive."),
    FactorSpec("oi_change5", "1", 6, "fractional_change", "Current close OI / close OI five bars earlier - 1; all six OI values finite nonnegative, lagged OI positive."),
)


def factor_registry() -> tuple[FactorSpec, ...]:
    """Return deterministic version-1 definitions and maximum input lookbacks."""
    return _REGISTRY


def _valid_price(bar: ContractBar) -> bool:
    return validate_bar(bar) and all(
        math.isfinite(float(value)) and float(value) > 0
        for value in (bar.open, bar.high, bar.low, bar.close)
    )


def _valid_quantity(value: Decimal | None) -> bool:
    return (isinstance(value, Decimal) and value.is_finite()
            and value >= 0 and math.isfinite(float(value)))


def _structure(bars: Sequence[ContractBar]) -> StructureFrame:
    try:
        return build_structure_frame(bars, entry_window=20)
    except (ArithmeticError, ValueError):
        # Finite inputs can overflow float intermediate sums/returns. Preserve
        # independently computable statistics with Decimal arithmetic.
        closes = [item.close for item in bars]

        def mean(count: int) -> float | None:
            return float(sum(closes[-count:]) / count) if len(closes) >= count else None

        er = zscore = vol = None
        if len(closes) >= 21:
            reference = closes[-21:-1]
            moves = sum(abs(current - previous) for previous, current in zip(closes[-21:-1], closes[-20:]))
            if moves > 0:
                er = float(abs(closes[-1] - closes[-21]) / moves)
            std = statistics.stdev(reference)
            if std > 0:
                zscore = float((closes[-1] - sum(reference) / 20) / std)
            returns = [current / previous - 1 for previous, current in zip(closes[-21:-1], closes[-20:])]
            std_returns = statistics.stdev(returns)
            if std_returns > 0:
                vol = float(std_returns)
        return StructureFrame(close=float(closes[-1]), fast_ma=mean(20), slow_ma=mean(60),
                              er20=er, zscore=zscore, daily_vol20=vol)


def build_factor_rows(
    contract_id: str, symbol: str, product: str, bars: Sequence[ContractBar],
) -> tuple[FactorRow, ...]:
    """Keep one causally computed row for every input observation."""
    rows = []
    previous_nano = None
    for index, bar in enumerate(bars):
        if bar.symbol != symbol:
            raise ValueError("all bars must have the requested symbol")
        if (isinstance(bar.datetime_nano, bool) or not isinstance(bar.datetime_nano, int)
                or (previous_nano is not None and bar.datetime_nano <= previous_nano)):
            raise ValueError("bar timestamps must be integers in strictly ascending order")
        previous_nano = bar.datetime_nano
        window = bars[max(0, index - 60):index + 1]
        # A bad price observation breaks only windows that include it. Do not
        # remove it from the timeline or let it poison the whole contract.
        valid_start = 0
        for position, item in enumerate(window):
            if not _valid_price(item):
                valid_start = position + 1
        valid_window = window[valid_start:]
        frame = _structure(valid_window)
        prior = window[-21:-1] if len(window) >= 21 else ()
        prior_valid = len(prior) == 20 and all(_valid_price(item) for item in prior)
        values = {
            "close": frame.close,
            "prior_high20": max(float(item.high) for item in prior) if prior_valid else None,
            "prior_low20": min(float(item.low) for item in prior) if prior_valid else None,
            "atr14": None,
            "er20": frame.er20, "vol20": frame.daily_vol20,
            "ma20": frame.fast_ma, "ma60": frame.slow_ma,
            "ma_spread_atr": None, "zscore20": frame.zscore,
            "distance_high_atr": None, "distance_low_atr": None,
            "volume_ratio20": None, "oi_change5": None,
        }
        if len(valid_window) >= 15:
            true_ranges = [
                max(current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close))
                for previous, current in zip(window[-15:-1], window[-14:])
            ]
            values["atr14"] = float(sum(true_ranges) / 14)
        atr = values["atr14"]
        if atr is not None and atr > 0:
            if frame.fast_ma is not None and frame.slow_ma is not None:
                values["ma_spread_atr"] = (frame.fast_ma - frame.slow_ma) / atr
            if frame.close is not None and values["prior_high20"] is not None:
                values["distance_high_atr"] = (values["prior_high20"] - frame.close) / atr
                values["distance_low_atr"] = (frame.close - values["prior_low20"]) / atr
        if len(window) >= 21 and all(_valid_quantity(item.volume) for item in window[-21:]):
            average = sum(item.volume for item in window[-21:-1]) / 20
            if average > 0:
                values["volume_ratio20"] = float(bar.volume / average)
        if len(window) >= 6 and all(_valid_quantity(item.close_oi) for item in window[-6:]):
            if window[-6].close_oi > 0:
                values["oi_change5"] = float(bar.close_oi / window[-6].close_oi - 1)
        values = {name: value if value is None or math.isfinite(value) else None
                  for name, value in values.items()}
        context = "unavailable"
        high, low = values["distance_high_atr"], values["distance_low_atr"]
        if high is not None and low is not None:
            if abs(high) <= 0.5 and abs(high) <= abs(low):
                context = "near_high"
            elif abs(low) <= 0.5:
                context = "near_low"
            else:
                context = "middle"
        regime = "unavailable"
        if values["er20"] is not None:
            if values["er20"] <= 0.3:
                regime = "range"
            elif values["er20"] < 0.5:
                regime = "transition"
            elif values["ma20"] is not None and values["ma60"] is not None:
                spread = values["ma20"] - values["ma60"]
                regime = "uptrend" if spread > 0 else "downtrend" if spread < 0 else "transition"
        required = ("close", "atr14", "prior_high20", "prior_low20", "ma20", "ma60", "zscore20", "er20")
        missing = [name for name in required if values[name] is None or not math.isfinite(values[name])
                   or (name == "atr14" and values[name] <= 0)]
        if not missing:
            status, reason = "ready", "ready"
        elif any(index + 1 >= item.lookback for item in _REGISTRY if item.name in missing):
            status, reason = "unavailable", "invalid_or_undefined:" + ",".join(missing)
        else:
            status, reason = "warmup", "insufficient_history:" + ",".join(missing)
        rows.append(FactorRow(
            event_id=f"{contract_id}|{bar.datetime_nano}",
            contract_id=contract_id, symbol=symbol, product=product,
            index=index, timestamp_nano=bar.datetime_nano, values=values,
            status=status, reason=reason,
            context=context, regime=regime,
        ))
    return tuple(rows)
