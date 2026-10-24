"""Immutable local exchange-rule artifacts and a Windows-safe collection lock.

Local files are the canonical operational store. PostgreSQL import is optional
and never required to collect, review, or render the rules workbench.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from ak_trading.exchange_rules import (
    ExchangeRuleError,
    RuleCategory,
    RuleIdentity,
    RuleItem,
    RuleScope,
    RuleSnapshot,
    RuleStatus,
    rule_identity_sort_key,
)


class ExchangeRuleArtifactError(RuntimeError):
    """Raised when local exchange-rule artifacts are malformed or unsafe."""


class CollectionLockedError(ExchangeRuleArtifactError):
    """Raised when another exchange-rule collection process owns the lock."""


ARTIFACT_SCHEMA_VERSION = 1
CATALOG_RUN_SCHEMA_VERSION = 2


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
        raise ExchangeRuleArtifactError("artifact contains a non-JSON value") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ExchangeRuleArtifactError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ExchangeRuleArtifactError(f"{label} must be timezone-aware")
    return value


def _scope_field_key(
    value: object,
    label: str = "scope approval field key",
) -> RuleIdentity:
    if not isinstance(value, tuple) or len(value) != 5:
        raise ExchangeRuleArtifactError(f"{label} must contain five values")
    exchange, product, contract, category, field = value
    if not isinstance(exchange, str) or not exchange.strip():
        raise ExchangeRuleArtifactError(f"{label}.exchange must be non-empty")
    if not isinstance(product, str) or not product.strip():
        raise ExchangeRuleArtifactError(f"{label}.product must be non-empty")
    if contract is not None and (not isinstance(contract, str) or not contract.strip()):
        raise ExchangeRuleArtifactError(
            f"{label}.contract must be a non-empty string or null"
        )
    try:
        category_value = RuleCategory(category).value
    except (TypeError, ValueError) as exc:
        raise ExchangeRuleArtifactError(f"{label}.category is invalid") from exc
    if not isinstance(field, str) or not field.strip():
        raise ExchangeRuleArtifactError(f"{label}.field must be non-empty")
    return (
        exchange.strip().upper(),
        product.strip().upper(),
        contract.strip() if isinstance(contract, str) else None,
        category_value,
        field.strip(),
    )


def _scope_field_dict(key: RuleIdentity) -> dict[str, str | None]:
    exchange, product, contract, category, field = key
    return {
        "exchange": exchange,
        "product": product,
        "contract": contract,
        "category": category,
        "field": field,
    }


def _scope_field_key_from_mapping(value: object, label: str) -> RuleIdentity:
    if not isinstance(value, Mapping):
        raise ExchangeRuleArtifactError(f"{label} must be an object")
    return _scope_field_key(
        (
            value.get("exchange"),
            value.get("product"),
            value.get("contract"),
            value.get("category"),
            value.get("field"),
        ),
        label,
    )


def _atomic_write_bytes(path: Path, content: bytes, *, overwrite: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if not overwrite and path.exists():
            raise ExchangeRuleArtifactError(f"artifact already exists: {path}")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, payload: Mapping[str, Any], *, overwrite: bool = True) -> None:
    _atomic_write_bytes(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n",
        overwrite=overwrite,
    )


@dataclass(frozen=True)
class ApprovalRecord:
    """An append-only human decision attached to one reviewed candidate snapshot."""

    candidate_snapshot_id: str
    decision: str
    reviewer: str
    reason: str
    decided_at: datetime
    record_id: str
    approved_snapshot_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        candidate_snapshot_id: str,
        decision: str,
        reviewer: str,
        reason: str,
        decided_at: datetime,
        approved_snapshot_id: str | None = None,
    ) -> "ApprovalRecord":
        _require_digest(candidate_snapshot_id, "approval.candidate_snapshot_id")
        if decision not in {"approve", "reject"}:
            raise ExchangeRuleArtifactError("approval decision must be approve or reject")
        if decision == "approve":
            if approved_snapshot_id is None:
                raise ExchangeRuleArtifactError(
                    "approval requires an approved_snapshot_id"
                )
            _require_digest(approved_snapshot_id, "approval.approved_snapshot_id")
        elif approved_snapshot_id is not None:
            raise ExchangeRuleArtifactError(
                "rejection must not name an approved_snapshot_id"
            )
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ExchangeRuleArtifactError("approval reviewer must be non-empty")
        if not isinstance(reason, str) or not reason.strip():
            raise ExchangeRuleArtifactError("approval reason must be non-empty")
        _require_aware(decided_at, "approval decided_at")
        payload = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "candidate_snapshot_id": candidate_snapshot_id,
            "approved_snapshot_id": approved_snapshot_id,
            "decision": decision,
            "reviewer": reviewer.strip(),
            "reason": reason.strip(),
            "decided_at": decided_at.isoformat(),
        }
        return cls(
            candidate_snapshot_id=candidate_snapshot_id,
            approved_snapshot_id=approved_snapshot_id,
            decision=decision,
            reviewer=reviewer.strip(),
            reason=reason.strip(),
            decided_at=decided_at,
            record_id=_sha256(_canonical_json_bytes(payload)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "record_id": self.record_id,
            "candidate_snapshot_id": self.candidate_snapshot_id,
            "approved_snapshot_id": self.approved_snapshot_id,
            "decision": self.decision,
            "reviewer": self.reviewer,
            "reason": self.reason,
            "decided_at": self.decided_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ApprovalRecord":
        if (
            not isinstance(raw, Mapping)
            or raw.get("schema_version") != ARTIFACT_SCHEMA_VERSION
        ):
            raise ExchangeRuleArtifactError("unsupported approval record schema_version")
        try:
            decided_at = datetime.fromisoformat(str(raw.get("decided_at")))
        except ValueError as exc:
            raise ExchangeRuleArtifactError("approval decided_at must be ISO datetime") from exc
        raw_approved_snapshot_id = raw.get("approved_snapshot_id")
        result = cls.create(
            candidate_snapshot_id=_require_digest(
                raw.get("candidate_snapshot_id"), "approval.candidate_snapshot_id"
            ),
            approved_snapshot_id=(
                _require_digest(raw_approved_snapshot_id, "approval.approved_snapshot_id")
                if raw_approved_snapshot_id is not None
                else None
            ),
            decision=str(raw.get("decision") or ""),
            reviewer=str(raw.get("reviewer") or ""),
            reason=str(raw.get("reason") or ""),
            decided_at=decided_at,
        )
        if raw.get("record_id") != result.record_id:
            raise ExchangeRuleArtifactError(
                "approval record SHA-256 does not match payload"
            )
        return result


@dataclass(frozen=True)
class ScopeApprovalRecord:
    """Append-only approval or rejection of explicit fields in one rule scope."""

    candidate_snapshot_id: str
    scope: RuleScope
    field_keys: tuple[tuple[str, str, str | None, str, str], ...]
    decision: str
    reviewer: str
    reason: str
    decided_at: datetime
    catalog_id: str
    record_id: str

    @classmethod
    def create(
        cls,
        *,
        candidate_snapshot_id: str,
        scope: RuleScope,
        field_keys: Iterable[tuple[str, str, str | None, str, str]],
        decision: str,
        reviewer: str,
        reason: str,
        decided_at: datetime,
        catalog_id: str,
    ) -> "ScopeApprovalRecord":
        candidate = _require_digest(candidate_snapshot_id, "scope approval candidate_snapshot_id")
        if decision not in {"approve", "reject"}:
            raise ExchangeRuleArtifactError("scope approval decision must be approve or reject")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ExchangeRuleArtifactError("scope approval reviewer must be non-empty")
        if not isinstance(reason, str) or not reason.strip():
            raise ExchangeRuleArtifactError("scope approval reason must be non-empty")
        _require_aware(decided_at, "scope approval decided_at")
        digest = _require_digest(catalog_id, "scope approval catalog_id")
        if not isinstance(scope, RuleScope):
            raise ExchangeRuleArtifactError("scope approval scope is invalid")
        normalized_keys = tuple(
            sorted(
                {_scope_field_key(key) for key in field_keys},
                key=rule_identity_sort_key,
            )
        )
        if not normalized_keys:
            raise ExchangeRuleArtifactError("scope approval requires explicit field keys")
        payload = {
            "schema_version": CATALOG_RUN_SCHEMA_VERSION,
            "candidate_snapshot_id": candidate,
            "scope": scope.as_dict(),
            "field_keys": [_scope_field_dict(key) for key in normalized_keys],
            "decision": decision,
            "reviewer": reviewer.strip(),
            "reason": reason.strip(),
            "decided_at": decided_at.isoformat(),
            "catalog_id": digest,
        }
        return cls(
            candidate_snapshot_id=candidate,
            scope=scope,
            field_keys=normalized_keys,
            decision=decision,
            reviewer=reviewer.strip(),
            reason=reason.strip(),
            decided_at=decided_at,
            catalog_id=digest,
            record_id=_sha256(_canonical_json_bytes(payload)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CATALOG_RUN_SCHEMA_VERSION,
            "record_id": self.record_id,
            "candidate_snapshot_id": self.candidate_snapshot_id,
            "scope": self.scope.as_dict(),
            "field_keys": [
                {
                    "exchange": exchange,
                    "product": product,
                    "contract": contract,
                    "category": category,
                    "field": field,
                }
                for exchange, product, contract, category, field in self.field_keys
            ],
            "decision": self.decision,
            "reviewer": self.reviewer,
            "reason": self.reason,
            "decided_at": self.decided_at.isoformat(),
            "catalog_id": self.catalog_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ScopeApprovalRecord":
        if (
            not isinstance(raw, Mapping)
            or raw.get("schema_version") != CATALOG_RUN_SCHEMA_VERSION
        ):
            raise ExchangeRuleArtifactError("unsupported scope approval schema_version")
        raw_fields = raw.get("field_keys")
        if not isinstance(raw_fields, list):
            raise ExchangeRuleArtifactError("scope approval field_keys must be a list")
        field_keys = [
            _scope_field_key_from_mapping(
                raw_field, f"scope approval field_keys[{index}]"
            )
            for index, raw_field in enumerate(raw_fields)
        ]
        try:
            decided_at = datetime.fromisoformat(str(raw.get("decided_at")))
        except ValueError as exc:
            raise ExchangeRuleArtifactError("scope approval decided_at must be ISO datetime") from exc
        result = cls.create(
            candidate_snapshot_id=_require_digest(
                raw.get("candidate_snapshot_id"), "scope approval candidate_snapshot_id"
            ),
            scope=RuleScope.from_dict(raw.get("scope", {})),
            field_keys=field_keys,
            decision=str(raw.get("decision") or ""),
            reviewer=str(raw.get("reviewer") or ""),
            reason=str(raw.get("reason") or ""),
            decided_at=decided_at,
            catalog_id=_require_digest(raw.get("catalog_id"), "scope approval catalog_id"),
        )
        if raw.get("record_id") != result.record_id:
            raise ExchangeRuleArtifactError("scope approval record SHA-256 does not match payload")
        return result


class CollectionLock(AbstractContextManager["CollectionLock"]):
    """Non-blocking process lock using msvcrt on Windows and fcntl elsewhere."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle = None

    def __enter__(self) -> "CollectionLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        try:
            if os.fstat(self._handle.fileno()).st_size == 0:
                self._handle.write(b" ")
                self._handle.flush()
                os.fsync(self._handle.fileno())
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise CollectionLockedError(
                        f"another exchange-rule collector owns {self.path}"
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise CollectionLockedError(
                        f"another exchange-rule collector owns {self.path}"
                    ) from exc
        except Exception:
            self._handle.close()
            self._handle = None
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is None:
            return None
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
        return None


class ExchangeRuleArtifactStore:
    """Content-addressed local evidence, snapshots, decisions, and current pointer."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @property
    def documents_root(self) -> Path:
        return self.root / "documents"

    @property
    def snapshots_root(self) -> Path:
        return self.root / "snapshots"

    @property
    def runs_root(self) -> Path:
        return self.root / "runs"

    @property
    def catalog_runs_root(self) -> Path:
        return self.root / "catalog_runs"

    @property
    def approvals_root(self) -> Path:
        return self.root / "approvals"

    @property
    def scope_approvals_root(self) -> Path:
        return self.root / "scope_approvals"

    @property
    def scope_current_root(self) -> Path:
        return self.root / "scope_current"

    @property
    def current_path(self) -> Path:
        return self.root / "current.json"

    @property
    def lock_path(self) -> Path:
        return self.root / ".collect.lock"

    def document_relative_path(self, content_sha256: str, suffix: str) -> str:
        digest = _require_digest(content_sha256, "document.content_sha256")
        if not isinstance(suffix, str) or not suffix.startswith(".") or len(suffix) > 12:
            raise ExchangeRuleArtifactError("document suffix is invalid")
        return (Path("documents") / digest[:2] / f"{digest}{suffix}").as_posix()

    def document_path(self, content_sha256: str, suffix: str) -> Path:
        return self.root / self.document_relative_path(content_sha256, suffix)

    def store_document(self, *, content_sha256: str, suffix: str, content: bytes) -> Path:
        if _sha256(content) != _require_digest(content_sha256, "document.content_sha256"):
            raise ExchangeRuleArtifactError("document bytes do not match content SHA-256")
        path = self.document_path(content_sha256, suffix)
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise ExchangeRuleArtifactError(f"cannot read existing document: {path}") from exc
            if existing != content:
                raise ExchangeRuleArtifactError(
                    "document identity collision has different retained bytes"
                )
            return path
        _atomic_write_bytes(path, content, overwrite=False)
        return path

    def snapshot_path(self, snapshot_id: str) -> Path:
        return self.snapshots_root / f"{_require_digest(snapshot_id, 'snapshot_id')}.json"

    def store_snapshot(self, snapshot: RuleSnapshot) -> Path:
        path = self.snapshot_path(snapshot.snapshot_id)
        payload = snapshot.as_dict()
        if path.exists():
            existing = self.load_snapshot(snapshot.snapshot_id)
            if existing.as_dict() != payload:
                raise ExchangeRuleArtifactError(
                    "snapshot identity collision has different content"
                )
            return path
        _atomic_write_json(path, payload, overwrite=False)
        return path

    def load_snapshot(self, snapshot_id: str) -> RuleSnapshot:
        path = self.snapshot_path(snapshot_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExchangeRuleArtifactError(f"cannot load snapshot: {snapshot_id}") from exc
        try:
            return RuleSnapshot.from_dict(raw)
        except ExchangeRuleError as exc:
            raise ExchangeRuleArtifactError(f"invalid snapshot: {snapshot_id}") from exc

    def iter_snapshots(self) -> tuple[RuleSnapshot, ...]:
        if not self.snapshots_root.exists():
            return ()
        snapshots: list[RuleSnapshot] = []
        for path in sorted(self.snapshots_root.glob("*.json")):
            snapshots.append(self.load_snapshot(path.stem))
        return tuple(snapshots)

    def store_run(self, payload: Mapping[str, Any]) -> Path:
        if not isinstance(payload, Mapping) or payload.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
            raise ExchangeRuleArtifactError("unsupported collection run schema_version")
        run_id = _require_digest(payload.get("run_id"), "collection run_id")
        expected = dict(payload)
        expected.pop("run_id", None)
        if _sha256(_canonical_json_bytes(expected)) != run_id:
            raise ExchangeRuleArtifactError("collection run_id does not match payload")
        path = self.runs_root / f"{run_id}.json"
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ExchangeRuleArtifactError("cannot read existing collection run") from exc
            if existing != dict(payload):
                raise ExchangeRuleArtifactError("collection run identity collision")
            return path
        _atomic_write_json(path, dict(payload), overwrite=False)
        return path

    def iter_runs(self) -> tuple[dict[str, Any], ...]:
        if not self.runs_root.exists():
            return ()
        runs: list[dict[str, Any]] = []
        for path in sorted(self.runs_root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ExchangeRuleArtifactError(f"cannot load collection run: {path}") from exc
            if not isinstance(payload, dict):
                raise ExchangeRuleArtifactError(f"collection run must be an object: {path}")
            self.store_run(payload)
            runs.append(payload)
        return tuple(runs)

    def store_catalog_run(self, payload: Mapping[str, Any]) -> Path:
        """Store an immutable v2 all-futures coverage manifest."""

        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != CATALOG_RUN_SCHEMA_VERSION
        ):
            raise ExchangeRuleArtifactError("unsupported catalog collection run schema_version")
        run_id = _require_digest(payload.get("run_id"), "catalog collection run_id")
        expected = dict(payload)
        expected.pop("run_id", None)
        if _sha256(_canonical_json_bytes(expected)) != run_id:
            raise ExchangeRuleArtifactError("catalog collection run_id does not match payload")
        normalized, legacy_compatibility = _normalize_catalog_run_payload(payload)
        self._validate_catalog_run(
            normalized,
            legacy_compatibility=legacy_compatibility,
        )
        path = self.catalog_runs_root / f"{run_id}.json"
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ExchangeRuleArtifactError("cannot read existing catalog collection run") from exc
            if existing != dict(payload):
                raise ExchangeRuleArtifactError("catalog collection run identity collision")
            return path
        _atomic_write_json(path, dict(payload), overwrite=False)
        return path

    def _validate_catalog_run(
        self,
        payload: Mapping[str, Any],
        *,
        legacy_compatibility: bool = False,
    ) -> None:
        try:
            target_trading_day = date.fromisoformat(str(payload.get("target_trading_day")))
            started_at = datetime.fromisoformat(str(payload.get("started_at")))
            completed_at = datetime.fromisoformat(str(payload.get("completed_at")))
        except ValueError as exc:
            raise ExchangeRuleArtifactError(
                "catalog collection run dates must use ISO values"
            ) from exc
        _require_aware(started_at, "catalog collection started_at")
        _require_aware(completed_at, "catalog collection completed_at")
        if completed_at < started_at:
            raise ExchangeRuleArtifactError("catalog collection completed_at precedes started_at")
        exchange_statuses = payload.get("exchange_statuses")
        if not isinstance(exchange_statuses, Mapping):
            raise ExchangeRuleArtifactError("catalog collection exchange statuses are invalid")

        def pairs(field: str) -> tuple[tuple[str, str], ...]:
            raw_pairs = payload.get(field)
            if not isinstance(raw_pairs, list):
                raise ExchangeRuleArtifactError(f"catalog collection {field} must be a list")
            return tuple(
                _catalog_pair(
                    (item.get("exchange"), item.get("product"))
                    if isinstance(item, Mapping)
                    else item,
                    f"catalog collection {field}[{index}]",
                )
                for index, item in enumerate(raw_pairs)
            )

        expected = pairs("expected_futures")
        processed = pairs("processed_futures")
        closed = pairs("closed_futures")
        options = payload.get("excluded_options")
        if not isinstance(options, list):
            raise ExchangeRuleArtifactError("catalog collection excluded_options must be a list")
        snapshot_id = payload.get("snapshot_id")
        previous_snapshot_id = payload.get("previous_snapshot_id")
        rebuilt = build_catalog_collection_run(
            target_trading_day=target_trading_day,
            started_at=started_at,
            completed_at=completed_at,
            catalog_id=_require_digest(payload.get("catalog_id"), "catalog_id"),
            exchange_statuses={str(key): str(value) for key, value in exchange_statuses.items()},
            expected_futures=expected,
            processed_futures=processed,
            closed_futures=closed,
            excluded_options=options,
            snapshot_id=(
                _require_digest(snapshot_id, "catalog collection snapshot_id")
                if snapshot_id is not None
                else None
            ),
            errors=payload.get("errors", ()),
            review_reasons=payload.get("review_reasons", ()),
            document_ids=payload.get("document_ids", ()),
            previous_snapshot_id=(
                _require_digest(previous_snapshot_id, "catalog collection previous_snapshot_id")
                if previous_snapshot_id is not None
                else None
            ),
        )
        if legacy_compatibility:
            normalized_without_id = dict(payload)
            normalized_without_id.pop("run_id", None)
            rebuilt_without_id = dict(rebuilt)
            rebuilt_without_id.pop("run_id", None)
            if normalized_without_id != rebuilt_without_id:
                raise ExchangeRuleArtifactError(
                    "legacy catalog collection run does not match canonical coverage content"
                )
        elif dict(payload) != rebuilt:
            raise ExchangeRuleArtifactError(
                "catalog collection run does not match canonical coverage content"
            )

    def iter_catalog_runs(self) -> tuple[dict[str, Any], ...]:
        if not self.catalog_runs_root.exists():
            return ()
        runs: list[dict[str, Any]] = []
        for path in sorted(self.catalog_runs_root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ExchangeRuleArtifactError(
                    f"cannot load catalog collection run: {path}"
                ) from exc
            if not isinstance(payload, dict):
                raise ExchangeRuleArtifactError(
                    f"catalog collection run must be an object: {path}"
                )
            normalized, _ = _normalize_catalog_run_payload(payload)
            self.store_catalog_run(payload)
            runs.append(normalized)
        return tuple(runs)

    def append_approval(self, record: ApprovalRecord) -> Path:
        path = self.approvals_root / f"{record.record_id}.json"
        if path.exists():
            existing = self.load_approval(record.record_id)
            if existing != record:
                raise ExchangeRuleArtifactError("approval identity collision")
            return path
        _atomic_write_json(path, record.as_dict(), overwrite=False)
        return path

    def load_approval(self, record_id: str) -> ApprovalRecord:
        path = self.approvals_root / f"{_require_digest(record_id, 'approval.record_id')}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExchangeRuleArtifactError(f"cannot load approval: {record_id}") from exc
        return ApprovalRecord.from_dict(raw)

    def approvals_for(self, candidate_snapshot_id: str) -> tuple[ApprovalRecord, ...]:
        wanted = _require_digest(
            candidate_snapshot_id, "approval.candidate_snapshot_id"
        )
        if not self.approvals_root.exists():
            return ()
        records: list[ApprovalRecord] = []
        for path in sorted(self.approvals_root.glob("*.json")):
            record = self.load_approval(path.stem)
            if record.candidate_snapshot_id == wanted:
                records.append(record)
        return tuple(sorted(records, key=lambda record: (record.decided_at, record.record_id)))

    def iter_approvals(self) -> tuple[ApprovalRecord, ...]:
        """Return every append-only decision in deterministic order."""

        if not self.approvals_root.exists():
            return ()
        records = [
            self.load_approval(path.stem)
            for path in sorted(self.approvals_root.glob("*.json"))
        ]
        return tuple(sorted(records, key=lambda record: (record.decided_at, record.record_id)))

    def _scope_pointer_path(self, scope: RuleScope) -> Path:
        kind, exchange, product, contract = scope.identity
        material = "|".join((kind, exchange, product or "", contract or ""))
        return self.scope_current_root / f"{_sha256(material.encode('utf-8'))}.json"

    def _catalog_run_for_snapshot(
        self,
        *,
        candidate_snapshot_id: str,
        catalog_id: str,
    ) -> dict[str, Any]:
        matches = [
            run
            for run in self.iter_catalog_runs()
            if run.get("snapshot_id") == candidate_snapshot_id
            and run.get("catalog_id") == catalog_id
        ]
        if not matches:
            raise ExchangeRuleArtifactError(
                "scope decision candidate snapshot is not linked to the declared catalog"
            )
        return max(
            matches,
            key=lambda run: (str(run.get("completed_at") or ""), str(run.get("run_id") or "")),
        )

    def _read_scope_pointer(self, scope: RuleScope) -> dict[str, Any] | None:
        path = self._scope_pointer_path(scope)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExchangeRuleArtifactError("cannot load scope current pointer") from exc
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != CATALOG_RUN_SCHEMA_VERSION
        ):
            raise ExchangeRuleArtifactError("unsupported scope current pointer")
        try:
            pointer_scope = RuleScope.from_dict(payload.get("scope", {}))
            updated_at = datetime.fromisoformat(str(payload.get("updated_at")))
        except (ExchangeRuleError, ValueError) as exc:
            raise ExchangeRuleArtifactError("scope current pointer is malformed") from exc
        _require_aware(updated_at, "scope current updated_at")
        if pointer_scope != scope:
            raise ExchangeRuleArtifactError("scope current pointer does not match its path")
        return dict(payload)

    def append_scope_approval(self, record: ScopeApprovalRecord) -> Path:
        path = self.scope_approvals_root / f"{record.record_id}.json"
        if path.exists():
            existing = self.load_scope_approval(record.record_id)
            if existing != record:
                raise ExchangeRuleArtifactError("scope approval identity collision")
            return path
        _atomic_write_json(path, record.as_dict(), overwrite=False)
        return path

    def load_scope_approval(self, record_id: str) -> ScopeApprovalRecord:
        path = self.scope_approvals_root / f"{_require_digest(record_id, 'scope approval.record_id')}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExchangeRuleArtifactError(f"cannot load scope approval: {record_id}") from exc
        return ScopeApprovalRecord.from_dict(raw)

    def iter_scope_approvals(self) -> tuple[ScopeApprovalRecord, ...]:
        if not self.scope_approvals_root.exists():
            return ()
        records = [
            self.load_scope_approval(path.stem)
            for path in sorted(self.scope_approvals_root.glob("*.json"))
        ]
        return tuple(sorted(records, key=lambda record: (record.decided_at, record.record_id)))

    def iter_scope_currents(self) -> tuple[dict[str, Any], ...]:
        """Return every validated field-level scope current pointer.

        Scope pointer paths are hashes, so callers must not infer a scope from a
        filename. Reading each payload first lets this method verify that the
        embedded scope hashes back to the exact path before resolving its full
        approval and catalog provenance through :meth:`scope_current`.
        """

        if not self.scope_current_root.exists():
            return ()
        pointers: list[dict[str, Any]] = []
        for path in sorted(self.scope_current_root.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ExchangeRuleArtifactError(
                    f"cannot load scope current pointer: {path}"
                ) from exc
            if not isinstance(raw, Mapping):
                raise ExchangeRuleArtifactError(
                    f"scope current pointer must be an object: {path}"
                )
            try:
                scope = RuleScope.from_dict(raw.get("scope", {}))
            except ExchangeRuleError as exc:
                raise ExchangeRuleArtifactError(
                    f"scope current pointer is malformed: {path}"
                ) from exc
            if path != self._scope_pointer_path(scope):
                raise ExchangeRuleArtifactError(
                    "scope current pointer does not match its canonical path"
                )
            pointer = self.scope_current(scope)
            if pointer is None:
                raise ExchangeRuleArtifactError("scope current pointer disappeared")
            pointers.append(pointer)
        return tuple(
            sorted(
                pointers,
                key=lambda pointer: (
                    str(pointer["scope"]["kind"]),
                    str(pointer["scope"]["exchange"]),
                    str(pointer["scope"].get("product") or ""),
                    str(pointer["scope"].get("contract") or ""),
                    str(pointer["updated_at"]),
                ),
            )
        )

    def approve_scope_fields(
        self,
        candidate: RuleSnapshot,
        *,
        scope: RuleScope,
        field_keys: Iterable[RuleIdentity],
        reviewer: str,
        reason: str,
        decided_at: datetime,
        catalog_id: str,
    ) -> tuple[ScopeApprovalRecord, Path]:
        """Approve explicit candidate fields without changing legacy current.json."""

        if candidate.status not in {RuleStatus.CANDIDATE, RuleStatus.REVIEW_REQUIRED}:
            raise ExchangeRuleArtifactError(
                "only a candidate or review-required snapshot can receive scope decisions"
            )
        _require_aware(decided_at, "scope approval decided_at")
        requested = tuple(
            sorted({_scope_field_key(key) for key in field_keys}, key=rule_identity_sort_key)
        )
        if not requested:
            raise ExchangeRuleArtifactError("scope approval requires explicit field keys")
        items = {item.identity_key: item for item in candidate.items}
        for key in requested:
            item = items.get(key)
            if item is None:
                raise ExchangeRuleArtifactError("scope approval field is outside candidate snapshot")
            if not scope.contains(item):
                raise ExchangeRuleArtifactError(
                    "scope approval field is outside the declared scope"
                )
            if item.status is not RuleStatus.CANDIDATE:
                raise ExchangeRuleArtifactError(
                    "only deterministic candidate fields may be scope-approved"
                )
        record = ScopeApprovalRecord.create(
            candidate_snapshot_id=candidate.snapshot_id,
            scope=scope,
            field_keys=requested,
            decision="approve",
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
            catalog_id=catalog_id,
        )
        self._catalog_run_for_snapshot(
            candidate_snapshot_id=candidate.snapshot_id,
            catalog_id=record.catalog_id,
        )
        existing = self.scope_current(scope)
        existing_entries: dict[RuleIdentity, dict[str, Any]] = {}
        if existing is not None:
            try:
                existing_updated_at = datetime.fromisoformat(str(existing["updated_at"]))
            except (KeyError, ValueError) as exc:
                raise ExchangeRuleArtifactError("scope current pointer is malformed") from exc
            _require_aware(existing_updated_at, "scope current updated_at")
            raw_entries = existing.get("field_approvals")
            if not isinstance(raw_entries, list):
                raise ExchangeRuleArtifactError("scope current field approvals are invalid")
            for index, entry in enumerate(raw_entries):
                if not isinstance(entry, Mapping):
                    raise ExchangeRuleArtifactError(
                        f"scope current field_approvals[{index}] must be an object"
                    )
                key = _scope_field_key_from_mapping(
                    entry.get("field"), f"scope current field_approvals[{index}].field"
                )
                existing_entries[key] = dict(entry)
            if record.decided_at < existing_updated_at:
                raise ExchangeRuleArtifactError(
                    "scope approval decided_at must not precede the current scope pointer"
                )
            if record.decided_at == existing_updated_at:
                replacement_keys = set(record.field_keys)
                if all(
                    existing_entries.get(key, {}).get("approval_record_id") == record.record_id
                    for key in replacement_keys
                ):
                    return record, self._scope_pointer_path(scope)
                raise ExchangeRuleArtifactError(
                    "scope approval decided_at must be later than the current scope pointer"
                )
        for key in record.field_keys:
            existing_entries[key] = {
                "field": _scope_field_dict(key),
                "candidate_snapshot_id": record.candidate_snapshot_id,
                "approval_record_id": record.record_id,
                "catalog_id": record.catalog_id,
                "approved_at": record.decided_at.isoformat(),
            }
        pointer = {
            "schema_version": CATALOG_RUN_SCHEMA_VERSION,
            "scope": scope.as_dict(),
            "field_approvals": [
                existing_entries[key]
                for key in sorted(existing_entries, key=rule_identity_sort_key)
            ],
            "updated_at": record.decided_at.isoformat(),
        }
        self.store_snapshot(candidate)
        self.append_scope_approval(record)
        path = self._scope_pointer_path(scope)
        _atomic_write_json(path, pointer)
        return record, path

    def reject_scope_fields(
        self,
        candidate: RuleSnapshot,
        *,
        scope: RuleScope,
        field_keys: Iterable[RuleIdentity],
        reviewer: str,
        reason: str,
        decided_at: datetime,
        catalog_id: str,
    ) -> ScopeApprovalRecord:
        """Append a field rejection without changing any scope current pointer."""

        if candidate.status not in {RuleStatus.CANDIDATE, RuleStatus.REVIEW_REQUIRED}:
            raise ExchangeRuleArtifactError(
                "only a candidate or review-required snapshot can receive scope decisions"
            )
        _require_aware(decided_at, "scope rejection decided_at")
        requested = tuple(
            sorted({_scope_field_key(key) for key in field_keys}, key=rule_identity_sort_key)
        )
        if not requested:
            raise ExchangeRuleArtifactError("scope rejection requires explicit field keys")
        items = {item.identity_key: item for item in candidate.items}
        for key in requested:
            item = items.get(key)
            if item is None or not scope.contains(item):
                raise ExchangeRuleArtifactError(
                    "scope rejection field is outside declared candidate scope"
                )
        record = ScopeApprovalRecord.create(
            candidate_snapshot_id=candidate.snapshot_id,
            scope=scope,
            field_keys=requested,
            decision="reject",
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
            catalog_id=catalog_id,
        )
        self._catalog_run_for_snapshot(
            candidate_snapshot_id=candidate.snapshot_id,
            catalog_id=record.catalog_id,
        )
        self.store_snapshot(candidate)
        self.append_scope_approval(record)
        return record

    def scope_current(self, scope: RuleScope) -> dict[str, Any] | None:
        payload = self._read_scope_pointer(scope)
        if payload is None:
            return None
        raw_entries = payload.get("field_approvals")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ExchangeRuleArtifactError("scope current field approvals are invalid")
        try:
            updated_at = datetime.fromisoformat(str(payload.get("updated_at")))
        except ValueError as exc:
            raise ExchangeRuleArtifactError("scope current updated_at must be ISO datetime") from exc
        _require_aware(updated_at, "scope current updated_at")
        seen: set[RuleIdentity] = set()
        for index, entry in enumerate(raw_entries):
            if not isinstance(entry, Mapping):
                raise ExchangeRuleArtifactError(
                    f"scope current field_approvals[{index}] must be an object"
                )
            key = _scope_field_key_from_mapping(
                entry.get("field"), f"scope current field_approvals[{index}].field"
            )
            if key in seen:
                raise ExchangeRuleArtifactError("scope current field approvals must be unique")
            seen.add(key)
            candidate_snapshot_id = _require_digest(
                entry.get("candidate_snapshot_id"),
                "scope current candidate_snapshot_id",
            )
            approval_record_id = _require_digest(
                entry.get("approval_record_id"),
                "scope current approval_record_id",
            )
            catalog_id = _require_digest(entry.get("catalog_id"), "scope current catalog_id")
            try:
                approved_at = datetime.fromisoformat(str(entry.get("approved_at")))
            except ValueError as exc:
                raise ExchangeRuleArtifactError(
                    "scope current approved_at must be ISO datetime"
                ) from exc
            _require_aware(approved_at, "scope current approved_at")
            if approved_at > updated_at:
                raise ExchangeRuleArtifactError(
                    "scope current field approval is newer than its pointer"
                )
            record = self.load_scope_approval(approval_record_id)
            if (
                record.decision != "approve"
                or record.scope != scope
                or record.candidate_snapshot_id != candidate_snapshot_id
                or record.catalog_id != catalog_id
                or record.decided_at != approved_at
                or key not in record.field_keys
            ):
                raise ExchangeRuleArtifactError(
                    "scope current pointer does not have a valid approval trail"
                )
            self._catalog_run_for_snapshot(
                candidate_snapshot_id=candidate_snapshot_id,
                catalog_id=catalog_id,
            )
            snapshot = self.load_snapshot(candidate_snapshot_id)
            item = {item.identity_key: item for item in snapshot.items}.get(key)
            if (
                item is None
                or item.status is not RuleStatus.CANDIDATE
                or not scope.contains(item)
            ):
                raise ExchangeRuleArtifactError(
                    "scope current field is not a deterministic candidate in its snapshot"
                )
        return dict(payload)

    def current_approval(self) -> ApprovalRecord | None:
        """Resolve the current pointer to its validated approval record."""

        if not self.current_path.exists():
            return None
        try:
            payload = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExchangeRuleArtifactError("cannot load current exchange-rule pointer") from exc
        if not isinstance(payload, Mapping) or payload.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
            raise ExchangeRuleArtifactError("unsupported current exchange-rule pointer")
        snapshot_id = _require_digest(payload.get("snapshot_id"), "current.snapshot_id")
        approval_record_id = _require_digest(
            payload.get("approval_record_id"), "current.approval_record_id"
        )
        snapshot = self.load_snapshot(snapshot_id)
        approval = self.load_approval(approval_record_id)
        if (
            approval.decision != "approve"
            or approval.approved_snapshot_id != snapshot.snapshot_id
            or snapshot.status is not RuleStatus.CURRENT_APPROVED
        ):
            raise ExchangeRuleArtifactError(
                "current exchange-rule pointer does not have a valid approval trail"
            )
        return approval

    def current_snapshot_id(self) -> str | None:
        if not self.current_path.exists():
            return None
        try:
            payload = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExchangeRuleArtifactError("cannot load current exchange-rule pointer") from exc
        if not isinstance(payload, Mapping) or payload.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
            raise ExchangeRuleArtifactError("unsupported current exchange-rule pointer")
        snapshot_id = _require_digest(payload.get("snapshot_id"), "current.snapshot_id")
        approval_record_id = _require_digest(
            payload.get("approval_record_id"), "current.approval_record_id"
        )
        snapshot = self.load_snapshot(snapshot_id)
        approval = self.load_approval(approval_record_id)
        if (
            approval.decision != "approve"
            or approval.approved_snapshot_id != snapshot.snapshot_id
            or snapshot.status is not RuleStatus.CURRENT_APPROVED
        ):
            raise ExchangeRuleArtifactError(
                "current exchange-rule pointer does not have a valid approval trail"
            )
        return snapshot_id

    def current_snapshot(self) -> RuleSnapshot | None:
        snapshot_id = self.current_snapshot_id()
        return self.load_snapshot(snapshot_id) if snapshot_id else None

    def approve_snapshot(
        self,
        candidate: RuleSnapshot,
        *,
        reviewer: str,
        reason: str,
        decided_at: datetime,
    ) -> tuple[RuleSnapshot, ApprovalRecord, Path]:
        """Create a new immutable approved snapshot and move the current pointer."""

        if candidate.status not in {RuleStatus.CANDIDATE, RuleStatus.REVIEW_REQUIRED}:
            raise ExchangeRuleArtifactError(
                "only a candidate or review-required snapshot can be approved"
            )
        _require_aware(decided_at, "approval decided_at")
        approved = RuleSnapshot.create(
            target_trading_day=candidate.target_trading_day,
            collected_at=decided_at,
            status=RuleStatus.CURRENT_APPROVED,
            origin="manual_approval",
            documents=candidate.documents,
            items=(
                replace(item, status=RuleStatus.CURRENT_APPROVED)
                if item.status is RuleStatus.CANDIDATE
                else item
                for item in candidate.items
            ),
            review_reason=(
                "Approved from candidate snapshot "
                f"{candidate.snapshot_id}: {reason.strip()}"
            ),
            parent_snapshot_id=candidate.snapshot_id,
        )
        approval = ApprovalRecord.create(
            candidate_snapshot_id=candidate.snapshot_id,
            approved_snapshot_id=approved.snapshot_id,
            decision="approve",
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
        )
        self.store_snapshot(candidate)
        self.store_snapshot(approved)
        self.append_approval(approval)
        pointer = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "snapshot_id": approved.snapshot_id,
            "approval_record_id": approval.record_id,
            "approved_at": approval.decided_at.isoformat(),
        }
        _atomic_write_json(self.current_path, pointer)
        return approved, approval, self.current_path

    def reject_snapshot(
        self,
        candidate: RuleSnapshot,
        *,
        reviewer: str,
        reason: str,
        decided_at: datetime,
    ) -> ApprovalRecord:
        """Append a rejection record without changing the current approved pointer."""

        if candidate.status not in {RuleStatus.CANDIDATE, RuleStatus.REVIEW_REQUIRED}:
            raise ExchangeRuleArtifactError(
                "only a candidate or review-required snapshot can be rejected"
            )
        record = ApprovalRecord.create(
            candidate_snapshot_id=candidate.snapshot_id,
            decision="reject",
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
        )
        self.store_snapshot(candidate)
        self.append_approval(record)
        return record

    def latest_legacy_baseline(self) -> RuleSnapshot | None:
        snapshots = [
            snapshot
            for snapshot in self.iter_snapshots()
            if snapshot.status is RuleStatus.LEGACY_BASELINE
        ]
        if not snapshots:
            return None
        return max(snapshots, key=lambda snapshot: (snapshot.collected_at, snapshot.snapshot_id))


def build_collection_run(
    *,
    target_trading_day: date,
    started_at: datetime,
    completed_at: datetime,
    status: str,
    source_registry_sha256: str | None,
    snapshot_id: str | None,
    errors: Iterable[str],
    review_reasons: Iterable[str],
    document_ids: Iterable[str],
    previous_snapshot_id: str | None,
) -> dict[str, Any]:
    """Create a hash-identified collection run artifact without secrets."""

    _require_aware(started_at, "collection started_at")
    _require_aware(completed_at, "collection completed_at")
    if completed_at < started_at:
        raise ExchangeRuleArtifactError("collection completed_at precedes started_at")
    if status not in {"success", "review_required", "error", "skipped_closed_day"}:
        raise ExchangeRuleArtifactError("collection status is unsupported")
    registry_hash = (
        _require_digest(source_registry_sha256, "source_registry_sha256")
        if source_registry_sha256 is not None
        else None
    )
    if snapshot_id is not None:
        _require_digest(snapshot_id, "collection snapshot_id")
    if previous_snapshot_id is not None:
        _require_digest(previous_snapshot_id, "collection previous_snapshot_id")
    payload_without_id = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "target_trading_day": target_trading_day.isoformat(),
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "status": status,
        "source_registry_sha256": registry_hash,
        "snapshot_id": snapshot_id,
        "previous_snapshot_id": previous_snapshot_id,
        "document_ids": sorted({_require_digest(item, "document_id") for item in document_ids}),
        "errors": sorted({str(item)[:1000] for item in errors}),
        "review_reasons": sorted({str(item)[:1000] for item in review_reasons}),
    }
    payload = dict(payload_without_id)
    payload["run_id"] = _sha256(_canonical_json_bytes(payload_without_id))
    return payload


def _catalog_pair(value: object, label: str) -> tuple[str, str]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise ExchangeRuleArtifactError(f"{label} must contain exchange and product")
    exchange, product = value
    if not isinstance(exchange, str) or not exchange.strip():
        raise ExchangeRuleArtifactError(f"{label}.exchange must be non-empty")
    if not isinstance(product, str) or not product.strip():
        raise ExchangeRuleArtifactError(f"{label}.product must be non-empty")
    return exchange.strip().upper(), product.strip().upper()


def _excluded_option_payload(value: object, expected_futures: set[tuple[str, str]]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExchangeRuleArtifactError("excluded option must be an object")
    exchange, product = _catalog_pair(
        (value.get("exchange"), value.get("code")), "excluded option"
    )
    if (exchange, product) in expected_futures:
        raise ExchangeRuleArtifactError("excluded option cannot be a futures product")
    if value.get("instrument_kind") != "option":
        raise ExchangeRuleArtifactError("excluded option instrument_kind must be option")
    if value.get("status") != "excluded_option":
        raise ExchangeRuleArtifactError("excluded option status must be excluded_option")
    for field in ("name_zh", "source_url", "reason"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ExchangeRuleArtifactError(f"excluded option {field} must be non-empty")
    return {
        "exchange": exchange,
        "code": product,
        "name_zh": value["name_zh"].strip(),
        "instrument_kind": "option",
        "status": "excluded_option",
        "source_url": value["source_url"].strip(),
        "reason": value["reason"].strip(),
    }


def _normalize_catalog_run_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Normalize the v2 ``collected_futures`` spelling without rewriting it."""

    if "processed_futures" in payload:
        return dict(payload), False
    if "collected_futures" not in payload:
        raise ExchangeRuleArtifactError(
            "catalog collection processed_futures must be a list"
        )
    normalized = dict(payload)
    normalized["processed_futures"] = normalized.pop("collected_futures")
    return normalized, True


def build_catalog_collection_run(
    *,
    target_trading_day: date,
    started_at: datetime,
    completed_at: datetime,
    catalog_id: str,
    exchange_statuses: Mapping[str, str],
    expected_futures: Iterable[tuple[str, str]],
    processed_futures: Iterable[tuple[str, str]],
    closed_futures: Iterable[tuple[str, str]],
    excluded_options: Iterable[Mapping[str, Any]],
    snapshot_id: str | None,
    errors: Iterable[str],
    review_reasons: Iterable[str],
    document_ids: Iterable[str],
    previous_snapshot_id: str | None,
) -> dict[str, Any]:
    """Create an immutable v2 all-futures coverage manifest.

    Every expected product is explicitly processed, closed, or unresolved. A
    processed product can still contain review-required fields; it is never
    mislabeled as a verified collection result.
    """

    _require_aware(started_at, "catalog collection started_at")
    _require_aware(completed_at, "catalog collection completed_at")
    if completed_at < started_at:
        raise ExchangeRuleArtifactError("catalog collection completed_at precedes started_at")
    catalog_digest = _require_digest(catalog_id, "catalog_id")
    if snapshot_id is not None:
        _require_digest(snapshot_id, "catalog collection snapshot_id")
    if previous_snapshot_id is not None:
        _require_digest(previous_snapshot_id, "catalog collection previous_snapshot_id")
    if not isinstance(exchange_statuses, Mapping):
        raise ExchangeRuleArtifactError("catalog collection exchange statuses are invalid")
    normalized_statuses = {}
    supported_statuses = {"collected", "closed", "partial", "error", "review_required"}
    for exchange, exchange_status in exchange_statuses.items():
        if not isinstance(exchange, str) or not exchange.strip():
            raise ExchangeRuleArtifactError("catalog collection exchange status exchange is invalid")
        if exchange_status not in supported_statuses:
            raise ExchangeRuleArtifactError("catalog collection exchange statuses are invalid")
        normalized_statuses[exchange.strip().upper()] = exchange_status
    expected = sorted({_catalog_pair(pair, "expected future") for pair in expected_futures})
    processed = sorted({_catalog_pair(pair, "processed future") for pair in processed_futures})
    closed = sorted({_catalog_pair(pair, "closed future") for pair in closed_futures})
    if not expected:
        raise ExchangeRuleArtifactError("catalog collection requires expected futures")
    expected_set = set(expected)
    if set(normalized_statuses) != {exchange for exchange, _ in expected}:
        raise ExchangeRuleArtifactError(
            "catalog collection exchange statuses must cover exactly the expected exchanges"
        )
    unexpected = sorted((set(processed) | set(closed)) - expected_set)
    if unexpected:
        raise ExchangeRuleArtifactError(
            f"catalog collection contains products outside the futures universe: {unexpected[:20]}"
        )
    overlap = sorted(set(processed) & set(closed))
    if overlap:
        raise ExchangeRuleArtifactError(
            f"catalog collection cannot mark futures processed and closed: {overlap[:20]}"
        )
    unresolved = sorted(expected_set - set(processed) - set(closed))
    for exchange, exchange_status in normalized_statuses.items():
        exchange_expected = {pair for pair in expected_set if pair[0] == exchange}
        exchange_processed = {pair for pair in processed if pair[0] == exchange}
        exchange_closed = {pair for pair in closed if pair[0] == exchange}
        if exchange_status == "closed" and exchange_closed != exchange_expected:
            raise ExchangeRuleArtifactError(
                "closed exchange status must close every expected future"
            )
        if exchange_status == "collected" and exchange_processed != exchange_expected:
            raise ExchangeRuleArtifactError(
                "collected exchange status must process every expected future"
            )
        if exchange_status == "error" and exchange_processed:
            raise ExchangeRuleArtifactError(
                "error exchange status cannot mark futures processed"
            )
        if exchange_status == "partial" and not exchange_processed:
            raise ExchangeRuleArtifactError(
                "partial exchange status must process at least one expected future"
            )
        if exchange_status == "review_required" and not exchange_processed:
            raise ExchangeRuleArtifactError(
                "review-required exchange status must process expected futures"
            )
    option_payloads = sorted(
        (_excluded_option_payload(item, expected_set) for item in excluded_options),
        key=lambda item: (item["exchange"], item["code"]),
    )
    option_keys = [(item["exchange"], item["code"]) for item in option_payloads]
    if len(set(option_keys)) != len(option_keys):
        raise ExchangeRuleArtifactError("catalog collection excluded options must be unique")
    normalized_errors = sorted({str(item)[:1000] for item in errors})
    normalized_reviews = sorted({str(item)[:1000] for item in review_reasons})
    if len(closed) == len(expected):
        status = "skipped_closed_day"
    elif not processed and not closed:
        status = "error"
    elif not unresolved and not normalized_errors and not normalized_reviews:
        status = "success"
    else:
        status = "review_required"
    payload_without_id = {
        "schema_version": CATALOG_RUN_SCHEMA_VERSION,
        "target_trading_day": target_trading_day.isoformat(),
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "status": status,
        "catalog_id": catalog_digest,
        "exchange_statuses": dict(sorted(normalized_statuses.items())),
        "expected_futures": [
            {"exchange": exchange, "product": product}
            for exchange, product in expected
        ],
        "processed_futures": [
            {"exchange": exchange, "product": product}
            for exchange, product in processed
        ],
        "closed_futures": [
            {"exchange": exchange, "product": product}
            for exchange, product in closed
        ],
        "unresolved_futures": [
            {"exchange": exchange, "product": product}
            for exchange, product in unresolved
        ],
        "excluded_options": option_payloads,
        "snapshot_id": snapshot_id,
        "previous_snapshot_id": previous_snapshot_id,
        "document_ids": sorted({_require_digest(item, "document_id") for item in document_ids}),
        "errors": normalized_errors,
        "review_reasons": normalized_reviews,
    }
    payload = dict(payload_without_id)
    payload["run_id"] = _sha256(_canonical_json_bytes(payload_without_id))
    return payload


def source_registry_sha256(path: str | Path) -> str:
    try:
        return _sha256(Path(path).read_bytes())
    except OSError as exc:
        raise ExchangeRuleArtifactError(f"cannot hash source registry: {path}") from exc
