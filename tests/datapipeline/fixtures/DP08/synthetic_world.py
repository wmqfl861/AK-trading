"""DP08 SYNTHETIC integration world (tests/datapipeline/fixtures/DP08).

Everything in this module is SYNTHETIC test scaffolding, clearly labeled;
it never claims to be historical PIT data or a live-market fact.  It wires
the REAL implementations of the six delivered nodes (no mock substitutes
for actual dependencies): DP02 storage/registries on the SQLite double
(the PostgreSQL acceptance legs are NOT_RUN without an injected isolated
database), the DP04 CalendarBook over an authored SYNTHETIC evidence
document, the DP03 CollectionEngine with its own SYNTHETIC timeline
sessions (real SDK sessions are NOT_RUN), the DP05 validator, the DP07
seed register and the DP06 monitor/report pipeline.

The batch builder produces DP02-interface-shaped envelopes + pinned
acquisition ClassifiedRow sequences (the documented SqlPipelineStore.commit
input shape, mirrored after DP02's frozen ``fixtures/DP02/synthetic.py``
but aligned to this world's symbol/window).  The DP03 collector currently
emits DP01 RowObservation tuples instead - that cross-node seam mismatch
is defect X-08-1, pinned by its own tests, and must NOT be papered over
here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import sqlalchemy as sa

SYNTHETIC_MARKER = "SYNTHETIC-DP08-INTEGRATION-TEST-DATA"
DATASET = "futures_kline"
SOURCE_ID = "tqsdk-free-realtime-get-kline-serial"
SYMBOL_MAIN = "KQ.m@SHFE.rb"          # main continuous (frozen universe shape)
SYMBOL_INDEX = "KQ.i@SHFE.rb"         # index
DAY = 86_400_000_000_000
MIN = 60_000_000_000

#: 2026-09-21T01:30:00Z == 09:30 Asia/Shanghai, inside a SYNTHETIC trading day.
from ak_trading.datapipeline.contracts import iso_instant_to_nano  # noqa: E402

T0 = iso_instant_to_nano("2026-09-21T01:30:00+00:00")
#: session segments of the SYNTHETIC trading day (see calendar_payload)
SESSION_1 = (T0, T0 + 30 * MIN)                       # 09:30-10:00
SESSION_2 = (T0 + 40 * MIN, T0 + 75 * MIN)            # 10:40-11:15 (intraday break 10:00-10:40)
SESSION_SPAN = (SESSION_1[0], SESSION_2[1])
#: an observation instant comfortably after every bar in the world
OBSERVED_AT = T0 + 2 * 3600 * 1_000_000_000


# ---------------------------------------------------------------------------
# DP04 calendar evidence (SYNTHETIC, exchange-level applicability)
# ---------------------------------------------------------------------------

def calendar_payload() -> Dict[str, Any]:
    """Authored SYNTHETIC session-grid document for SHFE.rb-shaped symbols."""
    return {
        "schema_version": "datapipeline-calendar-evidence-v1",
        "dataset": DATASET,
        "source_id": SOURCE_ID,
        "data_origin": "SYNTHETIC",
        "synthetic_marker": SYNTHETIC_MARKER,
        "evidence_version": "dp08-e2e-calendar-r1",
        "evidence_kind": "SESSION_GRID",
        "applicability": {"exchanges": ["shfe"], "products": [], "source_symbols": []},
        "valid_from_nano": str(T0),
        "valid_through_nano": str(T0 + 30 * DAY),
        "coverage": "COMPLETE_IN_VALIDITY_DOMAIN",
        "pit_basis": "OBTAINED_AT",
        "obtained_at_nano": str(T0),
        "evidence_reference": "SYNTHETIC: DP08 authored integration fixture (tests/datapipeline/fixtures/DP08/synthetic_world.py)",
        "review_status": "synthetic_fixture",
        "sessions": [
            {
                "trading_day": "2026-09-21",
                "segments": [
                    {"kind": "DAY", "start_nano": str(SESSION_1[0]), "end_nano": str(SESSION_1[1])},
                    {"kind": "DAY", "start_nano": str(SESSION_2[0]), "end_nano": str(SESSION_2[1])},
                ],
            },
        ],
    }


def build_calendar_book():
    from ak_trading.datapipeline.calendar import CalendarBook, calendar_evidence_document_from_mapping

    return CalendarBook(
        (calendar_evidence_document_from_mapping(calendar_payload()),)
    )


def generation_evidence_payload() -> Dict[str, Any]:
    """SYNTHETIC FIXED_SLOT BarGenerationEvidence (proof-complete).

    Without generation evidence every real expectation stays
    EXPECTATION_UNKNOWN (H05.1); this authored SYNTHETIC claim carries all
    the frozen proof fields so the validator can build a VERIFIED minute
    denominator over the SYNTHETIC session day.  It is a LOGIC fixture,
    never a claim about the real tqsdk channel (whose rule stays UNKNOWN).
    """
    return {
        "source_id": SOURCE_ID,
        "dataset": DATASET,
        "applicable_symbols": (SYMBOL_MAIN, SYMBOL_INDEX),
        "applicable_products": (),
        "duration_seconds": 60,
        "valid_from_nano": T0,
        "valid_through_nano": T0 + 30 * DAY,
        "emission_policy": "FIXED_SLOT",
        "bar_time_anchor_and_close_rule": "EPOCH_ALIGNED_GRID",
        "boundary_short_bar_rule": "INCLUDE_BOUNDARY_SHORT",
        "evidence_text_sha256": "f" * 64,
        "evidence_reference": "SYNTHETIC: DP08 authored generation-rule fixture (synthetic_world.py)",
        "obtained_at_nano": T0,
        "data_origin": "SYNTHETIC",
    }


# ---------------------------------------------------------------------------
# DP02 storage world (SQLite double; PG legs NOT_RUN without injection)
# ---------------------------------------------------------------------------

class DeterministicClock:
    """Injectable integer-nanosecond clock (deterministic receipts)."""

    def __init__(self, start: int = OBSERVED_AT) -> None:
        self.now = start

    def __call__(self) -> int:
        self.now += 1_000_000_000
        return self.now


class MonotonicFake:
    """Deterministic monotonic double for engine deadlines.

    Advances by ``step`` seconds per call so group/readiness deadlines are
    bounded by ITERATIONS, not by wall-clock scheduling noise on a loaded
    host (DP08 integration reproducibility).
    """

    def __init__(self, step: float = 0.01) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class StorageWorld:
    """Real DP02 facades + registries on an isolated SQLite double."""

    def __init__(self, root: Path, *, acquisition: Any, interrupt_after: Optional[str] = None) -> None:
        from ak_trading.datapipeline import migrate, models, storage

        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.engine = sa.create_engine("sqlite://")
        models.attach_sqlite_schemas(
            self.engine,
            acquisition_path=str(root / "acq.db"),
            pipeline_path=str(root / "dp.db"),
        )
        with self.engine.connect() as connection:
            self.migration_result = migrate.upgrade_to_revision(connection)
        self.clock = DeterministicClock()
        self.store = storage.SqlPipelineStore(
            self.engine,
            dataset=DATASET,
            writer_id="w-dp08",
            acquisition=acquisition,
            time_source=self.clock,
            interrupt_after=interrupt_after,
        )
        self.run_registry = storage.SqlRunRegistry(self.engine, time_source=self.clock)
        self.task_registry = storage.SqlTaskRegistry(self.engine, time_source=self.clock)
        self.finding_sink = storage.SqlFindingSink(self.engine, time_source=self.clock)
        self.status_sink = storage.SqlStatusSink(self.engine, time_source=self.clock)

    def counts(self) -> Dict[str, int]:
        from ak_trading.datapipeline import models

        def one(table) -> int:
            with self.engine.connect() as c:
                return int(c.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())

        return {
            "bar_versions": one(models.bar_versions),
            "batch_rows": one(models.batch_rows),
            "ingest_batches": one(models.ingest_batches),
            "observation_records": one(models.observation_records),
            "pipeline_commits": one(models.pipeline_commits),
            "quality_findings": one(models.quality_findings),
            "status_events": one(models.status_events),
            "pipeline_runs": one(models.pipeline_runs),
            "task_checkpoints": one(models.task_checkpoints),
        }

    def status_events(self) -> List[Dict[str, Any]]:
        from ak_trading.datapipeline import models

        with self.engine.connect() as c:
            rows = c.execute(
                sa.select(
                    models.status_events.c.event_id,
                    models.status_events.c.run_id,
                    models.status_events.c.task_key,
                    models.status_events.c.kind,
                    models.status_events.c.occurred_at_nano,
                    models.status_events.c.reason_sanitized,
                ).order_by(models.status_events.c.occurred_at_nano, models.status_events.c.event_id)
            ).mappings().all()
        return [
            {
                "event_id": r["event_id"],
                "kind": r["kind"],
                "run_id": r["run_id"],
                "task_key": r["task_key"],
                "occurred_at_nano": int(r["occurred_at_nano"]),
                "reason_sanitized": r["reason_sanitized"],
            }
            for r in rows
        ]

    def runs(self) -> List[Dict[str, Any]]:
        from ak_trading.datapipeline import models

        with self.engine.connect() as c:
            rows = c.execute(sa.select(models.pipeline_runs)).mappings().all()
        return [dict(r) for r in rows]


def synthetic_batch(
    *,
    acquisition: Any,
    n_rows: int,
    symbol: str = SYMBOL_MAIN,
    duration_seconds: int = 60,
    base_nano: int,
    observed_at_nano: int = OBSERVED_AT,
    attempt: str = "att-1",
    task_id: str = "task-1",
    values: Optional[Dict[str, str]] = None,
    row_status: str = "CLOSED",
    row_statuses: Optional[Sequence[Optional[str]]] = None,
) -> Tuple[Any, List[Any]]:
    """One SYNTHETIC envelope + pinned-acquisition ClassifiedRow sequence.

    Mirrors DP02's frozen fixture builder (fixtures/DP02/synthetic.py) - the
    DOCUMENTED SqlPipelineStore.commit input shape - with this world's
    symbol and calendar-aligned windows.  ``row_statuses`` optionally gives
    a per-row status (None => padding-shaped invalid row) so one envelope
    can mix CLOSED backfill rows with PROVISIONAL (in-formation) rows.
    """
    from ak_trading.datapipeline import contracts as dp_contracts

    values = values or {
        "open": "4001.0", "high": "4004.0", "low": "3998.0", "close": "4002.0",
        "volume": "100", "open_oi": "5000", "close_oi": "5010",
    }

    def _status_at(i: int) -> Optional[str]:
        if row_statuses is not None:
            entry = row_statuses[i]
            return entry if isinstance(entry, str) else None
        return row_status

    rows_obs: List[Any] = []
    classified: List[Any] = []
    for i in range(n_rows):
        status_value = _status_at(i)
        nano = base_nano + i * duration_seconds * 1_000_000_000
        if status_value is None:
            # padding-shaped row: no key/status on the observation side and
            # a PADDING classified row on the storage side
            rows_obs.append(
                dp_contracts.RowObservation(
                    raw_row_ordinal=i,
                    frame_partition=dp_contracts.FramePartition.PADDING,
                    row_status=None,
                    data_origin=dp_contracts.DataOrigin.SYNTHETIC,
                    acquisition_mode=dp_contracts.AcquisitionMode.SESSION_BACKFILL,
                    observed_at_nano=observed_at_nano,
                    first_observed_at_nano=observed_at_nano,
                    connection_epoch=1, continuity_id="c1", update_seq=i,
                    key=None, content_sha256=None,
                    event_start_nano=None, event_end_nano=None,
                    availability_basis=dp_contracts.AvailabilityBasis.RETROSPECTIVE_CAPTURE,
                )
            )
            classified.append(
                acquisition.ClassifiedRow(
                    raw_row_ordinal=i, datetime_nano=None, source_symbol=symbol,
                    duration_seconds=duration_seconds,
                    bar_start_nano=None, bar_end_nano=None,
                    trading_day=None, trading_day_source="none",
                    frame_partition=acquisition.FramePartition.PADDING,
                    valid_status=None, values={}, content_sha256=None,
                    evidence_version="ev1", reason="padding head",
                )
            )
            continue
        status = dp_contracts.RowStatus(status_value)
        key = dp_contracts.BarKey(
            source_id=SOURCE_ID, source_symbol=symbol,
            duration_seconds=duration_seconds, datetime_nano=nano,
        )
        content = dp_contracts.canonical_bar_content(
            duration_seconds=duration_seconds, datetime_nano=nano,
            bar_start_nano=nano, bar_end_nano=nano + duration_seconds * 1_000_000_000,
            trading_day="2026-09-21", values=values,
        )
        rows_obs.append(
            dp_contracts.RowObservation(
                raw_row_ordinal=i,
                frame_partition=dp_contracts.FramePartition.VALID,
                row_status=status,
                data_origin=dp_contracts.DataOrigin.SYNTHETIC,
                acquisition_mode=dp_contracts.AcquisitionMode.SESSION_BACKFILL,
                observed_at_nano=observed_at_nano,
                first_observed_at_nano=observed_at_nano,
                connection_epoch=1, continuity_id="c1", update_seq=i,
                key=key, content_sha256=content,
                event_start_nano=nano,
                event_end_nano=nano + duration_seconds * 1_000_000_000,
                availability_basis=(
                    dp_contracts.AvailabilityBasis.RETROSPECTIVE_CAPTURE
                    if status is dp_contracts.RowStatus.CLOSED
                    else dp_contracts.AvailabilityBasis.LIVE_CAPTURE_EVIDENCE
                ),
            )
        )
        classified.append(
            acquisition.ClassifiedRow(
                raw_row_ordinal=i, datetime_nano=nano, source_symbol=symbol,
                duration_seconds=duration_seconds,
                bar_start_nano=nano, bar_end_nano=nano + duration_seconds * 1_000_000_000,
                trading_day="2026-09-21", trading_day_source="session_evidence",
                frame_partition=acquisition.FramePartition.VALID,
                valid_status=acquisition.ValidRowStatus(status_value),
                values=values, content_sha256=content,
                evidence_version="ev1", reason=None,
            )
        )
    valid_rows = sum(1 for i in range(n_rows) if _status_at(i) is not None)
    meta = dp_contracts.RawSnapshotMetaV1(
        task_id=task_id, attempt_id=attempt, source_symbol=symbol,
        duration_seconds=duration_seconds, requested_length=8192,
        returned_length=n_rows, frame_rows=n_rows,
        padding_rows=n_rows - valid_rows,
        invalid_rows=0, valid_rows=valid_rows,
        observed_at_nano=observed_at_nano,
        sdk_version="synthetic-sdk-dp08", serializer_version="v1",
        raw_path=f"raw/{attempt}/frame.jsonl",
        raw_sha256=hashlib.sha256(f"{SYNTHETIC_MARKER}|{attempt}".encode("utf-8")).hexdigest(),
        raw_bytes=len(f"{SYNTHETIC_MARKER}|{attempt}"),
        first_valid_nano=base_nano,
        last_valid_nano=base_nano + (n_rows - 1) * duration_seconds * 1_000_000_000,
    )
    envelope = dp_contracts.SnapshotEnvelope(
        snapshot_id=f"snap-{attempt}", task_id=task_id, attempt_id=attempt,
        coordinate=dp_contracts.SourceCoordinate(
            source_id=SOURCE_ID, source_symbol=symbol,
            duration_seconds=duration_seconds,
        ),
        connection_baseline=dp_contracts.ConnectionBaseline(
            connection_epoch=1, baseline_snapshot_id="b1", ready_update_seq=1,
        ),
        meta=meta, rows=tuple(rows_obs),
        raw_bytes_sha256=meta.raw_sha256,
        policy_sha256=hashlib.sha256(b"dp08-synthetic-policy").hexdigest(),
        source_bundle_sha256=hashlib.sha256(b"dp08-synthetic-source-bundle").hexdigest(),
        calendar_evidence_sha256=None, generation_evidence_sha256=None,
        universe_sha256=hashlib.sha256(b"dp08-synthetic-universe").hexdigest(),
        data_origin=dp_contracts.DataOrigin.SYNTHETIC,
        synthetic_marker=SYNTHETIC_MARKER,
    )
    return envelope, classified


def synthetic_fence(task_key: str, *, epoch: int = 1, lease: str = "lease-dp08-1"):
    from ak_trading.datapipeline.ports import FencingToken

    return FencingToken(dataset=DATASET, task_key=task_key, lease_id=lease, fencing_epoch=epoch)


# ---------------------------------------------------------------------------
# DP03 collector world (SYNTHETIC timeline sessions; real engine + registries)
# ---------------------------------------------------------------------------

def load_dp03_doubles(bootstrap_module) -> Any:
    """Load DP03's SYNTHETIC session doubles READ-ONLY (hash-verified).

    The doubles file is DP03's frozen test fixture; its sha256 is verified
    against DP03's freeze manifest before loading (no silent substitutes).
    """
    import hashlib as _hashlib

    path = bootstrap_module.REPO_ROOT / "tests/datapipeline/fixtures/DP03/synthetic_doubles.py"
    manifest_key = "tests/datapipeline/fixtures/DP03/synthetic_doubles.py"
    pins = bootstrap_module._manifest_pins("DP03")
    if manifest_key not in pins:
        raise RuntimeError("DP03 freeze manifest does not pin synthetic_doubles.py")
    actual = _hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != pins[manifest_key]:
        raise RuntimeError(
            f"DP03 synthetic_doubles.py drifted (manifest {pins[manifest_key]} vs {actual})"
        )
    spec = importlib.util.spec_from_file_location("dp08_dp03_doubles", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def universe_hash_for(symbols: Sequence[Tuple[str, str, str]], durations: Sequence[int]) -> str:
    from ak_trading.datapipeline.contracts import InstrumentKind, UniverseSymbolRecord, universe_sha256

    return universe_sha256(
        dataset=DATASET, source_id=SOURCE_ID, durations_seconds=tuple(durations),
        symbols=[
            UniverseSymbolRecord(
                source_symbol=s, instrument_kind=InstrumentKind(k), product_key=p
            )
            for s, k, p in symbols
        ],
    )


def build_engine(
    *,
    world: StorageWorld,
    doubles: Any,
    run_root: Path,
    frames: Mapping[Tuple[str, int], Any],
    timelines: Sequence[Sequence[Mapping[str, Any]]],
    clock: Any,
    symbols: Sequence[Tuple[str, str, str]],
    durations: Sequence[int] = (60,),
    store: Any = "real",
    scheduler_state_path: Optional[Path] = None,
    run_registry: Any = None,
    **engine_kwargs,
):
    """Real CollectionEngine over real DP02 registries + SYNTHETIC sessions.

    ``store``: "real" (default) wires the REAL DP02 store; "unwired" passes
    None (CLI r1 shape - snapshots classified but never published); any
    other object (diagnostic wrappers) is passed through verbatim.
    ``run_registry`` defaults to the REAL DP02 registry; pass a double ONLY
    in tests isolating a defect other than X-08-2 (documented per test).
    """
    from ak_trading.datapipeline.collector import CollectionEngine
    from ak_trading.datapipeline.scheduler import FairRotatingScheduler, SchedulerStateFile
    from ak_trading.datapipeline.session_factory import SyntheticSessionFactory

    if store == "real":
        store_arg = world.store
    elif store == "unwired":
        store_arg = None
    else:
        store_arg = store

    run_root = Path(run_root).resolve()
    scheduler = FairRotatingScheduler(
        source_id=SOURCE_ID, symbols=list(symbols), clock_nano=clock,
        state_file=SchedulerStateFile(scheduler_state_path or (run_root.parent / "scheduler-state.json")),
        durations_seconds=tuple(durations),
    )
    factory = SyntheticSessionFactory(frames=dict(frames), timelines=list(timelines), clock_nano=clock)
    return CollectionEngine(
        worker_id="w-dp08", dataset=DATASET, scheduler=scheduler,
        session_factory=factory,
        store=store_arg,
        status_sink=world.status_sink,
        task_registry=world.task_registry,
        run_registry=world.run_registry if run_registry is None else run_registry,
        calendar=build_calendar_book(),
        run_root=run_root,
        universe_sha256=universe_hash_for(symbols, durations),
        clock_nano=clock,
        clock_mono=MonotonicFake(),
        sleep=lambda _s: None,
        group_service_seconds=1.0,
        stale_after_seconds=0.25,
        run_deadline_seconds=engine_kwargs.pop("run_deadline_seconds", 30.0),
        finalize_wait_seconds=engine_kwargs.pop("finalize_wait_seconds", 5.0),
        **engine_kwargs,
    )


# ---------------------------------------------------------------------------
# DP06 facts wiring (real artifacts -> monitoring inputs)
# ---------------------------------------------------------------------------

def export_facts_tree(
    *,
    facts_root: Path,
    world: StorageWorld,
    tasks: Sequence[Mapping[str, Any]],
    quality_reports: Sequence[Mapping[str, Any]],
    calendar_answers: Sequence[Mapping[str, Any]],
) -> Dict[str, Path]:
    """Materialize DP06's local-facts inputs from REAL pipeline artifacts.

    DP06's production inputs are local fact files (H06.1); this exporter
    writes the real DB status events (read-only), the real scheduler task
    states and the real DP05 quality reports into that shape under
    ``facts_root``.  It is TEST scaffolding inside the DP08 scenario root -
    the collector heartbeat file is consumed in place (real DP03 output).
    """
    facts_root = Path(facts_root)
    (facts_root / "events").mkdir(parents=True, exist_ok=True)
    (facts_root / "quality").mkdir(parents=True, exist_ok=True)
    events_path = facts_root / "events" / "worker-w-dp08.jsonl"
    with events_path.open("w", encoding="utf-8") as handle:
        for event in world.status_events():
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    tasks_path = facts_root / "tasks.json"
    tasks_path.write_text(
        json.dumps({"schema": "dp08-facts/1", "data_origin": "SYNTHETIC", "tasks": list(tasks)},
                   indent=1, sort_keys=True),
        encoding="utf-8",
    )
    quality_path = facts_root / "quality" / "q-dp08.json"
    quality_path.write_text(
        json.dumps(list(quality_reports), indent=1, sort_keys=True), encoding="utf-8",
    )
    calendar_path = facts_root / "calendar_answers.json"
    calendar_path.write_text(
        json.dumps(list(calendar_answers), indent=1, sort_keys=True), encoding="utf-8",
    )
    runs_path = facts_root / "runs.json"
    runs_path.write_text(
        json.dumps(
            [
                {
                    "run_id": r["run_id"],
                    "mode": r["mode"],
                    "state": r.get("overall_state"),
                    "started_at_nano": int(r["started_at_nano"]) if r.get("started_at_nano") else None,
                    "ended_at_nano": int(r["ended_at_nano"]) if r.get("ended_at_nano") else None,
                }
                for r in world.runs()
            ],
            indent=1, sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "events": events_path,
        "tasks": tasks_path,
        "quality": quality_path,
        "calendar": calendar_path,
        "runs": runs_path,
    }


def task_fact(task_key: str, *, duration_seconds: int, symbol: str, state: str,
              window: Tuple[int, int]) -> Dict[str, Any]:
    return {
        "task_key": task_key,
        "run_id": "run-dp08-e2e",
        "source_id": SOURCE_ID,
        "source_symbol": symbol,
        "duration_seconds": duration_seconds,
        "instrument_kind": "MAIN_CONTINUOUS",
        "acquisition_state": state,
        "target_window": {"start_nano": window[0], "end_nano": window[1]},
    }
