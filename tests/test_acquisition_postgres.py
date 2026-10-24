"""Real-PostgreSQL storage tests against a disposable isolated instance.

dev-plan section 7 requires: actual PostgreSQL schema/unique/FK/ON CONFLICT/
transaction rollback and post-commit recovery on an isolated instance whose
database name matches ``ak_acq_w2_test_<nonce>``, with the connection
injected by the fixture only. SQLite/mocks cannot stand in for this file.

The instance is created by THIS test (initdb into a temp directory, trust
auth, random port), started and stopped here, and deleted afterwards. No
existing service, production database, or project default connection is
read, restarted, or modified. If PostgreSQL server binaries are not
available the module records an honest NOT_RUN marker and skips.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from ak_trading.acquisition import contracts as c
from ak_trading.acquisition import storage as sto
from ak_trading.acquisition.calendar import CalendarBook
from ak_trading.acquisition.capture import CaptureRequest, _build_meta, classify_frame
from ak_trading.acquisition import tq_adapter as tq

PG_BIN_CANDIDATES = (
    r"D:\PostgreSQL\18\bin",
    r"C:\Program Files\PostgreSQL\18\bin",
    r"C:\Program Files\PostgreSQL\17\bin",
    r"C:\Program Files\PostgreSQL\16\bin",
)


def _find_pg_bin() -> Path | None:
    found = shutil.which("initdb")
    if found:
        return Path(found).parent
    for candidate in PG_BIN_CANDIDATES:
        if (Path(candidate) / "initdb.exe").exists() or (
            Path(candidate) / "initdb"
        ).exists():
            return Path(candidate)
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(bin_dir: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(bin_dir / args[0]), *args[1:]],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _wait_ready(url: str, *, seconds: float = 90.0) -> bool:
    """Probe readiness ourselves; pg_ctl's wait probe is unreliable here."""
    import psycopg

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(url, connect_timeout=2, autocommit=True):
                return True
        except Exception:
            time.sleep(1.0)
    return False


@pytest.fixture(scope="module")
def postgres_url(tmp_path_factory):
    bin_dir = _find_pg_bin()
    if bin_dir is None:
        pytest.skip(
            "PostgreSQL server binaries not found: real-PG contract test "
            "recorded NOT_RUN (dev-plan section 7 forbids mock substitutes)"
        )
    cluster = tmp_path_factory.mktemp("pgcluster")
    port = _free_port()
    nonce = uuid.uuid4().hex[:8]
    init = _run(bin_dir, "initdb", "-D", str(cluster), "-U", "acqtest",
                "--auth=trust", "-E", "UTF8")
    assert init.returncode == 0, init.stderr
    # NOTE: do not restrict listen_addresses; the Windows pg_ctl readiness
    # probe and psycopg must agree on the stack. Explicit -t bounds the wait;
    # our own psycopg probe below is the authoritative readiness check.
    # pg_ctl's stdio goes to FILES, never pipes: the postmaster inherits the
    # handles and a pipe would deadlock communicate() until server exit.
    env = {**os.environ, "PGCTLTIMEOUT": "90"}
    start_log_path = cluster / "pgctl-start.log"
    with open(start_log_path, "ab") as start_log:
        start = subprocess.run(
            [str(bin_dir / "pg_ctl"), "-D", str(cluster),
             "-o", f"-p {port}", "-l", str(cluster / "pg.log"),
             "-t", "90", "-w", "start"],
            stdin=subprocess.DEVNULL,
            stdout=start_log,
            stderr=subprocess.STDOUT,
            timeout=150, check=False, env=env,
        )

    import psycopg

    # Raw psycopg URL for direct probes; SQLAlchemy URL (+psycopg driver)
    # matches the project's DatabaseSettings.sqlalchemy_url conversion.
    admin_raw = f"postgresql://acqtest@127.0.0.1:{port}/postgres"
    db_name = f"ak_acq_w2_test_{nonce}"
    try:
        if not _wait_ready(admin_raw):
            tail = ""
            if start_log_path.exists():
                tail = start_log_path.read_text(encoding="utf-8", errors="ignore")[-300:]
            raise AssertionError(
                f"disposable test instance never became ready: "
                f"pg_ctl rc={start.returncode} log={tail}"
            )
        with psycopg.connect(admin_raw, autocommit=True, connect_timeout=5) as conn:
            conn.execute(f'CREATE DATABASE "{db_name}"')
        yield f"postgresql+psycopg://acqtest@127.0.0.1:{port}/{db_name}"
    finally:
        subprocess.run(
            [str(bin_dir / "pg_ctl"), "-D", str(cluster), "-m", "fast", "-w", "stop"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=60, check=False,
        )
        shutil.rmtree(cluster, ignore_errors=True)


@pytest.fixture()
def pg_engine(postgres_url):
    from sqlalchemy import create_engine

    engine = create_engine(postgres_url)
    sto.ensure_schema_and_tables(engine)
    sto.install_immutability_guard(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def policy():
    return c.Policy.from_file(
        Path(__file__).resolve().parents[1] / "config/execution-policy.frozen.json"
    ).validate()


@pytest.fixture()
def book(synthetic_calendar_payload):
    return CalendarBook.from_mapping(synthetic_calendar_payload)


def _nano(iso: str) -> int:
    return c.iso_instant_to_nano(iso)


def _batch_plan(
    book, policy, *, attempt="a1", raw_sha="a" * 64, rows=6, padding=2,
    source_id="synthetic|pgtest",
):
    import dataclasses

    frame = tq.make_fake_frame(
        symbol="SHFE.rb2601", duration_seconds=60,
        start_nano=_nano("2026-09-18T14:00:00+08:00"),
        rows=rows, closed_rows=rows, padding_rows=padding,
    )
    classified, _, _ = classify_frame(
        frame, source_symbol="SHFE.rb2601", duration_seconds=60,
        product_key="SHFE.rb", calendar_book=book,
        watermark_nano=_nano("2026-09-18T15:30:00+08:00") - 60 * 10**9,
        evidence_version="ev1",
    )
    request = CaptureRequest("SHFE.rb2601", 60, "t1", attempt, "SHFE.rb")
    meta = _build_meta(
        request=request, policy=policy, snapshot=frame, rows=classified,
        observed_at_nano=_nano("2026-09-18T15:30:00+08:00"),
        sdk_version="SYNTHETIC-FAKE",
    )
    meta = dataclasses.replace(meta, raw_sha256=raw_sha)
    return sto.build_batch_plan(
        task_logical_key="SHFE.rb2601|60", task_id="t1", attempt_id=attempt,
        source_id=source_id, meta=meta, rows=classified,
        policy_sha256=policy.sha256, universe_sha256="u", source_sha256="s",
        calendar_sha256="cal", created_at_nano=_nano("2026-09-18T15:30:00+08:00"),
    )


class TestRealPostgres:
    def test_schema_is_exactly_three_tables(self, pg_engine):
        from sqlalchemy import inspect

        inspector = inspect(pg_engine)
        assert "acquisition_w2" in inspector.get_schema_names()
        tables = sorted(inspector.get_table_names(schema="acquisition_w2"))
        assert tables == ["bar_versions", "batch_rows", "ingest_batches"]
        # No leakage into public.
        assert not set(inspector.get_table_names(schema="public")) & set(tables)

    def test_unique_constraint_blocks_duplicate_versions(self, pg_engine, book, policy):
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy import exc as sa_exc

        plan = _batch_plan(book, policy, source_id="synthetic|pgtest|unique")
        sto.ingest_batch(pg_engine, plan)
        row = next(
            r for r in plan.rows
            if r.frame_partition is c.FramePartition.VALID and r.datetime_nano
        )
        with pytest.raises(sa_exc.IntegrityError):
            with pg_engine.begin() as conn:
                conn.execute(
                    pg_insert(sto.bar_versions)
                    .values(
                        source_id="synthetic|pgtest|unique",
                        source_symbol=row.source_symbol,
                        duration_seconds=row.duration_seconds,
                        datetime_nano=row.datetime_nano,
                        content_sha256=row.content_sha256,
                        row_status=row.valid_status.value,
                        evidence_version=row.evidence_version,
                        bar_start_nano=row.bar_start_nano,
                        bar_end_nano=row.bar_end_nano,
                        trading_day=row.trading_day,
                        observed_at_nano=1,
                        imported_at_nano=1,
                        open_s=row.values["open"],
                        high_s=row.values["high"],
                        low_s=row.values["low"],
                        close_s=row.values["close"],
                        volume_s=row.values["volume"],
                        open_oi_s=row.values["open_oi"],
                        close_oi_s=row.values["close_oi"],
                    )
                )

    def test_fk_rejects_orphan_batch_rows(self, pg_engine):
        from sqlalchemy import exc as sa_exc
        from sqlalchemy import insert

        with pytest.raises(sa_exc.IntegrityError):
            with pg_engine.begin() as conn:
                conn.execute(
                    insert(sto.batch_rows).values(
                        batch_id="nonexistent", raw_row_ordinal=0,
                        source_symbol="X", duration_seconds=60,
                        frame_partition="PADDING",
                        disposition="REJECTED_PADDING", conflict_flag=0,
                    )
                )

    def test_on_conflict_idempotency_and_recovery(self, pg_engine, book, policy):
        plan = _batch_plan(book, policy, source_id="synthetic|pgtest|idem")
        first = sto.ingest_batch(pg_engine, plan)
        assert first["counts"]["new_versions"] > 0
        # Post-commit "crash": the local caller lost everything; the batch
        # identity alone must recover with zero new rows (H09).
        recovered = sto.ingest_batch(pg_engine, plan)
        assert recovered["counts"]["new_versions"] == 0
        assert recovered["counts"]["recovered"] == 1
        assert recovered["state"] is c.TaskState.INGEST_VERIFIED
        recon = sto.reconcile_batch(pg_engine, plan)
        assert recon["verified"]

    def test_chunk_transaction_rollback_leaves_nothing(self, pg_engine, book, policy):
        plan = _batch_plan(book, policy, rows=6000, padding=100, source_id="synthetic|pgtest|rollback")
        ranges = plan.chunk_ranges()
        assert len(ranges) == 3
        first_range = ranges[0]
        # Sabotage the SECOND chunk so its transaction fails; the first chunk
        # stays committed, and nothing partial from chunk two survives.
        original = sto._ingest_chunk

        def broken_chunk(engine, plan_, chunk_range):
            if chunk_range == ranges[1]:
                raise RuntimeError("simulated crash mid-run")
            return original(engine, plan_, chunk_range)

        sto._ingest_chunk = broken_chunk
        try:
            # Chunk 0 commits standalone (creating the batch row with it);
            # chunks_committed is deliberately left empty to simulate a crash
            # between the chunk commit and the progress update.
            partial_counts = sto._ingest_chunk(pg_engine, plan, first_range)
            sto._update_batch_progress(pg_engine, plan, "committing", [], partial_counts)
            with pytest.raises(RuntimeError):
                sto.ingest_batch(pg_engine, plan)
        finally:
            sto._ingest_chunk = original
        from sqlalchemy import func, select

        with pg_engine.connect() as conn:
            stored = conn.execute(
                select(func.count()).select_from(sto.batch_rows).where(
                    sto.batch_rows.c.batch_id == plan.batch_id
                )
            ).scalar()
        assert stored == first_range[1] - first_range[0]  # only committed chunk
        # Resume finishes cleanly with no duplicates.
        resumed = sto.ingest_batch(pg_engine, plan)
        assert resumed["state"] is c.TaskState.INGEST_VERIFIED
        recon = sto.reconcile_batch(pg_engine, plan)
        assert recon["verified"]

    def test_repeatable_read_export_read_only(self, pg_engine, book, policy):
        plan = _batch_plan(book, policy, source_id="synthetic|pgtest|export")
        sto.ingest_batch(pg_engine, plan)
        rows = sto.export_existing_versions(
            pg_engine, source_symbol="SHFE.rb2601", duration_seconds=60,
            nano_start=0, nano_end=2**62,
        )
        assert rows
        assert all("content_sha256" in row for row in rows)

    def test_schema_qualification_no_search_path_tricks(self, pg_engine):
        from sqlalchemy import text

        with pg_engine.connect() as conn:
            current = conn.execute(text("SHOW search_path")).scalar()
        assert "acquisition_w2" not in current  # explicit qualification only
