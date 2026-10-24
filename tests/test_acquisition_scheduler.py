"""C4 scheduler tests: budgets, backoff persistence, rate limits, isolation,
H03 ordering (H10/H11/H12). All clocks are injected SYNTHETIC clocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ak_trading.acquisition import contracts as c
from ak_trading.acquisition import scheduler as sch
from ak_trading.acquisition import tq_adapter as tq


@pytest.fixture()
def policy():
    return c.Policy.from_file(
        Path(__file__).resolve().parents[1] / "config/execution-policy.frozen.json"
    ).validate()


class FakeClock:
    def __init__(self, start: int = 1_762_000_000_000_000_000):
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance_seconds(self, seconds: float) -> None:
        self.now += int(seconds * 1_000_000_000)


class TestBudgetLedger:
    def test_serial_attempt_cap(self, policy):
        ledger = sch.BudgetLedger(policy)
        for _ in range(24):
            ledger.charge_serial_attempt()
        with pytest.raises(sch.BudgetExhausted, match="DEFERRED_BUDGET"):
            ledger.charge_serial_attempt()

    def test_connection_start_rolling_window(self, policy):
        ledger = sch.BudgetLedger(policy)
        t0 = 1000.0
        ledger.charge_connection_start(t0)
        ledger.charge_connection_start(t0 + 100)
        ledger.charge_connection_start(t0 + 200)
        with pytest.raises(sch.BudgetExhausted, match="rolling 600"):
            ledger.charge_connection_start(t0 + 300)
        # After the window slides past the first start, one more is allowed.
        ledger.charge_connection_start(t0 + 700)

    def test_uncommitted_snapshot_cap(self, policy):
        ledger = sch.BudgetLedger(policy)
        ledger.charge_uncommitted_snapshot()
        ledger.charge_uncommitted_snapshot()
        with pytest.raises(sch.BudgetExhausted, match="uncommitted"):
            ledger.charge_uncommitted_snapshot()
        ledger.release_uncommitted_snapshot()
        ledger.charge_uncommitted_snapshot()


class TestRetryPersistence:
    def test_backoff_ladder_and_persistence_across_restart(self, policy, tmp_path):
        clock = FakeClock()
        store = sch.RunStateStore(tmp_path)
        gate = sch.RetryGate(store, clock)
        key = "SHFE.rb2601|60"
        assert gate.can_attempt_now(key)
        for expected_backoff, attempt in zip((60, 300, 900), range(3)):
            state = gate.record_failure(key, c.FailureClass.TRANSIENT)
            assert state.attempts == attempt + 1
            assert not gate.can_attempt_now(key)
            clock.advance_seconds(expected_backoff - 1)
            assert not gate.can_attempt_now(key)
            clock.advance_seconds(1)
            assert gate.can_attempt_now(key)
        # 4th failure exhausts attempts permanently (restart does not reset).
        state = gate.record_failure(key, c.FailureClass.TRANSIENT)
        assert state.attempts == 4
        assert state.state is c.TaskState.RETRYABLE_FAILURE
        clock.advance_seconds(10_000)
        assert not gate.can_attempt_now(key)
        # A NEW process (fresh store over the same directory) still refuses.
        store2 = sch.RunStateStore(tmp_path)
        gate2 = sch.RetryGate(store2, clock)
        assert not gate2.can_attempt_now(key)

    def test_failure_classification_mapping(self):
        assert sch.classify_exception(tq.RateLimitError(120)) == (
            c.FailureClass.RATE_LIMITED, c.TaskState.RETRYABLE_FAILURE
        )
        assert sch.classify_exception(tq.AccountDeniedError()) == (
            c.FailureClass.ACCOUNT_DENIED, c.TaskState.PERMISSION_DENIED
        )
        assert sch.classify_exception(tq.SymbolUnsupportedError("X")) == (
            c.FailureClass.SYMBOL_UNSUPPORTED, c.TaskState.UNSUPPORTED
        )
        assert sch.classify_exception(sch.BudgetExhausted("x")) == (
            c.FailureClass.BUDGET, c.TaskState.DEFERRED_BUDGET
        )


class TestRateLimitCoordinator:
    def test_first_pauses_second_stops(self):
        clock = FakeClock()
        coordinator = sch.RateLimitCoordinator()
        coordinator.observe(tq.RateLimitError(120), clock())
        assert coordinator.paused(clock())
        assert not coordinator.stop_new_requests
        clock.advance_seconds(121)
        assert not coordinator.paused(clock())
        coordinator.observe(tq.RateLimitError(60), clock())
        assert coordinator.stop_new_requests  # 2 consecutive account limits

    def test_longer_vendor_retry_after_respected(self):
        clock = FakeClock()
        coordinator = sch.RateLimitCoordinator()
        coordinator.observe(tq.RateLimitError(900), clock())
        clock.advance_seconds(700)
        assert coordinator.paused(clock())  # 900 s honored, not 60
        coordinator.observe_non_rate_limit()
        assert coordinator.consecutive_account_rate_limits == 0


class TestLease:
    def test_live_lease_is_not_stolen(self, tmp_path):
        import os

        clock = FakeClock()
        lease = sch.TaskLease(tmp_path, "collector")
        assert lease.acquire(111, clock())
        # Rewrite the lease as held by a LIVE pid (ours) with a fresh
        # heartbeat: another acquirer must be refused (H11 no stealing).
        lease._write(os.getpid(), clock())
        lease2 = sch.TaskLease(tmp_path, "other")
        assert not lease2.acquire(4194304, clock())

    def test_stale_dead_holder_lease_recovers(self, tmp_path):
        clock = FakeClock()
        stale_dir = tmp_path / "stale"
        stale_dir.mkdir()
        (stale_dir / "task-lease.json").write_text(
            '{"holder_pid": "4194303", "heartbeat_nano": "1", "holder_name": "x"}',
            encoding="utf-8",
        )
        lease = sch.TaskLease(stale_dir, "collector")
        assert lease.acquire(222, clock())

    def test_heartbeat_refuses_after_losing_lease(self, tmp_path):
        clock = FakeClock()
        lease = sch.TaskLease(tmp_path, "collector")
        lease.acquire(111, clock())
        lease._write(222, clock())
        with pytest.raises(c.ContractError, match="lease lost"):
            lease.heartbeat(111, clock())


class TestIsolationGuard:
    def test_output_root_cannot_shadow_protected_paths(self, tmp_path):
        guard = sch.IsolationGuard(
            sch.ProtectionList(paths=(str(tmp_path / "cold"),), db_objects=("market_bars",)),
            minimum_free_bytes=1,
            batch_peak_bytes=1,
        )
        with pytest.raises(sch.IsolationRefused, match="protected"):
            guard.validate_output_root(tmp_path / "cold" / "out")
        with pytest.raises(sch.IsolationRefused, match="protected"):
            guard.validate_output_root(tmp_path)  # root would contain protected

    def test_missing_health_signal_refuses(self, tmp_path):
        guard = sch.IsolationGuard(
            sch.ProtectionList(paths=(str(tmp_path),), db_objects=()),
            minimum_free_bytes=1, batch_peak_bytes=1,
        )
        failures = guard.check_health_signals(
            [
                sch.HealthCheckDescriptor(
                    name="observer_bridge", kind="process_alive", target="4194303"
                ),
                sch.HealthCheckDescriptor(
                    name="cold_marker", kind="file_exists",
                    target=str(tmp_path / "missing.txt"),
                ),
            ]
        )
        assert len(failures) == 2

    def test_disk_floor_refusal(self, tmp_path, monkeypatch):
        import shutil as shutil_mod

        class FakeUsage:
            free = 10 * 1024**3  # 10 GiB

        monkeypatch.setattr(
            shutil_mod, "disk_usage", lambda path: FakeUsage(), raising=True
        )
        guard = sch.IsolationGuard(
            sch.ProtectionList(paths=(), db_objects=()),
            minimum_free_bytes=60 * 1024**3,
            batch_peak_bytes=2 * 1024**3,
        )
        with pytest.raises(sch.IsolationRefused, match="disk"):
            guard.check_disk(tmp_path)


class TestH03Ordering:
    def test_pilot_first_then_fair_interleave(self):
        concrete = {
            "SHFE.rb": ["SHFE.rb2601", "SHFE.rb2605"],
            "DCE.i": ["DCE.i2601"],
            "SHFE.cu": ["SHFE.cu2601"],
            "CZCE.TA": ["CZCE.TA601"],
        }
        queue = sch.build_execution_queue(concrete)
        phases = [item.phase for item in queue]
        # Pilots (rb, i) occupy the head; cu/TA follow in the recent window.
        assert phases[0] == "PILOT_SAMPLE"
        pilot_keys = [item.product_key for item in queue if item.phase == "PILOT_SAMPLE"]
        assert set(pilot_keys) == {"SHFE.rb", "DCE.i"}
        first_non_pilot = next(
            item for item in queue if item.phase != "PILOT_SAMPLE"
        )
        assert first_non_pilot.phase == "RECENT_WINDOW"
        # No product monopolizes: cu and TA interleave before rb's second pass
        # in the historical phase.
        recent = [item.product_key for item in queue if item.phase == "RECENT_WINDOW"]
        assert set(recent) == {"SHFE.cu", "CZCE.TA"}
        historical = [item.product_key for item in queue if item.phase == "HISTORICAL_GAP"]
        assert historical  # every product returns for the historical phase

    def test_every_task_carries_both_durations(self):
        concrete = {"SHFE.rb": ["SHFE.rb2601"]}
        queue = sch.build_execution_queue(concrete)
        durations = {
            item.duration_seconds for item in queue if item.phase == "PILOT_SAMPLE"
        }
        assert durations == {60, 86400}
