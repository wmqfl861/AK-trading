from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from tqsdk import BacktestFinished, TargetPosTask

from ak_trading.risk import limit_target_position, require_positive_position_limit
from ak_trading.runtime import is_valid_price, wait_until
from ak_trading.strategy import CompletedBarStrategy

DecisionSink = Callable[[Mapping[str, Any]], None]
TargetTaskFactory = Callable[[Any, str], Any]


@dataclass(frozen=True)
class RunSummary:
    reason: str
    updates: int
    decisions: int
    target_changes: int
    final_position: int | None


@dataclass(frozen=True)
class ShutdownSummary:
    flattened: bool
    task_finished: bool
    message: str


def _noop_sink(event: Mapping[str, Any]) -> None:
    del event


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _position_value(position: Any) -> int | None:
    value = _finite_float(getattr(position, "pos", None))
    if value is None or not value.is_integer():
        return None
    return int(value)


def _has_alive_orders(position: Any) -> bool:
    orders = getattr(position, "orders", None)
    if not hasattr(orders, "values"):
        return False
    return any(getattr(order, "status", "") == "ALIVE" for order in orders.values())


class StrategyRunner:
    """Drive one concrete futures contract through TqSdk's update loop."""

    def __init__(
        self,
        api: Any,
        symbol: str,
        strategy: CompletedBarStrategy,
        *,
        mode: str,
        duration_seconds: int = 300,
        max_abs_position: int = 1,
        allow_short: bool = False,
        decision_sink: DecisionSink | None = None,
        target_task_factory: TargetTaskFactory = TargetPosTask,
        run_id: str | None = None,
    ) -> None:
        if mode not in {"sim", "backtest"}:
            raise ValueError("mode must be 'sim' or 'backtest'")
        if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int):
            raise ValueError("duration_seconds must be a positive integer")
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be a positive integer")

        # Validate strategy.required_bars before any TqSdk API access.
        required_bars = strategy.required_bars
        if isinstance(required_bars, bool) or not isinstance(required_bars, int):
            raise ValueError("strategy.required_bars must be a positive integer")
        if not (1 <= required_bars <= 9998):
            raise ValueError("strategy.required_bars must be between 1 and 9998 inclusive")

        # Validate the risk boundary before opening subscriptions or tasks.
        max_abs_position = require_positive_position_limit(max_abs_position)
        if run_id is not None:
            if not isinstance(run_id, str) or not run_id.strip():
                raise ValueError("run_id must be a non-empty string or None")
            run_id = run_id.strip()

        self.api = api
        self.symbol = symbol
        self.strategy = strategy
        self.mode = mode
        self.duration_seconds = duration_seconds
        self.max_abs_position = max_abs_position
        self.allow_short = allow_short
        self.run_id = run_id
        self._required_bars = required_bars
        self._sink = decision_sink or _noop_sink
        self._target_task_factory = target_task_factory

        self.quote = api.get_quote(symbol)
        self.klines = api.get_kline_serial(
            symbol,
            duration_seconds,
            data_length=required_bars + 2,
        )
        self.account = api.get_account()
        self.position = api.get_position(symbol)
        self.target_task = target_task_factory(api, symbol)

        self._last_completed_bar: str | None = None
        self._last_requested_target: int | None = None
        self._updates = 0
        self._decisions = 0
        self._target_changes = 0

    def run(self, *, max_updates: int | None = None) -> RunSummary:
        """Run until backtest completion, interruption, or an optional test bound."""
        if max_updates is not None:
            if isinstance(max_updates, bool) or not isinstance(max_updates, int):
                raise ValueError("max_updates must be a positive integer")
            if max_updates <= 0:
                raise ValueError("max_updates must be a positive integer")

        self._emit("run_started")
        reason = "max_updates"
        try:
            while max_updates is None or self._updates < max_updates:
                self.api.wait_update()
                self._updates += 1
                self._process_update()
        except BacktestFinished:
            reason = "backtest_finished"
        except KeyboardInterrupt:
            self._finish_run("interrupted")
            raise
        return self._finish_run(reason)

    def _finish_run(self, reason: str) -> RunSummary:
        summary = RunSummary(
            reason=reason,
            updates=self._updates,
            decisions=self._decisions,
            target_changes=self._target_changes,
            final_position=_position_value(self.position),
        )
        self._emit(
            "run_finished",
            reason=summary.reason,
            updates=summary.updates,
            decisions=summary.decisions,
            target_changes=summary.target_changes,
            final_position=summary.final_position,
        )
        return summary

    def _process_update(self) -> None:
        if len(self.klines.index) < 2:
            return
        if not self.api.is_changing(self.klines.iloc[-1], "datetime"):
            return

        completed_bar = self.klines.iloc[-2]
        bar_datetime = completed_bar.get("datetime")
        if bar_datetime is None:
            return
        bar_key = str(bar_datetime)
        if not bar_key or bar_key == "nan" or bar_key == self._last_completed_bar:
            return
        self._last_completed_bar = bar_key
        self._decisions += 1

        # The last row is the newly opened, incomplete bar and is never a signal input.
        signal = self.strategy.evaluate(self.klines["close"].iloc[:-1])
        risk = limit_target_position(
            signal.target_position,
            max_abs_position=self.max_abs_position,
            allow_short=self.allow_short,
        )
        current_position = _position_value(self.position)
        last_price = _finite_float(getattr(self.quote, "last_price", None))
        action = "unchanged"
        block_reason: str | None = None

        if current_position is None:
            action = "blocked"
            block_reason = "position_not_ready"
        elif not is_valid_price(last_price):
            action = "blocked"
            block_reason = "quote_not_ready"
        elif risk.target_position is None:
            action = "no_signal"
        elif self.target_task.is_finished():
            action = "blocked"
            block_reason = "target_task_finished"
        elif risk.target_position == self._last_requested_target:
            action = "target_already_requested"
        elif self._last_requested_target is None and risk.target_position == current_position:
            self._last_requested_target = risk.target_position
            action = "already_at_target"
        else:
            self.target_task.set_target_volume(risk.target_position)
            self._last_requested_target = risk.target_position
            self._target_changes += 1
            action = "set_target"

        self._emit(
            "decision",
            bar_datetime=bar_key,
            close=_finite_float(completed_bar.get("close")),
            fast_ma=signal.fast_ma,
            slow_ma=signal.slow_ma,
            signal_target=signal.target_position,
            signal_reason=signal.reason,
            risk_target=risk.target_position,
            risk_reason=risk.reason,
            current_position=current_position,
            requested_target=self._last_requested_target,
            last_price=last_price,
            action=action,
            block_reason=block_reason,
        )

    def shutdown_live(self, *, timeout: float = 15.0) -> ShutdownSummary:
        """Flatten a live local-sim position, then cancel and drain its target task."""
        if self.mode != "sim":
            raise RuntimeError("shutdown_live must not run after historical data ends")
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout must be a finite positive number") from exc
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("timeout must be a finite positive number")
        deadline = time.monotonic() + timeout_value

        def wait_with_budget(predicate: Callable[[], bool]) -> bool:
            if predicate():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            return wait_until(self.api, predicate, timeout=remaining)

        messages: list[str] = []
        task = self.target_task
        current_position = _position_value(self.position)
        if task.is_finished() and current_position not in {None, 0}:
            task = self._target_task_factory(self.api, self.symbol)
            self.target_task = task
            messages.append("replaced_finished_task")

        if not task.is_finished():
            task.set_target_volume(0)
            # Send the target change before checking an initially-flat position.
            remaining = max(0.0, deadline - time.monotonic())
            if remaining > 0:
                self.api.wait_update(deadline=time.time() + min(remaining, 0.5))

        flattened = wait_with_budget(
            lambda: _position_value(self.position) == 0
            and not _has_alive_orders(self.position),
        )
        messages.append("flat" if flattened else "flatten_timeout")

        if not task.is_finished():
            task.cancel()
            task_finished = wait_with_budget(task.is_finished)
        else:
            task_finished = True
        messages.append("task_finished" if task_finished else "cancel_timeout")

        final_flat = _position_value(self.position) == 0 and not _has_alive_orders(
            self.position
        )
        self._emit(
            "shutdown",
            flattened=final_flat,
            task_finished=task_finished,
            message=",".join(messages),
        )
        return ShutdownSummary(final_flat, task_finished, ",".join(messages))

    def _emit(self, event: str, **values: Any) -> None:
        record = {
            "event": event,
            "mode": self.mode,
            "symbol": self.symbol,
            "duration_seconds": self.duration_seconds,
            "balance": _finite_float(getattr(self.account, "balance", None)),
            "available": _finite_float(getattr(self.account, "available", None)),
            "margin": _finite_float(getattr(self.account, "margin", None)),
            "position": _position_value(self.position),
        }
        if self.run_id is not None:
            record["run_id"] = self.run_id
        record.update(values)
        self._sink(record)
