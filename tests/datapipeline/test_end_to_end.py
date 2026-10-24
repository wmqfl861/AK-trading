"""DP08 end-to-end integration tests (V13 chain, G1-G4 stages; SYNTHETIC).

AUTHORITY: work-orders/DP08-integration.md (H08.1/H08.2), 03-data-quality-
contract.md, 04-validation-and-release.md (V13), DP01 frozen ports.

MODE (honest scope): OFFLINE / SYNTHETIC.  The REAL implementations of all
six delivered nodes are wired together (no mock substitutes for actual
dependencies): DP02 storage/registries on the SQLite double, the DP04
CalendarBook over an authored SYNTHETIC evidence document, the DP03
CollectionEngine with SYNTHETIC timeline sessions, the DP05 validator, the
DP07 seed register and the DP06 monitor/report pipeline.  PostgreSQL
acceptance legs are explicit NOT_RUN/skips (no isolated database injected);
real TqSdk sessions and V15 live canary are NOT_RUN.

RESOLVED CROSS-NODE DEFECTS (r2 re-integration; fixed by DP-X08FIX r1,
verified here by the four FLIPPED pins - each now asserts the FIX and
fails loudly if the defect regresses; at r1 the same tests asserted the
defects existed):

* X-08-1 - the DP03 engine now converts its DP01 ``RowObservation`` rows
  into the wired store's pinned-acquisition ``ClassifiedRow`` shape before
  committing, so the real DP02 ``SqlPipelineStore`` publishes the batch
  (pin: commit through a wrapping store succeeds, replay is idempotent).
* X-08-2 - the engine now ADOPTS the run_id minted by
  ``RunRegistry.start_run``; the real DP02 ``SqlRunRegistry`` finalizes the
  run and ONE identity threads through the registry row, the summary, the
  heartbeats and every status event (pin: no ``StorageError``, identities
  unified).
* X-08-3 - the DP03 collector now speaks the DP06 monitor's frozen input
  vocabulary: commit-failure reasons carry the ``COMMIT_FAILED:`` prefix
  and heartbeat files carry ``heartbeat_at_nano`` + ``run_id`` (pin: a
  fresh heartbeat no longer fires HEARTBEAT_EXPIRED; a real collector
  commit failure fires DB_COMMIT_FAILED).
* X-08-5 - DP02 ``query_available(source_symbol=...)`` now matches the
  canonical ``BarKey`` JSON's alphabetically-LAST key (no trailing comma),
  so symbol-scoped as_of views return that symbol's published rows (pin:
  per-symbol views return exactly the right rows, never empty).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "DP08"
_spec = importlib.util.spec_from_file_location("dp08_bootstrap_e2e", _FIXTURES / "bootstrap.py")
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
_STAGING_SRC = _bootstrap.ensure_importable()
_world = _bootstrap.load_module("synthetic_world")

import sqlalchemy as sa  # noqa: E402

from ak_trading.datapipeline import migrate, models, readback  # noqa: E402
from ak_trading.datapipeline.contracts import (  # noqa: E402
    AcquisitionMode,
    DataOrigin,
    RowStatus,
)
from ak_trading.datapipeline.ports import CalendarAnswerStatus  # noqa: E402
from ak_trading.datapipeline.storage import PipelineInterrupted  # noqa: E402

SYNTHETIC = _world.SYNTHETIC_MARKER
SYMBOL = _world.SYMBOL_MAIN
T0 = _world.T0
MIN = _world.MIN
SESSION_1 = _world.SESSION_1
SESSION_2 = _world.SESSION_2
OBSERVED_AT = _world.OBSERVED_AT

#: authoritative sha256 provenance of every integrated file (checked by
#: test_release.py / the release verifier as well)
STAGED_PROVENANCE = _bootstrap.staged_file_hashes()


@pytest.fixture(scope="module")
def acquisition():
    return _bootstrap.pinned_acquisition()


@pytest.fixture()
def world(tmp_path, acquisition):
    return _world.StorageWorld(tmp_path / "storage", acquisition=acquisition)


def _commit_rows(world, acquisition, *, n_rows, base_nano, attempt, values=None,
                 row_statuses=None, symbol=SYMBOL, observed_at_nano=OBSERVED_AT):
    envelope, classified = _world.synthetic_batch(
        acquisition=acquisition, n_rows=n_rows, base_nano=base_nano,
        attempt=attempt, values=values, row_statuses=row_statuses,
        symbol=symbol, observed_at_nano=observed_at_nano,
    )
    fence = _world.synthetic_fence(f"{_world.SOURCE_ID}|{symbol}|60")
    return world.store.commit(envelope, classified, fence), envelope


# ===========================================================================
# G1 - storage + calendar
# ===========================================================================

class TestG1StorageAndCalendar:
    def test_g1_migration_isolated_version_and_nine_tables(self, world):
        """Explicit revision 20260923_dp01 on an isolated version table; the
        root alembic chain is never touched (no root alembic_version)."""
        with world.engine.connect() as c:
            version = migrate.current_revision(c)
            assert version == "20260923_dp01"
            names = set()
            for schema in ("acquisition_w2", "datapipeline_v1", "main"):
                rows = c.execute(sa.text(
                    f"SELECT name FROM {schema}.sqlite_master WHERE type='table'"
                )).mappings().all()
                names.update(f"{schema}.{r['name']}" for r in rows)
        # acquisition_w2 three tables + six datapipeline_v1 tables
        for expected in (
            "acquisition_w2.bar_versions", "acquisition_w2.ingest_batches",
            "acquisition_w2.batch_rows",
            "datapipeline_v1.pipeline_runs", "datapipeline_v1.task_checkpoints",
            "datapipeline_v1.observation_records", "datapipeline_v1.pipeline_commits",
            "datapipeline_v1.quality_findings", "datapipeline_v1.status_events",
            "datapipeline_v1.alembic_version",
        ):
            assert expected in names, expected
        # the ROOT chain's version table must not exist anywhere
        assert not [n for n in names if n.endswith("public.alembic_version")]
        assert not [n for n in names if n.split(".")[-1] == "alembic_version"
                    and not n.startswith("datapipeline_v1.")]

    def test_g1_calendar_domains_and_pit_gates(self):
        """DP04 CalendarBook answers: VERIFIED in domain, NO_TRADING for the
        evidenced intraday break, UNKNOWN before the evidence domain and
        UNKNOWN for as_of before obtained_at (PIT gate)."""
        book = _world.build_calendar_book()
        from ak_trading.datapipeline.contracts import IntervalNano

        verified = book.query(
            dataset=_world.DATASET, source_symbol=SYMBOL, duration_seconds=60,
            window=IntervalNano(start_nano=SESSION_1[0], end_nano=SESSION_1[1]),
            as_of_nano=OBSERVED_AT,
        )
        assert verified.status is CalendarAnswerStatus.VERIFIED_SESSIONS

        with_break = book.query(
            dataset=_world.DATASET, source_symbol=SYMBOL, duration_seconds=60,
            window=IntervalNano(start_nano=SESSION_1[0], end_nano=SESSION_2[0]),
            as_of_nano=OBSERVED_AT,
        )
        assert with_break.status is CalendarAnswerStatus.VERIFIED_SESSIONS
        assert any(
            s.reason and "INTRADAY_BREAK" in s.reason for s in with_break.subranges
        )

        pre_domain = book.query(
            dataset=_world.DATASET, source_symbol=SYMBOL, duration_seconds=60,
            window=IntervalNano(start_nano=T0 - 7 * 86_400_000_000_000, end_nano=T0 - 6 * 86_400_000_000_000),
            as_of_nano=OBSERVED_AT,
        )
        assert pre_domain.status is CalendarAnswerStatus.UNKNOWN  # D06: no fabrication

        before_obtained = book.query(
            dataset=_world.DATASET, source_symbol=SYMBOL, duration_seconds=60,
            window=IntervalNano(start_nano=SESSION_1[0], end_nano=SESSION_1[1]),
            as_of_nano=T0 - 1,
        )
        assert before_obtained.status is CalendarAnswerStatus.UNKNOWN

    def test_g1_commit_publish_asof_visibility_and_same_source_replay(self, world, acquisition):
        """H08.2 same-source duplicate: committing the same batch twice adds
        zero versions; the default view only sees published CLOSED/VALID
        versions at/after available_at."""
        receipt, envelope = _commit_rows(
            world, acquisition, n_rows=10, base_nano=SESSION_1[0], attempt="att-g1-a"
        )
        assert receipt.outcome.value == "COMMITTED_NEW"
        assert receipt.available_at_nano is not None
        counts_1 = world.counts()
        assert counts_1["bar_versions"] == 10
        assert counts_1["pipeline_commits"] == 1

        # same-source duplicate replay: zero new versions, same publish
        receipt_2, _ = _commit_rows(
            world, acquisition, n_rows=10, base_nano=SESSION_1[0], attempt="att-g1-a"
        )
        assert receipt_2.outcome.value == "COMMITTED_SAME_CONTENT"
        counts_2 = world.counts()
        assert counts_2["bar_versions"] == 10  # exactly-once content versions
        assert counts_2["pipeline_commits"] == 1

        # default query: before available_at nothing is visible ...
        view_before = world.store.query_available(as_of_nano=receipt.available_at_nano - 1)
        assert view_before == ()
        # ... at available_at exactly the 10 published versions
        view = world.store.query_available(as_of_nano=receipt.available_at_nano)
        assert len(view) == 10
        for row in view:
            assert row.row_status == "CLOSED"
            assert row.quality_state == "VALID"
            assert row.data_origin == DataOrigin.SYNTHETIC.value
            assert row.acquisition_mode == AcquisitionMode.SESSION_BACKFILL.value
            assert row.available_at_nano <= receipt.available_at_nano

    def test_g1_x08_5_symbol_filter_defect_pin(self, world, acquisition):
        """X-08-5 FIX PIN (r2 flip: at r1 this pinned the defect; it now
        pins the fix and fails loudly if the defect regresses).

        The real DP02 ``query_available(source_symbol=...)`` filter matches
        the canonical BarKey JSON where ``source_symbol`` is the
        alphabetically LAST key (no trailing comma).  Publishing two symbols
        and querying per symbol returns EXACTLY that symbol's published
        rows - at r1 the filter dropped every row and returned ``()``.
        """
        receipt_main, _ = _commit_rows(
            world, acquisition, n_rows=5, base_nano=SESSION_1[0], attempt="att-x085-m"
        )
        receipt_other, _ = _commit_rows(
            world, acquisition, n_rows=3, base_nano=SESSION_1[0], attempt="att-x085-i",
            symbol=_world.SYMBOL_INDEX,
        )
        as_of = max(receipt_main.available_at_nano, receipt_other.available_at_nano)
        # unfiltered and duration-filtered views see both symbols
        assert len(world.store.query_available(as_of_nano=as_of)) == 8
        assert len(world.store.query_available(as_of_nano=as_of, duration_seconds=60)) == 8
        # the symbol filter now returns that symbol's rows (r1 defect: ())
        main_view = world.store.query_available(as_of_nano=as_of, source_symbol=SYMBOL)
        assert len(main_view) == 5
        assert all(f'"source_symbol":"{SYMBOL}"' in row.key_canonical for row in main_view)
        other_view = world.store.query_available(
            as_of_nano=as_of, source_symbol=_world.SYMBOL_INDEX
        )
        assert len(other_view) == 3
        assert all(
            f'"source_symbol":"{_world.SYMBOL_INDEX}"' in row.key_canonical
            for row in other_view
        )

    def test_g1_backfill_and_provisional_mixed_batch(self, world, acquisition):
        """H08.2 real-shape backfill mixed with in-formation rows: one batch
        carries CLOSED backfill rows plus a PROVISIONAL live row; the
        default view exposes only the CLOSED versions (Q02/Q03)."""
        statuses = ["CLOSED"] * 9 + ["PROVISIONAL"]
        receipt, envelope = _commit_rows(
            world, acquisition, n_rows=10, base_nano=SESSION_1[0],
            attempt="att-g1-mixed", row_statuses=statuses,
        )
        assert receipt.outcome.value == "COMMITTED_NEW"
        counts = world.counts()
        assert counts["bar_versions"] == 10
        view = world.store.query_available(as_of_nano=receipt.available_at_nano)
        assert len(view) == 9  # the PROVISIONAL row never enters the view
        assert all(row.row_status == "CLOSED" for row in view)
        # the in-formation bar is physically persisted as a version but not
        # consumable (row-level, not batch-level classification)
        with world.engine.connect() as c:
            provisional = readback.iter_available_versions(
                c, as_of_nano=receipt.available_at_nano
            )
            assert all(r["row_status"] == "CLOSED" for r in provisional)

    def test_g1_late_revision_conflict_blocks_view_no_retro_block(self, world, acquisition):
        """H08.2 late revision: same K, different CLOSED content -> CONFLICT,
        both versions kept, default view blocks the key from detection time
        onward while earlier as_of still resolves the original version."""
        receipt_a, _ = _commit_rows(
            world, acquisition, n_rows=5, base_nano=SESSION_1[0], attempt="att-rev-a"
        )
        assert receipt_a.outcome.value == "COMMITTED_NEW"
        later = receipt_a.available_at_nano + 10_000_000_000

        receipt_b, _ = _commit_rows(
            world, acquisition, n_rows=5, base_nano=SESSION_1[0], attempt="att-rev-b",
            values={"open": "4100.0", "high": "4104.0", "low": "4098.0",
                    "close": "4102.0", "volume": "100", "open_oi": "5000",
                    "close_oi": "5010"},
        )
        assert receipt_b.outcome.value == "CONFLICT"
        counts = world.counts()
        assert counts["bar_versions"] == 10  # both content versions kept
        assert counts["quality_findings"] >= 1  # CONTENT_CONFLICT finding

        # earlier as_of (A's own publish time, before B was attempted): the
        # original version is still resolvable - the later conflict never
        # rewrites the earlier as_of view (Q03 no-retro-blocking)
        view_early = world.store.query_available(as_of_nano=receipt_a.available_at_nano)
        assert len(view_early) == 5
        # from the conflict's detection time the key is blocked entirely
        view_late = world.store.query_available(as_of_nano=later + 10**9)
        assert view_late == ()


# ===========================================================================
# G2 - collection + recovery (real collector, real registries)
# ===========================================================================

class _CapturingStore:
    """Diagnostic wrapper: records the first commit call's arguments (the
    cross-node seam) and then fails like the real store does today."""

    def __init__(self, inner):
        self.inner = inner
        self.captured = None
        self.calls = 0
        self.failures = 0

    def commit(self, envelope, classified_rows, fence):
        self.calls += 1
        if self.captured is None:
            self.captured = (envelope, classified_rows, fence)
        try:
            return self.inner.commit(envelope, classified_rows, fence)
        except Exception:
            self.failures += 1
            raise

    def recover(self, batch_id):
        return self.inner.recover(batch_id)

    def query_available(self, **kwargs):
        return self.inner.query_available(**kwargs)


class _FailingStore:
    """SYNTHETIC DB failure wrapper (H08.2 DB-failure leg)."""

    def __init__(self, inner, failures=99):
        self.inner = inner
        self.remaining = failures

    def commit(self, envelope, classified_rows, fence):
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("synthetic db failure (SYNTHETIC-DP08)")
        return self.inner.commit(envelope, classified_rows, fence)

    def recover(self, batch_id):
        return self.inner.recover(batch_id)

    def query_available(self, **kwargs):
        return self.inner.query_available(**kwargs)


class _NullRunRegistry:
    """Registry double used ONLY where a test isolates a defect other than
    X-08-2 (documented per test)."""

    def start_run(self, **kwargs):
        return "run-dp08-null"

    def transition_run(self, *args, **kwargs):
        return None

    def finalize_run(self, *args, **kwargs):
        return None


class TestG2CollectorAndRecovery:
    @pytest.fixture()
    def collector(self, tmp_path, world, acquisition):
        from ak_trading.datapipeline.session_factory import build_synthetic_frame

        doubles = _world.load_dp03_doubles(_bootstrap)
        self.doubles = doubles
        self.clock = doubles.ClockNano(OBSERVED_AT)
        self.frames = {(SYMBOL, 60): build_synthetic_frame(
            start_nano=SESSION_1[0], duration_seconds=60, closed_rows=8,
        )}
        self.symbols = [(SYMBOL, "MAIN_CONTINUOUS", "rb")]
        self.scenario_root = tmp_path / "scenario"
        self.scenario_root.mkdir(parents=True, exist_ok=True)
        return self

    def _engine(self, world, *, store="real", run_registry="real", **kwargs):
        doubles = self.doubles
        if store == "capture":
            store_arg: Any = _CapturingStore(world.store)
        else:
            store_arg = store  # "real" / "unwired" / wrapper object
        if run_registry == "null":
            registry_arg: Any = _NullRunRegistry()
        else:
            registry_arg = world.run_registry
        engine = _world.build_engine(
            world=world, doubles=doubles,
            run_root=self.scenario_root / "run",
            frames=self.frames, timelines=[[]], clock=self.clock,
            symbols=self.symbols, durations=(60,),
            store=store_arg,
            run_registry=registry_arg,
            scheduler_state_path=self.scenario_root / "scheduler-state.json",
            **kwargs,
        )
        return engine, store_arg

    def test_g2_collector_wires_real_registries_and_raw_evidence(self, world, acquisition, collector):
        """Real engine over real DP02 task/status registries with the store
        intentionally UNWIRED (store=None, CLI r1 shape): leases, attempts
        and status events land in the real database; raw snapshot +
        envelope evidence and the heartbeat file land in the run tree; the
        honest terminal state is RECOVERY_REQUIRED (5)."""
        engine, _ = self._engine(world, store="unwired", run_registry="null")
        summary = engine.run(run_id="run-dp08-g2a")
        assert summary.snapshot_count == 1
        assert summary.task_states[f"{_world.SOURCE_ID}|{SYMBOL}|60"] == "RAW_VERIFIED"
        assert summary.terminal_state == "RECOVERY_REQUIRED"
        assert summary.exit_code == 5

        counts = world.counts()
        assert counts["task_checkpoints"] >= 1      # real lease/checkpoint row
        assert counts["status_events"] >= 2         # RUN + TASK events via SqlStatusSink
        assert counts["pipeline_commits"] == 0      # unwired store publishes nothing
        assert counts["bar_versions"] == 0

        run_root = self.scenario_root / "run"
        raw_files = sorted((run_root / "raw").rglob("frame.jsonl"))
        envelopes = sorted((run_root / "raw").rglob("envelope.json"))
        assert len(raw_files) == 1 and len(envelopes) == 1
        envelope_payload = json.loads(envelopes[0].read_text(encoding="utf-8"))
        assert envelope_payload["data_origin"] == "SYNTHETIC"
        assert "SYNTHETIC" in (envelope_payload.get("synthetic_marker") or "")
        heartbeat = run_root / "heartbeats" / "w-dp08.json"
        assert heartbeat.is_file()
        payload = json.loads(heartbeat.read_text(encoding="utf-8"))
        assert payload["worker_id"] == "w-dp08"

    def test_g2_x08_1_commit_row_shape_seam_defect_pin(self, world, acquisition, collector):
        """X-08-1 FIX PIN (r2 flip: at r1 this pinned the defect; it now
        pins the fix and fails loudly if the seam regresses).

        The real DP03 engine now converts its DP01 ``RowObservation`` rows
        into the wired store's pinned-acquisition ``ClassifiedRow`` shape
        before committing: the commit through the wrapping store SUCCEEDS,
        the snapshot is published (task ACCEPTED, run SUCCEEDED, no pending
        spool) and a direct replay of the captured (envelope, rows, fence)
        is accepted idempotently by the real store - at r1 the same commit
        raised ``AttributeError`` and the snapshot was pended forever.
        """
        engine, cap = self._engine(world, store="capture", run_registry="null")
        summary = engine.run(run_id="run-dp08-x081")
        assert cap.calls >= 1 and cap.failures == 0          # r1: failures == calls
        task_key = f"{_world.SOURCE_ID}|{SYMBOL}|60"
        assert summary.task_states[task_key] == "ACCEPTED"
        assert summary.terminal_state == "SUCCEEDED"
        assert summary.exit_code == 0
        assert summary.pending_uncommitted == 0
        assert not (self.scenario_root / "run" / "spool" / "w-dp08" / "pending.jsonl").exists()

        # the captured cross-node seam payload is the pinned acquisition shape
        envelope, rows, fence = cap.captured
        assert rows and type(rows[0]).__name__ == "ClassifiedRow"  # r1: RowObservation
        counts = world.counts()
        assert counts["pipeline_commits"] == 1
        assert counts["bar_versions"] == counts["observation_records"] == 8

        # minimal direct replay at the seam: the real store accepts the same
        # payload idempotently (r1: AttributeError 'duration_seconds')
        replay = world.store.commit(envelope, rows, fence)
        assert replay.outcome.value in ("COMMITTED_SAME_CONTENT", "NOTHING_TO_DO")
        assert world.counts()["pipeline_commits"] == 1

    def test_g2_x08_2_run_id_identity_defect_pin(self, world, acquisition, collector):
        """X-08-2 FIX PIN (r2 flip: at r1 this pinned the defect; it now
        pins the fix and fails loudly if the identity split regresses).

        The engine now ADOPTS the run_id minted by the real
        ``SqlRunRegistry``: the run finalizes without ``StorageError`` and
        ONE registry-minted identity threads through the pipeline_runs row,
        the summary, the heartbeat file and every status event - at r1 the
        registry refused the finalize (``unknown run_id``) and the run row
        kept a different id than the engine's.
        """
        engine, _ = self._engine(world, store="unwired", run_registry="real")
        summary = engine.run(run_id="run-dp08-x082")  # r1: StorageError here
        assert summary.run_id != "run-dp08-x082"      # registry-minted, not engine-local

        runs = world.runs()
        assert len(runs) == 1
        assert runs[0]["run_id"] == summary.run_id    # r1: identity split
        assert runs[0]["overall_state"] == "RECOVERY_REQUIRED"  # unwired store stays honest
        # every engine/registry-scoped event carries the adopted identity;
        # (lease-scoped DP02 events key by dataset by frozen design - they
        # never carried the engine id, before or after the fix)
        events = world.status_events()
        assert [e for e in events if e["run_id"] == summary.run_id]  # start/task/finalize
        assert not [e for e in events if e["run_id"] == "run-dp08-x082"]  # r1 split id gone
        heartbeat = json.loads(
            (self.scenario_root / "run" / "heartbeats" / "w-dp08.json").read_text(
                encoding="utf-8"
            )
        )
        assert heartbeat["run_id"] == summary.run_id

    def test_g2_db_failure_pends_never_publishes(self, world, acquisition, collector):
        """H08.2 DB-failure leg with a SYNTHETIC failing store: commits fail,
        snapshots are pended to spool (never dropped), no publish, no
        checkpoint advance, terminal RECOVERY_REQUIRED."""
        engine, failing = self._engine(world, store=_FailingStore(world.store), run_registry="null")
        summary = engine.run(run_id="run-dp08-dbfail")
        assert summary.pending_uncommitted >= 1
        assert summary.terminal_state == "RECOVERY_REQUIRED"
        counts = world.counts()
        assert counts["pipeline_commits"] == 0
        assert counts["bar_versions"] == 0
        reasons = [e["reason_sanitized"] for e in world.status_events()]
        assert any("store commit failed" in r for r in reasons)
        assert any("checkpoint NOT advanced" in r for r in reasons)

    def test_g2_permission_denied_terminal_and_sanitized_events(self, world, acquisition, collector):
        """H08.2 permission-denial leg: the SYNTHETIC session script denies
        permission; the engine records PERMISSION_DENIED, fails the run (2)
        and the real DB keeps only sanitized reasons."""
        from ak_trading.datapipeline.session_factory import SubscriptionKey
        from ak_trading.datapipeline.contracts import SourceCoordinate
        from ak_trading.datapipeline.session_factory import PermissionDeniedError

        key = (SYMBOL, 60)
        factory_kwargs = dict(
            frames=self.frames, timelines=[[]], clock_nano=self.clock,
            subscribe_errors={key: PermissionDeniedError()},
        )
        from ak_trading.datapipeline.session_factory import SyntheticSessionFactory
        from ak_trading.datapipeline.scheduler import FairRotatingScheduler, SchedulerStateFile
        from ak_trading.datapipeline.collector import CollectionEngine

        run_root = (self.scenario_root / "run-perm").resolve()
        engine = CollectionEngine(
            worker_id="w-dp08", dataset=_world.DATASET,
            scheduler=FairRotatingScheduler(
                source_id=_world.SOURCE_ID, symbols=self.symbols,
                clock_nano=self.clock,
                state_file=SchedulerStateFile(self.scenario_root / "sched-perm.json"),
                durations_seconds=(60,),
            ),
            session_factory=SyntheticSessionFactory(**factory_kwargs),
            store=None, status_sink=world.status_sink,
            task_registry=world.task_registry, run_registry=_NullRunRegistry(),
            calendar=_world.build_calendar_book(), run_root=run_root,
            universe_sha256=_world.universe_hash_for(self.symbols, (60,)),
            clock_nano=self.clock, sleep=lambda _s: None,
            group_service_seconds=1.0, stale_after_seconds=0.25,
            run_deadline_seconds=20.0, finalize_wait_seconds=5.0,
        )
        summary = engine.run(run_id="run-dp08-perm")
        assert summary.permission_denied is True
        assert summary.terminal_state == "FAILED"
        assert summary.exit_code == 2
        reasons = [e["reason_sanitized"] for e in world.status_events()]
        assert any("permission" in r.lower() for r in reasons)
        for reason in reasons:  # Q07: no credential shapes anywhere
            assert "password" not in reason.lower() and "token" not in reason.lower()

    @pytest.mark.parametrize("interrupt_stage,expected_recover", [
        # sidecar committed but unpublished: recovery finishes the publish
        ("after_sidecar_commit", "COMPLETED_BY_RECOVERY"),
        # chunks complete, sidecar absent: recovery says re-drive commit()
        ("after_reconcile", "RESUMED_UNPUBLISHED"),
    ])
    def test_g2_store_crash_recovery_exactly_once(self, tmp_path, acquisition, interrupt_stage, expected_recover):
        """H08.2 crash + restart leg at the storage boundary (V04 shape): a
        crash mid-commit leaves an unpublished batch invisible to consumers;
        a NEW store instance recovers/re-drives it exactly-once; replaying
        the same envelope afterwards adds zero versions."""
        root = tmp_path / f"crash-{interrupt_stage}"
        root.mkdir(parents=True)
        from ak_trading.datapipeline import storage as dp_storage

        crashing = _world.StorageWorld(
            root, acquisition=acquisition, interrupt_after=interrupt_stage
        )
        envelope, classified = _world.synthetic_batch(
            acquisition=acquisition, n_rows=12, base_nano=SESSION_1[0], attempt="att-crash",
        )
        with pytest.raises(PipelineInterrupted):
            crashing.store.commit(envelope, classified, _world.synthetic_fence(f"{_world.SOURCE_ID}|{SYMBOL}|60"))
        assert crashing.counts()["pipeline_commits"] in (0, 1)
        assert crashing.store.query_available(as_of_nano=OBSERVED_AT + 10**12) == ()  # never visible mid-crash

        # "restart": fresh facades over the same database files
        engine = sa.create_engine("sqlite://")
        models.attach_sqlite_schemas(
            engine, acquisition_path=str(root / "acq.db"), pipeline_path=str(root / "dp.db"),
        )
        with engine.connect() as c:
            unpublished = readback.find_unpublished_batches(c)
        assert len(unpublished) == 1
        batch_id = unpublished[0]["batch_id"]

        restarted_store = dp_storage.SqlPipelineStore(
            engine, dataset=_world.DATASET, writer_id="w-dp08-restart",
            acquisition=acquisition, time_source=_world.DeterministicClock(),
        )
        outcome = restarted_store.recover(batch_id)
        assert outcome.status.value == expected_recover
        if outcome.status.value == "RESUMED_UNPUBLISHED":
            # re-drive the same envelope: idempotent publish, 0 new versions
            replay = restarted_store.commit(
                envelope, classified, _world.synthetic_fence(f"{_world.SOURCE_ID}|{SYMBOL}|60")
            )
            assert replay.outcome.value in ("COMMITTED_NEW", "COMMITTED_SAME_CONTENT")
            assert replay.available_at_nano is not None

        with engine.connect() as c:
            bars = c.execute(sa.select(sa.func.count()).select_from(models.bar_versions)).scalar_one()
            commits = c.execute(sa.select(sa.func.count()).select_from(models.pipeline_commits)).scalar_one()
            obs = c.execute(sa.select(sa.func.count()).select_from(models.observation_records)).scalar_one()
        assert (bars, obs, commits) == (12, 12, 1)  # exactly-once

        # replaying again after recovery: nothing left to do
        replay2 = restarted_store.commit(
            envelope, classified, _world.synthetic_fence(f"{_world.SOURCE_ID}|{SYMBOL}|60")
        )
        assert replay2.outcome.value in ("NOTHING_TO_DO", "COMMITTED_SAME_CONTENT")
        with engine.connect() as c:
            bars2 = c.execute(sa.select(sa.func.count()).select_from(models.bar_versions)).scalar_one()
        assert bars2 == 12

        # and the recovered batch is now consumable
        view = restarted_store.query_available(as_of_nano=OBSERVED_AT + 10**12)
        assert len(view) == 12


# ===========================================================================
# G3 - quality + legacy (real commit view, real calendar, real finding sink)
# ===========================================================================

def _report_facts(report) -> dict:
    return {
        "evaluation_id": report.evaluation_id,
        "scope": report.scope,
        "as_of_nano": report.as_of_nano,
        "check_execution": report.check_execution.value,
        "coverage_state": report.coverage_state.value if report.coverage_state else None,
        "target_total": report.target_total,
        "verified_denominator": report.verified_denominator,
        "valid_unique_closed": report.valid_unique_closed,
        "missing": report.missing,
        "conflicts": report.conflicts,
        "quarantined": report.quarantined,
        "provisional": report.provisional,
        "calendar_unknown_ranges": report.calendar_unknown_ranges,
        "source_unknown_ranges": report.source_unknown_ranges,
        "generation_evidence_sha256": report.generation_evidence_sha256,
        "universe_sha256": report.universe_sha256,
        "commit_view_sha256": report.commit_view_sha256,
        "data_origin": "SYNTHETIC",
    }


class TestG3QualityAndLegacy:
    def _validator(self, world):
        from ak_trading.datapipeline.gaps import GapRegistry
        from ak_trading.datapipeline.validation import CompletenessValidatorImpl

        registry = GapRegistry(sink=world.finding_sink)
        return CompletenessValidatorImpl(finding_registry=registry), registry

    def test_g3_partial_coverage_missing_bars_findings_in_real_sink(self, world, acquisition):
        """Real store view (10 of 30 session-1 minute bars) + real calendar
        denominator + SYNTHETIC FIXED_SLOT generation evidence -> PARTIAL
        coverage with MISSING_BAR findings persisted by the real DP02
        FindingSink; the conclusion is deterministic."""
        receipt, _ = _commit_rows(
            world, acquisition, n_rows=10, base_nano=SESSION_1[0], attempt="att-g3"
        )
        as_of = receipt.available_at_nano + 1
        known_gaps = {"generation_evidence": _world.generation_evidence_payload()}
        validator, registry = self._validator(world)
        scope = f"TARGET|{_world.DATASET}|{_world.SOURCE_ID}|{SYMBOL}|60|{SESSION_1[0]}|{SESSION_1[1]}"
        view = world.store.query_available(as_of_nano=as_of, duration_seconds=60)
        report = validator.validate(
            scope=scope, commit_view=view,
            calendar=_world.build_calendar_book(),
            known_gaps=known_gaps, as_of_nano=as_of,
        )
        assert report.check_execution.value == "COMPLETED"
        assert report.coverage_state.value == "PARTIAL"
        assert report.verified_denominator == 30
        # SYNTHETIC rows are physically published but NEVER count toward
        # REAL_SOURCE consumption (Q02 isolation; the reason names it)
        assert report.valid_unique_closed == 0
        assert report.missing == 20
        assert "NON_REAL_ORIGIN:SYNTHETIC" in report.reasons
        assert "MISSING_BAR" in report.reasons
        assert world.counts()["quality_findings"] >= 1
        # determinism: identical inputs and as_of give identical conclusions
        report2 = validator.validate(
            scope=scope,
            commit_view=world.store.query_available(as_of_nano=as_of, duration_seconds=60),
            calendar=_world.build_calendar_book(),
            known_gaps=known_gaps, as_of_nano=as_of,
        )
        assert _report_facts(report) == _report_facts(report2)

    def test_g3_historical_unknown_calendar_never_complete(self, world, acquisition):
        """H08.2 historical calendar UNKNOWN: a pre-evidence-domain window
        yields coverage UNKNOWN (never COMPLETE) with calendar-unknown
        segments and no fabricated denominator."""
        receipt, _ = _commit_rows(
            world, acquisition, n_rows=3,
            base_nano=T0 - 7 * 86_400_000_000_000, attempt="att-g3-unk",
        )
        as_of = receipt.available_at_nano + 1
        validator, _ = self._validator(world)
        report = validator.validate(
            scope=(
                f"TARGET|{_world.DATASET}|{_world.SOURCE_ID}|{SYMBOL}|60"
                f"|{T0 - 7 * 86_400_000_000_000}|{T0 - 7 * 86_400_000_000_000 + 10 * MIN}"
            ),
            commit_view=world.store.query_available(as_of_nano=as_of, duration_seconds=60),
            calendar=_world.build_calendar_book(),
            known_gaps={},
            as_of_nano=as_of,
        )
        assert report.coverage_state is None or report.coverage_state.value == "UNKNOWN"
        assert report.coverage_state is None or report.coverage_state.value != "COMPLETE"
        assert (report.calendar_unknown_ranges or 0) >= 1

    def test_g3_conflict_blocks_default_view_and_reports_missing(self, world, acquisition):
        """Late-revision content conflict: DP02 keeps both versions and
        blocks the key from the default view (Q04); DP05 over the real view
        reports those keys as MISSING (never COMPLETE), and the CONTENT_CONFLICT
        finding sits in the shared quality_findings table (DP02 wrote it)."""
        receipt_a, _ = _commit_rows(
            world, acquisition, n_rows=5, base_nano=SESSION_1[0], attempt="att-g3c-a"
        )
        _commit_rows(
            world, acquisition, n_rows=5, base_nano=SESSION_1[0], attempt="att-g3c-b",
            values={"open": "4200.0", "high": "4204.0", "low": "4198.0",
                    "close": "4202.0", "volume": "100", "open_oi": "5000",
                    "close_oi": "5010"},
        )
        as_of = receipt_a.available_at_nano + 10**10
        # the whole key range is invisible in the default view
        view = world.store.query_available(as_of_nano=as_of, duration_seconds=60)
        assert view == ()

        validator, registry = self._validator(world)
        report = validator.validate(
            scope=f"TARGET|{_world.DATASET}|{_world.SOURCE_ID}|{SYMBOL}|60|{SESSION_1[0]}|{SESSION_1[1]}",
            commit_view=view,
            calendar=_world.build_calendar_book(),
            known_gaps={"generation_evidence": _world.generation_evidence_payload()},
            as_of_nano=as_of,
        )
        assert report.coverage_state is not None
        assert report.coverage_state.value != "COMPLETE"
        assert report.missing == 30  # conflicted keys are missing from the view
        assert report.valid_unique_closed == 0
        # DP02 persisted the CONTENT_CONFLICT finding in the shared table
        from ak_trading.datapipeline import models

        with world.engine.connect() as c:
            conflict_rows = c.execute(
                sa.select(models.quality_findings.c.reason).where(
                    models.quality_findings.c.reason == "CONTENT_CONFLICT"
                )
            ).scalars().all()
        assert conflict_rows

    def test_g3_dp07_seeds_enter_real_finding_sink_verbatim(self, world):
        """DP07's frozen known-gap register (hash-verified staging copy)
        projects candidates that land in the REAL DP02 FindingSink with the
        Q06 reasons verbatim; Q06-reason seeds import into the DP05 registry
        (the three documented DP07 extensions are Q06-external and stay
        candidate-only, as the frozen seed-reason domain requires)."""
        from ak_trading.datapipeline.gaps import GapRegistry, KNOWN_GAP_SEED_REASONS
        from ak_trading.datapipeline import legacy as dp07_legacy

        known_gaps_path = (
            _bootstrap.STAGING_ROOT / "config/datapipeline/known-gaps.json"
        )
        payload = json.loads(known_gaps_path.read_text(encoding="utf-8"))
        dp07_legacy.validate_known_gaps(payload)
        candidates = dp07_legacy.known_gap_candidate_findings(payload)
        assert len(candidates) >= 11

        registry = GapRegistry(sink=world.finding_sink)
        q06_gaps = [g for g in payload["gaps"] if g["reason"] in KNOWN_GAP_SEED_REASONS]
        assert q06_gaps, "the Q06 seed subset must be non-empty"
        seeds = [
            {
                "dataset": gap["datasets"][0] if gap["datasets"] else "",
                "symbols": list(gap["scope"].get("symbols") or ()),
                "key": str(gap["scope"].get("key", gap["reason"])),
                "reason": gap["reason"],
                "window": f"{gap['observed_at']}/{gap.get('observed_through') or gap['observed_at']}",
                "evidence_sha256": "0" * 64,
                "observed_at_nano": 0,
            }
            for gap in q06_gaps
        ]
        ids = registry.import_known_gap_seeds(seeds)
        assert len(ids) == len(q06_gaps)
        # every Q06 reason preserved verbatim in the SQL sink
        rows = world.counts()["quality_findings"]
        assert rows >= len(q06_gaps)

    def test_g3_validator_budget_marks_unchecked_not_run(self, world, acquisition):
        """H05.7 orthogonality at integration: with a budget that exhausts
        after the first target, the started target keeps its own coverage
        and the aggregate reports DEFERRED_BUDGET, never COMPLETE; the
        unstarted target is an explicit NOT_RUN with null numbers."""
        from ak_trading.datapipeline.contracts import IntervalNano
        from ak_trading.datapipeline.validation import (
            CompletenessValidatorImpl, TargetScope, ValidationBudget, run_scan,
        )
        from ak_trading.datapipeline.gaps import GapRegistry

        receipt, _ = _commit_rows(
            world, acquisition, n_rows=10, base_nano=SESSION_1[0], attempt="att-g3b"
        )
        as_of = receipt.available_at_nano + 1
        validator = CompletenessValidatorImpl(
            finding_registry=GapRegistry(sink=world.finding_sink)
        )
        targets = [
            TargetScope(
                dataset=_world.DATASET, source_id=_world.SOURCE_ID, source_symbol=SYMBOL,
                duration_seconds=60,
                window=IntervalNano(start_nano=SESSION_1[0], end_nano=SESSION_1[1]),
            ),
            TargetScope(
                dataset=_world.DATASET, source_id=_world.SOURCE_ID, source_symbol=_world.SYMBOL_INDEX,
                duration_seconds=60,
                window=IntervalNano(start_nano=SESSION_1[0], end_nano=SESSION_1[1]),
            ),
        ]
        view = world.store.query_available(as_of_nano=as_of)
        outcome = run_scan(
            validator,
            targets=targets,
            commit_view=view,
            calendar=_world.build_calendar_book(),
            known_gaps={},
            as_of_nano=as_of,
            budget=ValidationBudget(max_targets=1),
        )
        assert outcome.check_execution.value == "DEFERRED_BUDGET"
        assert outcome.deferred is True
        assert outcome.not_run_targets  # unstarted target explicit
        ordered_scopes = sorted(
            (t.scope_string() for t in targets),
            key=lambda s: s.split("|")[3],
        )
        started_scope = ordered_scopes[0]           # scan order: by symbol
        not_run_scope = ordered_scopes[1]
        assert outcome.not_run_targets == [not_run_scope]
        started_report = outcome.per_target[started_scope]
        assert started_report.check_execution.value == "COMPLETED"
        assert started_report.coverage_state is not None
        not_run_report = outcome.per_target[not_run_scope]
        assert not_run_report.check_execution.value == "NOT_RUN"
        assert not_run_report.missing is None  # null numbers, never fake zeros
        if outcome.aggregate is not None:
            assert outcome.aggregate.coverage_state is None


# ===========================================================================
# G4 - monitoring over real artifacts
# ===========================================================================

class TestG4MonitoringOverRealArtifacts:
    def _run_collector(self, world, scenario_root, *, store=None):
        from ak_trading.datapipeline.session_factory import build_synthetic_frame

        doubles = _world.load_dp03_doubles(_bootstrap)
        clock = doubles.ClockNano(OBSERVED_AT)
        frames = {(SYMBOL, 60): build_synthetic_frame(
            start_nano=SESSION_1[0], duration_seconds=60, closed_rows=8,
        )}
        symbols = [(SYMBOL, "MAIN_CONTINUOUS", "rb")]
        engine = _world.build_engine(
            world=world, doubles=doubles, run_root=scenario_root / "run",
            frames=frames, timelines=[[]], clock=clock, symbols=symbols,
            durations=(60,), store=store if store is not None else world.store,
            run_registry=_NullRunRegistry(),
            scheduler_state_path=scenario_root / "scheduler-state.json",
        )
        summary = engine.run(run_id="run-dp08-g4")
        return engine, summary, clock

    def test_g4_monitor_over_real_artifacts_and_x08_3_pin(self, world, tmp_path):
        """Real chain artifacts -> DP06 monitor (X-08-3 FIX PIN; r2 flip:
        at r1 this pinned the defect; it now pins the fix and fails loudly
        if the vocabulary mismatch regresses).

        Success leg: the collector's REAL heartbeat now speaks the monitor's
        frozen vocabulary (``heartbeat_at_nano`` + ``run_id``); a fresh
        heartbeat 60s old no longer fires HEARTBEAT_EXPIRED (r1: it fired
        even when fresh), and a fully published run raises no false
        DB_COMMIT_FAILED.  Failure leg: a SYNTHETIC failing store drives the
        real collector to emit ``COMMIT_FAILED:``-prefixed reasons, which
        the monitor detects as DB_COMMIT_FAILED over REAL collector events
        (r1: the free-form reasons never matched, detection was silent).
        """
        from ak_trading.datapipeline import monitoring

        # --- success leg: real store, everything published (X-08-1 fixed) ---
        scenario = tmp_path / "g4-ok"
        scenario.mkdir(parents=True)
        engine, summary, clock = self._run_collector(world, scenario)
        task_key = f"{_world.SOURCE_ID}|{SYMBOL}|60"
        assert summary.task_states[task_key] == "ACCEPTED"
        assert summary.pending_uncommitted == 0           # r1: >= 1 (X-08-1 pends)
        assert summary.terminal_state == "SUCCEEDED"

        # the REAL heartbeat file carries the monitor vocabulary (X-08-3b)
        heartbeat_file = scenario / "run" / "heartbeats" / "w-dp08.json"
        heartbeat_payload = json.loads(heartbeat_file.read_text(encoding="utf-8"))
        assert "heartbeat_at_nano" in heartbeat_payload    # r1: heartbeat_nano
        assert heartbeat_payload["run_id"] == summary.run_id

        facts = _world.export_facts_tree(
            facts_root=scenario / "facts", world=world,
            tasks=[_world.task_fact(
                task_key, duration_seconds=60,
                symbol=SYMBOL, state=summary.task_states[task_key],
                window=(SESSION_1[0], SESSION_1[1]),
            )],
            quality_reports=[],
            calendar_answers=[],
        )
        snapshot = monitoring.load_pipeline_facts(
            heartbeats_root=scenario / "run" / "heartbeats",
            events_root=scenario / "facts" / "events",
            quality_root=scenario / "facts" / "quality",
            tasks_file=facts["tasks"],
            calendar_file=facts["calendar"],
            runs_file=facts["runs"],
        )
        assert snapshot["heartbeats"] and snapshot["heartbeats"][0]["worker_id"] == "w-dp08"
        heartbeat_at = int(snapshot["heartbeats"][0]["heartbeat_at_nano"])
        now = heartbeat_at + 60 * 1_000_000_000  # one fresh minute after the real heartbeat
        tick = monitoring.evaluate_tick(monitoring.MonitorState(monitor_id="mon-dp08"), snapshot, now)
        reasons = {e.reason for e in tick.events}
        # X-08-3(b) fixed: the heartbeat IS fresh (60s old, inside the 120s
        # frozen timeout) and the monitor reads the field the writer wrote.
        assert "HEARTBEAT_EXPIRED" not in reasons   # r1 pin: fired on a fresh heartbeat
        # a clean, fully published run raises no false commit-failure alert
        assert "DB_COMMIT_FAILED" not in reasons

        # --- failure leg: SYNTHETIC failing store -> REAL collector events ---
        scenario_fail = tmp_path / "g4-fail"
        scenario_fail.mkdir(parents=True)
        engine_f, summary_f, _ = self._run_collector(
            world, scenario_fail, store=_FailingStore(world.store)
        )
        assert summary_f.pending_uncommitted >= 1
        assert summary_f.terminal_state == "RECOVERY_REQUIRED"

        facts_f = _world.export_facts_tree(
            facts_root=scenario_fail / "facts", world=world,
            tasks=[_world.task_fact(
                task_key, duration_seconds=60,
                symbol=SYMBOL, state=summary_f.task_states[task_key],
                window=(SESSION_1[0], SESSION_1[1]),
            )],
            quality_reports=[],
            calendar_answers=[],
        )
        snapshot_f = monitoring.load_pipeline_facts(
            heartbeats_root=scenario_fail / "run" / "heartbeats",
            events_root=scenario_fail / "facts" / "events",
            quality_root=scenario_fail / "facts" / "quality",
            tasks_file=facts_f["tasks"],
            calendar_file=facts_f["calendar"],
            runs_file=facts_f["runs"],
        )
        real_reasons = [e["reason_sanitized"] for e in snapshot_f["status_events"]]
        # X-08-3(a) fixed: the collector's failure reason carries the frozen
        # structured prefix (r1: free-form "store commit failed after ...").
        assert any(r.startswith("COMMIT_FAILED:") for r in real_reasons)
        heartbeat_at_f = int(snapshot_f["heartbeats"][0]["heartbeat_at_nano"])
        tick_f = monitoring.evaluate_tick(
            monitoring.MonitorState(monitor_id="mon-dp08-f"), snapshot_f,
            heartbeat_at_f + 60 * 1_000_000_000,
        )
        reasons_f = {e.reason for e in tick_f.events}
        # the monitor now DETECTS the real commit failure (r1 pin: silent)
        assert "DB_COMMIT_FAILED" in reasons_f
        # and the same fresh heartbeat produces no false expiry alarm
        assert "HEARTBEAT_EXPIRED" not in reasons_f

    def test_g4_report_deterministic_publish_and_not_run_nulls(self, world, tmp_path):
        """Real facts -> deterministic report id, r1/r2 revisions, NOT_RUN
        scopes render nulls (no fake 0%/100%), SYNTHETIC marker present,
        no trading tokens anywhere in the outputs."""
        from ak_trading.datapipeline import dashboard, monitoring, reporting

        scenario = tmp_path / "g4r"
        scenario.mkdir(parents=True)
        engine, summary, clock = self._run_collector(world, scenario)
        facts_inputs = _world.export_facts_tree(
            facts_root=scenario / "facts", world=world,
            tasks=[
                _world.task_fact(
                    f"{_world.SOURCE_ID}|{SYMBOL}|60", duration_seconds=60, symbol=SYMBOL,
                    state="RAW_VERIFIED", window=(SESSION_1[0], SESSION_1[1]),
                ),
                _world.task_fact(
                    f"{_world.SOURCE_ID}|{SYMBOL}|86400", duration_seconds=86400, symbol=SYMBOL,
                    state="PLANNED", window=(SESSION_1[0], SESSION_2[1]),
                ),
            ],
            quality_reports=[
                {
                    "evaluation_id": "q-dp08-run", "scope": f"TARGET|{_world.DATASET}|{_world.SOURCE_ID}|{SYMBOL}|60|{SESSION_1[0]}|{SESSION_1[1]}",
                    "as_of_nano": OBSERVED_AT + MIN, "check_execution": "COMPLETED",
                    "coverage_state": "PARTIAL", "target_total": 30,
                    "verified_denominator": 30, "valid_unique_closed": 8,
                    "missing": 22, "conflicts": 0, "quarantined": 0, "provisional": 0,
                    "calendar_unknown_ranges": 0, "source_unknown_ranges": 0,
                    "generation_evidence_sha256": None, "universe_sha256": None,
                    "commit_view_sha256": None, "data_origin": "SYNTHETIC",
                },
                {
                    "evaluation_id": "q-dp08-daily", "scope": f"TARGET|{_world.DATASET}|{_world.SOURCE_ID}|{SYMBOL}|86400|{SESSION_1[0]}|{SESSION_2[1]}",
                    "as_of_nano": OBSERVED_AT + MIN, "check_execution": "NOT_RUN",
                    "coverage_state": None, "target_total": None,
                    "verified_denominator": None, "valid_unique_closed": None,
                    "missing": None, "conflicts": None, "quarantined": None,
                    "provisional": None, "calendar_unknown_ranges": None,
                    "source_unknown_ranges": None,
                    "generation_evidence_sha256": None, "universe_sha256": None,
                    "commit_view_sha256": None, "data_origin": "SYNTHETIC",
                },
            ],
            calendar_answers=[],
        )
        snapshot = monitoring.load_pipeline_facts(
            heartbeats_root=scenario / "run" / "heartbeats",
            events_root=scenario / "facts" / "events",
            quality_root=scenario / "facts" / "quality",
            tasks_file=facts_inputs["tasks"],
            calendar_file=facts_inputs["calendar"],
            runs_file=facts_inputs["runs"],
        )
        evidence_hashes = {"run": "0" * 64, "commit": "1" * 64, "calendar": "2" * 64,
                           "universe": "3" * 64, "policy": "4" * 64,
                           "generation": None, "source_bundle": "5" * 64}
        facts = reporting.build_report(
            monitor_id="mon-dp08", as_of_nano=OBSERVED_AT + 2 * MIN,
            snapshot=snapshot, alert_state={}, health={}, health_reasons={},
            evidence_hashes=evidence_hashes,
        )
        facts2 = reporting.build_report(
            monitor_id="mon-dp08", as_of_nano=OBSERVED_AT + 2 * MIN,
            snapshot=snapshot, alert_state={}, health={}, health_reasons={},
            evidence_hashes=evidence_hashes,
        )
        assert facts["report_id"] == facts2["report_id"]  # determinism (V11)
        markdown = reporting.render_markdown(facts)
        html = dashboard.render_html(facts)
        payload = markdown + html + json.dumps(facts, sort_keys=True)
        assert SYNTHETIC in payload or "SYNTHETIC" in payload
        for token in ("insert_order", "cancel_order", "enable_trading"):
            assert token not in payload
        # NOT_RUN scope keeps nulls, never a fabricated percentage
        daily_lines = [l for l in markdown.splitlines() if "86400" in l]
        assert daily_lines
        for line in daily_lines:
            assert "100%" not in line and "0%" not in line

        out_root = scenario / "report-out"
        published = reporting.publish_report(
            facts=facts, markdown=markdown, html=html,
            output_root=out_root, monitor_id="mon-dp08",
        )
        assert (out_root / "reports" / facts["report_id"] / "r1").is_dir()
        published2 = reporting.publish_report(
            facts=facts, markdown=markdown, html=html,
            output_root=out_root, monitor_id="mon-dp08",
        )
        assert (out_root / "reports" / facts["report_id"] / "r2").is_dir()


# ===========================================================================
# V13 - full-chain baseline reproducibility (SYNTHETIC)
# ===========================================================================

class TestV13Chain:
    def _scenario(self, root, acquisition):
        """One full SYNTHETIC chain pass: storage -> commit -> as_of view ->
        validator -> findings (deterministic clock, no wall time)."""
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        world = _world.StorageWorld(root / "storage", acquisition=acquisition)
        receipt, _ = _commit_rows(
            world, acquisition, n_rows=10, base_nano=SESSION_1[0], attempt="att-v13"
        )
        as_of = receipt.available_at_nano + 1
        view = world.store.query_available(as_of_nano=as_of, duration_seconds=60)
        from ak_trading.datapipeline.gaps import GapRegistry
        from ak_trading.datapipeline.validation import CompletenessValidatorImpl

        validator = CompletenessValidatorImpl(finding_registry=GapRegistry(sink=world.finding_sink))
        report = validator.validate(
            scope=f"TARGET|{_world.DATASET}|{_world.SOURCE_ID}|{SYMBOL}|60|{SESSION_1[0]}|{SESSION_1[1]}",
            commit_view=view, calendar=_world.build_calendar_book(),
            known_gaps={"generation_evidence": _world.generation_evidence_payload()},
            as_of_nano=as_of,
        )
        return {
            "available_keys": sorted((r.key_canonical, r.content_sha256) for r in view),
            "counts": world.counts(),
            "report": _report_facts(report),
            "report_available_at": receipt.available_at_nano,
        }

    def test_v13_same_baseline_reruns_produce_identical_results(self, tmp_path, acquisition):
        """V13: the same SYNTHETIC baseline re-run from scratch produces the
        same published keys/content hashes, conserved table counts and the
        same quality conclusion (deterministic clock, no wall time)."""
        first = self._scenario(tmp_path / "rerun-a", acquisition)
        second = self._scenario(tmp_path / "rerun-b", acquisition)
        assert first["available_keys"] == second["available_keys"]
        assert first["counts"] == second["counts"]
        assert first["report"] == second["report"]
        assert first["report"]["coverage_state"] == "PARTIAL"
        assert first["report"]["missing"] == 20

    def test_v13_counts_conserve(self, tmp_path, acquisition):
        """Frame conservation through the chain: every committed row is a
        bar version AND an observation record; the default view exposes
        exactly the CLOSED/VALID published subset."""
        result = self._scenario(tmp_path / "conserve", acquisition)
        counts = result["counts"]
        assert counts["bar_versions"] == counts["observation_records"] == 10
        assert counts["pipeline_commits"] == 1
        assert len(result["available_keys"]) == 10


# ===========================================================================
# V13b - full chain THROUGH the collector publish path (r2; unblocks r1's
#         NR-DP08-04 which X-08-1/X-08-2 blocked)
# ===========================================================================

class TestV13ChainThroughCollector:
    def test_v13b_collect_publish_validate_monitor_single_identity(self, tmp_path, acquisition):
        """V13b (r2): the full SYNTHETIC chain THROUGH the real collector
        publish path - the real DP03 engine (real run registry, real store)
        collects and PUBLISHES, the as_of view (symbol-scoped, X-08-5 fixed)
        feeds the real DP05 validator, and the real artifacts feed the DP06
        monitor with zero false alerts - one run identity end to end.

        At r1 this leg was NOT_RUN (NR-DP08-04): X-08-1 pended every commit
        and X-08-2 broke finalization on the real registry.
        """
        from ak_trading.datapipeline import monitoring
        from ak_trading.datapipeline.gaps import GapRegistry
        from ak_trading.datapipeline.session_factory import build_synthetic_frame
        from ak_trading.datapipeline.validation import CompletenessValidatorImpl

        world = _world.StorageWorld(tmp_path / "storage", acquisition=acquisition)
        doubles = _world.load_dp03_doubles(_bootstrap)
        clock = doubles.ClockNano(OBSERVED_AT)
        task_key = f"{_world.SOURCE_ID}|{SYMBOL}|60"
        engine = _world.build_engine(
            world=world, doubles=doubles, run_root=tmp_path / "run",
            frames={(SYMBOL, 60): build_synthetic_frame(
                start_nano=SESSION_1[0], duration_seconds=60, closed_rows=8,
            )},
            timelines=[[]], clock=clock,
            symbols=[(SYMBOL, "MAIN_CONTINUOUS", "rb")], durations=(60,),
            scheduler_state_path=tmp_path / "scheduler-state.json",
            # store defaults to the REAL DP02 store; run_registry defaults
            # to the REAL DP02 registry (both exercised on purpose).
        )
        summary = engine.run(run_id="run-dp08-v13b")

        # -- collector leg published (X-08-1 fixed, X-08-2 fixed) ----------
        assert summary.snapshot_count == 1
        assert summary.task_states[task_key] == "ACCEPTED"
        assert summary.terminal_state == "SUCCEEDED"
        assert summary.exit_code == 0
        assert summary.pending_uncommitted == 0
        counts = world.counts()
        assert counts["pipeline_commits"] == 1
        assert counts["bar_versions"] == counts["observation_records"] == 8

        # one registry-minted identity everywhere it is engine/registry
        # scoped (DP02's lease-scoped events key by dataset by frozen
        # design - before and after the fix)
        runs = world.runs()
        assert len(runs) == 1
        assert runs[0]["run_id"] == summary.run_id
        assert runs[0]["overall_state"] == "SUCCEEDED"
        events = world.status_events()
        assert [e for e in events if e["run_id"] == summary.run_id]  # start/task/finalize
        assert not [e for e in events if e["run_id"] == "run-dp08-v13b"]
        assert any(
            e["run_id"] == summary.run_id and "commit accepted" in e["reason_sanitized"]
            for e in events
        )
        heartbeat = json.loads(
            (tmp_path / "run" / "heartbeats" / "w-dp08.json").read_text(encoding="utf-8")
        )
        assert heartbeat["run_id"] == summary.run_id
        assert "heartbeat_at_nano" in heartbeat

        # -- published view feeds the real DP05 validator (X-08-5 fixed) ---
        as_of = OBSERVED_AT + 10**12
        view = world.store.query_available(as_of_nano=as_of, duration_seconds=60)
        assert len(view) == 8
        symbol_view = world.store.query_available(as_of_nano=as_of, source_symbol=SYMBOL)
        assert len(symbol_view) == 8  # the collector's own symbol
        assert world.store.query_available(
            as_of_nano=as_of, source_symbol=_world.SYMBOL_INDEX
        ) == ()  # and only that symbol
        validator = CompletenessValidatorImpl(finding_registry=GapRegistry(sink=world.finding_sink))
        report = validator.validate(
            scope=f"TARGET|{_world.DATASET}|{_world.SOURCE_ID}|{SYMBOL}|60|{SESSION_1[0]}|{SESSION_1[1]}",
            commit_view=view,
            calendar=_world.build_calendar_book(),
            known_gaps={"generation_evidence": _world.generation_evidence_payload()},
            as_of_nano=as_of,
        )
        assert report.check_execution.value == "COMPLETED"
        assert report.coverage_state.value == "PARTIAL"
        assert report.verified_denominator == 30
        assert report.missing == 22
        assert report.valid_unique_closed == 0  # SYNTHETIC origin never counts REAL
        assert "NON_REAL_ORIGIN:SYNTHETIC" in report.reasons
        assert world.counts()["quality_findings"] >= 1

        # -- real artifacts feed the DP06 monitor with zero false alerts ---
        facts = _world.export_facts_tree(
            facts_root=tmp_path / "facts", world=world,
            tasks=[_world.task_fact(
                task_key, duration_seconds=60, symbol=SYMBOL,
                state=summary.task_states[task_key],
                window=(SESSION_1[0], SESSION_1[1]),
            )],
            quality_reports=[],
            calendar_answers=[],
        )
        snapshot = monitoring.load_pipeline_facts(
            heartbeats_root=tmp_path / "run" / "heartbeats",
            events_root=tmp_path / "facts" / "events",
            quality_root=tmp_path / "facts" / "quality",
            tasks_file=facts["tasks"],
            calendar_file=facts["calendar"],
            runs_file=facts["runs"],
        )
        heartbeat_at = int(snapshot["heartbeats"][0]["heartbeat_at_nano"])
        tick = monitoring.evaluate_tick(
            monitoring.MonitorState(monitor_id="mon-dp08-v13b"), snapshot,
            heartbeat_at + 60 * 1_000_000_000,
        )
        reasons = {e.reason for e in tick.events}
        assert "HEARTBEAT_EXPIRED" not in reasons
        assert "DB_COMMIT_FAILED" not in reasons
        assert "RUN_LOST" not in reasons  # finalized run, fresh evidence


# ===========================================================================
# V14 - capacity leg (SQLite-bound; PG capacity legs NOT_RUN)
# ===========================================================================

class TestV14Capacity:
    def test_v14_volume_bounded_readback(self, tmp_path, acquisition):
        """A multi-batch volume leg (2 batches x 1200 rows) commits within
        the frozen chunk bound and the paginated available-view reads back
        every key; PG capacity/EXPLAIN legs remain NOT_RUN (register)."""
        world = _world.StorageWorld(tmp_path / "v14", acquisition=acquisition)
        for attempt, base in (
            ("att-v14-a", SESSION_1[0] - 1200 * MIN),
            ("att-v14-b", SESSION_1[0] - 2400 * MIN),
        ):
            receipt, _ = _commit_rows(
                world, acquisition, n_rows=1200, base_nano=base, attempt=attempt,
            )
            assert receipt.outcome.value == "COMMITTED_NEW"
        counts = world.counts()
        assert counts["bar_versions"] == 2400
        view = world.store.query_available(as_of_nano=OBSERVED_AT + 10**13)
        assert len(view) == 2400  # keyset pagination returns every key


# ===========================================================================
# PostgreSQL legs - explicit NOT_RUN without an injected isolated database
# ===========================================================================

class TestPostgresLegsNotRun:
    def test_pg_legs_skip_without_injection(self):
        """V03/V05/V14 PostgreSQL acceptance legs stay NOT_RUN (explicit
        skip, never a silent pass) while no isolated PG database is
        injected by the controlled test launcher (04-validation rule)."""
        pytest.skip(
            "NOT_RUN: isolated PostgreSQL not injected "
            "(DP08_TEST_POSTGRES_URL absent); SQLite legs prove logic only"
        )
