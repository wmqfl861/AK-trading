"""Immutable exchange-rule snapshots and conservative account estimates.

Official exchange facts, candidate parsing results, and user-specific estimates
are intentionally separate. Missing or ambiguous exchange rules stay explicit;
they are never converted to a numeric default.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol


MONEY_QUANTUM = Decimal("0.01")
RATE_QUANTUM = Decimal("0.00000001")
ACCOUNT_FEE_ADD_PER_LOT = Decimal("0.01")
ACCOUNT_MARGIN_RATE_ADD = Decimal("0.03")


class ExchangeRuleError(ValueError):
    """Raised when an exchange-rule artifact is malformed or unsafe to use."""


class RuleStatus(str, Enum):
    """Review state for a snapshot or individual rule item."""

    CURRENT_APPROVED = "current_approved"
    LEGACY_BASELINE = "legacy_baseline"
    CANDIDATE = "candidate"
    REVIEW_REQUIRED = "review_required"
    REJECTED = "rejected"
    STALE = "stale"
    UNCHANGED = "unchanged"
    UNKNOWN = "unknown"
    ERROR = "error"


class RuleCategory(str, Enum):
    FEE = "fee"
    MARGIN = "margin"
    LOCK_MARGIN = "lock_margin"
    LIMIT = "limit"
    SESSION = "session"
    DELIVERY = "delivery"
    RISK = "risk"


class RuleValueKind(str, Enum):
    DECIMAL = "decimal"
    TEXT = "text"
    EXPLICIT_ZERO = "explicit_zero"
    UNKNOWN = "unknown"


class LockCalculationMethod(str, Enum):
    LARGER_SIDE_ONLY = "larger_side_only"
    BOTH_SIDES_REDUCED = "both_sides_reduced"
    TEXT_ONLY = "text_only"
    UNKNOWN = "unknown"


class FeeBasis(str, Enum):
    CNY_PER_LOT = "cny_per_lot"
    RATE_PER_NOTIONAL = "rate_per_notional"


RuleIdentity = tuple[str, str, str | None, str, str]


def rule_identity_sort_key(identity: RuleIdentity) -> tuple[str, str, str, str, str]:
    """Sort field identities while keeping product-level contracts null."""

    exchange, product, contract, category, field = identity
    return exchange, product, contract or "", category, field


class OfficialDocumentFetcher(Protocol):
    """Fetch a source document after the collector has verified its domain."""

    def fetch(self, url: str) -> "FetchedDocument": ...


class ExchangeRuleParser(Protocol):
    """Conservatively extract candidate rule items from one official document."""

    def parse(
        self,
        document: "SourceDocument",
        *,
        product: str,
        contract: str | None,
    ) -> tuple["RuleItem", ...]: ...


def _decimal(value: object, field_name: str, *, allow_zero: bool = True) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ExchangeRuleError(
            f"{field_name} must be a decimal string, integer, or Decimal"
        )
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExchangeRuleError(f"{field_name} is not a decimal") from exc
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        qualifier = "positive" if not allow_zero else "non-negative"
        raise ExchangeRuleError(f"{field_name} must be a finite {qualifier} decimal")
    return result


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExchangeRuleError(f"{field_name} must be a non-empty string")
    return value.strip()


def _iso_date(value: object, field_name: str, *, required: bool = False) -> date | None:
    if value is None:
        if required:
            raise ExchangeRuleError(f"{field_name} is required")
        return None
    if not isinstance(value, str):
        raise ExchangeRuleError(f"{field_name} must be an ISO date or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ExchangeRuleError(f"{field_name} must be YYYY-MM-DD") from exc


def _iso_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ExchangeRuleError(f"{field_name} must be an ISO datetime")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ExchangeRuleError(f"{field_name} must be an ISO datetime") from exc
    if result.tzinfo is None:
        raise ExchangeRuleError(f"{field_name} must include a timezone offset")
    return result


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
        raise ExchangeRuleError("rule artifact contains a non-JSON value") from exc


def canonical_sha256(value: Mapping[str, Any]) -> str:
    """Return the SHA-256 identity for a canonical JSON mapping."""

    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class FetchedDocument:
    """Normalized result returned by a local official-document fetcher."""

    url: str
    title: str
    content: bytes
    content_type: str
    retrieved_at: datetime
    publication_date: date | None = None
    effective_date: date | None = None

    def __post_init__(self) -> None:
        _nonempty(self.url, "fetched_document.url")
        _nonempty(self.title, "fetched_document.title")
        _nonempty(self.content_type, "fetched_document.content_type")
        if not self.content:
            raise ExchangeRuleError("fetched_document.content must not be empty")
        if self.retrieved_at.tzinfo is None:
            raise ExchangeRuleError("fetched_document.retrieved_at must be timezone-aware")


@dataclass(frozen=True)
class SourceDocument:
    """Content-addressed official evidence retained before rule parsing."""

    document_id: str
    exchange: str
    url: str
    title: str
    content_type: str
    content_sha256: str
    content_path: str
    retrieved_at: datetime
    content: str
    raw_content: bytes
    publication_date: date | None = None
    effective_date: date | None = None

    def __post_init__(self) -> None:
        _nonempty(self.exchange, "source_document.exchange")
        _nonempty(self.url, "source_document.url")
        _nonempty(self.title, "source_document.title")
        _nonempty(self.content_type, "source_document.content_type")
        _nonempty(self.content_path, "source_document.content_path")
        if self.retrieved_at.tzinfo is None:
            raise ExchangeRuleError("source_document.retrieved_at must be timezone-aware")
        for field_name, digest in (
            ("source_document.document_id", self.document_id),
            ("source_document.content_sha256", self.content_sha256),
        ):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ExchangeRuleError(f"{field_name} must be a lowercase SHA-256 digest")
        expected_id = canonical_sha256(
            {
                "schema_version": 1,
                "exchange": self.exchange.upper(),
                "url": self.url,
                "content_sha256": self.content_sha256,
            }
        )
        if self.document_id != expected_id:
            raise ExchangeRuleError("source_document.document_id does not match identity")
        if self.raw_content:
            digest = hashlib.sha256(self.raw_content).hexdigest()
            if digest != self.content_sha256:
                raise ExchangeRuleError("source_document content SHA-256 does not match bytes")

    @classmethod
    def from_fetched(
        cls,
        fetched: FetchedDocument,
        *,
        exchange: str,
        content_path: str,
    ) -> "SourceDocument":
        digest = hashlib.sha256(fetched.content).hexdigest()
        normalized_exchange = _nonempty(exchange, "source_document.exchange").upper()
        try:
            content = fetched.content.decode("utf-8")
        except UnicodeDecodeError:
            content = fetched.content.decode("utf-8", errors="replace")
        return cls(
            document_id=canonical_sha256(
                {
                    "schema_version": 1,
                    "exchange": normalized_exchange,
                    "url": _nonempty(fetched.url, "source_document.url"),
                    "content_sha256": digest,
                }
            ),
            exchange=normalized_exchange,
            url=_nonempty(fetched.url, "source_document.url"),
            title=_nonempty(fetched.title, "source_document.title"),
            content_type=_nonempty(fetched.content_type, "source_document.content_type"),
            content_sha256=digest,
            content_path=_nonempty(content_path, "source_document.content_path"),
            retrieved_at=fetched.retrieved_at,
            content=content,
            raw_content=fetched.content,
            publication_date=fetched.publication_date,
            effective_date=fetched.effective_date,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "exchange": self.exchange,
            "url": self.url,
            "title": self.title,
            "content_type": self.content_type,
            "content_sha256": self.content_sha256,
            "content_path": self.content_path,
            "retrieved_at": self.retrieved_at.isoformat(),
            "publication_date": (
                self.publication_date.isoformat() if self.publication_date else None
            ),
            "effective_date": (
                self.effective_date.isoformat() if self.effective_date else None
            ),
        }


@dataclass(frozen=True)
class LockMarginRule:
    """Official same-contract lock-margin rule, or an explicit unknown state."""

    rate: Decimal | None
    price_basis: str | None
    formula_text: str
    calculation_method: LockCalculationMethod
    status: RuleStatus

    def __post_init__(self) -> None:
        if self.rate is not None:
            rate = _decimal(self.rate, "lock_margin.rate")
            if rate > 1:
                raise ExchangeRuleError("lock_margin.rate must be a ratio, not a percent")
        if self.calculation_method in {
            LockCalculationMethod.LARGER_SIDE_ONLY,
            LockCalculationMethod.BOTH_SIDES_REDUCED,
        } and (self.rate is None or self.price_basis is None):
            raise ExchangeRuleError(
                "computable lock-margin rules require rate and price_basis"
            )
        if self.status in {RuleStatus.UNKNOWN, RuleStatus.REVIEW_REQUIRED} and self.rate is not None:
            raise ExchangeRuleError(
                "unknown or review-required lock-margin rules cannot carry a numeric rate"
            )
        _nonempty(self.formula_text, "lock_margin.formula_text")

    def as_dict(self) -> dict[str, Any]:
        return {
            "rate": _decimal_text(self.rate),
            "price_basis": self.price_basis,
            "formula_text": self.formula_text,
            "calculation_method": self.calculation_method.value,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class RuleItem:
    """One attributable field from an official exchange rule document."""

    exchange: str
    product: str
    contract: str | None
    category: RuleCategory
    field: str
    value_kind: RuleValueKind
    status: RuleStatus
    value: Decimal | None
    unit: str | None
    applies_to: str
    effective_from: date | None
    effective_through: date | None
    source_document_id: str | None
    source_url: str | None
    excerpt: str
    note: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.exchange, "rule_item.exchange")
        _nonempty(self.product, "rule_item.product")
        _nonempty(self.field, "rule_item.field")
        _nonempty(self.applies_to, "rule_item.applies_to")
        _nonempty(self.excerpt, "rule_item.excerpt")
        if self.value is not None:
            numeric = _decimal(self.value, "rule_item.value")
            if self.value_kind in {RuleValueKind.TEXT, RuleValueKind.UNKNOWN}:
                raise ExchangeRuleError("text or unknown rule items cannot carry a numeric value")
            if self.value_kind is RuleValueKind.EXPLICIT_ZERO and numeric != 0:
                raise ExchangeRuleError("explicit-zero rule items must have value zero")
        elif self.value_kind in {RuleValueKind.DECIMAL, RuleValueKind.EXPLICIT_ZERO}:
            raise ExchangeRuleError("numeric rule items require a value")
        if self.effective_from and self.effective_through:
            if self.effective_through < self.effective_from:
                raise ExchangeRuleError("rule_item effective_through precedes effective_from")
        if self.source_document_id is None and self.source_url is not None:
            raise ExchangeRuleError("rule_item.source_url requires source_document_id")

    @property
    def identity_key(self) -> tuple[str, str, str | None, str, str]:
        return (
            self.exchange.upper(),
            self.product.upper(),
            self.contract,
            self.category.value,
            self.field,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange.upper(),
            "product": self.product.upper(),
            "contract": self.contract,
            "category": self.category.value,
            "field": self.field,
            "value_kind": self.value_kind.value,
            "status": self.status.value,
            "value": _decimal_text(self.value),
            "unit": self.unit,
            "applies_to": self.applies_to,
            "effective_from": (
                self.effective_from.isoformat() if self.effective_from else None
            ),
            "effective_through": (
                self.effective_through.isoformat() if self.effective_through else None
            ),
            "source_document_id": self.source_document_id,
            "source_url": self.source_url,
            "excerpt": self.excerpt,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RuleItem":
        if not isinstance(raw, Mapping):
            raise ExchangeRuleError("rule item must be an object")
        try:
            category = RuleCategory(raw.get("category"))
            value_kind = RuleValueKind(raw.get("value_kind"))
            status = RuleStatus(raw.get("status"))
        except (TypeError, ValueError) as exc:
            raise ExchangeRuleError("rule item has an invalid enum") from exc
        raw_value = raw.get("value")
        value = None if raw_value is None else _decimal(raw_value, "rule_item.value")
        raw_contract = raw.get("contract")
        raw_unit = raw.get("unit")
        raw_document_id = raw.get("source_document_id")
        raw_url = raw.get("source_url")
        raw_note = raw.get("note")
        return cls(
            exchange=_nonempty(raw.get("exchange"), "rule_item.exchange").upper(),
            product=_nonempty(raw.get("product"), "rule_item.product").upper(),
            contract=raw_contract if isinstance(raw_contract, str) else None,
            category=category,
            field=_nonempty(raw.get("field"), "rule_item.field"),
            value_kind=value_kind,
            status=status,
            value=value,
            unit=raw_unit if isinstance(raw_unit, str) else None,
            applies_to=_nonempty(raw.get("applies_to"), "rule_item.applies_to"),
            effective_from=_iso_date(raw.get("effective_from"), "rule_item.effective_from"),
            effective_through=_iso_date(
                raw.get("effective_through"), "rule_item.effective_through"
            ),
            source_document_id=(
                raw_document_id if isinstance(raw_document_id, str) else None
            ),
            source_url=raw_url if isinstance(raw_url, str) else None,
            excerpt=_nonempty(raw.get("excerpt"), "rule_item.excerpt"),
            note=raw_note if isinstance(raw_note, str) else None,
        )


@dataclass(frozen=True)
class RuleSnapshot:
    """Canonical, content-addressed collection result for one target trading day."""

    snapshot_id: str
    target_trading_day: date
    collected_at: datetime
    status: RuleStatus
    origin: str
    documents: tuple[SourceDocument, ...]
    items: tuple[RuleItem, ...]
    review_reason: str | None = None
    parent_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        if len(self.snapshot_id) != 64 or any(
            character not in "0123456789abcdef" for character in self.snapshot_id
        ):
            raise ExchangeRuleError("snapshot_id must be a lowercase SHA-256 digest")
        if self.collected_at.tzinfo is None:
            raise ExchangeRuleError("snapshot.collected_at must be timezone-aware")
        _nonempty(self.origin, "snapshot.origin")
        if self.status in {RuleStatus.CANDIDATE, RuleStatus.REVIEW_REQUIRED}:
            _nonempty(self.review_reason, "snapshot.review_reason")
        if self.parent_snapshot_id is not None and (
            len(self.parent_snapshot_id) != 64
            or any(character not in "0123456789abcdef" for character in self.parent_snapshot_id)
        ):
            raise ExchangeRuleError("parent_snapshot_id must be a lowercase SHA-256 digest")

    def content_without_id(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "target_trading_day": self.target_trading_day.isoformat(),
            "collected_at": self.collected_at.isoformat(),
            "status": self.status.value,
            "origin": self.origin,
            "documents": [document.as_dict() for document in self.documents],
            "items": [item.as_dict() for item in self.items],
            "review_reason": self.review_reason,
            "parent_snapshot_id": self.parent_snapshot_id,
        }

    def as_dict(self) -> dict[str, Any]:
        result = self.content_without_id()
        result["snapshot_id"] = self.snapshot_id
        result["content_sha256"] = self.snapshot_id
        return result

    @classmethod
    def create(
        cls,
        *,
        target_trading_day: date,
        collected_at: datetime,
        status: RuleStatus,
        origin: str,
        documents: Iterable[SourceDocument],
        items: Iterable[RuleItem],
        review_reason: str | None = None,
        parent_snapshot_id: str | None = None,
    ) -> "RuleSnapshot":
        ordered_documents = tuple(sorted(documents, key=lambda item: item.document_id))
        ordered_items = tuple(
            sorted(items, key=lambda item: rule_identity_sort_key(item.identity_key))
        )
        document_ids = [document.document_id for document in ordered_documents]
        if len(set(document_ids)) != len(document_ids):
            raise ExchangeRuleError("snapshot documents must have unique identities")
        item_keys = [item.identity_key for item in ordered_items]
        if len(set(item_keys)) != len(item_keys):
            raise ExchangeRuleError("snapshot rule items must have unique field identities")
        known_document_ids = set(document_ids)
        for item in ordered_items:
            if (
                item.source_document_id is not None
                and item.source_document_id not in known_document_ids
            ):
                raise ExchangeRuleError(
                    "snapshot rule item references a document outside the snapshot"
                )
        payload = {
            "schema_version": 1,
            "target_trading_day": target_trading_day.isoformat(),
            "collected_at": collected_at.isoformat(),
            "status": status.value,
            "origin": origin,
            "documents": [document.as_dict() for document in ordered_documents],
            "items": [item.as_dict() for item in ordered_items],
            "review_reason": review_reason,
            "parent_snapshot_id": parent_snapshot_id,
        }
        return cls(
            snapshot_id=canonical_sha256(payload),
            target_trading_day=target_trading_day,
            collected_at=collected_at,
            status=status,
            origin=origin,
            documents=ordered_documents,
            items=ordered_items,
            review_reason=review_reason,
            parent_snapshot_id=parent_snapshot_id,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RuleSnapshot":
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            raise ExchangeRuleError("unsupported exchange-rule snapshot schema_version")
        try:
            status = RuleStatus(raw.get("status"))
        except (TypeError, ValueError) as exc:
            raise ExchangeRuleError("snapshot.status is invalid") from exc
        raw_documents = raw.get("documents")
        raw_items = raw.get("items")
        if not isinstance(raw_documents, list) or not isinstance(raw_items, list):
            raise ExchangeRuleError("snapshot documents and items must be lists")
        documents: list[SourceDocument] = []
        for raw_document in raw_documents:
            if not isinstance(raw_document, Mapping):
                raise ExchangeRuleError("source document must be an object")
            digest = _nonempty(raw_document.get("content_sha256"), "content_sha256")
            documents.append(
                SourceDocument(
                    document_id=_nonempty(raw_document.get("document_id"), "document_id"),
                    exchange=_nonempty(raw_document.get("exchange"), "exchange").upper(),
                    url=_nonempty(raw_document.get("url"), "url"),
                    title=_nonempty(raw_document.get("title"), "title"),
                    content_type=_nonempty(raw_document.get("content_type"), "content_type"),
                    content_sha256=digest,
                    content_path=_nonempty(raw_document.get("content_path"), "content_path"),
                    retrieved_at=_iso_datetime(raw_document.get("retrieved_at"), "retrieved_at"),
                    content="",
                    raw_content=b"",
                    publication_date=_iso_date(
                        raw_document.get("publication_date"), "publication_date"
                    ),
                    effective_date=_iso_date(
                        raw_document.get("effective_date"), "effective_date"
                    ),
                )
            )
        target_day = _iso_date(
            raw.get("target_trading_day"), "target_trading_day", required=True
        )
        assert target_day is not None
        snapshot = cls(
            snapshot_id=_nonempty(raw.get("snapshot_id"), "snapshot_id"),
            target_trading_day=target_day,
            collected_at=_iso_datetime(raw.get("collected_at"), "collected_at"),
            status=status,
            origin=_nonempty(raw.get("origin"), "origin"),
            documents=tuple(documents),
            items=tuple(RuleItem.from_dict(item) for item in raw_items),
            review_reason=(
                raw.get("review_reason") if isinstance(raw.get("review_reason"), str) else None
            ),
            parent_snapshot_id=(
                raw.get("parent_snapshot_id")
                if isinstance(raw.get("parent_snapshot_id"), str)
                else None
            ),
        )
        document_ids = [document.document_id for document in snapshot.documents]
        if len(set(document_ids)) != len(document_ids):
            raise ExchangeRuleError("snapshot documents must have unique identities")
        item_keys = [item.identity_key for item in snapshot.items]
        if len(set(item_keys)) != len(item_keys):
            raise ExchangeRuleError("snapshot rule items must have unique field identities")
        known_document_ids = set(document_ids)
        for item in snapshot.items:
            if (
                item.source_document_id is not None
                and item.source_document_id not in known_document_ids
            ):
                raise ExchangeRuleError(
                    "snapshot rule item references a document outside the snapshot"
                )
        expected = canonical_sha256(snapshot.content_without_id())
        if expected != snapshot.snapshot_id or raw.get("content_sha256") != snapshot.snapshot_id:
            raise ExchangeRuleError("snapshot content SHA-256 does not match payload")
        return snapshot


class RuleScopeKind(str, Enum):
    EXCHANGE = "exchange"
    PRODUCT = "product"
    CONTRACT = "contract"


@dataclass(frozen=True)
class RuleScope:
    """A reviewable exchange, product, or contract rule scope."""

    kind: RuleScopeKind
    exchange: str
    product: str | None = None
    contract: str | None = None

    def __post_init__(self) -> None:
        exchange = _nonempty(self.exchange, "rule_scope.exchange").upper()
        if self.kind is RuleScopeKind.EXCHANGE:
            if self.product is not None or self.contract is not None:
                raise ExchangeRuleError(
                    "exchange rule scopes must not name a product or contract"
                )
            product = None
            contract = None
        elif self.kind is RuleScopeKind.PRODUCT:
            product = _nonempty(self.product, "rule_scope.product").upper()
            if self.contract is not None:
                raise ExchangeRuleError("product rule scopes must not name a contract")
            contract = None
        elif self.kind is RuleScopeKind.CONTRACT:
            product = _nonempty(self.product, "rule_scope.product").upper()
            contract = _nonempty(self.contract, "rule_scope.contract")
        else:
            raise ExchangeRuleError("rule_scope.kind is invalid")
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "product", product)
        object.__setattr__(self, "contract", contract)

    @property
    def identity(self) -> tuple[str, str, str | None, str | None]:
        return (
            self.kind.value,
            self.exchange.upper(),
            self.product.upper() if self.product else None,
            self.contract,
        )

    def contains(self, item: RuleItem) -> bool:
        if item.exchange.upper() != self.exchange.upper():
            return False
        if self.kind is RuleScopeKind.EXCHANGE:
            return True
        if item.product.upper() != (self.product or "").upper():
            return False
        if self.kind is RuleScopeKind.PRODUCT:
            return True
        return item.contract == self.contract

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "exchange": self.exchange.upper(),
            "product": self.product.upper() if self.product else None,
            "contract": self.contract,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RuleScope":
        if not isinstance(raw, Mapping):
            raise ExchangeRuleError("rule scope must be an object")
        try:
            kind = RuleScopeKind(raw.get("kind"))
        except (TypeError, ValueError) as exc:
            raise ExchangeRuleError("rule scope kind is invalid") from exc
        product = raw.get("product")
        contract = raw.get("contract")
        return cls(
            kind=kind,
            exchange=_nonempty(raw.get("exchange"), "rule_scope.exchange").upper(),
            product=product.upper() if isinstance(product, str) and product.strip() else None,
            contract=contract if isinstance(contract, str) and contract.strip() else None,
        )


@dataclass(frozen=True)
class RuleChange:
    identity: tuple[str, str, str | None, str, str]
    change_type: str
    previous: RuleItem | None
    current: RuleItem | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity": {
                "exchange": self.identity[0],
                "product": self.identity[1],
                "contract": self.identity[2],
                "category": self.identity[3],
                "field": self.identity[4],
            },
            "change_type": self.change_type,
            "previous": self.previous.as_dict() if self.previous else None,
            "current": self.current.as_dict() if self.current else None,
        }


def _value_comparison_payload(item: RuleItem) -> tuple[object, ...]:
    return (
        item.value_kind,
        item.value,
        item.unit,
        item.applies_to,
        item.effective_from,
        item.effective_through,
    )


def _evidence_comparison_payload(item: RuleItem) -> tuple[object, ...]:
    return (
        item.source_document_id,
        item.source_url,
        item.excerpt,
        item.note,
    )


def diff_snapshots(
    previous: RuleSnapshot | None, current: RuleSnapshot
) -> tuple[RuleChange, ...]:
    """Return a field-level diff without mistaking refreshed evidence for values.

    ``changed`` means a rule value, unit, applicability, or effective interval
    changed. ``review_status_changed`` calls out an unresolved state transition.
    ``evidence_changed`` means the official source/excerpt changed while the
    normalized field value stayed the same.
    """

    before = {item.identity_key: item for item in previous.items} if previous else {}
    after = {item.identity_key: item for item in current.items}
    changes: list[RuleChange] = []
    for identity in sorted(
        set(before) | set(after), key=rule_identity_sort_key
    ):
        left = before.get(identity)
        right = after.get(identity)
        if left is None:
            change_type = "added"
        elif right is None:
            change_type = "removed"
        elif _value_comparison_payload(left) != _value_comparison_payload(right):
            change_type = "changed"
        elif left.status is not right.status:
            change_type = "review_status_changed"
        elif _evidence_comparison_payload(left) != _evidence_comparison_payload(right):
            change_type = "evidence_changed"
        else:
            continue
        changes.append(RuleChange(identity, change_type, left, right))
    return tuple(changes)


def estimate_fee(
    exchange_fee: Decimal,
    *,
    lots: int = 1,
    per_lot_addition: Decimal = ACCOUNT_FEE_ADD_PER_LOT,
) -> Decimal:
    """Apply the user's CNY 0.01 per-lot per-charged-direction estimate."""

    if isinstance(lots, bool) or not isinstance(lots, int) or lots <= 0:
        raise ExchangeRuleError("lots must be a positive integer")
    fee = _decimal(exchange_fee, "exchange_fee")
    addition = _decimal(per_lot_addition, "per_lot_addition")
    return (fee + addition * lots).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def estimate_margin_rate(
    exchange_margin_rate: Decimal,
    *,
    percentage_point_addition: Decimal = ACCOUNT_MARGIN_RATE_ADD,
) -> Decimal:
    """Apply an additive margin increase in percentage points, not a multiplier."""

    rate = _decimal(exchange_margin_rate, "exchange_margin_rate")
    addition = _decimal(percentage_point_addition, "percentage_point_addition")
    estimate = rate + addition
    if estimate > 1:
        raise ExchangeRuleError("estimated margin rate cannot exceed 100%")
    return estimate.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)


def compute_same_contract_lock_margin(
    rule: LockMarginRule,
    *,
    long_lots: int,
    short_lots: int,
    settlement_price: Decimal,
    contract_size: Decimal,
) -> Decimal | None:
    """Compute a lock estimate only for an explicit same-contract rule formula."""

    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (long_lots, short_lots)
    ):
        raise ExchangeRuleError("long_lots and short_lots must be non-negative integers")
    price = _decimal(settlement_price, "settlement_price", allow_zero=False)
    size = _decimal(contract_size, "contract_size", allow_zero=False)
    if rule.status not in {RuleStatus.CURRENT_APPROVED, RuleStatus.CANDIDATE}:
        return None
    if rule.rate is None:
        return None
    if rule.calculation_method is LockCalculationMethod.LARGER_SIDE_ONLY:
        charged_lots = max(long_lots, short_lots)
    elif rule.calculation_method is LockCalculationMethod.BOTH_SIDES_REDUCED:
        charged_lots = long_lots + short_lots
    else:
        return None
    return (price * size * rule.rate * charged_lots).quantize(
        MONEY_QUANTUM, rounding=ROUND_HALF_UP
    )


def legacy_reference_snapshot(
    path: str | Path,
    *,
    target_trading_day: date,
    collected_at: datetime,
    document_path: str,
) -> RuleSnapshot:
    """Map the legacy reference JSON into a non-approved baseline snapshot.

    The baseline is intentionally marked ``legacy_baseline``. It is useful for
    field-level diffs, but it is not evidence that stale transcribed values are
    safe to trade with.
    """

    from ak_trading.market_reference import FeeValueStatus, load_reference_book

    source = Path(path)
    raw_bytes = source.read_bytes()
    book = load_reference_book(source)
    fetched = FetchedDocument(
        url=f"file://{source.resolve().as_posix()}",
        title="Legacy market_reference.json baseline",
        content=raw_bytes,
        content_type="application/json",
        retrieved_at=collected_at,
    )
    document = SourceDocument.from_fetched(
        fetched,
        exchange="LEGACY",
        content_path=document_path,
    )
    items: list[RuleItem] = []
    for product in book.products:
        for action, component in product.fees.components.items():
            if component.status is FeeValueStatus.UNKNOWN:
                value_kind = RuleValueKind.UNKNOWN
                value = None
                status = RuleStatus.UNKNOWN
            elif component.status is FeeValueStatus.EXPLICIT_ZERO:
                value_kind = RuleValueKind.EXPLICIT_ZERO
                value = component.value
                status = RuleStatus.LEGACY_BASELINE
            else:
                value_kind = RuleValueKind.DECIMAL
                value = component.value
                status = RuleStatus.LEGACY_BASELINE
            items.append(
                RuleItem(
                    exchange=product.exchange,
                    product=product.code,
                    contract=product.research_contract,
                    category=RuleCategory.FEE,
                    field=action,
                    value_kind=value_kind,
                    status=status,
                    value=value,
                    unit=product.fees.basis.value,
                    applies_to=", ".join(product.fees.applicable_contracts),
                    effective_from=product.fees.effective_from,
                    effective_through=product.fees.must_reverify_before,
                    source_document_id=document.document_id,
                    source_url=document.url,
                    excerpt="Legacy transcribed fee value; requires official re-verification.",
                    note="values_verified is false in the legacy source.",
                )
            )
        items.extend(
            (
                RuleItem(
                    exchange=product.exchange,
                    product=product.code,
                    contract=product.research_contract,
                    category=RuleCategory.MARGIN,
                    field="ordinary_rate",
                    value_kind=RuleValueKind.DECIMAL,
                    status=RuleStatus.LEGACY_BASELINE,
                    value=product.margin.rate_of_notional,
                    unit="rate_of_notional",
                    applies_to=", ".join(product.margin.applicable_contracts),
                    effective_from=product.margin.effective_from,
                    effective_through=product.margin.must_reverify_before,
                    source_document_id=document.document_id,
                    source_url=document.url,
                    excerpt="Legacy transcribed ordinary margin rate; requires official re-verification.",
                    note=product.margin.note,
                ),
                RuleItem(
                    exchange=product.exchange,
                    product=product.code,
                    contract=product.research_contract,
                    category=RuleCategory.LOCK_MARGIN,
                    field="same_contract",
                    value_kind=RuleValueKind.UNKNOWN,
                    status=RuleStatus.REVIEW_REQUIRED,
                    value=None,
                    unit=None,
                    applies_to=product.research_contract,
                    effective_from=None,
                    effective_through=None,
                    source_document_id=document.document_id,
                    source_url=document.url,
                    excerpt="No same-contract lock-margin formula exists in the legacy reference source.",
                    note="Collect an official exchange rule before calculating lock margin.",
                ),
                RuleItem(
                    exchange=product.exchange,
                    product=product.code,
                    contract=product.research_contract,
                    category=RuleCategory.SESSION,
                    field="trading_sessions",
                    value_kind=RuleValueKind.TEXT,
                    status=RuleStatus.LEGACY_BASELINE,
                    value=None,
                    unit=None,
                    applies_to=product.research_contract,
                    effective_from=None,
                    effective_through=None,
                    source_document_id=document.document_id,
                    source_url=document.url,
                    excerpt=(
                        f"Day: {', '.join(product.day_sessions)}; "
                        f"Night: {', '.join(product.night_sessions)}"
                    ),
                    note="Legacy session transcription; special notices may supersede it.",
                ),
                RuleItem(
                    exchange=product.exchange,
                    product=product.code,
                    contract=product.research_contract,
                    category=RuleCategory.DELIVERY,
                    field="delivery_and_closeout",
                    value_kind=RuleValueKind.TEXT,
                    status=RuleStatus.LEGACY_BASELINE,
                    value=None,
                    unit=None,
                    applies_to=product.research_contract,
                    effective_from=None,
                    effective_through=product.last_trading_day,
                    source_document_id=document.document_id,
                    source_url=document.url,
                    excerpt=f"Legacy last trading day: {product.last_trading_day.isoformat()}",
                    note="Delivery rules and forced exit policies require official review.",
                ),
                RuleItem(
                    exchange=product.exchange,
                    product=product.code,
                    contract=product.research_contract,
                    category=RuleCategory.LIMIT,
                    field="price_and_position_limits",
                    value_kind=RuleValueKind.UNKNOWN,
                    status=RuleStatus.REVIEW_REQUIRED,
                    value=None,
                    unit=None,
                    applies_to=product.research_contract,
                    effective_from=None,
                    effective_through=None,
                    source_document_id=document.document_id,
                    source_url=document.url,
                    excerpt="No price-limit or position-limit rule exists in the legacy reference source.",
                ),
                RuleItem(
                    exchange=product.exchange,
                    product=product.code,
                    contract=product.research_contract,
                    category=RuleCategory.RISK,
                    field="risk_controls_and_forced_liquidation",
                    value_kind=RuleValueKind.UNKNOWN,
                    status=RuleStatus.REVIEW_REQUIRED,
                    value=None,
                    unit=None,
                    applies_to=product.research_contract,
                    effective_from=None,
                    effective_through=None,
                    source_document_id=document.document_id,
                    source_url=document.url,
                    excerpt="No forced-liquidation or temporary risk-control policy exists in the legacy reference source.",
                ),
            )
        )
    return RuleSnapshot.create(
        target_trading_day=target_trading_day,
        collected_at=collected_at,
        status=RuleStatus.LEGACY_BASELINE,
        origin="legacy_market_reference",
        documents=(document,),
        items=items,
        review_reason=None,
    )
