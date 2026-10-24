"""DP03 collector tests (V02 origin/PIT, V07 reconnect/window, DP03-owned
V12 aspects: trading surface / import purity / secret shapes; SYNTHETIC,
offline).  Real TqSdk sessions are NOT_RUN (acceptance blocker); every
session here is a SYNTHETIC contract double.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "DP03"
_spec = importlib.util.spec_from_file_location("dp03_bootstrap_c", _FIXTURES / "bootstrap.py")
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
_STAGING_SRC = _bootstrap.ensure_importable()
_DOUBLES = _bootstrap.load_module("synthetic_doubles")

from ak_trading.datapipeline.collector import (  # noqa: E402
    CollectionEngine,
    SnapshotClassifier,
    serialize_raw_rows,
)
from ak_trading.datapipeline.contracts import (  # noqa: E402
    AcquisitionMode,
    AvailabilityBasis,
    ContractError,
    DataOrigin,
    FramePartition,
    RowStatus,
    SourceCoordinate,
    TaskState,
    UniverseSymbolRecord,
    InstrumentKind,
    universe_sha256,
)
from ak_trading.datapipeline.ports import (  # noqa: E402
    CalendarAnswerStatus,
    assert_no_trading_surface,
)
from ak_trading.datapipeline.scheduler import (  # noqa: E402
    FairRotatingScheduler,
    SchedulerStateFile,
)
from ak_trading.datapipeline.session_factory import (  # noqa: E402
    PermissionDeniedError,
    RealSdkSessionFactory,
    RealSourceAuthorization,
    SdkSessionFacade,
    SubscriptionKey,
    SyntheticSessionFactory,
    SyntheticTimelineSession,
    build_synthetic_frame,
)

VECTORS = json.loads((_FIXTURES / "vectors_collector.json").read_text(encoding="utf-8"))
UNIVERSE = json.loads((_FIXTURES / "synthetic_universe_dp03.json").read_text(encoding="utf-8"))
SOURCE_ID = VECTORS["source_id"]
T0 = int(VECTORS["time_base_nano"])           # float-exact multiple of 256 ns
OBS2026 = int(VECTORS["observed_2026_nano"])   # 2026-era observation instant
MIN = 60 * 1_000_000_000
DAY = 86_400 * 1_000_000_000

SYMBOLS = [(e["source_symbol"], e["instrument_kind"], e["product_key"]) for e in UNIVERSE["symbols"]]
SYMBOL_A = SYMBOLS[0][0]  # KQ.m@SHFE.rb
KEY_A60 = SubscriptionKey(
    coordinate=SourceCoordinate(source_id=SOURCE_ID, source_symbol=SYMBOL_A, duration_seconds=60)
)


def _universe_hash(symbols=None) -> str:
    rows = symbols or SYMBOLS
    return universe_sha256(
        dataset="futures_kline",
        source_id=SOURCE_ID,
        durations_seconds=(60, 86400),
        symbols=[
            UniverseSymbolRecord(
                source_symbol=s, instrument_kind=InstrumentKind(k), product_key=p
            )
            for s, k, p in rows
        ],
    )


def _make_engine(
    tmp_path,
    *,
    frames,
    timelines,
    clock,
    store=None,
    calendar=None,
    symbols=None,
    sleep=None,
    ready_after_updates=0,
    never_ready=(),
    subscribe_errors=None,
    session_factory=None,
    run_root=None,
    scheduler_state=True,
    **engine_kwargs,
):
    run_root = Path(run_root or (tmp_path / "run"))
    scheduler = FairRotatingScheduler(
        source_id=SOURCE_ID,
        symbols=symbols or SYMBOLS[:2],
        clock_nano=clock,
        state_file=(SchedulerStateFile(tmp_path / "scheduler-state.json") if scheduler_state else None),
    )
    factory = session_factory or SyntheticSessionFactory(
        frames=frames,
        timelines=timelines,
        clock_nano=clock,
        ready_after_updates=ready_after_updates,
        never_ready_keys=never_ready,
        subscribe_errors=subscribe_errors,
    )
    status_sink = _DOUBLES.FakeStatusSink()
    # F-HARD-01 harness fix: deterministic wall clock.  Every engine
    # deadline/staleness decision reads the injected virtual monotonic
    # clock (fixed step per read) instead of time.monotonic, and sleeps
    # advance virtual time with a bounded real yield so the storage worker
    # thread keeps running - no test outcome depends on real elapsed
    # seconds or host load anymore.  A test-provided sleep double (the
    # backoff recorders) overrides only the sleep side.
    virtual_mono = _DOUBLES.VirtualMonoClock()
    engine = CollectionEngine(
        worker_id="worker-test",
        dataset="futures_kline",
        scheduler=scheduler,
        session_factory=factory,
        store=store if store is not None else _DOUBLES.FakeStore(),
        status_sink=status_sink,
        task_registry=_DOUBLES.FakeTaskRegistry(),
        run_registry=_DOUBLES.FakeRunRegistry(),
        calendar=calendar,
        run_root=run_root,
        universe_sha256=_universe_hash(),
        clock_nano=clock,
        clock_mono=virtual_mono.mono,
        sleep=sleep if sleep is not None else virtual_mono.sleep,
        group_service_seconds=1.0,
        stale_after_seconds=0.25,
        run_deadline_seconds=engine_kwargs.pop("run_deadline_seconds", 15.0),
        finalize_wait_seconds=engine_kwargs.pop("finalize_wait_seconds", 5.0),
        **engine_kwargs,
    )
    return engine, status_sink


def _run_simple(tmp_path, frames, timelines, clock, **kwargs):
    engine, sink = _make_engine(tmp_path, frames=frames, timelines=timelines, clock=clock, **kwargs)
    summary = engine.run(run_id=kwargs.pop("run_id", "run-test"))
    return engine, sink, summary


# ---------------------------------------------------------------------------
# V12 aspects owned by DP03: import purity, trading surface, secret shapes
# ---------------------------------------------------------------------------


def test_import_purity_no_sdk_no_socket():
    for module_name in (
        "ak_trading.datapipeline.collector",
        "ak_trading.datapipeline.scheduler",
        "ak_trading.datapipeline.session_factory",
    ):
        importlib.import_module(module_name)
    assert "tqsdk" not in sys.modules


def test_facade_surface_exactly_five_methods_no_delegation():
    clock = _DOUBLES.ClockNano(T0)
    frame = build_synthetic_frame(start_nano=T0, duration_seconds=60, closed_rows=2)
    api = _DOUBLES.RawApiWithTradingMethods({(SYMBOL_A, 60): frame}, clock)
    facade = SdkSessionFacade(api, connection_epoch=1, clock_nano=clock)
    assert_no_trading_surface(facade)
    serial = facade.subscribe(KEY_A60)
    assert facade.wait_update(1.0) is True
    assert facade.ready(serial) is True
    snapshot = facade.copy_snapshot(serial)
    assert snapshot.detached is True
    facade.close()
    # forbidden methods are NOT reachable through the facade (no delegation)
    for name in ("insert_order", "cancel_order", "get_account", "get_position"):
        with pytest.raises(AttributeError):
            getattr(facade, name)
    assert api.trading_calls == []
    assert api.get_kline_serial_calls == [(SYMBOL_A, 60, 8192)]


def test_synthetic_session_has_no_trading_surface():
    session = SyntheticTimelineSession(
        frames={}, timeline=[], clock_nano=_DOUBLES.ClockNano(T0)
    )
    assert_no_trading_surface(session)


def test_real_source_entry_refused_before_any_tqsdk_import():
    class _TqSdkBlocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "tqsdk":
                raise AssertionError("tqsdk import attempted without authorization")
            return None

    blocker = _TqSdkBlocker()
    sys.meta_path.insert(0, blocker)
    try:
        with pytest.raises(ContractError):
            RealSdkSessionFactory().create()
        with pytest.raises(ContractError):
            RealSdkSessionFactory(RealSourceAuthorization()).create()
    finally:
        sys.meta_path.remove(blocker)
    assert "tqsdk" not in sys.modules


def test_credential_shaped_status_reason_rejected():
    from ak_trading.datapipeline.contracts import sanitize_status_reason

    with pytest.raises(ContractError):
        sanitize_status_reason("password: hunter2secret")


# ---------------------------------------------------------------------------
# Deep-copy isolation (H03.3)
# ---------------------------------------------------------------------------


def test_snapshot_deep_copy_isolated_from_live_frame_mutation():
    from ak_trading.datapipeline.ports import SnapshotCopy

    clock = _DOUBLES.ClockNano(T0)
    frame = build_synthetic_frame(start_nano=T0, duration_seconds=60, closed_rows=3, live_rows=1)
    session = SyntheticTimelineSession(
        frames={(SYMBOL_A, 60): frame}, timeline=[{"event": "deadline"}], clock_nano=clock
    )
    serial = session.subscribe(KEY_A60)
    snapshot = session.copy_snapshot(serial)
    before = [dict(r) for r in snapshot.rows]
    frame.loc[len(frame)] = {  # SDK-like in-place mutation AFTER the copy
        "datetime": float(T0 + 10 * MIN),
        "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
        "volume": 1.0, "open_oi": 1.0, "close_oi": 1.0,
    }
    frame.iloc[0, frame.columns.get_loc("close")] = 99999.0
    assert [dict(r) for r in snapshot.rows] == before
    with pytest.raises(ContractError):
        SnapshotCopy(
            serial=snapshot.serial, rows=snapshot.rows, copied_at_update_seq=0, detached=False
        )


# ---------------------------------------------------------------------------
# V02: origin / PIT labeling
# ---------------------------------------------------------------------------


def test_v02_two_year_old_baseline_bars_are_session_backfill(tmp_path):
    clock = _DOUBLES.ClockNano(OBS2026)
    frame = build_synthetic_frame(start_nano=T0, duration_seconds=60, closed_rows=4)
    store = _DOUBLES.FakeStore()
    engine, sink, summary = _run_simple(
        tmp_path, frames={(SYMBOL_A, 60): frame}, timelines=[[]], clock=clock, store=store
    )
    assert summary.snapshot_count >= 1
    envelope, rows = store.captured[0]
    valid_closed = [
        r for r in rows
        if r.frame_partition is FramePartition.VALID and r.row_status is RowStatus.CLOSED
    ]
    assert valid_closed, "baseline frame must carry CLOSED rows"
    for row in valid_closed:
        assert row.acquisition_mode is AcquisitionMode.SESSION_BACKFILL
        assert row.observed_at_nano >= row.event_start_nano  # zero backdating
        assert row.first_observed_at_nano == row.observed_at_nano  # first sight
        assert row.availability_basis is AvailabilityBasis.RETROSPECTIVE_CAPTURE
        assert row.event_end_nano <= row.observed_at_nano  # two years earlier
    assert envelope.data_origin is DataOrigin.SYNTHETIC
    assert "SYNTHETIC" in (envelope.synthetic_marker or "")


def test_v02_realtime_chain_provisional_then_closed(tmp_path):
    clock = _DOUBLES.ClockNano(T0 + 3 * MIN + 30 * 1_000_000_000)  # inside live bar 3
    frame = build_synthetic_frame(start_nano=T0, duration_seconds=60, closed_rows=3, live_rows=1)
    timeline = [
        {"event": "deadline"},  # readiness pass (no frame change)
        {"event": "update", "bar_start_nano": T0 + 4 * MIN,
         "set_clock_nano": T0 + 4 * MIN + 10 * 1_000_000_000},
        {"event": "update", "bar_start_nano": T0 + 5 * MIN,
         "set_clock_nano": T0 + 5 * MIN + 10 * 1_000_000_000},
    ]
    store = _DOUBLES.FakeStore()
    engine, sink, summary = _run_simple(
        tmp_path, frames={(SYMBOL_A, 60): frame}, timelines=[timeline], clock=clock, store=store
    )
    bar4 = T0 + 4 * MIN
    saw_provisional = saw_realtime_closed = False
    for envelope, rows in store.captured:
        for row in rows:
            if row.key is None or row.key.datetime_nano != bar4:
                continue
            if row.row_status is RowStatus.PROVISIONAL:
                # First PROVISIONAL sighting is never in the baseline.
                if not row.acquisition_mode == AcquisitionMode.SESSION_BACKFILL:
                    saw_provisional = True
                    assert row.acquisition_mode is AcquisitionMode.REALTIME
                    assert row.evidence_class == "PROVISIONAL_OBSERVATION"
                    assert row.availability_basis is AvailabilityBasis.LIVE_CAPTURE_EVIDENCE
            if (
                row.row_status is RowStatus.CLOSED
                and row.acquisition_mode is AcquisitionMode.REALTIME
            ):
                # The P->C chain on the original epoch/continuity is REALTIME;
                # later re-serves legitimately re-observe it as backfill.
                saw_realtime_closed = True
                assert row.evidence_class == "REALTIME_EVIDENCED"
                assert row.availability_basis is AvailabilityBasis.LIVE_CAPTURE_EVIDENCE
                assert row.event_start_nano <= T0 + 4 * MIN + 10 * 1_000_000_000 < row.event_end_nano
    assert saw_provisional and saw_realtime_closed


def test_v02_reconnect_backfill_resets_continuity(tmp_path):
    clock = _DOUBLES.AutoClock(T0 + 3 * MIN + 30 * 1_000_000_000)
    frame = build_synthetic_frame(start_nano=T0, duration_seconds=60, closed_rows=3, live_rows=1)
    step = {"event": "update", "bar_start_nano": T0 + 4 * MIN,
            "set_clock_nano": T0 + 4 * MIN + 10 * 1_000_000_000}
    timeline_1 = [step, {"event": "disconnect"}]
    timeline_2 = [{"event": "update", "bar_start_nano": T0 + 6 * MIN,
                   "set_clock_nano": T0 + 7 * MIN}]
    store = _DOUBLES.FakeStore()
    sleeper = _DOUBLES.SleepRecorder()
    engine, sink, summary = _run_simple(
        tmp_path,
        frames={(SYMBOL_A, 60): frame},
        timelines=[timeline_1, timeline_2],
        clock=clock,
        store=store,
        sleep=sleeper,
        run_deadline_seconds=8.0,
    )
    assert summary.reconnects >= 1
    assert engine.session_factory.created_sessions
    assert all(s.closed for s in engine.session_factory.created_sessions)
    bar4 = T0 + 4 * MIN
    reconnect_epochs = {s.connection_epoch for s in engine.session_factory.created_sessions[1:]}
    backfill_seen = False
    for envelope, rows in store.captured:
        if envelope.connection_baseline.connection_epoch not in reconnect_epochs:
            continue
        for row in rows:
            if row.key is not None and row.key.datetime_nano == bar4 and row.row_status is RowStatus.CLOSED:
                backfill_seen = True
                assert row.acquisition_mode is AcquisitionMode.SESSION_BACKFILL
    assert backfill_seen, "post-reconnect first-sight CLOSED bar must be SESSION_BACKFILL"


def test_v02_same_content_idempotent_conflict_quarantines(tmp_path):
    clock = _DOUBLES.ClockNano(T0 + 10 * MIN)
    frame = build_synthetic_frame(start_nano=T0, duration_seconds=60, closed_rows=4)
    store = _DOUBLES.FakeStore()
    engine1, sink1 = _make_engine(
        tmp_path, frames={(SYMBOL_A, 60): frame}, timelines=[[]], clock=clock,
        store=store, run_root=tmp_path / "run-1",
    )
    engine1.run(run_id="idem-1")
    # re-subscription re-delivers the committed window: fresh rotation state
    # by design (Q04 reconnect redelivery), same shared store
    engine2, sink2 = _make_engine(
        tmp_path, frames={(SYMBOL_A, 60): frame}, timelines=[[]], clock=clock,
        store=store, run_root=tmp_path / "run-2", scheduler_state=False,
    )
    engine2.run(run_id="idem-2")
    outcomes = [c["outcome"] for c in store.commits]
    assert "COMMITTED_NEW" in outcomes
    assert "COMMITTED_SAME_CONTENT" in outcomes  # idempotent replay (Q04)

    conflict_task = f"{SOURCE_ID}|{SYMBOL_A}|60"
    store2 = _DOUBLES.FakeStore(conflict_tasks=[conflict_task])
    engine3, sink3 = _make_engine(
        tmp_path, frames={(SYMBOL_A, 60): frame}, timelines=[[]], clock=clock,
        store=store2, run_root=tmp_path / "run-conflict", scheduler_state=False,
    )
    summary3 = engine3.run(run_id="conflict")
    assert summary3.task_states[conflict_task] == TaskState.QUARANTINED.value
    assert summary3.conflicts >= 1
    assert sink3.find("conflicting CLOSED content")


# ---------------------------------------------------------------------------
# V07: reconnect / window / failures
# ---------------------------------------------------------------------------


def test_v07_two_disconnects_bounded_backoff_jitter(tmp_path):
    clock = _DOUBLES.AutoClock(T0 + 3 * MIN + 30 * 1_000_000_000)
    frame = build_synthetic_frame(start_nano=T0, duration_seconds=60, closed_rows=3, live_rows=1)
    step = {"event": "update", "bar_start_nano": T0 + 4 * MIN,
            "set_clock_nano": T0 + 4 * MIN + 10 * 1_000_000_000}
    timelines = [
        [step, {"event": "disconnect"}],
        [step, {"event": "disconnect"}],
        [{"event": "update", "bar_start_nano": T0 + 6 * MIN, "set_clock_nano": T0 + 7 * MIN}],
    ]
    sleeper = _DOUBLES.SleepRecorder()
    engine, sink, summary = _run_simple(
        tmp_path,
        frames={(SYMBOL_A, 60): frame},
        timelines=timelines,
        clock=clock,
        sleep=sleeper,
        run_deadline_seconds=10.0,
    )
    assert summary.reconnects == 2
    ladder = VECTORS["v07"]["disconnect_backoff"]["ladder_seconds"]
    factor = VECTORS["v07"]["disconnect_backoff"]["jitter_bound_factor"]
    backoffs = sorted(s for s in sleeper.calls if s >= ladder[0])
    assert len(backoffs) >= 2
    for value, base in zip(backoffs[:2], ladder[:2]):
        assert base <= value <= base * (1 + factor)
    for session in engine.session_factory.created_sessions:
        assert session.close_count <= 1  # single-connection discipline


def test_v07_rate_limit_two_strikes_stop_new_requests(tmp_path):
    clock = _DOUBLES.ClockNano(T0 + 10 * MIN)
    from ak_trading.datapipeline.session_factory import RateLimitError

    # two groups: rb products (4 symbols) then ha (1 symbol); rate-limiting
    # the KQ.m subscribes strikes twice and defers the still-PLANNED ha group
    frames = {
        (symbol, 60): build_synthetic_frame(start_nano=T0, duration_seconds=60, closed_rows=2)
        for symbol, _k, _p in FIVE_SYMBOLS
    }
    engine, sink, summary = _run_simple(
        tmp_path,
        frames=frames,
        timelines=[[]],
        clock=clock,
        symbols=FIVE_SYMBOLS,
        subscribe_errors={
            (SYMBOL_A, 60): RateLimitError(60.0),
            (SYMBOL_A, 86400): RateLimitError(60.0),
        },
        run_deadline_seconds=1.5,
        run_root=tmp_path / "run-rl",
    )
    assert summary.rate_limit_pauses == 2
    assert any(s == TaskState.DEFERRED_BUDGET.value for s in summary.task_states.values())
    assert sink.find("stopping new requests")


def test_v07_permission_denied_circuit_breaker(tmp_path):
    clock = _DOUBLES.ClockNano(T0 + 10 * MIN)
    frame = build_synthetic_frame(start_nano=T0, duration_seconds=60, closed_rows=2)
    engine, sink, summary = _run_simple(
        tmp_path,
        frames={(SYMBOL_A, 60): frame},
        timelines=[[{"event": "update"}]],
        clock=clock,
        subscribe_errors={(SYMBOL_A, 60): PermissionDeniedError()},
    )
    assert summary.permission_denied is True
    assert summary.terminal_state == "FAILED"
    assert int(summary.exit_code) == 2
    assert any(s == TaskState.PERMISSION_DENIED.value for s in summary.task_states.values())
    assert engine.session_factory.sessions_created == 1  # no subscribe storm
    assert sink.find("circuit breaker")


def test_v07_db_failure_persists_pending_no_checkpoint(tmp_path):
    clock = _DOUBLES.ClockNano(T0 + 10 * MIN)
    frames = {
        (symbol, duration): build_synthetic_frame(
            start_nano=T0, duration_seconds=duration, closed_rows=2
        )
        for symbol, _k, _p in SYMBOLS[:2]
        for duration in (60, 86400)
    }
    store = _DOUBLES.FakeStore(fail_commits=99)
    engine, sink, summary = _run_simple(
        tmp_path, frames=frames, timelines=[[]], clock=clock, store=store
    )
    assert summary.pending_uncommitted == summary.snapshot_count
    assert summary.snapshot_count >= 1
    pending_path = engine.run_root / "spool" / "worker-test" / "pending.jsonl"
    lines = pending_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == summary.snapshot_count
    for attempt_dir in (engine.run_root / "raw").iterdir():
        assert (attempt_dir / "envelope.json").exists()
        assert (attempt_dir / "frame.jsonl").exists()
    for task_key, state in summary.task_states.items():
        assert state in (TaskState.RAW_VERIFIED.value,)
    assert summary.terminal_state == "RECOVERY_REQUIRED"
    assert int(summary.exit_code) == 5
    assert sink.find("checkpoint NOT advanced")


def test_v07_queue_full_backpressure_never_drops(tmp_path):
    clock = _DOUBLES.ClockNano(T0 + 10 * MIN)
    frame = build_synthetic_frame(start_nano=T0, duration_seconds=60, closed_rows=3)
    block = threading.Event()
    store = _DOUBLES.FakeStore(block_event=block)
    engine, sink = _make_engine(
        tmp_path,
        frames={(SYMBOL_A, 60): frame},
        timelines=[[{"event": "update"}, {"event": "update"}, {"event": "update"}]],
        clock=clock,
        store=store,
        queue_bound=1,
        run_deadline_seconds=3.0,
    )

    def release():
        # F-HARD-01: release EVENT-DRIVEN, not at a fixed wall instant.  The
        # blocked store keeps the bounded queue full, so the worker's
        # queue-full check fires deterministically; releasing only after the
        # backpressure evidence is observed removes the old real-time race
        # where a slow group let the 0.8s release drain the queue before
        # the worker ever saw it full.  The 10s bound is a safety valve only
        # (the blocked store keeps the queue full, so the evidence arrives
        # with the worker's first post-group loop iteration).
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if sink.find("backpressure") or sink.find("queue full"):
                break
            time.sleep(0.005)
        block.set()

    threading.Thread(target=release, daemon=True).start()
    summary = engine.run(run_id="backpressure")
    assert summary.backpressure_events >= 1
    assert len(store.commits) + summary.pending_uncommitted == summary.snapshot_count
    assert sink.find("backpressure") or sink.find("queue full")


def test_v07_schema_padding_invalid_conservation_and_daily_unknown(tmp_path):
    clock = _DOUBLES.ClockNano(T0 + 3 * DAY)
    frame_60 = build_synthetic_frame(
        start_nano=T0, duration_seconds=60, closed_rows=3, live_rows=1,
        padding_rows=2, invalid_rows=1,
    )
    frame_day = build_synthetic_frame(start_nano=T0, duration_seconds=86400, closed_rows=2)
    engine, sink, summary = _run_simple(
        tmp_path,
        frames={(SYMBOL_A, 60): frame_60, (SYMBOL_A, 86400): frame_day},
        timelines=[[]],
        clock=clock,
    )
    metas = {}
    for attempt_dir in sorted((engine.run_root / "raw").iterdir()):
        envelope = json.loads((attempt_dir / "envelope.json").read_text(encoding="utf-8"))
        metas[envelope["meta"]["duration_seconds"]] = envelope["meta"]
    minute = metas[60]
    assert minute["requested_length"] == 8192
    assert minute["returned_length"] == 6  # 2 padding + 1 invalid + 3 valid
    assert minute["padding_rows"] == 2 and minute["invalid_rows"] == 1
    assert minute["valid_rows"] == 3
    assert (
        minute["padding_rows"] + minute["invalid_rows"] + minute["valid_rows"]
        == minute["frame_rows"]
    )
    # H03.8: daily rows WITHOUT calendar session-end evidence stay UNKNOWN
    day_rows = [
        row
        for envelope, rows in engine.store.captured
        for row in rows
        if row.key is not None and row.key.duration_seconds == 86400
    ]
    assert day_rows and all(r.row_status is RowStatus.UNKNOWN for r in day_rows)


def test_daily_closed_with_qualified_calendar(tmp_path):
    clock = _DOUBLES.ClockNano(T0 + 3 * DAY)
    frame_day = build_synthetic_frame(start_nano=T0, duration_seconds=86400, closed_rows=2)
    calendar = _DOUBLES.FakeCalendar(CalendarAnswerStatus.VERIFIED_SESSIONS)
    engine, sink, summary = _run_simple(
        tmp_path,
        frames={(SYMBOL_A, 86400): frame_day},
        timelines=[[]],
        clock=clock,
        calendar=calendar,
    )
    rows = [
        row
        for envelope, rws in engine.store.captured
        for row in rws
        if row.key is not None and row.key.duration_seconds == 86400
    ]
    assert rows
    assert all(r.row_status is RowStatus.CLOSED for r in rows)


def test_classifier_invalid_reasons_and_padding_unit():
    coordinate = SourceCoordinate(source_id=SOURCE_ID, source_symbol=SYMBOL_A, duration_seconds=60)
    rows = [
        {"datetime": float("nan"), "open": float("nan"), "high": float("nan"),
         "low": float("nan"), "close": float("nan"), "volume": float("nan"),
         "open_oi": float("nan"), "close_oi": float("nan")},
        {"datetime": float(T0), "open": 10.0, "high": 5.0, "low": 20.0, "close": 10.0,
         "volume": 1.0, "open_oi": 1.0, "close_oi": 1.0},  # broken OHLC -> INVALID
        {"datetime": float(T0 + MIN), "open": 10.0, "high": 12.0, "low": 9.0,
         "close": 11.0, "volume": 1.0, "open_oi": 1.0, "close_oi": 1.0},
    ]
    observations, stats = SnapshotClassifier.classify(
        coordinate=coordinate, rows=rows, observed_at_nano=T0 + 10 * MIN
    )
    assert (stats.padding_rows, stats.invalid_rows, stats.valid_rows) == (1, 1, 1)
    assert stats.frame_rows == 3
    assert any("values-invalid" in reason for reason in stats.invalid_reasons)
    assert observations[0].frame_partition is FramePartition.PADDING
    assert observations[1].frame_partition is FramePartition.INVALID
    assert observations[2].frame_partition is FramePartition.VALID


def test_serialize_raw_rows_maps_nan_to_null():
    payload = serialize_raw_rows(
        [{"datetime": float("nan"), "open": 1.0, "high": 2.0, "low": 0.5,
          "close": 1.5, "volume": 0.0, "open_oi": 0.0, "close_oi": 0.0}]
    )
    parsed = json.loads(payload.decode("utf-8"))
    assert parsed[0]["datetime"] is None and parsed[0]["volume"] == 0.0


# ---------------------------------------------------------------------------
# H03.3 stale / market-closed classification, H03.6 window evidence
# ---------------------------------------------------------------------------


def test_market_closed_is_not_a_failure(tmp_path):
    clock = _DOUBLES.ClockNano(T0 + 10 * MIN)
    frame = build_synthetic_frame(start_nano=T0, duration_seconds=60, closed_rows=2)
    calendar = _DOUBLES.FakeCalendar(CalendarAnswerStatus.NO_TRADING_EVIDENCED)
    engine, sink, summary = _run_simple(
        tmp_path, frames={(SYMBOL_A, 60): frame}, timelines=[[]], clock=clock, calendar=calendar
    )
    assert summary.market_closed_events >= 1
    assert sink.find("market closed")


def test_unknown_calendar_empty_frame_source_empty_unexplained(tmp_path):
    clock = _DOUBLES.ClockNano(T0 + 10 * MIN)
    frame = build_synthetic_frame(start_nano=T0, duration_seconds=60, closed_rows=0)
    calendar = _DOUBLES.FakeCalendar(CalendarAnswerStatus.UNKNOWN)
    engine, sink, summary = _run_simple(
        tmp_path, frames={(SYMBOL_A, 60): frame}, timelines=[[]], clock=clock, calendar=calendar
    )
    task_key = f"{SOURCE_ID}|{SYMBOL_A}|60"
    assert summary.task_states[task_key] == TaskState.SOURCE_EMPTY_UNEXPLAINED.value
    assert sink.find("SOURCE_EMPTY_UNEXPLAINED")


def test_empty_unexplained_terminal_state_order_independent(tmp_path):
    # F-HARD-01 (DP-HARD-r1 run1) counterexample: a blocked store forces the
    # empty snapshot's commit receipt to land AFTER the worker's no-data
    # classification (the interleaving that used to end ACCEPTED by
    # last-writer-wins).  The terminal state must stay
    # SOURCE_EMPTY_UNEXPLAINED regardless of worker-vs-storage ordering.
    clock = _DOUBLES.ClockNano(T0 + 10 * MIN)
    frame = build_synthetic_frame(start_nano=T0, duration_seconds=60, closed_rows=0)
    calendar = _DOUBLES.FakeCalendar(CalendarAnswerStatus.UNKNOWN)
    block = threading.Event()
    store = _DOUBLES.FakeStore(block_event=block)

    def release():
        time.sleep(0.3)  # real seconds: classification happens within ms
        block.set()

    threading.Thread(target=release, daemon=True).start()
    engine, sink = _make_engine(
        tmp_path,
        frames={(SYMBOL_A, 60): frame},
        timelines=[[]],
        clock=clock,
        calendar=calendar,
        store=store,
    )
    summary = engine.run(run_id="empty-late-receipt")
    task_key = f"{SOURCE_ID}|{SYMBOL_A}|60"
    assert summary.snapshot_count >= 1
    assert len(store.commits) >= 1  # raw evidence WAS committed ...
    assert summary.task_states[task_key] == TaskState.SOURCE_EMPTY_UNEXPLAINED.value
    assert summary.task_states[task_key] != TaskState.ACCEPTED.value  # ... but never green
    assert sink.find("SOURCE_EMPTY_UNEXPLAINED")
    assert sink.find("ACCEPTED withheld")  # H01.5 commit-decision rule fired


def test_empty_frame_verified_calendar_never_green(tmp_path):
    # F-HARD-01 companion pin: even with a VERIFIED calendar, a zero-valid-
    # row snapshot commit must not produce the green task state; the task
    # stays in the non-green RAW_VERIFIED recovery hold (exit 0 unreachable).
    clock = _DOUBLES.ClockNano(T0 + 10 * MIN)
    frames = {
        (SYMBOL_A, duration): build_synthetic_frame(
            start_nano=T0, duration_seconds=duration, closed_rows=0
        )
        for duration in (60, 86400)
    }
    calendar = _DOUBLES.FakeCalendar(CalendarAnswerStatus.VERIFIED_SESSIONS)
    engine, sink, summary = _run_simple(
        tmp_path,
        frames=frames,
        timelines=[[]],
        clock=clock,
        calendar=calendar,
        symbols=[(SYMBOL_A, "CONTRACT", "shfe.rb")],
    )
    for task_key, state in summary.task_states.items():
        assert state == TaskState.RAW_VERIFIED.value  # committed, held, never ACCEPTED
    assert summary.terminal_state == "RECOVERY_REQUIRED"
    assert int(summary.exit_code) == 5
    assert sink.find("ACCEPTED withheld")


def test_outside_free_window_gap_evidence(tmp_path):
    clock = _DOUBLES.ClockNano(T0 + 10 * MIN)
    frame = build_synthetic_frame(start_nano=T0, duration_seconds=60, closed_rows=3)
    engine, sink, summary = _run_simple(
        tmp_path, frames={(SYMBOL_A, 60): frame}, timelines=[[]], clock=clock
    )
    assert summary.outside_window_gap_nanos > 0
    assert sink.find("OUTSIDE_FREE_WINDOW")
    progress = engine.scheduler.task_progress(f"{SOURCE_ID}|{SYMBOL_A}|60")
    assert progress.min_window_first_nano == T0  # window floor recorded


# ---------------------------------------------------------------------------
# H03.4 worker isolation / H03.9 stop and restart
# ---------------------------------------------------------------------------


def test_construction_hang_total_deadline_abandons_owned_worker(tmp_path):
    hanging = _DOUBLES.HangingSessionFactory()
    try:
        engine, sink = _make_engine(
            tmp_path, frames={}, timelines=[[]], clock=_DOUBLES.ClockNano(T0),
            session_factory=hanging, run_deadline_seconds=1.2,
        )
        summary = engine.run(run_id="hang")
        assert summary.worker_abandoned is True
        assert any("H03.4" in note for note in summary.notes)
        assert int(summary.exit_code) != 0
    finally:
        hanging.release()


def test_graceful_stop_no_new_tasks_after_stop_event(tmp_path):
    clock = _DOUBLES.ClockNano(T0 + 10 * MIN)
    frame = build_synthetic_frame(start_nano=T0, duration_seconds=60, closed_rows=2)
    stop = threading.Event()
    stop.set()
    engine, sink = _make_engine(
        tmp_path, frames={(SYMBOL_A, 60): frame}, timelines=[[]], clock=clock,
        run_root=tmp_path / "run-stop",
    )
    summary = engine.run(run_id="stop-run", stop_event=stop)
    assert summary.snapshot_count == 0  # stopped before accepting any task
    assert int(summary.exit_code) == 3  # planned remaining -> known gaps, not 0


FIVE_SYMBOLS = SYMBOLS + [
    ("SHFE.rb2605", "CONTRACT", "shfe.rb"),
    ("SHFE.ha2601", "CONTRACT", "shfe.ha"),
]


def test_restart_resumes_pointer_and_keeps_gaps_scheduled(tmp_path):
    clock = _DOUBLES.ClockNano(T0 + 10 * MIN)
    frames = {
        (symbol, duration): build_synthetic_frame(
            start_nano=T0, duration_seconds=duration, closed_rows=2
        )
        for symbol, _k, _p in FIVE_SYMBOLS
        for duration in (60, 86400)
    }
    engine1, sink1 = _make_engine(
        tmp_path, frames=frames, timelines=[[]], clock=clock,
        symbols=FIVE_SYMBOLS, run_root=tmp_path / "run-1", run_deadline_seconds=20.0,
    )
    summary1 = engine1.run(run_id="cycle-1")
    # clean full cycle: 10 tasks, all committed -> exit 0 reachable ONLY here
    assert int(summary1.exit_code) == 0
    assert summary1.terminal_state == "SUCCEEDED"
    assert len(engine1.committed_windows) == 10
    pointer_after_run1 = engine1.scheduler.rotation_pointer

    # restart: same persisted scheduler state; a registered gap re-arms ONLY
    # the gapped task (never a max(ts) skip)
    gap_task = f"{SOURCE_ID}|{SYMBOL_A}|60"
    engine2, sink2 = _make_engine(
        tmp_path, frames=frames, timelines=[[]], clock=clock,
        symbols=FIVE_SYMBOLS, run_root=tmp_path / "run-2", run_deadline_seconds=20.0,
    )
    assert engine2.scheduler.rotation_pointer == pointer_after_run1
    engine2.scheduler.restart_hint(
        committed_windows=dict(engine1.committed_windows),
        known_gaps={gap_task: [T0 + MIN]},
    )
    progress = engine2.scheduler.task_progress(gap_task)
    assert progress.state is TaskState.PLANNED and progress.attempts == 0
    summary2 = engine2.run(run_id="cycle-2")
    assert gap_task in engine2.committed_windows  # gap re-served and committed


# ---------------------------------------------------------------------------
# CLI entry (offline SYNTHETIC; real source NOT_RUN refusal)
# ---------------------------------------------------------------------------


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "collect_datapipeline.py"


def _write_timeline(path: Path):
    doc = {
        "ready_after_updates": 0,
        "frames": [
            {"symbol": SYMBOL_A, "duration_seconds": 60, "start_nano": T0, "closed_rows": 3},
            {"symbol": SYMBOL_A, "duration_seconds": 86400, "start_nano": T0, "closed_rows": 2},
        ],
        "timelines": [[]],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_cli_refuses_missing_output_root():
    result = subprocess.run(
        [sys.executable, "-B", str(_SCRIPT)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode != 0


def test_cli_refuses_relative_output_root(tmp_path):
    timeline = _write_timeline(tmp_path / "timeline.json")
    result = subprocess.run(
        [
            sys.executable, "-B", str(_SCRIPT),
            "--output-root", "relative/run-root",
            "--universe", str(_FIXTURES / "synthetic_universe_dp03.json"),
            "--symbols", SYMBOL_A,
            "--synthetic-timeline", str(timeline),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 1


def test_cli_real_source_refused_not_run(tmp_path):
    timeline = _write_timeline(tmp_path / "timeline.json")
    result = subprocess.run(
        [
            sys.executable, "-B", str(_SCRIPT),
            "--output-root", str(tmp_path / "run"),
            "--real-source",
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert "NOT_RUN" in (result.stderr + result.stdout)


def test_cli_synthetic_run_writes_summary(tmp_path):
    timeline = _write_timeline(tmp_path / "timeline.json")
    run_root = tmp_path / "run"
    result = subprocess.run(
        [
            sys.executable, "-B", str(_SCRIPT),
            "--output-root", str(run_root),
            "--universe", str(_FIXTURES / "synthetic_universe_dp03.json"),
            "--symbols", SYMBOL_A,
            "--synthetic-timeline", str(timeline),
            "--run-id", "cli-run",
            "--group-service-seconds", "1.0",
            "--stale-after-seconds", "0.25",
        ],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(_STAGING_SRC)},
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert result.returncode == int(payload["exit_code"])
    # no store wired in the r1 CLI -> RAW_VERIFIED -> RECOVERY_REQUIRED (5)
    assert payload["terminal_state"] == "RECOVERY_REQUIRED"
    assert payload["exit_code"] == 5
    assert (run_root / "spool" / "worker-1" / "run-summary.json").exists()
