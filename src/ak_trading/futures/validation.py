"""Fixed folds, split documents and performance metrics (finalized-plan §十一).

Pure functions over frozen D sequences and completed ledgers. Null
semantics are explicit: zero-variance Sharpe, insolvent folds and empty
trade samples return None with reasons, never fabricated numbers.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from statistics import median
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping, Sequence

from ak_trading.futures.data import day_sequence_digest

__all__ = [
    "MIN_FOLD_HISTORY",
    "DEV_WINDOW",
    "HOLDOUT_DAYS",
    "TRAIN_WINDOW",
    "MAX_RETAINED_FOLDS",
    "Fold",
    "Folds",
    "build_folds",
    "build_splits_document",
    "daily_returns",
    "sharpe_ratio",
    "annualized_volatility",
    "max_drawdown",
    "annualized_turnover",
    "trade_statistics",
    "summarize_run",
    "aggregate_unit_index",
]

MIN_FOLD_HISTORY = 441  # 252 train + >=63 dev OOS + 126 holdout
TRAIN_WINDOW = 252
DEV_WINDOW = 63
HOLDOUT_DAYS = 126
MAX_RETAINED_FOLDS = 8


@dataclass(frozen=True)
class Fold:
    kind: str  # development | final_holdout
    index: int
    train: tuple[date, ...]
    oos: tuple[date, ...]
    retained: bool = True


@dataclass(frozen=True)
class Folds:
    status: str  # available | insufficient_history
    reason: str | None
    n_days: int
    holdout_start_index: int
    k_all: tuple[int, ...]
    retained_k: tuple[int, ...]
    dropped_k: tuple[int, ...]
    folds: tuple[Fold, ...]
    final_holdout: tuple[date, ...]
    final_holdout_train_context: tuple[date, ...]


def build_folds(days: Sequence[date]) -> Folds:
    """Fixed-252 rolling folds right-aligned to the holdout (§十一.2).

    J_count = floor((h-252)/63), K_all = sorted(h - 63*j for j in 1..J_count),
    K = last min(8, J_count) entries; train=D[k-252:k], dev OOS=D[k:k+63].
    The final holdout D[h:n] uses D[h-252:h] as frozen context.
    """
    n = len(days)
    h = n - HOLDOUT_DAYS
    if n < MIN_FOLD_HISTORY:
        return Folds(
            status="insufficient_history",
            reason=f"n={n}<{MIN_FOLD_HISTORY}",
            n_days=n,
            holdout_start_index=h,
            k_all=(),
            retained_k=(),
            dropped_k=(),
            folds=(),
            final_holdout=(),
            final_holdout_train_context=(),
        )
    j_count = (h - TRAIN_WINDOW) // DEV_WINDOW
    k_all = tuple(sorted(h - DEV_WINDOW * j for j in range(1, j_count + 1)))
    retained = k_all[-MAX_RETAINED_FOLDS:]
    dropped = k_all[: max(0, len(k_all) - MAX_RETAINED_FOLDS)]
    folds: list[Fold] = []
    for index, k in enumerate(k_all, start=1):
        folds.append(
            Fold(
                kind="development",
                index=index,
                train=tuple(days[k - TRAIN_WINDOW : k]),
                oos=tuple(days[k : k + DEV_WINDOW]),
                retained=k in retained,
            )
        )
    return Folds(
        status="available",
        reason=None,
        n_days=n,
        holdout_start_index=h,
        k_all=k_all,
        retained_k=retained,
        dropped_k=dropped,
        folds=tuple(folds),
        final_holdout=tuple(days[h:n]),
        final_holdout_train_context=tuple(days[h - TRAIN_WINDOW : h]),
    )


def build_splits_document(
    days: Sequence[date],
    folds: Folds,
    *,
    universe: Sequence[str],
    span: tuple[str, str],
    construction_sha256: str,
    terminal_schedule: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    """splits.json content: full K_all, retained/dropped, dates, hashes."""
    return {
        "rule": "fixed_252_rolling_right_aligned_holdout_v1",
        "day_sequence_sha256": day_sequence_digest(days),
        "construction_sha256": construction_sha256,
        "universe": list(universe),
        "span": list(span),
        "n_days": folds.n_days,
        "holdout_start_index": folds.holdout_start_index,
        "k_all": list(folds.k_all),
        "retained_k": list(folds.retained_k),
        "dropped_k": list(folds.dropped_k),
        "retention_note": (
            "only the last min(8, J_count) development folds are retained; "
            "earlier history never enters development OOS (§十一.3)"
        ),
        "folds": [
            {
                "kind": fold.kind,
                "index": fold.index,
                "retained": fold.retained,
                "train": [d.isoformat() for d in fold.train],
                "oos": [d.isoformat() for d in fold.oos],
            }
            for fold in folds.folds
        ],
        "final_holdout": [d.isoformat() for d in folds.final_holdout],
        "final_holdout_train_context": [
            d.isoformat() for d in folds.final_holdout_train_context
        ],
        "terminal_schedule": dict(terminal_schedule or {}),
    }


def daily_returns(nav_close_by_day: Sequence[Decimal], initial_nav: Decimal) -> list[float | None]:
    """r_d = NAV_d / NAV_{d-1} - 1 with NAV_{-1} = initial NAV (§十一.8).

    A zero previous NAV yields None (undefined), never a substituted zero;
    downstream statistics skip None days and report the skipped count.
    """
    returns: list[float | None] = []
    previous: Decimal | float = initial_nav
    for nav in nav_close_by_day:
        if previous == 0:
            returns.append(None)
        else:
            returns.append(float(nav / previous) - 1.0)
        previous = nav
    return returns


def sharpe_ratio(returns: Sequence[float | None]) -> tuple[float | None, str | None]:
    """Zero-rate Sharpe = sqrt(252)*mean/std (ddof=1); zero variance -> None."""
    values = [r for r in returns if r is not None]
    if len(values) < 2:
        return None, "insufficient_observations"
    returns = values
    mean = math.fsum(returns) / len(returns)
    variance = math.fsum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if variance <= 0:
        return None, "zero_variance"
    return math.sqrt(252.0) * mean / math.sqrt(variance), None


def annualized_volatility(returns: Sequence[float | None]) -> float | None:
    returns = [r for r in returns if r is not None]
    if len(returns) < 2:
        return None
    mean = math.fsum(returns) / len(returns)
    variance = math.fsum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if variance <= 0:
        return 0.0
    return math.sqrt(252.0) * math.sqrt(variance)


def max_drawdown(
    nav_close_by_day: Sequence[Decimal],
    initial_nav: Decimal,
    days: Sequence[date],
) -> Mapping[str, object]:
    """Peak-to-trough duration, with initial NAV at the start of D[0].

    D-day counts include peak and trough reporting dates, matching the
    existing close-peak convention. No prior trading date is inferred.
    """
    unavailable = (
        "empty_fold" if not days else
        "nav_missing_for_fold_days" if len(nav_close_by_day) != len(days) else
        "non_positive_initial_nav" if initial_nav <= 0 else None
    )
    peak = initial_nav
    peak_day = days[0] if days else None
    peak_basis = "initial_nav_start_of_first_reporting_day"
    max_dd = 0.0
    trough_day = None
    worst_peak_day = None
    worst_peak_basis = None
    if unavailable is None:
        for nav, day in zip(nav_close_by_day, days):
            if nav > peak:
                peak, peak_day, peak_basis = nav, day, "reporting_day_close"
            dd = float((peak - nav) / peak)
            if dd > max_dd:
                max_dd, trough_day = dd, day
                worst_peak_day, worst_peak_basis = peak_day, peak_basis
    has_drawdown = trough_day is not None
    no_peak_reason = unavailable or (None if has_drawdown else "no_drawdown")
    return {
        "max_drawdown": None if unavailable else max_dd,
        "max_drawdown_reason": unavailable,
        "peak_day": worst_peak_day.isoformat() if worst_peak_day else None,
        "peak_day_reason": no_peak_reason,
        "peak_basis": worst_peak_basis,
        "peak_basis_reason": no_peak_reason,
        "trough_day": trough_day.isoformat() if trough_day else None,
        "trough_day_reason": no_peak_reason,
        "drawdown_duration_d_days": (
            None if unavailable else
            sum(worst_peak_day <= day <= trough_day for day in days) if has_drawdown else 0
        ),
        "drawdown_duration_d_days_reason": unavailable,
        "drawdown_duration_calendar_days": (
            None if unavailable else (trough_day - worst_peak_day).days if has_drawdown else 0
        ),
        "drawdown_duration_calendar_days_reason": unavailable,
        "drawdown_duration_basis": "peak_to_trough_inclusive_reporting_dates",
    }


def annualized_turnover(
    *,
    turnover_notional: Decimal,
    nav_close_by_day: Sequence[Decimal],
    fold_days: Sequence[date],
    incomplete: bool,
    insolvency: bool,
) -> tuple[float | None, str | None]:
    """turnover / mean(NAV over ALL fold D days) * 252 / len(D) (§十一.9)."""
    if incomplete or insolvency:
        return None, "incomplete_or_insolved_ledger"
    if not fold_days or not nav_close_by_day:
        return None, "empty_fold"
    if len(nav_close_by_day) != len(fold_days):
        return None, "nav_missing_for_fold_days"
    mean_nav = sum(nav_close_by_day, Decimal("0")) / Decimal(len(nav_close_by_day))
    if mean_nav <= 0:
        return None, "non_positive_mean_nav"
    value = float(turnover_notional / mean_nav) * 252.0 / len(fold_days)
    return value, None


def trade_statistics(
    trades: Sequence[object], *, open_trade_count: int | None = None,
) -> Mapping[str, object]:
    """Stable closed-trade schema; open counts mean nonempty FIFO batches."""
    if open_trade_count is not None and (
        isinstance(open_trade_count, bool) or not isinstance(open_trade_count, int)
        or open_trade_count < 0
    ):
        raise ValueError("open_trade_count must be a non-negative integer or None")
    nets = [float(t.net_pnl) for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    gross_win = math.fsum(wins)
    gross_loss = abs(math.fsum(losses))
    empty_reason = "no_closed_trades" if not nets else None
    loss_reason = empty_reason or ("no_losses" if not losses else None)
    mean_win = gross_win / len(wins) if wins else None
    mean_loss = gross_loss / len(losses) if losses else None
    loss_streak = max_loss_streak = 0
    for trade in sorted(trades, key=lambda t: getattr(t, "closed_event_time", datetime.min)):
        loss_streak = loss_streak + 1 if trade.net_pnl < 0 else 0
        max_loss_streak = max(max_loss_streak, loss_streak)
    return {
        "closed_trade_count": len(trades),
        "open_trade_count": open_trade_count,
        "open_trade_count_reason": "open_fifo_batches_not_supplied" if open_trade_count is None else None,
        "open_trade_count_basis": "nonempty_fifo_open_batches",
        "reason": empty_reason,
        "median_convention": "interpolated_midpoint",
        "win_rate": len(wins) / len(nets) if nets else None,
        "win_rate_reason": empty_reason,
        "mean_net_pnl": math.fsum(nets) / len(nets) if nets else None,
        "mean_net_pnl_reason": empty_reason,
        "median_net_pnl": median(nets) if nets else None,
        "median_net_pnl_reason": empty_reason,
        "mean_win": mean_win,
        "mean_win_reason": empty_reason or ("no_wins" if not wins else None),
        "mean_loss": mean_loss,
        "mean_loss_reason": loss_reason,
        "mean_win_loss_ratio": mean_win / mean_loss if mean_win is not None and mean_loss else None,
        "mean_win_loss_ratio_reason": loss_reason or ("no_wins" if not wins else None),
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "profit_factor_reason": loss_reason,
        "max_consecutive_losses": max_loss_streak if nets else None,
        "max_consecutive_losses_reason": empty_reason,
    }


def summarize_run(
    *,
    days: Sequence[date],
    nav_close_by_day: Sequence[Decimal],
    initial_nav: Decimal,
    turnover_notional: Decimal,
    trades: Sequence[object],
    status: str,
    marks: Sequence[object] | None = None,
    blocked: Sequence[Mapping[str, str]] | None = None,
    fees_cny: Decimal | None = None,
    slippage_cny: Decimal | None = None,
    roll_costs_cny: Decimal | None = None,
    final_unrealized_pnl: Decimal | None = None,
    total_gross_pnl: Decimal | None = None,
    open_trade_count: int | None = None,
) -> Mapping[str, object]:
    """Standard metric block for one fold ledger (§十一.8 mandatory set).

    Includes the full mandatory metric list with §十一.9 null semantics:
    annualized turnover is null for incomplete/insolvent ledgers or a
    non-positive mean NAV; exposure/margin come from the equity marks;
    blocked actions are counted by reason. Missing inputs stay None with a
    reason instead of being fabricated.
    """
    returns = daily_returns(nav_close_by_day, initial_nav)
    sharpe, sharpe_reason = sharpe_ratio(returns)
    final_nav = nav_close_by_day[-1] if nav_close_by_day else initial_nav
    total_return = float(final_nav / initial_nav) - 1.0 if initial_nav else None
    skipped_return_days = sum(1 for r in returns if r is None)

    incomplete = status in ("incomplete", "insolvent")
    ann_turnover, turnover_reason = annualized_turnover(
        turnover_notional=turnover_notional,
        nav_close_by_day=nav_close_by_day,
        fold_days=days,
        incomplete=incomplete,
        insolvency=status == "insolvent",
    )

    exposure_mean = exposure_max = None
    margin_mean = margin_max = None
    invested_days = None
    if marks:
        ratios = [
            float(getattr(m, "gross_notional", Decimal("0"))) / float(getattr(m, "equity", Decimal("1")))
            for m in marks
            if getattr(m, "equity", 0)
        ]
        if ratios:
            exposure_mean = math.fsum(ratios) / len(ratios)
            exposure_max = max(ratios)
        margin_ratios = [
            float(m.required_margin) / float(m.equity)
            for m in marks
            if getattr(m, "required_margin", None) is not None and m.equity
        ]
        if margin_ratios:
            margin_mean = math.fsum(margin_ratios) / len(margin_ratios)
            margin_max = max(margin_ratios)
        invested_days = sum(1 for m in marks if getattr(m, "invested", False))

    blocked_counts: dict[str, int] = {}
    for entry in blocked or ():
        reason = str(entry.get("reason", "unknown"))
        blocked_counts[reason] = blocked_counts.get(reason, 0) + 1

    volatility = annualized_volatility(returns)
    span_days = (days[-1] - days[0]).days if days else 0
    if incomplete:
        cagr, cagr_reason = None, "incomplete_or_insolved_ledger"
    elif not days or len(days) != len(nav_close_by_day):
        cagr, cagr_reason = None, "nav_missing_for_fold_days"
    elif initial_nav <= 0 or any(nav <= 0 for nav in nav_close_by_day):
        cagr, cagr_reason = None, "non_positive_nav"
    elif span_days < 365.25:
        cagr, cagr_reason = None, "span_less_than_one_year"
    else:
        cagr = math.expm1(math.log(float(final_nav / initial_nav)) * 365.25 / span_days)
        cagr_reason = None

    return {
        "status": status,
        "days": len(days),
        "initial_nav": str(initial_nav),
        "final_nav": str(final_nav),
        "total_gross_pnl": str(total_gross_pnl) if total_gross_pnl is not None else None,
        "total_gross_pnl_reason": "ledger_gross_not_supplied" if total_gross_pnl is None else None,
        "total_net_pnl": str(final_nav - initial_nav),
        "total_return": total_return,
        "total_return_reason": "zero_initial_nav" if total_return is None else None,
        "cagr": cagr,
        "cagr_reason": cagr_reason,
        "sharpe": sharpe,
        "sharpe_reason": sharpe_reason,
        "annualized_volatility": volatility,
        "annualized_volatility_reason": "insufficient_observations" if volatility is None else None,
        "skipped_return_days": skipped_return_days,
        **dict(max_drawdown(nav_close_by_day, initial_nav, days)),
        "turnover_notional": str(turnover_notional),
        "annualized_turnover": ann_turnover,
        "annualized_turnover_reason": turnover_reason,
        "exposure_mean": exposure_mean,
        "exposure_mean_reason": "exposure_marks_unavailable" if exposure_mean is None else None,
        "exposure_max": exposure_max,
        "exposure_max_reason": "exposure_marks_unavailable" if exposure_max is None else None,
        "invested_days": invested_days,
        "invested_days_reason": "marks_not_supplied" if invested_days is None else None,
        "margin_mean_ratio": margin_mean,
        "margin_mean_ratio_reason": "margin_marks_unavailable" if margin_mean is None else None,
        "margin_max_ratio": margin_max,
        "margin_max_ratio_reason": "margin_marks_unavailable" if margin_max is None else None,
        "fees_cny": str(fees_cny) if fees_cny is not None else None,
        "fees_cny_reason": "fill_costs_not_supplied" if fees_cny is None else None,
        "slippage_cny": str(slippage_cny) if slippage_cny is not None else None,
        "slippage_cny_reason": "fill_costs_not_supplied" if slippage_cny is None else None,
        "roll_costs_cny": str(roll_costs_cny) if roll_costs_cny is not None else None,
        "roll_costs_cny_reason": "roll_fill_attribution_not_supplied" if roll_costs_cny is None else None,
        "roll_costs_basis": "fees_plus_slippage_on_explicitly_roll_tagged_fills",
        "final_unrealized_pnl": (
            str(final_unrealized_pnl) if final_unrealized_pnl is not None else None
        ),
        "final_unrealized_pnl_reason": "unrealized_mark_not_supplied" if final_unrealized_pnl is None else None,
        "blocked_action_counts": blocked_counts,
        "trades": dict(trade_statistics(trades, open_trade_count=open_trade_count)),
    }


def aggregate_unit_index(
    fold_unit_returns: Sequence[Sequence[float]],
) -> Mapping[str, object]:
    """Concatenate per-fold UNIT-return indices (§十一.12), never CNY NAV."""
    if not fold_unit_returns:
        return {"status": "no_complete_folds", "unit_index_final": None}
    index = 1.0
    path: list[float] = [1.0]
    for returns in fold_unit_returns:
        for r in returns:
            index *= 1.0 + r
            path.append(index)
    return {
        "status": "available",
        "reset_initial_capital_per_fold": True,
        "non_continuous_account_path": True,
        "unit_index_final": index,
        "unit_index_path_length": len(path),
    }
