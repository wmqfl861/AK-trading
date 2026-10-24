"""DP03 scheduler tests (V06 subscription fairness; SYNTHETIC, offline).

Fake-clock proofs: full-rotation fairness (no starvation), retry/deadline/
priority separation, capacity deferral, invalid-period rejection BEFORE any
subscribe, restart pointer persistence with gap retention, window tracking
and mixed instrument keys.  All vectors are SYNTHETIC contract vectors.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "DP03"
_spec = importlib.util.spec_from_file_location("dp03_bootstrap", _FIXTURES / "bootstrap.py")
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
_bootstrap.ensure_importable()

from ak_trading.datapipeline.contracts import (  # noqa: E402
    CLIExitCode,
    ContractError,
    TaskState,
    resolve_exit_code,
)
from ak_trading.datapipeline.policy import DP01_BUDGET_FROZEN  # noqa: E402
from ak_trading.datapipeline.scheduler import (  # noqa: E402
    ATTEMPTS_MAX_INCLUDING_INITIAL,
    BACKOFF_LADDER_SECONDS,
    BudgetExhausted,
    BudgetLedger,
    FairRotatingScheduler,
    SchedulerStateFile,
)

VECTORS = json.loads((_FIXTURES / "vectors_scheduler.json").read_text(encoding="utf-8"))
SOURCE_ID = VECTORS["source_id"]


class FakeClock:
    def __init__(self, start: int = 1_700_000_000_000_000_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now


def _symbols(count: int):
    rows = []
    for i in range(count):
        rows.append((f"KQ.m@EXCH.p{i:03d}", "MAIN_CONTINUOUS", f"exch.p{i:03d}"))
    return rows


# ---------------------------------------------------------------------------
# Scale and group structure (V06)
# ---------------------------------------------------------------------------


def test_scale_942_symbols_1884_tasks_236_groups():
    scale = VECTORS["scale"]
    scheduler = FairRotatingScheduler(
        source_id=SOURCE_ID, symbols=_symbols(scale["r1_target_symbols"]), clock_nano=FakeClock()
    )
    stats = scheduler.service_stats()
    assert stats["tasks"] == scale["tasks"] == 1884
    assert stats["groups"] == scale["expected_groups"] == 236
    for group in scheduler.groups:
        assert len(group.symbols) <= scale["symbols_per_group"]
        assert len(group.keys) <= scale["serials_per_group"]
        assert len(group.keys) == len(group.symbols) * 2  # x {60, 86400}
    # frozen budget wiring (read from the policy table, not hand-copied)
    assert DP01_BUDGET_FROZEN["active_symbols_max"] == scale["symbols_per_group"]
    assert DP01_BUDGET_FROZEN["active_serials_max"] == scale["serials_per_group"]
    assert (
        DP01_BUDGET_FROZEN["serial_request_attempts_per_run_max"] == scale["serial_attempts_per_run"]
    )


def test_full_rotation_is_fair_no_starvation():
    scheduler = FairRotatingScheduler(
        source_id=SOURCE_ID, symbols=_symbols(24), clock_nano=FakeClock()
    )
    groups = scheduler.groups
    # Serve exactly one full rotation: every group exactly once.
    seen = []
    for _ in range(len(groups)):
        group = scheduler.next_group()
        assert group is not None
        seen.append(group.group_index)
        scheduler.record_group_started(group)
    assert sorted(seen) == list(range(len(groups)))
    stats = scheduler.service_stats()
    served = [
        scheduler.task_progress(key).served_count for key in scheduler.all_task_states()
    ]
    assert stats["max_served"] == stats["min_served"] == 1
    assert max(served) - min(served) <= 1
    assert stats["rounds_completed"] == 1
    # Second rotation repeats uniformly (round-robin persistence).
    for _ in range(len(groups)):
        scheduler.record_group_started(scheduler.next_group())
    assert scheduler.service_stats()["max_served"] == 2


def test_retry_ineligible_group_skipped_without_pointer_advance():
    clock = FakeClock()
    scheduler = FairRotatingScheduler(
        source_id=SOURCE_ID, symbols=_symbols(12), clock_nano=clock
    )
    first = scheduler.next_group()
    assert first is not None
    scheduler.record_group_started(first)
    # Fail every task of the NEW head group with a future retry deadline.
    head = scheduler.next_group()
    assert head is not None
    for task_key in head.task_keys:
        scheduler.record_task_started(task_key)
        scheduler.record_task_result(task_key, TaskState.RETRYABLE_FAILURE)
    clock.now += 1  # still far below the 60 s ladder deadline
    served = scheduler.next_group()
    assert served is not None and served.group_index != head.group_index
    # The head group is still owed: once eligible it is served FIRST.
    clock.now += BACKOFF_LADDER_SECONDS[0] * 1_000_000_000 + 1
    resumed = scheduler.next_group()
    assert resumed is not None and resumed.group_index == head.group_index


def test_backoff_ladder_and_attempt_cap():
    clock = FakeClock()
    scheduler = FairRotatingScheduler(
        source_id=SOURCE_ID, symbols=_symbols(4), clock_nano=clock
    )
    task_key = next(iter(scheduler.all_task_states()))
    ladder = VECTORS["fairness"]["retry_separation"]["backoff_ladder_seconds"]
    for attempt in range(1, ATTEMPTS_MAX_INCLUDING_INITIAL + 1):
        scheduler.record_task_started(task_key)
        progress = scheduler.task_progress(task_key)
        assert progress.attempts == attempt
        expected = clock.now + ladder[min(attempt, len(ladder)) - 1] * 1_000_000_000
        assert progress.next_eligible_at_nano == expected
        scheduler.record_task_result(task_key, TaskState.RETRYABLE_FAILURE)
    # attempts capped: when EVERY task of the universe is capped, nothing is
    # eligible regardless of the clock
    for key in scheduler.all_task_states():
        for _ in range(ATTEMPTS_MAX_INCLUDING_INITIAL):
            scheduler.record_task_started(key)
            scheduler.record_task_result(key, TaskState.RETRYABLE_FAILURE)
    clock.now += 10_000 * 1_000_000_000
    assert scheduler.next_group() is None
    assert not scheduler.has_pending_retries(clock.now)


def test_invalid_duration_unsupported_before_any_subscribe():
    scheduler = FairRotatingScheduler(
        source_id=SOURCE_ID,
        symbols=_symbols(3),
        durations_seconds=(60, 300),
        clock_nano=FakeClock(),
    )
    states = scheduler.all_task_states()
    unsupported = [k for k, v in states.items() if v == "UNSUPPORTED"]
    assert len(unsupported) == 3  # one per symbol for the 300 s period
    assert all(k.endswith("|300") for k in unsupported)
    for group in scheduler.groups:
        for key in group.keys:
            assert key.coordinate.duration_seconds in (60, 86400)
    # bool / string durations are equally rejected PER TASK (group survives)
    bool_scheduler = FairRotatingScheduler(
        source_id=SOURCE_ID, symbols=[("X", "CONTRACT", "x")], durations_seconds=(True,), clock_nano=FakeClock()
    )
    assert set(bool_scheduler.all_task_states().values()) == {"UNSUPPORTED"}
    assert bool_scheduler.groups == ()  # nothing subscribable remains


def test_continuous_mode_gate():
    assert not FairRotatingScheduler(
        source_id=SOURCE_ID, symbols=_symbols(942), clock_nano=FakeClock()
    ).continuous_eligible()
    assert FairRotatingScheduler(
        source_id=SOURCE_ID, symbols=_symbols(3), clock_nano=FakeClock()
    ).continuous_eligible()
    assert not FairRotatingScheduler(
        source_id=SOURCE_ID, symbols=_symbols(5), clock_nano=FakeClock()
    ).continuous_eligible()


# ---------------------------------------------------------------------------
# Budget ledger caps (H03.5)
# ---------------------------------------------------------------------------


class MonoClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_budget_ledger_serial_attempt_cap():
    ledger = BudgetLedger(clock_mono=MonoClock())
    cap = DP01_BUDGET_FROZEN["serial_request_attempts_per_run_max"]
    for _ in range(cap):
        ledger.charge_serial_attempt()
    with pytest.raises(BudgetExhausted):
        ledger.charge_serial_attempt()


def test_budget_ledger_connection_start_rolling_window():
    clock = MonoClock()
    ledger = BudgetLedger(clock_mono=clock)
    cap = DP01_BUDGET_FROZEN["api_connection_starts_per_rolling_600_seconds_max"]
    for _ in range(cap):
        ledger.charge_connection_start()
    with pytest.raises(BudgetExhausted):
        ledger.charge_connection_start()
    clock.now += 601.0  # window slides; budget available again
    ledger.charge_connection_start()


def test_budget_ledger_wall_reserve_and_uncommitted_cap():
    clock = MonoClock()
    ledger = BudgetLedger(clock_mono=clock)
    reserve = DP01_BUDGET_FROZEN["safe_finalize_reserve_seconds"]
    wall = DP01_BUDGET_FROZEN["run_wall_seconds_max"]
    assert ledger.wall_remaining() == wall - reserve
    clock.now += wall
    assert ledger.wall_remaining() <= 0
    cap = DP01_BUDGET_FROZEN["uncommitted_raw_snapshots_max"]
    for _ in range(cap):
        ledger.charge_uncommitted_snapshot()
    with pytest.raises(BudgetExhausted):
        ledger.charge_uncommitted_snapshot()
    ledger.release_uncommitted_snapshot()
    ledger.charge_uncommitted_snapshot()


def test_budget_exhausted_maps_to_deferred_exit_code():
    states = {f"t{i}": "DEFERRED_BUDGET" for i in range(3)}
    assert resolve_exit_code(states) == CLIExitCode.DEFERRED_BUDGET
    assert int(CLIExitCode.DEFERRED_BUDGET.value) == VECTORS["capacity"]["deferral"]["exit_code"]


# ---------------------------------------------------------------------------
# Restart discipline (H03.9) and windows (H03.6)
# ---------------------------------------------------------------------------


def test_pointer_persists_and_gap_keeps_task_scheduled(tmp_path):
    state_file = SchedulerStateFile(tmp_path / "scheduler-state.json")
    scheduler = FairRotatingScheduler(
        source_id=SOURCE_ID, symbols=_symbols(20), clock_nano=FakeClock(), state_file=state_file
    )
    for _ in range(3):
        scheduler.record_group_started(scheduler.next_group())
    pointer = scheduler.rotation_pointer
    assert pointer == 3

    restarted = FairRotatingScheduler(
        source_id=SOURCE_ID, symbols=_symbols(20), clock_nano=FakeClock(), state_file=state_file
    )
    assert restarted.rotation_pointer == pointer
    group = restarted.next_group()
    assert group is not None and group.group_index == pointer  # resumes, never restarts at 0

    # Committed window WITHOUT gaps suppresses the task (ACCEPTED, capped).
    task_key = group.task_keys[0]
    clock = FakeClock()
    restarted.restart_hint(
        committed_windows={task_key: (100, 200)},
        known_gaps={},
    )
    assert restarted.task_progress(task_key).state is TaskState.ACCEPTED

    # The SAME committed window WITH a registered gap keeps it scheduled
    # with a fresh attempt budget - restart cannot skip mid-window gaps.
    restarted.restart_hint(
        committed_windows={task_key: (100, 200)},
        known_gaps={task_key: [150]},
    )
    progress = restarted.task_progress(task_key)
    assert progress.state is TaskState.PLANNED
    assert progress.attempts == 0
    assert 150 in progress.registered_gap_nanos


def test_window_min_first_never_decreases_and_outside_gap():
    scheduler = FairRotatingScheduler(
        source_id=SOURCE_ID, symbols=_symbols(4), clock_nano=FakeClock()
    )
    task_key = next(iter(scheduler.all_task_states()))
    scheduler.record_window(task_key, 1_000, 2_000)
    scheduler.record_window(task_key, 900, 2_100)   # deeper first row
    scheduler.record_window(task_key, 1_200, 1_500)  # shallower reconnect
    progress = scheduler.task_progress(task_key)
    assert progress.min_window_first_nano == 900
    gap = scheduler.outside_free_window(task_key, 400)
    assert gap == 500
    assert scheduler.outside_free_window(task_key, 900) is None
    # a task with no recorded window yet has no outside-window evidence
    other = [k for k in scheduler.all_task_states() if k != task_key][0]
    assert scheduler.outside_free_window(other, 1) is None


def test_mixed_instrument_keys_never_folded():
    mixed = VECTORS["mixed_keys"]["symbols"]
    kinds = ("MAIN_CONTINUOUS", "INDEX", "CONTRACT")
    scheduler = FairRotatingScheduler(
        source_id=SOURCE_ID,
        symbols=[(symbol, kinds[i % len(kinds)], f"exch.{i}") for i, symbol in enumerate(mixed)],
        clock_nano=FakeClock(),
    )
    keys = set(scheduler.all_task_states())
    for symbol in mixed:
        for duration in (60, 86400):
            assert f"{SOURCE_ID}|{symbol}|{duration}" in keys
    assert len(keys) == len(mixed) * 2


def test_service_stats_bounds_after_partial_service():
    scheduler = FairRotatingScheduler(
        source_id=SOURCE_ID, symbols=_symbols(50), clock_nano=FakeClock()
    )
    for _ in range(5):
        scheduler.record_group_started(scheduler.next_group())
    stats = scheduler.service_stats()
    assert stats["max_rounds_unserved"] >= 1  # unserved groups aged exactly one round
    assert stats["max_served"] == 1 and stats["min_served"] == 0
