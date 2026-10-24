from types import SimpleNamespace
from typing import Iterable

import pandas as pd
import pytest

import ak_trading.runner as runner_module
from ak_trading.runner import StrategyRunner
from ak_trading.strategy import DualMAStrategy, SignalDecision, CompletedBarStrategy


class FakeTargetTask:
    def __init__(self, api, symbol):
        self.api = api
        self.symbol = symbol
        self.targets = []
        self.cancelled = False

    def set_target_volume(self, target):
        self.targets.append(target)
        if target == 0 and self.api.flatten_immediately:
            self.api.position.pos = 0

    def cancel(self):
        self.cancelled = True

    def is_finished(self):
        return self.cancelled


class FakeApi:
    def __init__(self, closes):
        self.frame = pd.DataFrame(
            {
                "datetime": list(range(1, len(closes) + 1)),
                "close": closes,
            }
        )
        self.quote = SimpleNamespace(last_price=100.0)
        self.account = SimpleNamespace(balance=1_000_000, available=900_000, margin=0)
        self.position = SimpleNamespace(pos=0, orders={})
        self.wait_calls = 0
        self.flatten_immediately = True
        self.data_length = None
        self.get_quote_calls = 0
        self.get_kline_serial_calls = 0
        self.get_account_calls = 0
        self.get_position_calls = 0

    def get_quote(self, symbol):
        self.get_quote_calls += 1
        return self.quote

    def get_kline_serial(self, symbol, duration_seconds, data_length):
        self.get_kline_serial_calls += 1
        self.data_length = data_length
        return self.frame

    def get_account(self):
        self.get_account_calls += 1
        return self.account

    def get_position(self, symbol):
        self.get_position_calls += 1
        return self.position

    def wait_update(self, deadline=None):
        self.wait_calls += 1
        return True

    def is_changing(self, obj, field):
        return True


def test_runner_excludes_incomplete_last_bar_and_deduplicates():
    api = FakeApi([1.0, 2.0, 3.0, -100.0])
    records = []
    runner = StrategyRunner(
        api,
        "SHFE.rb2610",
        DualMAStrategy(2, 3),
        mode="sim",
        decision_sink=records.append,
        target_task_factory=FakeTargetTask,
    )

    summary = runner.run(max_updates=2)

    assert runner.target_task.targets == [1]
    assert summary.decisions == 1
    assert summary.target_changes == 1
    decisions = [record for record in records if record["event"] == "decision"]
    assert len(decisions) == 1
    assert decisions[0]["close"] == 3.0
    assert decisions[0]["signal_target"] == 1


def test_runner_handles_backtest_finished(monkeypatch: pytest.MonkeyPatch):
    class FakeBacktestFinished(Exception):
        pass

    api = FakeApi([1.0, 2.0, 3.0, 4.0])

    def finish(deadline=None):
        raise FakeBacktestFinished

    api.wait_update = finish
    monkeypatch.setattr(runner_module, "BacktestFinished", FakeBacktestFinished)
    runner = StrategyRunner(
        api,
        "SHFE.rb2610",
        DualMAStrategy(2, 3),
        mode="backtest",
        target_task_factory=FakeTargetTask,
    )

    summary = runner.run()
    assert summary.reason == "backtest_finished"
    assert summary.updates == 0


def test_runner_records_summary_before_reraising_keyboard_interrupt():
    api = FakeApi([1.0, 2.0, 3.0, 4.0])
    records = []
    interruption = KeyboardInterrupt("stop")

    def interrupt(deadline=None):
        raise interruption

    api.wait_update = interrupt
    runner = StrategyRunner(
        api,
        "SHFE.rb2610",
        DualMAStrategy(2, 3),
        mode="sim",
        run_id="run-a",
        decision_sink=records.append,
        target_task_factory=FakeTargetTask,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        runner.run()

    assert raised.value is interruption
    assert records[-1] == {
        "event": "run_finished",
        "mode": "sim",
        "symbol": "SHFE.rb2610",
        "duration_seconds": 300,
        "balance": 1_000_000.0,
        "available": 900_000.0,
        "margin": 0.0,
        "position": 0,
        "run_id": "run-a",
        "reason": "interrupted",
        "updates": 0,
        "decisions": 0,
        "target_changes": 0,
        "final_position": 0,
    }


def test_live_shutdown_flattens_then_cancels_task():
    api = FakeApi([1.0, 2.0, 3.0, 4.0])
    api.position.pos = 1
    runner = StrategyRunner(
        api,
        "SHFE.rb2610",
        DualMAStrategy(2, 3),
        mode="sim",
        target_task_factory=FakeTargetTask,
    )

    summary = runner.shutdown_live(timeout=1.0)

    assert runner.target_task.targets == [0]
    assert runner.target_task.cancelled
    assert summary.flattened
    assert summary.task_finished


def test_alive_orders_are_detected():
    api = FakeApi([1.0, 2.0, 3.0, 4.0])
    api.position.orders = {"pending": SimpleNamespace(status="ALIVE")}
    assert runner_module._has_alive_orders(api.position)
    api.position.orders["pending"].status = "FINISHED"
    assert not runner_module._has_alive_orders(api.position)


def test_runner_ignores_short_or_unidentified_completed_bars():
    short_api = FakeApi([1.0])
    short_runner = StrategyRunner(
        short_api,
        "SHFE.rb2610",
        DualMAStrategy(2, 3),
        mode="sim",
        target_task_factory=FakeTargetTask,
    )
    assert short_runner.run(max_updates=1).decisions == 0

    nan_bar_api = FakeApi([1.0, 2.0, 3.0, 4.0])
    nan_bar_api.frame.loc[len(nan_bar_api.frame) - 2, "datetime"] = float("nan")
    nan_runner = StrategyRunner(
        nan_bar_api,
        "SHFE.rb2610",
        DualMAStrategy(2, 3),
        mode="sim",
        target_task_factory=FakeTargetTask,
    )
    assert nan_runner.run(max_updates=1).decisions == 0
    assert nan_runner.target_task.targets == []

    none_bar_api = FakeApi([1.0, 2.0, 3.0, 4.0])
    none_bar_api.frame["datetime"] = none_bar_api.frame["datetime"].astype(object)
    none_bar_api.frame.at[len(none_bar_api.frame) - 2, "datetime"] = None
    none_runner = StrategyRunner(
        none_bar_api,
        "SHFE.rb2610",
        DualMAStrategy(2, 3),
        mode="sim",
        target_task_factory=FakeTargetTask,
    )
    assert none_runner.run(max_updates=1).decisions == 0
    assert none_runner.target_task.targets == []


@pytest.mark.parametrize(
    ("setup", "expected_reason"),
    [
        (lambda api: setattr(api.position, "pos", None), "position_not_ready"),
        (lambda api: setattr(api.quote, "last_price", float("nan")), "quote_not_ready"),
        (lambda api: setattr(api, "finished_target", True), "target_task_finished"),
    ],
)
def test_runner_journals_defensive_blocks(setup, expected_reason):
    class ControlledTargetTask(FakeTargetTask):
        def is_finished(self):
            return self.cancelled or self.api.finished_target

    api = FakeApi([1.0, 2.0, 3.0, 4.0])
    api.finished_target = False
    setup(api)
    records = []
    runner = StrategyRunner(
        api,
        "SHFE.rb2610",
        DualMAStrategy(2, 3),
        mode="sim",
        decision_sink=records.append,
        target_task_factory=ControlledTargetTask,
    )

    runner.run(max_updates=1)

    decision = next(record for record in records if record["event"] == "decision")
    assert decision["action"] == "blocked"
    assert decision["block_reason"] == expected_reason
    assert runner.target_task.targets == []


def test_backtest_cannot_use_live_shutdown():
    api = FakeApi([1.0, 2.0, 3.0, 4.0])
    runner = StrategyRunner(
        api,
        "SHFE.rb2610",
        DualMAStrategy(2, 3),
        mode="backtest",
        target_task_factory=FakeTargetTask,
    )
    with pytest.raises(RuntimeError, match="historical"):
        runner.shutdown_live()


def test_runner_requests_correct_data_length_for_protocol():
    """Prove runner uses strategy.required_bars + 2 and passes only completed closes (excludes forming final row)."""

    class StubStrategy:
        def __init__(self, required_bars: int = 3):
            self.required_bars = required_bars
            self.completed_closes = []

        def evaluate(self, completed_closes: Iterable[object]) -> SignalDecision:
            self.completed_closes = list(completed_closes)
            # Minimal stub returning a no-target signal so no target task change
            return SignalDecision(None, "stub_no_target")

    api = FakeApi([1.0, 2.0, 3.0, 4.0, 5.0])
    records = []
    runner = StrategyRunner(
        api,
        "SHFE.rb2610",
        StubStrategy(3),  # required_bars=3 so data_length=5
        mode="sim",
        decision_sink=records.append,
        target_task_factory=FakeTargetTask,
    )

    summary = runner.run(max_updates=1)

    assert api.data_length == 5  # required_bars + 2
    assert runner.target_task.targets == []  # no target change for no-signal
    assert summary.decisions == 1
    assert len(records) == 3  # run_started, decision, run_finished
    assert runner.strategy.completed_closes == [1, 2, 3, 4]  # exactly the completed closes passed to evaluate




@pytest.mark.parametrize(
    ("bad_bars", "expected_msg"),
    [
        (True, "strategy.required_bars must be a positive integer"),
        (0, "strategy.required_bars must be between 1 and 9998 inclusive"),
        (-1, "strategy.required_bars must be between 1 and 9998 inclusive"),
        (9999, "strategy.required_bars must be between 1 and 9998 inclusive"),
        (3.5, "strategy.required_bars must be a positive integer"),
    ],
)
def test_runner_rejects_invalid_required_bars(bad_bars, expected_msg):
    """Prove invalid required_bars raises before any TqSdk API access or target task construction."""
    target_task_factory_calls = [0]

    def wrapped_factory(api, symbol):
        target_task_factory_calls[0] += 1
        return FakeTargetTask(api, symbol)

    class StubStrategy:
        def __init__(self, required_bars: int = bad_bars):
            self.required_bars = required_bars

        def evaluate(self, completed_closes: Iterable[object]) -> SignalDecision:
            return SignalDecision(None, "stub_no_target")

    api = FakeApi([1.0, 2.0, 3.0, 4.0, 5.0])
    with pytest.raises((ValueError, TypeError), match=expected_msg):
        StrategyRunner(
            api,
            "SHFE.rb2610",
            StubStrategy(bad_bars),
            mode="sim",
            target_task_factory=wrapped_factory,
        )
    # No API calls or target construction occurred because raise happens in __init__; data_length was never set
    assert api.get_quote_calls == 0
    assert api.get_kline_serial_calls == 0
    assert api.get_account_calls == 0
    assert api.get_position_calls == 0
    assert target_task_factory_calls[0] == 0
    # target-task construction was prevented by the raise in __init__
