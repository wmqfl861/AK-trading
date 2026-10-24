"""C3 isolated PostgreSQL storage: acquisition_w2 three-table contract (H08/H09).

Boundaries (decision.md H08, dev-plan section 5):

- Writes only the new, explicitly qualified schema ``acquisition_w2`` with
  tables ``bar_versions`` / ``ingest_batches`` / ``batch_rows``. DDL is
  limited to this schema and these three tables. The legacy
  ``import_market_bars`` / ``store_reference_snapshot`` paths and every
  existing ``market_bars`` / ingestion / reference table are NOT touched by
  this module (no import, no statements).
- ``bar_versions`` is append-only: no UPDATE/DELETE statement is ever emitted
  against it (a test guard installs a listener proving this). Version
  identity = (source, authority key, content hash, classification and
  evidence version); identical re-capture inserts nothing, value or status
  evolution appends a new version, and same-key cross-content CLOSED rows
  are flagged as conflicts that block completeness.
- Chunks of at most 2500 frame rows (<= 5000 inserted rows per transaction)
  commit independently; each chunk records its committed ordinal range in
  ``ingest_batches.chunks_committed`` inside the same transaction, making
  every interruption point recoverable from the immutable batch identity.
- ``INGEST_VERIFIED`` requires all chunks present AND a bidirectional
  readback reconciling keys, canonical content hashes and classifications
  (not just row counts).
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    event,
    insert,
    select,
)
from sqlalchemy.engine import Engine

from .contracts import (
    ContractError,
    FramePartition,
    RawSnapshotMeta,
    ValidRowStatus,
    canonical_bar_content,
    sha256_bytes,
)
from .contracts import ClassifiedRow, TaskState

SCHEMA_NAME = "acquisition_w2"
CHUNK_FRAME_ROWS = 2500  # <=5000 inserted rows per transaction (H10)

BAR_VERSIONS_IMMUTABLE = True

metadata = MetaData(schema=SCHEMA_NAME)

_VERSION_IDENTITY_COLUMNS = (
    "source_id",
    "source_symbol",
    "duration_seconds",
    "datetime_nano",
    "content_sha256",
    "row_status",
    "evidence_version",
)

bar_versions = Table(
    "bar_versions",
    metadata,
    Column("version_id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("source_id", String(128), nullable=False),
    Column("source_symbol", String(64), nullable=False),
    Column("duration_seconds", Integer, nullable=False),
    Column("datetime_nano", BigInteger, nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("row_status", String(16), nullable=False),
    Column("evidence_version", String(128), nullable=False),
    Column("bar_start_nano", BigInteger, nullable=False),
    Column("bar_end_nano", BigInteger, nullable=False),
    Column("trading_day", String(10), nullable=True),
    Column("open_s", String(64), nullable=False),
    Column("high_s", String(64), nullable=False),
    Column("low_s", String(64), nullable=False),
    Column("close_s", String(64), nullable=False),
    Column("volume_s", String(64), nullable=False),
    Column("open_oi_s", String(64), nullable=False),
    Column("close_oi_s", String(64), nullable=False),
    Column("observed_at_nano", BigInteger, nullable=False),
    Column("imported_at_nano", BigInteger, nullable=False),
    UniqueConstraint(
        *_VERSION_IDENTITY_COLUMNS,
        name="uq_bar_version_identity",
    ),
)

ingest_batches = Table(
    "ingest_batches",
    metadata,
    Column("batch_id", String(64), primary_key=True),
    Column("batch_identity_sha256", String(64), nullable=False),
    Column("logical_task_key", String(128), nullable=False),
    Column("task_id", String(128), nullable=False),
    Column("attempt_id", String(128), nullable=False),
    Column("source_symbol", String(64), nullable=False),
    Column("duration_seconds", Integer, nullable=False),
    Column("raw_sha256", String(64), nullable=False),
    Column("policy_sha256", String(64), nullable=False),
    Column("universe_sha256", String(64), nullable=False),
    Column("source_sha256", String(64), nullable=False),
    Column("calendar_sha256", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("chunks_committed", String(4096), nullable=False),
    Column("counts_json", String(4096), nullable=False),
    Column("created_at_nano", BigInteger, nullable=False),
    Column("committed_at_nano", BigInteger, nullable=True),
    UniqueConstraint("batch_identity_sha256", name="uq_ingest_batch_identity"),
)

batch_rows = Table(
    "batch_rows",
    metadata,
    Column("batch_id", String(64), ForeignKey(f"{SCHEMA_NAME}.ingest_batches.batch_id"), nullable=False),
    Column("raw_row_ordinal", Integer, nullable=False),
    Column("source_symbol", String(64), nullable=False),
    Column("duration_seconds", Integer, nullable=False),
    Column("datetime_nano", BigInteger, nullable=True),
    Column("frame_partition", String(8), nullable=False),
    Column("valid_status", String(12), nullable=True),
    Column("content_sha256", String(64), nullable=True),
    Column("version_id", BigInteger, ForeignKey(f"{SCHEMA_NAME}.bar_versions.version_id"), nullable=True),
    Column("disposition", String(24), nullable=False),
    Column("conflict_flag", Integer, nullable=False),
    Column("reason", String(256), nullable=True),
    UniqueConstraint("batch_id", "raw_row_ordinal", name="uq_batch_row_ordinal"),
)

Index(
    "ix_bar_versions_key",
    bar_versions.c.source_id,
    bar_versions.c.source_symbol,
    bar_versions.c.duration_seconds,
    bar_versions.c.datetime_nano,
)


class StorageError(RuntimeError):
    """Sanitized storage contract violation."""


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------

def ensure_schema_and_tables(engine: Engine) -> None:
    """CREATE SCHEMA IF NOT EXISTS + the three tables only (H08 DDL bounds)."""
    if engine.dialect.name == "postgresql":
        from sqlalchemy import text

        with engine.begin() as connection:
            connection.execute(
                text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA_NAME}"')
            )
    metadata.create_all(engine)


def install_immutability_guard(engine: Engine) -> list[str]:
    """Test/admin guard: any UPDATE/DELETE against bar_versions raises."""
    violations: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _guard(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        head = statement.lstrip()[:64].upper() if isinstance(statement, str) else ""
        if head.startswith("UPDATE") or head.startswith("DELETE"):
            if "bar_versions" in statement:
                violations.append(statement[:120])
                raise StorageError(
                    "bar_versions is append-only: UPDATE/DELETE refused (H08)"
                )

    return violations


def attach_sqlite_schema(engine: Engine, schema_file: str) -> None:
    """SQLite test helper: ATTACH a file db as the acquisition_w2 schema."""
    from sqlalchemy import text

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, connection_record):  # noqa: ANN001
        dbapi_connection.execute(
            f"ATTACH DATABASE '{schema_file}' AS {SCHEMA_NAME}"
        )


# ---------------------------------------------------------------------------
# Batch identity and chunk planning
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class IngestBatchPlan:
    batch_id: str
    batch_identity_sha256: str
    logical_task_key: str
    task_id: str
    attempt_id: str
    source_id: str
    source_symbol: str
    duration_seconds: int
    raw_sha256: str
    policy_sha256: str
    universe_sha256: str
    source_sha256: str
    calendar_sha256: str
    rows: tuple[ClassifiedRow, ...]
    observed_at_nano: int
    created_at_nano: int

    def chunk_ranges(self) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        total = len(self.rows)
        index = 0
        while index < total:
            end = min(index + CHUNK_FRAME_ROWS, total)
            ranges.append((index, end))
            index = end
        return ranges or [(0, 0)]

    def chunk_id(self, chunk_range: tuple[int, int]) -> str:
        payload = (
            self.batch_identity_sha256
            + f":{chunk_range[0]}:{chunk_range[1]}"
        )
        return sha256_bytes(payload.encode("utf-8"))


def build_batch_plan(
    *,
    task_logical_key: str,
    task_id: str,
    attempt_id: str,
    source_id: str,
    meta: RawSnapshotMeta,
    rows: Sequence[ClassifiedRow],
    policy_sha256: str,
    universe_sha256: str,
    source_sha256: str,
    calendar_sha256: str,
    created_at_nano: int,
) -> IngestBatchPlan:
    if not meta.raw_sha256:
        raise StorageError("raw snapshot must be persisted and hashed before ingest")
    identity_payload = {
        "attempt_id": attempt_id,
        "calendar_sha256": calendar_sha256,
        "chunk_rows": CHUNK_FRAME_ROWS,
        "duration_seconds": meta.duration_seconds,
        "logical_task_key": task_logical_key,
        "policy_sha256": policy_sha256,
        "raw_sha256": meta.raw_sha256,
        "row_count": len(rows),
        "source_id": source_id,
        "source_sha256": source_sha256,
        "source_symbol": meta.source_symbol,
        "task_id": task_id,
        "universe_sha256": universe_sha256,
    }
    from .contracts import canonical_sha256

    identity = canonical_sha256(identity_payload)
    return IngestBatchPlan(
        batch_id=identity[:32],
        batch_identity_sha256=identity,
        logical_task_key=task_logical_key,
        task_id=task_id,
        attempt_id=attempt_id,
        source_id=source_id,
        source_symbol=meta.source_symbol,
        duration_seconds=meta.duration_seconds,
        raw_sha256=meta.raw_sha256,
        policy_sha256=policy_sha256,
        universe_sha256=universe_sha256,
        source_sha256=source_sha256,
        calendar_sha256=calendar_sha256,
        rows=tuple(rows),
        observed_at_nano=meta.observed_at_nano,
        created_at_nano=created_at_nano,
    )


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _dialect_insert(engine: Engine, table: Table):
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        return pg_insert(table)
    if engine.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert(table)
    return insert(table)


EMPTY_COUNTS: dict[str, int] = {
    "attempted": 0,
    "new_versions": 0,
    "existing_identical": 0,
    "conflicts": 0,
    "rejected_padding": 0,
    "rejected_invalid": 0,
    "provisional": 0,
    "unknown": 0,
    "closed": 0,
}


def find_batch(engine: Engine, batch_identity_sha256: str) -> Optional[dict[str, Any]]:
    statement = select(ingest_batches).where(
        ingest_batches.c.batch_identity_sha256 == batch_identity_sha256
    )
    with engine.connect() as connection:
        row = connection.execute(statement).mappings().first()
    return dict(row) if row else None


def _decode_chunks(raw: Any) -> list[list[int]]:
    import json

    return [list(item) for item in json.loads(raw or "[]")]


def ingest_batch(
    engine: Engine,
    plan: IngestBatchPlan,
) -> dict[str, Any]:
    """Idempotent chunked ingest. Returns per-disposition counters and state.

    Recovery semantics (H09): a batch whose identity already exists resumes
    from ``chunks_committed``; completed chunks are never rewritten, missing
    chunks are re-ingested (row-level ON CONFLICT dedup makes this safe), and
    a fully committed batch returns zero new versions.
    """
    existing = find_batch(engine, plan.batch_identity_sha256)
    if existing is not None and existing["status"] == "committed":
        committed = _decode_chunks(existing["chunks_committed"])
        if _covers_all(committed, plan.chunk_ranges()):
            # Fully committed batch identity re-presented: nothing re-ingests.
            # Report zero NEW additions for this call; the originally stored
            # totals ride along separately for audit (H09 idempotency).
            return {
                "state": TaskState.INGEST_VERIFIED,
                "counts": {**EMPTY_COUNTS, "recovered": 1},
                "stored_counts": _json_loads(existing["counts_json"]),
            }

    counts = dict(EMPTY_COUNTS)
    counts["attempted"] = len(plan.rows)
    if existing is not None:
        counts = {**EMPTY_COUNTS, **_json_loads(existing["counts_json"])}

    ranges = plan.chunk_ranges()
    done: list[list[int]] = (
        _decode_chunks(existing["chunks_committed"]) if existing is not None else []
    )
    status = "committing"

    for chunk_range in ranges:
        if _covered(done, chunk_range):
            continue
        # The chunk covering ordinal 0 also creates the batch row inside the
        # SAME transaction (H09: three tables in one bounded transaction; a
        # committed chunk always implies its batch row exists).
        chunk_counts = _ingest_chunk(engine, plan, chunk_range)
        for key, value in chunk_counts.items():
            counts[key] = counts.get(key, 0) + value
        done.append([chunk_range[0], chunk_range[1]])
        status = "committed" if _covers_all(done, ranges) else "committing"
        _update_batch_progress(engine, plan, status, done, counts)

    return {
        "state": (
            TaskState.INGEST_VERIFIED if status == "committed" else TaskState.RUNNING
        ),
        "counts": counts,
    }


def _update_batch_progress(
    engine: Engine,
    plan: IngestBatchPlan,
    status: str,
    done: Sequence[Sequence[int]],
    counts: Mapping[str, int],
) -> None:
    """Progress updates touch only ingest_batches (bar_versions stays frozen)."""
    from sqlalchemy import update

    committed_at = plan.created_at_nano if status == "committed" else None
    statement = (
        update(ingest_batches)
        .where(ingest_batches.c.batch_identity_sha256 == plan.batch_identity_sha256)
        .values(
            status=status,
            chunks_committed=_json_dumps([list(item) for item in done]),
            counts_json=_json_dumps(dict(counts)),
            committed_at_nano=committed_at,
        )
    )
    with engine.begin() as connection:
        connection.execute(statement)


def _ingest_chunk(
    engine: Engine, plan: IngestBatchPlan, chunk_range: tuple[int, int]
) -> dict[str, int]:
    start, end = chunk_range
    rows = plan.rows[start:end]
    counts = {key: 0 for key in EMPTY_COUNTS}
    imported_at = plan.created_at_nano

    with engine.begin() as connection:
        if start == 0:
            # First chunk carries the batch row in the same transaction so a
            # committed chunk always implies its parent batch (FK invariant).
            connection.execute(
                _dialect_insert(engine, ingest_batches)
                .values(
                    batch_id=plan.batch_id,
                    batch_identity_sha256=plan.batch_identity_sha256,
                    logical_task_key=plan.logical_task_key,
                    task_id=plan.task_id,
                    attempt_id=plan.attempt_id,
                    source_symbol=plan.source_symbol,
                    duration_seconds=plan.duration_seconds,
                    raw_sha256=plan.raw_sha256,
                    policy_sha256=plan.policy_sha256,
                    universe_sha256=plan.universe_sha256,
                    source_sha256=plan.source_sha256,
                    calendar_sha256=plan.calendar_sha256,
                    status="committing",
                    chunks_committed=_json_dumps([]),
                    counts_json=_json_dumps(dict(EMPTY_COUNTS)),
                    created_at_nano=plan.created_at_nano,
                    committed_at_nano=None,
                )
                .on_conflict_do_nothing(index_elements=["batch_identity_sha256"])
            )
        # Idempotent re-run guard: rows already stored for this chunk are
        # counted from their stored dispositions instead of being rewritten
        # (crash between chunk commit and progress update undercounts nothing).
        stored_rows = {
            int(row["raw_row_ordinal"]): dict(row)
            for row in connection.execute(
                select(batch_rows).where(
                    batch_rows.c.batch_id == plan.batch_id,
                    batch_rows.c.raw_row_ordinal >= start,
                    batch_rows.c.raw_row_ordinal < end,
                )
            ).mappings()
        }
        for stored in stored_rows.values():
            _count_stored_row(counts, stored)
        todo = [row for row in rows if row.raw_row_ordinal not in stored_rows]

        # Preselect existing versions for the keys in this chunk.
        keys = [
            (row.datetime_nano, row.source_symbol, row.duration_seconds)
            for row in todo
            if row.frame_partition is FramePartition.VALID
        ]
        existing_versions: dict[tuple[int, str, int], list[dict[str, Any]]] = {}
        if keys:
            nanos = {key[0] for key in keys}
            found = connection.execute(
                select(bar_versions).where(
                    bar_versions.c.source_id == plan.source_id,
                    bar_versions.c.source_symbol == plan.source_symbol,
                    bar_versions.c.duration_seconds == plan.duration_seconds,
                    bar_versions.c.datetime_nano.in_(sorted(nanos)),
                )
            ).mappings()
            for row in found:
                entry = existing_versions.setdefault(
                    (row["datetime_nano"], row["source_symbol"], row["duration_seconds"]),
                    [],
                )
                entry.append(dict(row))

        row_payloads: list[dict[str, Any]] = []
        for row in todo:
            if row.frame_partition is FramePartition.PADDING:
                counts["rejected_padding"] += 1
                row_payloads.append(
                    _batch_row_payload(plan, row, version_id=None, disposition="REJECTED_PADDING", conflict=0)
                )
                continue
            if row.frame_partition is FramePartition.INVALID:
                counts["rejected_invalid"] += 1
                row_payloads.append(
                    _batch_row_payload(plan, row, version_id=None, disposition="REJECTED_INVALID", conflict=0)
                )
                continue
            # VALID row
            if row.valid_status is ValidRowStatus.PROVISIONAL:
                counts["provisional"] += 1
            elif row.valid_status is ValidRowStatus.UNKNOWN:
                counts["unknown"] += 1
            else:
                counts["closed"] += 1

            key = (row.datetime_nano, row.source_symbol, row.duration_seconds)
            assert row.content_sha256 is not None and row.valid_status is not None
            identical = None
            conflict = 0
            for candidate in existing_versions.get(key, []):
                if (
                    candidate["content_sha256"] == row.content_sha256
                    and candidate["row_status"] == row.valid_status.value
                    and candidate["evidence_version"] == row.evidence_version
                ):
                    identical = candidate
                elif (
                    candidate["row_status"] == ValidRowStatus.CLOSED.value
                    and row.valid_status is ValidRowStatus.CLOSED
                    and candidate["content_sha256"] != row.content_sha256
                ):
                    conflict = 1
            if identical is not None:
                counts["existing_identical"] += 1
                row_payloads.append(
                    _batch_row_payload(
                        plan,
                        row,
                        version_id=identical["version_id"],
                        disposition="EXISTING_IDENTICAL",
                        conflict=conflict,
                    )
                )
                continue

            version_id = _insert_version(
                connection,
                engine,
                plan,
                row,
                imported_at,
            )
            counts["new_versions"] += 1
            counts["conflicts"] += conflict
            entry = {
                "content_sha256": row.content_sha256,
                "evidence_version": row.evidence_version,
                "row_status": row.valid_status.value,
                "version_id": version_id,
            }
            existing_versions.setdefault(key, []).append(entry)
            row_payloads.append(
                _batch_row_payload(
                    plan, row, version_id=version_id, disposition="NEW_VERSION", conflict=conflict
                )
            )

        if row_payloads:
            connection.execute(
                _dialect_insert(engine, batch_rows)
                .values(row_payloads)
                .on_conflict_do_nothing(index_elements=["batch_id", "raw_row_ordinal"])
            )
    return counts


def _insert_version(
    connection: Any,
    engine: Engine,
    plan: IngestBatchPlan,
    row: ClassifiedRow,
    imported_at_nano: int,
) -> int:
    assert row.datetime_nano is not None and row.valid_status is not None
    values = {
        "source_id": plan.source_id,
        "source_symbol": row.source_symbol,
        "duration_seconds": row.duration_seconds,
        "datetime_nano": row.datetime_nano,
        "content_sha256": row.content_sha256,
        "row_status": row.valid_status.value,
        "evidence_version": row.evidence_version,
        "bar_start_nano": row.bar_start_nano,
        "bar_end_nano": row.bar_end_nano,
        "trading_day": row.trading_day,
        "open_s": row.values["open"],
        "high_s": row.values["high"],
        "low_s": row.values["low"],
        "close_s": row.values["close"],
        "volume_s": row.values["volume"],
        "open_oi_s": row.values["open_oi"],
        "close_oi_s": row.values["close_oi"],
        "observed_at_nano": plan.observed_at_nano,
        "imported_at_nano": imported_at_nano,
    }
    statement = (
        _dialect_insert(engine, bar_versions)
        .values(**values)
        .on_conflict_do_nothing(index_elements=list(_VERSION_IDENTITY_COLUMNS))
        .returning(bar_versions.c.version_id)
    )
    result = connection.execute(statement).scalar_one_or_none()
    if result is not None:
        return int(result)
    # Lost an insert race (same identity): read back the winner.
    assert row.valid_status is not None
    found = connection.execute(
        select(bar_versions.c.version_id).where(
            bar_versions.c.source_id == plan.source_id,
            bar_versions.c.source_symbol == row.source_symbol,
            bar_versions.c.duration_seconds == row.duration_seconds,
            bar_versions.c.datetime_nano == row.datetime_nano,
            bar_versions.c.content_sha256 == row.content_sha256,
            bar_versions.c.row_status == row.valid_status.value,
            bar_versions.c.evidence_version == row.evidence_version,
        )
    ).scalar_one()
    return int(found)


def _count_stored_row(counts: dict[str, int], stored: Mapping[str, Any]) -> None:
    """Fold an already-committed batch_row into chunk counters on re-run."""
    disposition = stored.get("disposition")
    if disposition == "REJECTED_PADDING":
        counts["rejected_padding"] += 1
    elif disposition == "REJECTED_INVALID":
        counts["rejected_invalid"] += 1
    elif disposition == "EXISTING_IDENTICAL":
        counts["existing_identical"] += 1
    elif disposition == "NEW_VERSION":
        counts["new_versions"] += 1
    status = stored.get("valid_status")
    if status == "CLOSED":
        counts["closed"] += 1
    elif status == "PROVISIONAL":
        counts["provisional"] += 1
    elif status == "UNKNOWN":
        counts["unknown"] += 1
    counts["conflicts"] += int(stored.get("conflict_flag") or 0)


def _batch_row_payload(
    plan: IngestBatchPlan,
    row: ClassifiedRow,
    *,
    version_id: Optional[int],
    disposition: str,
    conflict: int,
) -> dict[str, Any]:
    return {
        "batch_id": plan.batch_id,
        "raw_row_ordinal": row.raw_row_ordinal,
        "source_symbol": row.source_symbol,
        "duration_seconds": row.duration_seconds,
        "datetime_nano": row.datetime_nano,
        "frame_partition": row.frame_partition.value,
        "valid_status": row.valid_status.value if row.valid_status else None,
        "content_sha256": row.content_sha256,
        "version_id": version_id,
        "disposition": disposition,
        "conflict_flag": conflict,
        "reason": row.reason,
    }


def _covered(done: Sequence[Sequence[int]], chunk: tuple[int, int]) -> bool:
    return any(int(s) == chunk[0] and int(e) == chunk[1] for s, e in done)


def _covers_all(done: Sequence[Sequence[int]], ranges: Sequence[tuple[int, int]]) -> bool:
    return all(_covered(done, item) for item in ranges)


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str) -> dict[str, Any]:
    import json

    return dict(json.loads(value or "{}"))


# ---------------------------------------------------------------------------
# Bidirectional reconciliation (H09)
# ---------------------------------------------------------------------------

def reconcile_batch(
    engine: Engine,
    plan: IngestBatchPlan,
) -> dict[str, Any]:
    """Key + content-hash + classification reconciliation, both directions.

    Source->DB: every frame ordinal must appear as a batch_row with the same
    partition/status/content hash. DB->source: no extra rows. Stored versions
    are re-hashed from their columns to catch pseudo-equal content (H08/H09
    counterexample: equal totals, different keys/values).
    """
    report: dict[str, Any] = {
        "batch_id": plan.batch_id,
        "source_to_db_missing": [],
        "db_to_source_extra": [],
        "content_mismatch": [],
        "classification_mismatch": [],
        "verified": False,
    }
    with engine.connect() as connection:
        stored_rows = {
            int(row["raw_row_ordinal"]): dict(row)
            for row in connection.execute(
                select(batch_rows).where(batch_rows.c.batch_id == plan.batch_id)
            ).mappings()
        }
        version_ids = {
            int(row["version_id"]): dict(row)
            for row in connection.execute(
                select(bar_versions).where(
                    bar_versions.c.source_id == plan.source_id,
                    bar_versions.c.source_symbol == plan.source_symbol,
                    bar_versions.c.duration_seconds == plan.duration_seconds,
                )
            ).mappings()
        }

    for row in plan.rows:
        stored = stored_rows.get(row.raw_row_ordinal)
        if stored is None:
            report["source_to_db_missing"].append(row.raw_row_ordinal)
            continue
        if stored["frame_partition"] != row.frame_partition.value:
            report["classification_mismatch"].append(
                {
                    "ordinal": row.raw_row_ordinal,
                    "expected": row.frame_partition.value,
                    "stored": stored["frame_partition"],
                }
            )
        if row.frame_partition is FramePartition.VALID:
            if stored["valid_status"] != (row.valid_status.value if row.valid_status else None):
                report["classification_mismatch"].append(
                    {
                        "ordinal": row.raw_row_ordinal,
                        "expected_status": row.valid_status.value if row.valid_status else None,
                        "stored_status": stored["valid_status"],
                    }
                )
            version_id = stored["version_id"]
            version = version_ids.get(int(version_id)) if version_id is not None else None
            if version is None:
                report["content_mismatch"].append(
                    {"ordinal": row.raw_row_ordinal, "reason": "missing_version"}
                )
                continue
            recomputed = canonical_bar_content(
                duration_seconds=version["duration_seconds"],
                datetime_nano=int(version["datetime_nano"]),
                bar_start_nano=int(version["bar_start_nano"]),
                bar_end_nano=int(version["bar_end_nano"]),
                trading_day=version["trading_day"],
                values={
                    "open": version["open_s"],
                    "high": version["high_s"],
                    "low": version["low_s"],
                    "close": version["close_s"],
                    "volume": version["volume_s"],
                    "open_oi": version["open_oi_s"],
                    "close_oi": version["close_oi_s"],
                },
            )
            if recomputed != version["content_sha256"] or version["content_sha256"] != row.content_sha256:
                report["content_mismatch"].append(
                    {"ordinal": row.raw_row_ordinal, "reason": "hash_divergence"}
                )

    source_ordinals = {row.raw_row_ordinal for row in plan.rows}
    for ordinal in sorted(stored_rows):
        if ordinal not in source_ordinals:
            report["db_to_source_extra"].append(ordinal)

    report["verified"] = not any(
        report[key]
        for key in (
            "source_to_db_missing",
            "db_to_source_extra",
            "content_mismatch",
            "classification_mismatch",
        )
    )
    return report


# ---------------------------------------------------------------------------
# Reuse exports (H03/H08 bounded read-only)
# ---------------------------------------------------------------------------

def export_existing_versions(
    engine: Engine,
    *,
    source_symbol: str,
    duration_seconds: int,
    nano_start: int,
    nano_end: int,
    page_size: int = 5000,
) -> list[dict[str, Any]]:
    """Keyset-paginated read-only export under REPEATABLE READ (H08)."""
    from sqlalchemy import and_, or_, text

    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
        raise StorageError("page_size must be a positive integer")
    if nano_start > nano_end:
        raise StorageError("nano_start must not exceed nano_end")
    if nano_start == nano_end:
        return []

    results: list[dict[str, Any]] = []
    connection_context = engine.connect()
    if engine.dialect.name == "postgresql":
        connection_context = connection_context.execution_options(
            isolation_level="REPEATABLE READ"
        )
    with connection_context as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("SET TRANSACTION READ ONLY"))
        cursor: Optional[tuple[int, int]] = None
        while True:
            page = (
                select(
                    bar_versions.c.datetime_nano,
                    bar_versions.c.content_sha256,
                    bar_versions.c.row_status,
                    bar_versions.c.evidence_version,
                    bar_versions.c.version_id,
                )
                .where(
                    bar_versions.c.source_symbol == source_symbol,
                    bar_versions.c.duration_seconds == duration_seconds,
                    bar_versions.c.datetime_nano >= nano_start,
                    bar_versions.c.datetime_nano < nano_end,
                )
                .order_by(bar_versions.c.datetime_nano, bar_versions.c.version_id)
                .limit(page_size)
            )
            if cursor is not None:
                page = page.where(
                    or_(
                        bar_versions.c.datetime_nano > cursor[0],
                        and_(
                            bar_versions.c.datetime_nano == cursor[0],
                            bar_versions.c.version_id > cursor[1],
                        ),
                    )
                )
            rows = [dict(row) for row in connection.execute(page).mappings()]
            if not rows:
                break
            results.extend(rows)
            if len(rows) < page_size:
                break
            cursor = (int(rows[-1]["datetime_nano"]), int(rows[-1]["version_id"]))
    return results


# ---------------------------------------------------------------------------
# Project engine factory (H08: reuse create_database_engine, no dotenv)
# ---------------------------------------------------------------------------

def discover_project_root():
    """Locate the repository root containing src/ak_trading (read-only walk)."""
    from pathlib import Path

    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "src" / "ak_trading" / "persistence" / "database.py").exists():
            return parent
    raise StorageError("project root with src/ak_trading not found")


def build_project_engine(url: str):
    """Build an engine via the project factory with a directly constructed
    DatabaseSettings (never load_database_settings / dotenv, H01/H08)."""
    import sys

    root = discover_project_root()
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from ak_trading.persistence.config import DatabaseSettings
    from ak_trading.persistence.database import create_database_engine

    settings = DatabaseSettings(
        url=url,
        connect_timeout=5,
        statement_timeout_ms=30000,
    )
    return create_database_engine(settings)


def safe_database_location(url: str) -> str:
    """Non-sensitive identity of a DB target (host/port/dbname only)."""
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or 5432
    path = parsed.path.strip("/")
    return f"{host}:{port}/{path}"


__all__ = [
    "BAR_VERSIONS_IMMUTABLE",
    "CHUNK_FRAME_ROWS",
    "SCHEMA_NAME",
    "StorageError",
    "IngestBatchPlan",
    "attach_sqlite_schema",
    "bar_versions",
    "batch_rows",
    "build_batch_plan",
    "build_project_engine",
    "discover_project_root",
    "ensure_schema_and_tables",
    "export_existing_versions",
    "find_batch",
    "ingest_batches",
    "ingest_batch",
    "install_immutability_guard",
    "metadata",
    "reconcile_batch",
    "safe_database_location",
]
