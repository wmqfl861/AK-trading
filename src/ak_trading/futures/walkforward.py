"""Pure walk-forward windows and aggregation over independent OOS accounts.

The host owns replay, account resets, and causal warmup. This module only
supplies immutable windows and preserves the callback's opaque payloads.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, DecimalException, localcontext
from typing import Callable, Generic, Mapping, Sequence, TypeVar

from ak_trading.futures.validation import Folds, TRAIN_WINDOW, build_folds

__all__ = [
    "ReplayWindow",
    "WindowResult",
    "WalkForwardResult",
    "run_walk_forward",
    "aggregate_oos_ledgers",
]

T = TypeVar("T")


@dataclass(frozen=True)
class ReplayWindow:
    kind: str
    fold_index: int | None
    days: tuple[date, ...]
    warmup_days: tuple[date, ...]

    @property
    def window_id(self) -> str:
        if self.kind == "final_holdout":
            return "final_holdout"
        phases = {"development_train": "train", "development_oos": "oos"}
        if self.kind not in phases or self.fold_index is None:
            raise ValueError("development window requires a known kind and fold_index")
        return f"development/fold-{self.fold_index:02d}/{phases[self.kind]}"


@dataclass(frozen=True)
class WindowResult(Generic[T]):
    window: ReplayWindow
    payload: T


@dataclass(frozen=True)
class WalkForwardResult(Generic[T]):
    status: str
    reason: str | None
    folds: Folds
    results: tuple[WindowResult[T], ...]


def run_walk_forward(
    days: Sequence[date],
    replay_window: Callable[[ReplayWindow], T],
) -> WalkForwardResult[T]:
    """Execute retained train/OOS windows followed by one final holdout.

    Each callback must create its own account and mutable replay state. It
    may warm indicators on ``warmup_days`` and use the last warmup close
    for the first scored open, but must not execute or score warmup days.
    No parameters are selected here and no callback payload is inspected.
    ``executed`` means every scheduled callback returned, not that its
    ledger was complete or profitable. Callback exceptions propagate.
    """
    frozen_days = tuple(days)
    for index, day in enumerate(frozen_days):
        if not isinstance(day, date) or isinstance(day, datetime):
            raise TypeError(f"days[{index}] must be a date, not a datetime or other value")
        if index and day <= frozen_days[index - 1]:
            raise ValueError("days must be strictly ascending and unique")

    folds = build_folds(frozen_days)
    if folds.status != "available":
        return WalkForwardResult(
            status=folds.status, reason=folds.reason, folds=folds, results=(),
        )

    positions = {day: index for index, day in enumerate(frozen_days)}
    windows: list[ReplayWindow] = []
    for fold in folds.folds:
        if not fold.retained:
            continue
        train_start = positions[fold.train[0]]
        windows.append(ReplayWindow(
            kind="development_train",
            fold_index=fold.index,
            days=fold.train,
            warmup_days=frozen_days[max(0, train_start - TRAIN_WINDOW):train_start],
        ))
        windows.append(ReplayWindow(
            kind="development_oos",
            fold_index=fold.index,
            days=fold.oos,
            warmup_days=fold.train,
        ))
    windows.append(ReplayWindow(
        kind="final_holdout",
        fold_index=None,
        days=folds.final_holdout,
        warmup_days=folds.final_holdout_train_context,
    ))
    results = tuple(WindowResult(window, replay_window(window)) for window in windows)
    return WalkForwardResult(status="executed", reason=None, folds=folds, results=results)


def _positive_nav(value: object) -> tuple[Decimal | None, str | None]:
    if not isinstance(value, (Decimal, str)):
        return None, "invalid_nav_type"
    try:
        nav = value if isinstance(value, Decimal) else Decimal(value)
    except DecimalException:
        return None, "invalid_decimal"
    if not nav.is_finite():
        return None, "non_finite_nav"
    if nav <= 0:
        return None, "non_positive_nav"
    return nav, None


def aggregate_oos_ledgers(
    records: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    """Join unit returns from complete development OOS ledgers only.

    The caller selects development OOS records, excluding training and the
    final holdout. Each record supplies ``window_id``, ``status``,
    ``initial_nav``, and a nonempty ``nav_close`` sequence. NAV values must
    be Decimal or decimal strings. No incomplete record is silently dropped:
    every validation failure is retained and any failure nulls all aggregate
    metrics. CNY balances are never joined across independent accounts.
    """
    reasons: list[dict[str, object]] = []
    result: dict[str, object] = {
        "status": "unavailable",
        "reasons": reasons,
        "unit_index_path": None,
        "unit_index_final": None,
        "total_return": None,
        "reset_initial_capital_per_fold": True,
        "non_continuous_account_path": True,
    }
    if not records:
        reasons.append({"window_id": None, "reason": "no_oos_ledgers"})
        return result

    validated: list[tuple[str, Decimal, list[Decimal]]] = []
    seen: set[str] = set()
    for record_index, record in enumerate(records):
        window_id = record.get("window_id")
        context: dict[str, object] = {
            "record_index": record_index,
            "window_id": window_id if isinstance(window_id, str) else None,
        }
        if not isinstance(window_id, str) or not window_id:
            reasons.append({**context, "reason": "invalid_window_id"})
        elif window_id in seen:
            reasons.append({**context, "reason": "duplicate_window_id"})
        else:
            seen.add(window_id)
        status = record.get("status")
        if status != "complete":
            reasons.append({
                **context, "reason": "ledger_not_complete", "status": str(status),
            })
        initial, reason = _positive_nav(record.get("initial_nav"))
        if reason is not None:
            reasons.append({**context, "field": "initial_nav", "reason": reason})
        nav_close = record.get("nav_close")
        navs: list[Decimal] = []
        if not isinstance(nav_close, Sequence) or isinstance(nav_close, (str, bytes)):
            reasons.append({**context, "field": "nav_close", "reason": "invalid_nav_sequence"})
        elif not nav_close:
            reasons.append({**context, "field": "nav_close", "reason": "empty_nav_close"})
        else:
            for nav_index, value in enumerate(nav_close):
                nav, reason = _positive_nav(value)
                if reason is not None:
                    reasons.append({
                        **context, "field": "nav_close", "nav_index": nav_index,
                        "reason": reason,
                    })
                else:
                    assert nav is not None
                    navs.append(nav)
        if isinstance(window_id, str) and initial is not None and navs:
            validated.append((window_id, initial, navs))

    if reasons:
        result["status"] = "incomplete"
        return result

    path = [1.0]
    with localcontext() as decimal_context:
        decimal_context.prec = 50
        unit_base = Decimal("1")
        for window_id, initial, navs in validated:
            # Daily returns telescope to NAV_t / initial_NAV within each
            # independent account; unit_base carries only the unit index.
            for nav_index, nav in enumerate(navs):
                try:
                    unit_value = unit_base * (nav / initial)
                    point = float(unit_value)
                    if not math.isfinite(point) or point <= 0:
                        raise OverflowError("unit index cannot be represented as a positive finite float")
                    path.append(point)
                except (DecimalException, OverflowError, ValueError):
                    reasons.append({
                        "window_id": window_id, "nav_index": nav_index,
                        "reason": "unit_index_not_representable",
                    })
            try:
                unit_base *= navs[-1] / initial
            except DecimalException:
                reasons.append({"window_id": window_id, "reason": "unit_index_not_representable"})
                break
    if reasons:
        result["status"] = "unavailable"
        return result
    result.update({
        "status": "available",
        "unit_index_path": path,
        "unit_index_final": path[-1],
        "total_return": path[-1] - 1.0,
    })
    return result
