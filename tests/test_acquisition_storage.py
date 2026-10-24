"""C3 storage tests on an isolated SQLite engine with the acquisition_w2
schema ATTACHed (SYNTHETIC). Real-PostgreSQL DDL/unique/FK/ON CONFLICT/
rollback semantics are covered separately in test_acquisition_postgres.py
against a disposable instance; nothing here touches any existing database.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from ak_trading.acquisition import contracts as c
from ak_trading.acquisition import storage as sto
from ak_trading.acquisition.calendar import CalendarBook
from ak_trading.acquisition.capture import (
    CaptureRequest,
    _build_meta,
    classify_frame,
)
from ak_trading.acquisition import tq_adapter as tq


def _nano(iso: str) -> int:
    return c.iso_instant_to_nano(iso)


@pytest.fixture()
def engine(tmp_path):
    main_db = tmp_path / "test.db"
    schema_db = tmp_path / "acq.db"
    engine = create_engine(f"sqlite:///{main_db}", echo=False)
    sto.attach_sqlite_schema(engine, str(schema_db).replace("\\", "/"))
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


def _make_batch(
    book,
    policy,
    *,
    symbol="SHFE.rb2601",
    duration=60,
    rows=6,
    padding=2,
    start_iso="2026-09-18T14:00:00+08:00",
    observed_iso="2026-09-18T15:30:00+08:00",
    raw_sha="deadbeef" * 8,
    attempt="a1",
    values_shift=0.0,
):
    frame = tq.make_fake_frame(
        symbol=symbol, duration_seconds=duration,
        start_nano=_nano(start_iso), rows=rows, closed_rows=rows,
        padding_rows=padding, price=4000.0 + values_shift,
    )
    classified, _, _ = classify_frame(
        frame, source_symbol=symbol, duration_seconds=duration,
        product_key="SHFE.rb", calendar_book=book,
        watermark_nano=_nano(observed_iso) - 60 * 10**9, evidence_version="ev1",
    )
    request = CaptureRequest(symbol, duration, "t1", attempt, "SHFE.rb")
    meta = _build_meta(
        request=request, policy=policy, snapshot=frame, rows=classified,
        observed_at_nano=_nano(observed_iso), sdk_version="SYNTHETIC-FAKE",
    )
    meta = dataclasses.replace(meta, raw_sha256=raw_sha)
    plan = sto.build_batch_plan(
        task_logical_key=f"{symbol}|{duration}", task_id="t1", attempt_id=attempt,
        source_id="synthetic|test", meta=meta, rows=classified,
        policy_sha256=policy.sha256, universe_sha256="u", source_sha256="s",
        calendar_sha256="cal", created_at_nano=_nano(observed_iso),
    )
    return plan, classified


class TestSchema:
    def test_only_three_tables_in_schema(self, engine):
        from sqlalchemy import inspect

        inspector = inspect(engine)
        tables = sorted(inspector.get_table_names(schema="acquisition_w2"))
        assert tables == ["bar_versions", "batch_rows", "ingest_batches"]

    def test_bar_versions_is_append_only(self, engine, book, policy):
        plan, _ = _make_batch(book, policy)
        sto.ingest_batch(engine, plan)
        with pytest.raises(sto.StorageError, match="append-only"):
            from sqlalchemy import update

            with engine.begin() as conn:
                conn.execute(
                    update(sto.bar_versions).values(close_s="0.0")
                )


class TestIdempotencyAndVersions:
    def test_same_content_rerun_adds_zero_versions(self, engine, book, policy):
        plan, _ = _make_batch(book, policy)
        first = sto.ingest_batch(engine, plan)
        assert first["counts"]["new_versions"] > 0
        with engine.connect() as conn:
            versions_before = conn.execute(
                select(__import__("sqlalchemy").func.count()).select_from(sto.bar_versions)
            ).scalar()
        second = sto.ingest_batch(engine, plan)
        # Recovered batch identity: this call added nothing (H09).
        assert second["counts"]["new_versions"] == 0
        assert second["counts"]["recovered"] == 1
        assert second["stored_counts"]["new_versions"] == first["counts"]["new_versions"]
        with engine.connect() as conn:
            versions_after = conn.execute(
                select(__import__("sqlalchemy").func.count()).select_from(sto.bar_versions)
            ).scalar()
        assert versions_before == versions_after
        assert second["state"] is c.TaskState.INGEST_VERIFIED

    def test_new_attempt_same_content_same_version_count(self, engine, book, policy):
        plan1, _ = _make_batch(book, policy, attempt="a1", raw_sha="a" * 64)
        plan2, _ = _make_batch(book, policy, attempt="a2", raw_sha="b" * 64)
        first = sto.ingest_batch(engine, plan1)
        second = sto.ingest_batch(engine, plan2)
        assert second["counts"]["new_versions"] == 0
        assert second["counts"]["existing_identical"] == first["counts"]["new_versions"]

    def test_same_key_different_value_appends_version_and_flags_conflict(
        self, engine, book, policy
    ):
        plan1, _ = _make_batch(book, policy, attempt="a1", raw_sha="a" * 64)
        plan2, _ = _make_batch(
            book, policy, attempt="a2", raw_sha="b" * 64, values_shift=1.5
        )
        sto.ingest_batch(engine, plan1)
        second = sto.ingest_batch(engine, plan2)
        # Both closed value sets coexist as versions; conflicts recorded.
        assert second["counts"]["new_versions"] == 6  # every valid key diverged
        assert second["counts"]["conflicts"] > 0
        with engine.connect() as conn:
            rows = conn.execute(
                select(sto.bar_versions.c.datetime_nano,
                       sto.bar_versions.c.content_sha256)
                .where(sto.bar_versions.c.source_symbol == "SHFE.rb2601")
            ).all()
        keys = {}
        for nano, digest in rows:
            keys.setdefault(nano, set()).add(digest)
        multi = {k: v for k, v in keys.items() if len(v) > 1}
        assert multi, "both value versions must be retained"

    def test_provisional_to_closed_same_values_is_status_evolution(
        self, engine, book, policy
    ):
        # Capture mid-session: bars past the watermark are PROVISIONAL.
        plan1, rows1 = _make_batch(
            book, policy, attempt="a1", raw_sha="a" * 64,
            observed_iso="2026-09-18T14:03:30+08:00",
        )
        assert any(
            r.valid_status is c.ValidRowStatus.PROVISIONAL
            for r in rows1
            if r.frame_partition is c.FramePartition.VALID
        )
        sto.ingest_batch(engine, plan1)
        # Re-capture after close: same values, now CLOSED -> new versions by
        # status evolution (dev-plan section 2), not a conflict.
        plan2, rows2 = _make_batch(
            book, policy, attempt="a2", raw_sha="b" * 64,
            observed_iso="2026-09-18T15:30:00+08:00",
        )
        assert all(
            r.valid_status is c.ValidRowStatus.CLOSED
            for r in rows2
            if r.frame_partition is c.FramePartition.VALID
        )
        second = sto.ingest_batch(engine, plan2)
        assert second["counts"]["new_versions"] > 0
        assert second["counts"]["conflicts"] == 0


def _valid_rows_of(plan):
    return sum(1 for r in plan.rows if r.frame_partition is c.FramePartition.VALID)


class TestChunkingAndRecovery:
    def test_large_snapshot_chunks_and_recovers(self, engine, book, policy):
        # 6100 frame rows (6000 bars + 100 padding) -> chunks of 2500
        # (H10: <=5000 inserted rows per transaction).
        plan, _ = _make_batch(book, policy, rows=6000, padding=100)
        ranges = plan.chunk_ranges()
        assert [r[1] - r[0] for r in ranges] == [2500, 2500, 1100]
        result = sto.ingest_batch(engine, plan)
        assert result["state"] is c.TaskState.INGEST_VERIFIED
        committed = sto.find_batch(engine, plan.batch_identity_sha256)
        chunks = json.loads(committed["chunks_committed"])
        assert [tuple(ch) for ch in ranges] and len(chunks) == 3

    def test_crash_between_chunks_resumes_without_duplicates(
        self, engine, book, policy
    ):
        plan, _ = _make_batch(book, policy, rows=3000, padding=50)
        # Simulate a crash after the first chunk committed but BEFORE its
        # progress update landed: chunk 0 ran standalone (it creates the
        # batch row in its own transaction), chunks_committed stays empty.
        first_range = plan.chunk_ranges()[0]
        partial_counts = sto._ingest_chunk(engine, plan, first_range)
        sto._update_batch_progress(
            engine, plan, "committing", [], partial_counts
        )
        resumed = sto.ingest_batch(engine, plan)
        assert resumed["state"] is c.TaskState.INGEST_VERIFIED
        with engine.connect() as conn:
            from sqlalchemy import func

            total_rows = conn.execute(
                select(func.count()).select_from(
                    sto.batch_rows
                ).where(sto.batch_rows.c.batch_id == plan.batch_id)
            ).scalar()
        assert total_rows == len(plan.rows)  # no duplicated ordinals
        recon = sto.reconcile_batch(engine, plan)
        assert recon["verified"]


class TestBidirectionalReconciliation:
    def test_reconciliation_passes_on_clean_ingest(self, engine, book, policy):
        plan, _ = _make_batch(book, policy)
        sto.ingest_batch(engine, plan)
        report = sto.reconcile_batch(engine, plan)
        assert report["verified"]
        assert report["source_to_db_missing"] == []
        assert report["db_to_source_extra"] == []

    def test_reconciliation_detects_missing_and_extra(self, engine, book, policy):
        plan, _ = _make_batch(book, policy)
        sto.ingest_batch(engine, plan)
        # Plant an extra batch_row the source never had.
        with engine.begin() as conn:
            from sqlalchemy import insert

            conn.execute(
                insert(sto.batch_rows).values(
                    batch_id=plan.batch_id, raw_row_ordinal=999,
                    source_symbol="SHFE.rb2601", duration_seconds=60,
                    datetime_nano=1, frame_partition="PADDING",
                    disposition="REJECTED_PADDING", conflict_flag=0,
                )
            )
        report = sto.reconcile_batch(engine, plan)
        assert not report["verified"]
        assert 999 in report["db_to_source_extra"]

    def test_reconciliation_detects_content_tamper(self, engine, book, policy):
        plan, _ = _make_batch(book, policy)
        sto.ingest_batch(engine, plan)
        # Tamper a stored version's value WITHOUT touching its hash: the
        # recomputed canonical hash must expose the divergence.
        with pytest.raises(sto.StorageError):
            from sqlalchemy import update

            with engine.begin() as conn:
                conn.execute(
                    update(sto.bar_versions).values(close_s="12345.0")
                )
        # Direct SQL tamper (bypassing the guard) is what reconciliation sees.
        raw_conn = engine.raw_connection()
        cursor = raw_conn.cursor()
        cursor.execute(
            "UPDATE acquisition_w2.bar_versions SET close_s = '12345.0' "
            "WHERE rowid = (SELECT MIN(rowid) FROM acquisition_w2.bar_versions)"
        )
        raw_conn.commit()
        raw_conn.close()
        report = sto.reconcile_batch(engine, plan)
        assert not report["verified"]
        assert report["content_mismatch"]


class TestReuseExport:
    def test_export_existing_versions_bounded_keyset(self, engine, book, policy):
        plan, _ = _make_batch(book, policy)
        sto.ingest_batch(engine, plan)
        rows = sto.export_existing_versions(
            engine, source_symbol="SHFE.rb2601", duration_seconds=60,
            nano_start=0, nano_end=2**62, page_size=3,
        )
        assert rows, "expected exported versions for the ingested symbol"
        nanos = [r["datetime_nano"] for r in rows]
        assert nanos == sorted(nanos)
