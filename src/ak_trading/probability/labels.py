"""Retrospective price-path labels; never inputs to current factor metadata.

The next stored observation's open anchors both barriers using the decision's
frozen ATR14. The terminal regime is fixed by terminal displacement relative to
half that ATR, independently of the current ER/MA regime heuristic.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Sequence

from ak_trading.futures.spec import ContractBar
from ak_trading.probability.contracts import FactorRow, PathLabel, PathScenario, validate_bar


def label_paths(
    observation: FactorRow, bars: Sequence[ContractBar], scenario: PathScenario,
) -> PathLabel:
    """Label stored observations i+1..i+h for one contract-local event."""
    def incomplete(status: str, reason: str, terminal_nano: int | None = None) -> PathLabel:
        return PathLabel(
            event_id=observation.event_id, scenario_id=scenario.scenario_id,
            contract_id=observation.contract_id, index=observation.index,
            horizon=scenario.horizon, terminal_nano=terminal_nano,
            status=status, reason=reason, outcome=None,
        )

    if observation.status != "ready":
        return incomplete("unavailable", "observation_not_ready:" + observation.status)
    atr = observation.values.get("atr14")
    if isinstance(atr, bool) or not isinstance(atr, (int, float)) or not math.isfinite(atr) or atr <= 0:
        return incomplete("unavailable", "invalid_decision_atr14")
    index = observation.index
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(bars):
        return incomplete("invalid", "observation_index_out_of_range")
    if (bars[index].datetime_nano != observation.timestamp_nano
            or observation.event_id != f"{observation.contract_id}|{observation.timestamp_nano}"):
        return incomplete("invalid", "observation_identity_mismatch")
    # Factor construction validates the full series once. Per-event labels
    # inspect only the decision and its horizon, independent of any suffix.
    previous_nano = None
    for item in bars[index:index + scenario.horizon + 1]:
        if item.symbol != observation.symbol:
            return incomplete("invalid", "bar_symbol_mismatch")
        if (isinstance(item.datetime_nano, bool) or not isinstance(item.datetime_nano, int)
                or (previous_nano is not None and item.datetime_nano <= previous_nano)):
            return incomplete("invalid", "bar_timestamps_not_strictly_ascending")
        previous_nano = item.datetime_nano
    future = bars[index + 1:index + scenario.horizon + 1]
    if len(future) < scenario.horizon:
        return incomplete("unmatured", "insufficient_future_bars")
    for step, item in enumerate(future, 1):
        if not validate_bar(item) or any(
            not math.isfinite(float(value)) or float(value) <= 0
            for value in (item.open, item.high, item.low, item.close)
        ):
            return incomplete("invalid", f"invalid_future_price:step={step}", future[-1].datetime_nano)
    # Decimal comparisons preserve equality at fractional-price barriers.
    entry = future[0].open
    atr_decimal = Decimal(str(atr))
    upper = entry + atr_decimal * Decimal(str(scenario.upper_atr))
    lower = entry - atr_decimal * Decimal(str(scenario.lower_atr))
    terminal_points = future[-1].close - entry
    terminal_return = terminal_points / entry
    mfe = max(Decimal(0), max(item.high for item in future) - entry)
    mae = max(Decimal(0), entry - min(item.low for item in future))
    if not all(math.isfinite(float(value)) for value in (upper, lower, terminal_points, terminal_return, mfe, mae)):
        return incomplete("invalid", "nonfinite_path_calculation", future[-1].datetime_nano)
    outcome, first_step = "neither", None
    for step, item in enumerate(future, 1):
        if item.open >= upper:
            outcome, first_step = "up_first", step
            break
        if item.open <= lower:
            outcome, first_step = "down_first", step
            break
        up = item.high >= upper
        down = item.low <= lower
        if up or down:
            outcome = "ambiguous" if up and down else "up_first" if up else "down_first"
            first_step = step
            break
    terminal_regime = ("uptrend" if terminal_points > Decimal("0.5") * atr_decimal
                       else "downtrend" if terminal_points < -Decimal("0.5") * atr_decimal else "range")
    return PathLabel(
        event_id=observation.event_id, scenario_id=scenario.scenario_id,
        contract_id=observation.contract_id, index=observation.index,
        horizon=scenario.horizon, terminal_nano=future[-1].datetime_nano,
        status="mature", reason="complete_horizon", outcome=outcome,
        entry_price=float(entry), upper_price=float(upper), lower_price=float(lower),
        first_hit_step=first_step, terminal_return=float(terminal_return),
        terminal_points=float(terminal_points),
        mfe_points=float(mfe), mae_points=float(mae),
        terminal_regime=terminal_regime,
    )
