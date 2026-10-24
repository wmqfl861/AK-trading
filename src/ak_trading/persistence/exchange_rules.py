"""Optional PostgreSQL persistence for local exchange-rule artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from ak_trading.exchange_rule_artifacts import (
    ApprovalRecord,
    ExchangeRuleArtifactStore,
)
from ak_trading.exchange_rules import RuleItem, RuleSnapshot, RuleStatus
from ak_trading.persistence.storage import (
    PersistenceConflictError,
    PersistenceError,
    _safe_failure,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine


class ExchangeRulePersistenceError(PersistenceError):
    """Raised when an exchange-rule artifact cannot be safely persisted."""


@dataclass(frozen=True)
class ExchangeRuleImportResult:
    """Counts from one explicit local-artifact database import."""

    source_contents_inserted: int
    source_documents_inserted: int
    snapshots_inserted: int
    items_inserted: int
    runs_inserted: int
    approvals_inserted: int
    current_updated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_contents_inserted": self.source_contents_inserted,
            "source_documents_inserted": self.source_documents_inserted,
            "snapshots_inserted": self.snapshots_inserted,
            "items_inserted": self.items_inserted,
            "runs_inserted": self.runs_inserted,
            "approvals_inserted": self.approvals_inserted,
            "current_updated": self.current_updated,
        }


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExchangeRulePersistenceError("exchange-rule payload is not JSON-safe") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ExchangeRulePersistenceError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ExchangeRulePersistenceError(f"{field} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ExchangeRulePersistenceError(f"{field} must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise ExchangeRulePersistenceError(f"{field} must include a timezone offset")
    return parsed


def _iso_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise ExchangeRulePersistenceError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ExchangeRulePersistenceError(f"{field} must be YYYY-MM-DD") from exc


def _item_key(item: RuleItem) -> str:
    return _sha256(
        _canonical_json_bytes(
            {
                "schema_version": 1,
                "identity": {
                    "exchange": item.identity_key[0],
                    "product": item.identity_key[1],
                    "contract": item.identity_key[2],
                    "category": item.identity_key[3],
                    "field": item.identity_key[4],
                },
            }
        )
    )


def _safe_artifact_path(root: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ExchangeRulePersistenceError("source document content_path must be relative")
    root_resolved = root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ExchangeRulePersistenceError(
            "source document content_path escapes the artifact root"
        ) from exc
    return resolved


def _document_bytes(document, artifact_root: Path | None) -> bytes:
    if document.raw_content:
        content = document.raw_content
    else:
        if artifact_root is None:
            raise ExchangeRulePersistenceError(
                "source document raw evidence is not loaded; provide artifact_root"
            )
        path = _safe_artifact_path(artifact_root, document.content_path)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ExchangeRulePersistenceError(
                f"cannot read source evidence: {document.content_path}"
            ) from exc
    if _sha256(content) != document.content_sha256:
        raise ExchangeRulePersistenceError(
            f"source evidence hash does not match {document.document_id}"
        )
    return content


def _insert_content(connection: Connection, document, content: bytes) -> bool:
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from ak_trading.persistence.schema import exchange_rule_source_contents

    statement = (
        insert(exchange_rule_source_contents)
        .values(
            content_sha256=document.content_sha256,
            content=content,
            content_path=document.content_path,
        )
        .on_conflict_do_nothing(
            index_elements=[exchange_rule_source_contents.c.content_sha256]
        )
        .returning(exchange_rule_source_contents.c.content_sha256)
    )
    inserted = connection.execute(statement).scalar_one_or_none() is not None
    if not inserted:
        existing = connection.execute(
            select(exchange_rule_source_contents.c.content)
            .where(
                exchange_rule_source_contents.c.content_sha256
                == document.content_sha256
            )
        ).scalar_one()
        if bytes(existing) != content:
            raise PersistenceConflictError(
                f"source content hash collision: {document.content_sha256}"
            )
    return inserted


def _insert_document(connection: Connection, document) -> bool:
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from ak_trading.persistence.schema import exchange_rule_source_documents

    payload = document.as_dict()
    statement = (
        insert(exchange_rule_source_documents)
        .values(
            document_id=document.document_id,
            exchange=document.exchange,
            url=document.url,
            title=document.title,
            content_type=document.content_type,
            content_sha256=document.content_sha256,
            content_path=document.content_path,
            retrieved_at=document.retrieved_at,
            publication_date=document.publication_date,
            effective_date=document.effective_date,
            payload=payload,
        )
        .on_conflict_do_nothing(
            index_elements=[exchange_rule_source_documents.c.document_id]
        )
        .returning(exchange_rule_source_documents.c.document_id)
    )
    inserted = connection.execute(statement).scalar_one_or_none() is not None
    if not inserted:
        existing = connection.execute(
            select(exchange_rule_source_documents.c.payload).where(
                exchange_rule_source_documents.c.document_id == document.document_id
            )
        ).scalar_one()
        if dict(existing) != payload:
            raise PersistenceConflictError(
                f"source document identity collision: {document.document_id}"
            )
    return inserted


def _insert_snapshot(connection: Connection, snapshot: RuleSnapshot) -> bool:
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from ak_trading.persistence.schema import exchange_rule_snapshots

    payload = snapshot.as_dict()
    statement = (
        insert(exchange_rule_snapshots)
        .values(
            snapshot_id=snapshot.snapshot_id,
            target_trading_day=snapshot.target_trading_day,
            collected_at=snapshot.collected_at,
            status=snapshot.status.value,
            origin=snapshot.origin,
            review_reason=snapshot.review_reason,
            parent_snapshot_id=snapshot.parent_snapshot_id,
            payload=payload,
        )
        .on_conflict_do_nothing(
            index_elements=[exchange_rule_snapshots.c.snapshot_id]
        )
        .returning(exchange_rule_snapshots.c.snapshot_id)
    )
    inserted = connection.execute(statement).scalar_one_or_none() is not None
    if not inserted:
        existing = connection.execute(
            select(exchange_rule_snapshots.c.payload).where(
                exchange_rule_snapshots.c.snapshot_id == snapshot.snapshot_id
            )
        ).scalar_one()
        if dict(existing) != payload:
            raise PersistenceConflictError(
                f"snapshot identity collision: {snapshot.snapshot_id}"
            )
    return inserted


def _insert_item(connection: Connection, snapshot_id: str, item: RuleItem) -> bool:
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from ak_trading.persistence.schema import exchange_rule_items

    payload = item.as_dict()
    item_key = _item_key(item)
    statement = (
        insert(exchange_rule_items)
        .values(
            snapshot_id=snapshot_id,
            item_key=item_key,
            exchange=item.exchange.upper(),
            product=item.product.upper(),
            contract=item.contract,
            category=item.category.value,
            field=item.field,
            value_kind=item.value_kind.value,
            status=item.status.value,
            value_text=payload["value"],
            unit=item.unit,
            applies_to=item.applies_to,
            effective_from=item.effective_from,
            effective_through=item.effective_through,
            source_document_id=item.source_document_id,
            source_url=item.source_url,
            excerpt=item.excerpt,
            note=item.note,
            payload=payload,
        )
        .on_conflict_do_nothing(
            index_elements=[exchange_rule_items.c.snapshot_id, exchange_rule_items.c.item_key]
        )
        .returning(exchange_rule_items.c.item_key)
    )
    inserted = connection.execute(statement).scalar_one_or_none() is not None
    if not inserted:
        existing = connection.execute(
            select(exchange_rule_items.c.payload).where(
                exchange_rule_items.c.snapshot_id == snapshot_id,
                exchange_rule_items.c.item_key == item_key,
            )
        ).scalar_one()
        if dict(existing) != payload:
            raise PersistenceConflictError(
                f"rule item identity collision: {snapshot_id}/{item_key}"
            )
    return inserted


def _insert_run(connection: Connection, run: Mapping[str, Any]) -> bool:
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from ak_trading.persistence.schema import exchange_rule_runs

    if run.get("schema_version") != 1:
        raise ExchangeRulePersistenceError("unsupported exchange-rule run schema_version")
    run_id = _digest(run.get("run_id"), "run.run_id")
    payload = dict(run)
    started_at = _aware_datetime(run.get("started_at"), "run.started_at")
    completed_at = _aware_datetime(run.get("completed_at"), "run.completed_at")
    statement = (
        insert(exchange_rule_runs)
        .values(
            run_id=run_id,
            target_trading_day=_iso_date(run.get("target_trading_day"), "run.target_trading_day"),
            started_at=started_at,
            completed_at=completed_at,
            status=str(run.get("status")),
            source_registry_sha256=run.get("source_registry_sha256"),
            snapshot_id=run.get("snapshot_id"),
            previous_snapshot_id=run.get("previous_snapshot_id"),
            payload=payload,
        )
        .on_conflict_do_nothing(index_elements=[exchange_rule_runs.c.run_id])
        .returning(exchange_rule_runs.c.run_id)
    )
    inserted = connection.execute(statement).scalar_one_or_none() is not None
    if not inserted:
        existing = connection.execute(
            select(exchange_rule_runs.c.payload).where(
                exchange_rule_runs.c.run_id == run_id
            )
        ).scalar_one()
        if dict(existing) != payload:
            raise PersistenceConflictError(f"run identity collision: {run_id}")
    return inserted


def _insert_approval(connection: Connection, record: ApprovalRecord) -> bool:
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from ak_trading.persistence.schema import exchange_rule_approvals

    payload = record.as_dict()
    statement = (
        insert(exchange_rule_approvals)
        .values(
            record_id=record.record_id,
            candidate_snapshot_id=record.candidate_snapshot_id,
            approved_snapshot_id=record.approved_snapshot_id,
            decision=record.decision,
            reviewer=record.reviewer,
            reason=record.reason,
            decided_at=record.decided_at,
            payload=payload,
        )
        .on_conflict_do_nothing(index_elements=[exchange_rule_approvals.c.record_id])
        .returning(exchange_rule_approvals.c.record_id)
    )
    inserted = connection.execute(statement).scalar_one_or_none() is not None
    if not inserted:
        existing = connection.execute(
            select(exchange_rule_approvals.c.payload).where(
                exchange_rule_approvals.c.record_id == record.record_id
            )
        ).scalar_one()
        if dict(existing) != payload:
            raise PersistenceConflictError(
                f"approval identity collision: {record.record_id}"
            )
    return inserted


def _set_current(connection: Connection, record: ApprovalRecord) -> bool:
    from sqlalchemy import select, update
    from sqlalchemy.dialects.postgresql import insert

    from ak_trading.persistence.schema import exchange_rule_current, exchange_rule_snapshots

    if record.decision != "approve" or record.approved_snapshot_id is None:
        raise ExchangeRulePersistenceError(
            "only an approval with an approved snapshot can become current"
        )
    status = connection.execute(
        select(exchange_rule_snapshots.c.status).where(
            exchange_rule_snapshots.c.snapshot_id == record.approved_snapshot_id
        )
    ).scalar_one_or_none()
    if status != RuleStatus.CURRENT_APPROVED.value:
        raise ExchangeRulePersistenceError(
            "current pointer requires a current-approved snapshot"
        )
    values = {
        "singleton_id": 1,
        "snapshot_id": record.approved_snapshot_id,
        "approval_record_id": record.record_id,
        "approved_at": record.decided_at,
    }
    existing = connection.execute(
        select(exchange_rule_current).where(exchange_rule_current.c.singleton_id == 1)
    ).mappings().one_or_none()
    if existing is not None and all(existing[key] == value for key, value in values.items()):
        return False
    if existing is None:
        connection.execute(insert(exchange_rule_current).values(**values))
    else:
        connection.execute(
            update(exchange_rule_current)
            .where(exchange_rule_current.c.singleton_id == 1)
            .values(**values)
        )
    return True


def _ordered_snapshots(snapshots: Iterable[RuleSnapshot]) -> tuple[RuleSnapshot, ...]:
    by_id = {snapshot.snapshot_id: snapshot for snapshot in snapshots}
    ordered: list[RuleSnapshot] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(snapshot_id: str) -> None:
        if snapshot_id in visited:
            return
        if snapshot_id in visiting:
            raise ExchangeRulePersistenceError("snapshot parent graph contains a cycle")
        snapshot = by_id.get(snapshot_id)
        if snapshot is None:
            raise ExchangeRulePersistenceError(
                f"snapshot parent is missing from import set: {snapshot_id}"
            )
        visiting.add(snapshot_id)
        if snapshot.parent_snapshot_id is not None:
            visit(snapshot.parent_snapshot_id)
        visiting.remove(snapshot_id)
        visited.add(snapshot_id)
        ordered.append(snapshot)

    for snapshot_id in sorted(by_id):
        visit(snapshot_id)
    return tuple(ordered)


def persist_exchange_rule_artifacts(
    engine: Engine,
    *,
    snapshots: Iterable[RuleSnapshot],
    runs: Iterable[Mapping[str, Any]] = (),
    approvals: Iterable[ApprovalRecord] = (),
    current_approval: ApprovalRecord | None = None,
    artifact_root: str | Path | None = None,
    settings=None,
) -> ExchangeRuleImportResult:
    """Persist a validated local artifact set without activating candidates."""

    snapshot_values = _ordered_snapshots(tuple(snapshots))
    snapshot_ids = {snapshot.snapshot_id for snapshot in snapshot_values}
    run_values = tuple(runs)
    approval_values = tuple(approvals)
    approval_by_id = {record.record_id: record for record in approval_values}
    if current_approval is not None:
        if current_approval.record_id not in approval_by_id:
            raise ExchangeRulePersistenceError(
                "current approval must be included in the approval import set"
            )
        if current_approval.approved_snapshot_id not in snapshot_ids:
            raise ExchangeRulePersistenceError(
                "current approved snapshot must be included in the snapshot import set"
            )
    root = Path(artifact_root) if artifact_root is not None else None
    counts = {
        "source_contents_inserted": 0,
        "source_documents_inserted": 0,
        "snapshots_inserted": 0,
        "items_inserted": 0,
        "runs_inserted": 0,
        "approvals_inserted": 0,
        "current_updated": False,
    }
    try:
        with engine.begin() as connection:
            for snapshot in snapshot_values:
                for document in snapshot.documents:
                    content = _document_bytes(document, root)
                    counts["source_contents_inserted"] += int(
                        _insert_content(connection, document, content)
                    )
                    counts["source_documents_inserted"] += int(
                        _insert_document(connection, document)
                    )
            for snapshot in snapshot_values:
                counts["snapshots_inserted"] += int(
                    _insert_snapshot(connection, snapshot)
                )
                for item in snapshot.items:
                    counts["items_inserted"] += int(
                        _insert_item(connection, snapshot.snapshot_id, item)
                    )
            for run in run_values:
                counts["runs_inserted"] += int(_insert_run(connection, run))
            for record in approval_values:
                counts["approvals_inserted"] += int(_insert_approval(connection, record))
            if current_approval is not None:
                counts["current_updated"] = _set_current(connection, current_approval)
    except (ExchangeRulePersistenceError, PersistenceError):
        raise
    except Exception as exc:
        raise _safe_failure("exchange-rule artifact import", exc, settings) from None
    return ExchangeRuleImportResult(**counts)


def import_exchange_rule_artifacts(
    engine: Engine,
    artifact_root: str | Path,
    *,
    settings=None,
) -> ExchangeRuleImportResult:
    """Import all local exchange-rule artifacts, including the current approval."""

    root = Path(artifact_root)
    store = ExchangeRuleArtifactStore(root)
    snapshots = store.iter_snapshots()
    runs = store.iter_runs()
    approvals = store.iter_approvals()
    current_approval = store.current_approval()
    return persist_exchange_rule_artifacts(
        engine,
        snapshots=snapshots,
        runs=runs,
        approvals=approvals,
        current_approval=current_approval,
        artifact_root=root,
        settings=settings,
    )
