"""Retrospective candidate events from fixed strategies and raw bar prefixes.

Observation timestamps align reference prices only. Stored availability clocks
and expiry metadata do not establish historical execution readiness. This module
has no I/O and never computes subsequent price changes or position histories.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Mapping, Sequence

from ak_trading.futures.data import datetime_to_nano, pair_gap_days
from ak_trading.futures.spec import (
    ClockStatus,
    ContractBar,
    ContractMetadata,
    REGISTRY_TERM_STRUCTURE_CARRY,
    StrategyContext,
    StrategyState,
    parse_product_token,
)
from ak_trading.futures.strategies import default_registry
from ak_trading.futures.structures import build_structure_frame
from ak_trading.futures.study_spec import StudyContract, StudySignal

__all__ = ["generate_study_signals"]

# Fixed observation-row window, including invalid rows and the current row.
_OBSERVATION_WINDOW = 120


def _json_safe(value: object) -> object:
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    raise TypeError(f"unsupported study diagnostic type: {type(value).__name__}")


def _reference(bar: ContractBar) -> dict[str, object]:
    """Preserve input clocks without promoting them to decision/session proof."""
    return {
        "datetime_nano": bar.datetime_nano,
        "ts": bar.ts,
        "available_at": bar.available_at,
        "trading_day": bar.trading_day,
        "trading_day_source": bar.trading_day_source,
        "session_open": bar.session_open,
        "session_close": bar.session_close,
        "source": bar.source,
        "clock_quality": "unverified_observation_reference",
        "availability_quality": (
            "missing" if bar.available_at is None
            else "stored_availability_not_execution_clock"
        ),
        "trading_day_quality": (
            "missing" if bar.trading_day is None else "stored_reference_unverified"
        ),
        "missing_timing_fields": [
            name for name in (
                "available_at", "trading_day", "trading_day_source",
                "session_open", "session_close",
            ) if getattr(bar, name) in (None, "")
        ],
    }


def _finite_number(value: object, *, positive: bool) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    try:
        number = float(value)
        return math.isfinite(number) and (number > 0 if positive else number >= 0)
    except (ValueError, OverflowError):
        return False


def generate_study_signals(
    selected: Sequence[StudyContract], universe: Sequence[StudyContract]
) -> tuple[StudySignal, ...]:
    """Emit four fixed-parameter, non-actionable events at every original index.

    Input bars must be in increasing ``datetime_nano`` order, as supplied by
    the study sample. Near rows retain their original indices; far windows end
    at the raw near timestamp via bisect, including their trailing liquidity.
    The first three strategies receive no previous position state. Carry keeps
    only pair confirmation state, independently for each near instance.
    """
    registry = default_registry()
    signals: list[StudySignal] = []
    for near in sorted(selected, key=lambda item: item.contract_instance_id):
        product = parse_product_token(near.product)
        expiry_nano = datetime_to_nano(near.expiry_at)
        far_series = []
        far_symbols: set[str] = set()
        for far in sorted(universe, key=lambda item: item.contract_instance_id):
            if (
                parse_product_token(far.product) != product
                or far.raw_symbol == near.raw_symbol
                or not 60 <= pair_gap_days(near.expiry_at, far.expiry_at) <= 180
            ):
                continue
            # pair_bars is keyed by raw symbol in the existing strategy API.
            # Never silently choose between distinct instances of that symbol.
            if far.raw_symbol in far_symbols:
                raise ValueError(f"ambiguous far contract symbol: {far.raw_symbol}")
            far_symbols.add(far.raw_symbol)
            far_series.append((
                far,
                ContractMetadata(
                    contract_instance_id=far.contract_instance_id,
                    raw_symbol=far.raw_symbol,
                    product=product,
                    expiry_at=far.expiry_at,
                    identity_status="reconstructed_static",
                ),
                tuple(bar.datetime_nano for bar in far.bars),
            ))

        carry_previous: StrategyState | None = None
        for index, bar in enumerate(near.bars):
            start = max(0, index + 1 - _OBSERVATION_WINDOW)
            bars = near.bars[start:index + 1]
            expired = bar.datetime_nano >= expiry_nano or bar.ts >= near.expiry_at
            invalid_closes = any(
                not _finite_number(row.close, positive=True) for row in bars
            )
            # Do not coerce or remove invalid rows to manufacture a frame.
            frame = None if invalid_closes else build_structure_frame(bars)
            pair_candidates: list[ContractMetadata] = []
            pair_bars: dict[str, tuple[ContractBar, ...]] = {}
            far_references: dict[str, object] = {}
            if not expired:
                for far, metadata, timestamps in far_series:
                    stop = bisect_right(timestamps, bar.datetime_nano)
                    if stop == 0:
                        continue
                    prefix = far.bars[max(0, stop - _OBSERVATION_WINDOW):stop]
                    current = prefix[-1]
                    aligned = current.datetime_nano == bar.datetime_nano
                    invalid_price = not _finite_number(current.close, positive=True)
                    invalid_volume = any(
                        row.volume is not None
                        and not _finite_number(row.volume, positive=False)
                        for row in prefix[-20:]
                    )
                    far_references[far.raw_symbol] = {
                        "contract_instance_id": far.contract_instance_id,
                        "reference_expiry_at": far.expiry_at,
                        "prefix_rows": stop,
                        "window_rows": len(prefix),
                        "raw_timestamp_matches": aligned,
                        "invalid_close": invalid_price,
                        "invalid_volume_window": invalid_volume,
                        "last_observation": _reference(current),
                    }
                    # Missing rows are not forward filled. Reject invalid
                    # numeric inputs without compressing the liquidity window.
                    if not aligned or invalid_price or invalid_volume:
                        continue
                    pair_candidates.append(metadata)
                    pair_bars[far.raw_symbol] = prefix

            for strategy_id, strategy in sorted(registry.items()):
                is_carry = strategy_id == REGISTRY_TERM_STRUCTURE_CARRY
                context = StrategyContext(
                    strategy_id=strategy_id,
                    product=product,
                    contract_instance_id=near.contract_instance_id,
                    raw_symbol=near.raw_symbol,
                    decision_at=None,
                    clock_status=ClockStatus.OBSERVATION_ONLY,
                    completed_bar_id=f"{near.contract_instance_id}:{bar.datetime_nano}",
                    bars=bars,
                    structure=frame,
                    factors={},
                    expiry_at=near.expiry_at,
                    pair_candidates=tuple(pair_candidates) if is_carry else (),
                    pair_bars=pair_bars if is_carry else {},
                )
                # Evaluate every strategy on every original row, including
                # invalid windows. Numeric failures (e.g. signaling NaN) stay
                # explicit; no bar or clock is replaced to force a candidate.
                numeric_error = None
                try:
                    result = strategy.evaluate(context, carry_previous if is_carry else None)
                except (ValueError, OverflowError):
                    if not invalid_closes:
                        raise
                    result = None
                    numeric_error = "invalid_numeric_strategy_input"
                raw_candidate = (
                    result.diagnostics.get("candidate_direction") if result is not None else None
                )
                candidate = (
                    int(raw_candidate)
                    if isinstance(raw_candidate, int)
                    and not isinstance(raw_candidate, bool)
                    and raw_candidate in (-1, 0, 1)
                    else None
                )
                feature_status = (
                    "ready" if candidate is not None else
                    "warmup" if result is not None and result.status == "warmup"
                    else "unavailable"
                )
                reason = (
                    str(result.diagnostics.get("underlying_reason", result.reason))
                    if result is not None else numeric_error
                )
                if is_carry:
                    state = result.next_state if result is not None else None
                    carry_previous = (
                        StrategyState(
                            strategy_id=strategy_id,
                            contract_instance_id=near.contract_instance_id,
                            pair=state.pair,
                        ) if state is not None else None
                    )
                if invalid_closes:
                    candidate = None
                    feature_status = "unavailable"
                    reason = "invalid_close_window"
                if expired:
                    candidate = None
                    feature_status = "unavailable"
                    reason = "observation_at_or_after_reference_expiry"
                    carry_previous = None

                diagnostics = dict(result.diagnostics) if result is not None else {}
                diagnostics.update({
                    "candidate_direction": candidate,
                    "non_actionable": True,
                    "scope": "independent_candidate_events",
                    "state_scope": "pair_confirmation_only" if is_carry else "none",
                    "target_position": None,
                    "decision_at": None,
                    "clock_status": ClockStatus.OBSERVATION_ONLY.value,
                    "historical_execution_established": False,
                    "reference_expiry_at": near.expiry_at,
                    "expiry_quality": "reference_only",
                    "observation_eligible": not expired,
                    "observation_reference": _reference(bar),
                    "window_start_index": start,
                    "window_rows": len(bars),
                    "invalid_close_window": invalid_closes,
                    "strategy_parameters": dict(strategy.spec.parameters),
                    "strategy_status": result.status if result is not None else "unavailable",
                    "strategy_reason": result.reason if result is not None else reason,
                })
                if is_carry:
                    diagnostics.update({
                        "pair_alignment": "raw_timestamp_reference_only_not_verified_session",
                        "far_prefixes": far_references,
                        "pair_state": (
                            asdict(carry_previous.pair)
                            if carry_previous is not None and carry_previous.pair is not None
                            else None
                        ),
                    })
                signals.append(StudySignal(
                    contract_instance_id=near.contract_instance_id,
                    raw_symbol=near.raw_symbol,
                    product=near.product,
                    strategy_id=strategy_id,
                    index=index,
                    datetime_nano=bar.datetime_nano,
                    candidate_direction=candidate,
                    feature_status=feature_status,
                    reason=reason,
                    diagnostics={key: _json_safe(value) for key, value in diagnostics.items()},
                ))
    return tuple(signals)
