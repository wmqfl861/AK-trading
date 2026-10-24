"""Read-only local browser workbench for exchange-rule artifacts."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlparse

from ak_trading.exchange_rule_artifacts import (
    ApprovalRecord,
    ExchangeRuleArtifactError,
    ExchangeRuleArtifactStore,
)
from ak_trading.exchange_rules import (
    RuleChange,
    RuleItem,
    RuleSnapshot,
    RuleStatus,
    diff_snapshots,
    rule_identity_sort_key,
)
from ak_trading.futures_catalog import (
    FuturesCatalog,
    FuturesCatalogError,
    FuturesProduct,
    load_futures_catalog,
)


class WorkbenchError(RuntimeError):
    """Raised when the local workbench cannot read a valid artifact set."""


class WorkbenchRequestError(WorkbenchError):
    """Raised when a local workbench API request is malformed."""


class WorkbenchNotFoundError(WorkbenchError):
    """Raised when a requested workbench page has no matching catalog product."""


_CATEGORY_LABELS = {
    "fee": "Fees",
    "margin": "Ordinary margin",
    "lock_margin": "Lock margin",
    "limit": "Limits",
    "session": "Sessions",
    "delivery": "Delivery",
    "risk": "Risk controls",
}

_STATUS_LABELS = {
    "current_approved": "Current approved",
    "legacy_baseline": "Legacy baseline",
    "candidate": "Candidate",
    "review_required": "Review required",
    "rejected": "Rejected",
    "stale": "Stale",
    "unchanged": "Unchanged",
    "unknown": "Unknown",
    "error": "Error",
}

_DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "china_futures_catalog.json"
)

_REQUIRED_RULE_FIELDS = (
    ("fee", "open", "\u5f00\u4ed3\u624b\u7eed\u8d39"),
    ("fee", "close", "\u5e73\u4ed3\u624b\u7eed\u8d39"),
    ("fee", "close_today", "\u5e73\u4eca\u624b\u7eed\u8d39"),
    ("margin", "ordinary_rate", "\u4e00\u822c\u4fdd\u8bc1\u91d1\u7387"),
    ("lock_margin", "same_contract", "\u540c\u5408\u7ea6\u9501\u4ed3\u4fdd\u8bc1\u91d1"),
    ("limit", "price_and_position_limits", "\u6da8\u8dcc\u505c\u4e0e\u6301\u4ed3\u9650\u989d"),
    ("session", "trading_sessions", "\u4ea4\u6613\u65f6\u6bb5"),
    ("delivery", "delivery_and_closeout", "\u4ea4\u5272\u4e0e\u5e73\u4ed3"),
    ("risk", "risk_controls_and_forced_liquidation", "\u98ce\u9669\u63a7\u5236\u4e0e\u5f3a\u5e73"),
)

_PENDING_REVIEW = "\u5f85\u4eba\u5de5\u5ba1\u6838"
_UNKNOWN = "\u672a\u77e5"
_APPROVED = "\u5df2\u5ba1\u6838"
_EXTRACTED = "\u5df2\u63d0\u53d6"
_NO_VALUE = "\u672a\u83b7\u53d6\u5230\u6570\u503c"
_COVERAGE_LABELS = {
    "processed": "\u5df2\u91c7\u96c6",
    "closed": "\u4f11\u5e02",
    "unresolved": "\u672a\u89e3\u51b3",
    "unknown": _UNKNOWN,
}


def _sort_key_datetime(value: object) -> str:
    return value if isinstance(value, str) else ""


def _item_key(item: RuleItem) -> str:
    return "|".join(
        (
            item.identity_key[0],
            item.identity_key[1],
            item.identity_key[2] or "",
            item.identity_key[3],
            item.identity_key[4],
        )
    )


def _item_value(item: RuleItem | None) -> str | None:
    if item is None:
        return None
    if item.value is not None:
        return format(item.value, "f")
    return item.excerpt


def _change_map(changes: tuple[RuleChange, ...]) -> dict[tuple[str, str, str | None, str, str], RuleChange]:
    return {change.identity: change for change in changes}


def _document_dict(document) -> dict[str, Any]:
    result = document.as_dict()
    result["content_available"] = bool(document.raw_content)
    return result


def _approval_dict(record: ApprovalRecord) -> dict[str, Any]:
    return record.as_dict()


def _scope_approval_dict(record) -> dict[str, Any]:
    """Project scope records without dropping their field and catalog provenance."""

    return record.as_dict()


def _summary_page(items: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    return items[:limit]


def _catalog_run_summary(run: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if run is None:
        return None
    expected = run.get("expected_futures", [])
    processed = run.get("processed_futures", [])
    closed = run.get("closed_futures", [])
    unresolved = run.get("unresolved_futures", [])
    options = run.get("excluded_options", [])
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "target_trading_day": run.get("target_trading_day"),
        "completed_at": run.get("completed_at"),
        "catalog_id": run.get("catalog_id"),
        "snapshot_id": run.get("snapshot_id"),
        "expected_futures": len(expected) if isinstance(expected, list) else 0,
        "processed_futures": len(processed) if isinstance(processed, list) else 0,
        "closed_futures": len(closed) if isinstance(closed, list) else 0,
        "unresolved_futures": len(unresolved) if isinstance(unresolved, list) else 0,
        "excluded_options": len(options) if isinstance(options, list) else 0,
        "exchange_statuses": dict(run.get("exchange_statuses", {})),
        "error_count": len(run.get("errors", [])),
        "review_reason_count": len(run.get("review_reasons", [])),
    }


def _catalog_coverage_rows(run: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Turn one validated manifest into deterministic exchange coverage rows."""

    if run is None:
        return []
    exchanges = sorted(str(exchange).upper() for exchange in run["exchange_statuses"])
    pairs_by_field: dict[str, set[tuple[str, str]]] = {}
    for field in ("expected_futures", "processed_futures", "closed_futures", "unresolved_futures"):
        pairs_by_field[field] = {
            (str(item["exchange"]).upper(), str(item["product"]).upper())
            for item in run[field]
        }
    options = run["excluded_options"]
    rows = []
    for exchange in exchanges:
        expected = sorted(product for item_exchange, product in pairs_by_field["expected_futures"] if item_exchange == exchange)
        processed = sorted(product for item_exchange, product in pairs_by_field["processed_futures"] if item_exchange == exchange)
        closed = sorted(product for item_exchange, product in pairs_by_field["closed_futures"] if item_exchange == exchange)
        unresolved = sorted(product for item_exchange, product in pairs_by_field["unresolved_futures"] if item_exchange == exchange)
        excluded_options = sorted(
            str(item["code"]).upper()
            for item in options
            if str(item["exchange"]).upper() == exchange
        )
        rows.append(
            {
                "exchange": exchange,
                "status": run["exchange_statuses"][exchange],
                "expected_futures": len(expected),
                "processed_futures": len(processed),
                "closed_futures": len(closed),
                "unresolved_futures": len(unresolved),
                "excluded_options": len(excluded_options),
                "expected_products": expected,
                "processed_products": processed,
                "closed_products": closed,
                "unresolved_products": unresolved,
                "excluded_option_codes": excluded_options,
            }
        )
    return rows


def _catalog_coverage_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the initial state to exchange-level coverage counts only."""

    fields = (
        "exchange",
        "status",
        "expected_futures",
        "processed_futures",
        "closed_futures",
        "unresolved_futures",
        "excluded_options",
        "scope_current_fields",
    )
    return [{field: row[field] for field in fields} for row in rows]


def _scope_current_field_identities(
    pointers: tuple[dict[str, Any], ...],
) -> set[tuple[str, str, str | None, str, str]]:
    identities: set[tuple[str, str, str | None, str, str]] = set()
    for pointer in pointers:
        for entry in pointer["field_approvals"]:
            field = entry["field"]
            identities.add(
                (
                    str(field["exchange"]).upper(),
                    str(field["product"]).upper(),
                    field.get("contract"),
                    str(field["category"]),
                    str(field["field"]),
                )
            )
    return identities


def _scope_current_field_keys(pointers: tuple[dict[str, Any], ...]) -> set[str]:
    return {
        "|".join((exchange, product, contract or "", category, field))
        for exchange, product, contract, category, field in _scope_current_field_identities(
            pointers
        )
    }


def _scope_current_entries(
    store: ExchangeRuleArtifactStore,
    pointers: tuple[dict[str, Any], ...],
) -> dict[tuple[str, str, str | None, str, str], tuple[dict[str, Any], ...]]:
    """Resolve each validated scope-pointer field to its approved candidate item."""

    snapshots: dict[str, RuleSnapshot] = {}
    entries: dict[tuple[str, str, str | None, str, str], list[dict[str, Any]]] = {}
    for pointer in pointers:
        for approval in pointer["field_approvals"]:
            field = approval["field"]
            identity = (
                str(field["exchange"]).upper(),
                str(field["product"]).upper(),
                field.get("contract"),
                str(field["category"]),
                str(field["field"]),
            )
            snapshot_id = str(approval["candidate_snapshot_id"])
            snapshot = snapshots.get(snapshot_id)
            if snapshot is None:
                snapshot = store.load_snapshot(snapshot_id)
                snapshots[snapshot_id] = snapshot
            item = {candidate.identity_key: candidate for candidate in snapshot.items}.get(identity)
            if item is None:
                raise WorkbenchError("scope current approval field is missing from its snapshot")
            entries.setdefault(identity, []).append(
                {
                    "scope": dict(pointer["scope"]),
                    "candidate_snapshot_id": snapshot_id,
                    "approval_record_id": approval["approval_record_id"],
                    "catalog_id": approval["catalog_id"],
                    "approved_at": approval["approved_at"],
                    "value": _item_value(item),
                    "item": item.as_dict(),
                }
            )
    return {
        identity: tuple(
            sorted(
                values,
                key=lambda entry: (
                    entry["approved_at"],
                    entry["approval_record_id"],
                ),
                reverse=True,
            )
        )
        for identity, values in entries.items()
    }


def _snapshot_summary(snapshot: RuleSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    counts: dict[str, int] = {}
    for item in snapshot.items:
        counts[item.status.value] = counts.get(item.status.value, 0) + 1
    return {
        "snapshot_id": snapshot.snapshot_id,
        "target_trading_day": snapshot.target_trading_day.isoformat(),
        "collected_at": snapshot.collected_at.isoformat(),
        "status": snapshot.status.value,
        "origin": snapshot.origin,
        "review_reason": snapshot.review_reason,
        "document_count": len(snapshot.documents),
        "item_count": len(snapshot.items),
        "status_counts": counts,
    }


def _ordered_runs(runs: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    return sorted(
        runs,
        key=lambda run: (str(run.get("completed_at") or ""), str(run.get("run_id") or "")),
        reverse=True,
    )


def _run_summary(run: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "target_trading_day": run.get("target_trading_day"),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "snapshot_id": run.get("snapshot_id"),
        "previous_snapshot_id": run.get("previous_snapshot_id"),
        "document_count": len(run.get("document_ids", [])),
        "error_count": len(run.get("errors", [])),
        "review_reason_count": len(run.get("review_reasons", [])),
    }


def _scope_pointer_summary(pointer: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "scope": dict(pointer["scope"]),
        "field_count": len(pointer["field_approvals"]),
        "updated_at": pointer["updated_at"],
    }


def _scope_approval_summary(record: Any) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "candidate_snapshot_id": record.candidate_snapshot_id,
        "scope": record.scope.as_dict(),
        "field_count": len(record.field_keys),
        "decision": record.decision,
        "reviewer": record.reviewer,
        "reason": record.reason,
        "decided_at": record.decided_at.isoformat(),
        "catalog_id": record.catalog_id,
    }


def _artifact_view(store: ExchangeRuleArtifactStore) -> dict[str, Any]:
    """Load validated artifacts shared by the summary and paginated resources."""

    try:
        snapshots = store.iter_snapshots()
        runs = store.iter_runs()
        catalog_runs = store.iter_catalog_runs()
        approvals = store.iter_approvals()
        scope_approvals = store.iter_scope_approvals()
        scope_currents = store.iter_scope_currents()
        current = store.current_snapshot()
        current_approval = store.current_approval()
    except ExchangeRuleArtifactError as exc:
        raise WorkbenchError(str(exc)) from exc

    ordered_snapshots = tuple(
        sorted(
            snapshots,
            key=lambda snapshot: (snapshot.collected_at, snapshot.snapshot_id),
            reverse=True,
        )
    )
    pending_candidates = tuple(
        snapshot
        for snapshot in ordered_snapshots
        if snapshot.status in {RuleStatus.CANDIDATE, RuleStatus.REVIEW_REQUIRED}
        and not store.approvals_for(snapshot.snapshot_id)
    )
    return {
        "store": store,
        "ordered_snapshots": ordered_snapshots,
        "ordered_runs": _ordered_runs(runs),
        "ordered_catalog_runs": _ordered_runs(catalog_runs),
        "ordered_approvals": tuple(
            sorted(
                approvals,
                key=lambda record: (record.decided_at, record.record_id),
                reverse=True,
            )
        ),
        "ordered_scope_approvals": tuple(
            sorted(
                scope_approvals,
                key=lambda record: (record.decided_at, record.record_id),
                reverse=True,
            )
        ),
        "scope_currents": scope_currents,
        "current": current,
        "current_approval": current_approval,
        "candidate": pending_candidates[0] if pending_candidates else None,
    }


def _review_queue(
    candidate: RuleSnapshot | None,
    scope_current_keys: set[str],
) -> list[dict[str, Any]]:
    """Aggregate field review work by product without treating unknown as zero."""

    if candidate is None:
        return []
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in candidate.items:
        if item.status not in {RuleStatus.CANDIDATE, RuleStatus.REVIEW_REQUIRED}:
            continue
        key = (item.exchange.upper(), item.product.upper())
        row = grouped.setdefault(
            key,
            {
                "exchange": key[0],
                "product": key[1],
                "candidate_fields": 0,
                "review_required_fields": 0,
                "scope_current_fields": 0,
            },
        )
        if item.status is RuleStatus.CANDIDATE:
            row["candidate_fields"] += 1
        else:
            row["review_required_fields"] += 1
        if _item_key(item) in scope_current_keys:
            row["scope_current_fields"] += 1
    rows = []
    for row in grouped.values():
        row["priority"] = (
            "review_required" if row["review_required_fields"] else "candidate"
        )
        rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            row["priority"] != "review_required",
            -row["review_required_fields"],
            -row["candidate_fields"],
            row["exchange"],
            row["product"],
        ),
    )


def _coverage_with_scope_current(
    coverage: list[dict[str, Any]],
    scope_current_keys: set[str],
) -> list[dict[str, Any]]:
    """Annotate catalog coverage without inventing product-level approval state."""

    keys_by_exchange: dict[str, set[str]] = {}
    for key in scope_current_keys:
        exchange, product, *_ = key.split("|", 4)
        keys_by_exchange.setdefault(exchange, set()).add(product)
    rows = []
    for row in coverage:
        result = dict(row)
        current_products = sorted(
            set(row["expected_products"]) & keys_by_exchange.get(row["exchange"], set())
        )
        result["products_with_scope_current_fields"] = current_products
        result["products_without_scope_current_fields"] = sorted(
            set(row["expected_products"]) - set(current_products)
        )
        result["scope_current_fields"] = sum(
            1
            for key in scope_current_keys
            if key.split("|", 1)[0] == row["exchange"]
        )
        rows.append(result)
    return rows


def build_workbench_state(store: ExchangeRuleArtifactStore) -> dict[str, Any]:
    """Build a compact credential-free view of validated local artifacts.

    Full rules, documents, and history are deliberately excluded from this payload.
    The local handler serves those data through bounded, paginated read-only APIs.
    """

    view = _artifact_view(store)
    current = view["current"]
    candidate = view["candidate"]
    scope_current_keys = _scope_current_field_keys(view["scope_currents"])
    latest_run = view["ordered_runs"][0] if view["ordered_runs"] else None
    latest_failure = next(
        (
            run
            for run in view["ordered_runs"]
            if run.get("status") in {"error", "review_required"}
        ),
        None,
    )
    latest_catalog_run = (
        view["ordered_catalog_runs"][0] if view["ordered_catalog_runs"] else None
    )
    coverage = _coverage_with_scope_current(
        _catalog_coverage_rows(latest_catalog_run), scope_current_keys
    )
    current_items = {item.identity_key: item for item in current.items} if current else {}
    candidate_items = {item.identity_key: item for item in candidate.items} if candidate else {}
    scoped_identities = _scope_current_field_identities(view["scope_currents"])
    identities = set(current_items) | set(candidate_items) | scoped_identities
    review_queue = _review_queue(candidate, scope_current_keys)
    scope_approvals = view["ordered_scope_approvals"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "artifact_root": str(store.root.resolve()),
        "current": _snapshot_summary(current),
        "current_approval": _approval_dict(view["current_approval"])
        if view["current_approval"]
        else None,
        "candidate": _snapshot_summary(candidate),
        "latest_run": _run_summary(latest_run),
        "latest_failure": _run_summary(latest_failure),
        "catalog": {
            "latest_run": _catalog_run_summary(latest_catalog_run),
            "coverage": _catalog_coverage_summary(coverage),
            "recent_runs": _summary_page(
                [
                    summary
                    for run in view["ordered_catalog_runs"]
                    if (summary := _catalog_run_summary(run)) is not None
                ]
            ),
        },
        "scope": {
            "current_pointer_count": len(view["scope_currents"]),
            "current_field_count": len(scope_current_keys),
            "approval_count": sum(
                1 for record in scope_approvals if record.decision == "approve"
            ),
            "rejection_count": sum(
                1 for record in scope_approvals if record.decision == "reject"
            ),
            "review_queue_count": len(review_queue),
            "review_required_product_count": sum(
                1 for row in review_queue if row["priority"] == "review_required"
            ),
            "recent_currents": _summary_page(
                [_scope_pointer_summary(pointer) for pointer in view["scope_currents"]]
            ),
        },
        "history": {
            "snapshots": _summary_page(
                [
                    summary
                    for snapshot in view["ordered_snapshots"]
                    if (summary := _snapshot_summary(snapshot)) is not None
                ]
            ),
            "runs": _summary_page(
                [
                    summary
                    for run in view["ordered_runs"]
                    if (summary := _run_summary(run)) is not None
                ]
            ),
            "approvals": _summary_page(
                [_approval_dict(record) for record in view["ordered_approvals"]]
            ),
            "scope_approvals": _summary_page(
                [_scope_approval_summary(record) for record in scope_approvals]
            ),
        },
        "filters": {
            "exchanges": sorted(identity[0] for identity in identities),
            "products": sorted(identity[1] for identity in identities),
            "categories": sorted(identity[3] for identity in identities),
            "statuses": sorted(
                (
                    candidate_items[identity].status.value
                    if identity in candidate_items
                    else current_items[identity].status.value
                    if identity in current_items
                    else RuleStatus.CURRENT_APPROVED.value
                )
                for identity in identities
            ),
        },
    }


def _rule_items(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    current = view["current"]
    candidate = view["candidate"]
    changes = diff_snapshots(current, candidate) if candidate is not None else ()
    changes_by_identity = _change_map(changes)
    current_items = {item.identity_key: item for item in current.items} if current else {}
    candidate_items = {item.identity_key: item for item in candidate.items} if candidate else {}
    scope_current_keys = _scope_current_field_keys(view["scope_currents"])
    scope_current_entries = _scope_current_entries(
        view["store"], view["scope_currents"]
    )
    scoped_items: dict[tuple[str, str, str | None, str, str], RuleItem] = {}
    for identity, entries in scope_current_entries.items():
        if entries:
            scoped_items[identity] = RuleItem.from_dict(entries[0]["item"])
    rows: list[dict[str, Any]] = []
    for identity in sorted(
        set(current_items) | set(candidate_items) | set(scoped_items),
        key=rule_identity_sort_key,
    ):
        current_item = current_items.get(identity)
        candidate_item = candidate_items.get(identity)
        scoped_item = scoped_items.get(identity)
        item = candidate_item or current_item or scoped_item
        assert item is not None
        change = changes_by_identity.get(identity)
        key = _item_key(item)
        rows.append(
            {
                "key": key,
                "exchange": item.exchange.upper(),
                "product": item.product.upper(),
                "contract": item.contract,
                "category": item.category.value,
                "category_label": _CATEGORY_LABELS[item.category.value],
                "field": item.field,
                "current": current_item.as_dict() if current_item else None,
                "scoped_current": scoped_item.as_dict() if scoped_item else None,
                "candidate": candidate_item.as_dict() if candidate_item else None,
                "current_value": _item_value(current_item),
                "scoped_current_value": _item_value(scoped_item),
                "candidate_value": _item_value(candidate_item),
                "change_type": change.change_type if change else None,
                "status": item.status.value,
                "status_label": _STATUS_LABELS[item.status.value],
                "scope_current": key in scope_current_keys,
                "scope_approvals": list(scope_current_entries.get(identity, ())),
                "source_document_id": item.source_document_id,
                "source_url": item.source_url,
                "excerpt": item.excerpt,
                "note": item.note,
            }
        )
    return rows



def _documents(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for snapshot in view["ordered_snapshots"]:
        for document in snapshot.documents:
            documents[document.document_id] = _document_dict(document)
    return sorted(documents.values(), key=lambda document: document["document_id"])


def _bounded_query(query: Mapping[str, list[str]]) -> tuple[int, int]:
    def value(name: str, default: int) -> int:
        raw_values = query.get(name, [])
        if not raw_values:
            return default
        if len(raw_values) != 1:
            raise WorkbenchRequestError(f"{name} must occur once")
        try:
            parsed = int(raw_values[0])
        except ValueError as exc:
            raise WorkbenchRequestError(f"{name} must be an integer") from exc
        return parsed

    offset = value("offset", 0)
    limit = value("limit", 50)
    if offset < 0:
        raise WorkbenchRequestError("offset must be non-negative")
    if not 1 <= limit <= 100:
        raise WorkbenchRequestError("limit must be between 1 and 100")
    return offset, limit


def _single_filter(query: Mapping[str, list[str]], name: str) -> str | None:
    values = query.get(name, [])
    if not values:
        return None
    if len(values) != 1:
        raise WorkbenchRequestError(f"{name} must occur once")
    value = values[0].strip()
    if len(value) > 64:
        raise WorkbenchRequestError(f"{name} exceeds the maximum length")
    return value.upper() if value else None


def _paginate(items: list[dict[str, Any]], *, offset: int, limit: int) -> dict[str, Any]:
    return {
        "offset": offset,
        "limit": limit,
        "total": len(items),
        "items": items[offset : offset + limit],
    }


def build_workbench_resource(
    store: ExchangeRuleArtifactStore,
    resource: str,
    query: Mapping[str, list[str]],
) -> dict[str, Any]:
    """Build one bounded read-only workbench API resource."""

    view = _artifact_view(store)
    offset, limit = _bounded_query(query)
    if resource == "rules":
        rows = _rule_items(view)
        exchange = _single_filter(query, "exchange")
        product = _single_filter(query, "product")
        category = _single_filter(query, "category")
        status = _single_filter(query, "status")
        rows = [
            row
            for row in rows
            if (exchange is None or row["exchange"] == exchange)
            and (product is None or row["product"] == product)
            and (category is None or row["category"].upper() == category)
            and (status is None or row["status"].upper() == status)
        ]
        return _paginate(rows, offset=offset, limit=limit)
    if resource == "documents":
        rows = _documents(view)
        exchange = _single_filter(query, "exchange")
        if exchange is not None:
            rows = [row for row in rows if row["exchange"].upper() == exchange]
        return _paginate(rows, offset=offset, limit=limit)
    if resource == "catalog-products":
        latest = view["ordered_catalog_runs"][0] if view["ordered_catalog_runs"] else None
        scope_current_keys = _scope_current_field_keys(view["scope_currents"])
        rows = _coverage_with_scope_current(_catalog_coverage_rows(latest), scope_current_keys)
        exchange = _single_filter(query, "exchange")
        flattened = []
        for row in rows:
            if exchange is not None and row["exchange"] != exchange:
                continue
            processed = set(row["processed_products"])
            closed = set(row["closed_products"])
            unresolved = set(row["unresolved_products"])
            scoped = set(row["products_with_scope_current_fields"])
            for product in row["expected_products"]:
                flattened.append(
                    {
                        "exchange": row["exchange"],
                        "product": product,
                        "exchange_status": row["status"],
                        "coverage": (
                            "processed" if product in processed else "closed" if product in closed else "unresolved" if product in unresolved else "unknown"
                        ),
                        "has_scope_current_fields": product in scoped,
                    }
                )
        return _paginate(flattened, offset=offset, limit=limit)
    if resource == "excluded-options":
        latest = view["ordered_catalog_runs"][0] if view["ordered_catalog_runs"] else None
        rows = list(latest["excluded_options"]) if latest is not None else []
        exchange = _single_filter(query, "exchange")
        if exchange is not None:
            rows = [row for row in rows if row["exchange"].upper() == exchange]
        return _paginate(rows, offset=offset, limit=limit)
    if resource == "review-queue":
        rows = _review_queue(
            view["candidate"], _scope_current_field_keys(view["scope_currents"])
        )
        exchange = _single_filter(query, "exchange")
        if exchange is not None:
            rows = [row for row in rows if row["exchange"] == exchange]
        return _paginate(rows, offset=offset, limit=limit)
    if resource == "history":
        kind = _single_filter(query, "kind") or "SNAPSHOTS"
        if kind == "SNAPSHOTS":
            rows = [
                summary
                for snapshot in view["ordered_snapshots"]
                if (summary := _snapshot_summary(snapshot)) is not None
            ]
        elif kind == "RUNS":
            rows = [
                summary
                for run in view["ordered_runs"]
                if (summary := _run_summary(run)) is not None
            ]
        elif kind == "CATALOG_RUNS":
            rows = [
                summary
                for run in view["ordered_catalog_runs"]
                if (summary := _catalog_run_summary(run)) is not None
            ]
        elif kind == "APPROVALS":
            rows = [_approval_dict(record) for record in view["ordered_approvals"]]
        elif kind == "SCOPE_APPROVALS":
            rows = [
                _scope_approval_summary(record)
                for record in view["ordered_scope_approvals"]
            ]
        elif kind == "SCOPE_CURRENTS":
            rows = [_scope_pointer_summary(pointer) for pointer in view["scope_currents"]]
        else:
            raise WorkbenchRequestError("history kind is unsupported")
        return _paginate(rows, offset=offset, limit=limit)
    raise WorkbenchRequestError("workbench resource is unsupported")


def _load_workbench_catalog(catalog_path: str | Path | None) -> FuturesCatalog:
    path = Path(catalog_path) if catalog_path is not None else _DEFAULT_CATALOG_PATH
    try:
        return load_futures_catalog(path, require_all_exchanges=True)
    except FuturesCatalogError as exc:
        raise WorkbenchError(str(exc)) from exc


def _latest_product_coverage(
    view: Mapping[str, Any],
) -> dict[tuple[str, str], str]:
    if not view["ordered_catalog_runs"]:
        return {}
    latest = view["ordered_catalog_runs"][0]
    result = {
        (str(item["exchange"]).upper(), str(item["product"]).upper()): "unknown"
        for item in latest["expected_futures"]
    }
    for field, status in (
        ("processed_futures", "processed"),
        ("closed_futures", "closed"),
        ("unresolved_futures", "unresolved"),
    ):
        for item in latest[field]:
            result[
                (str(item["exchange"]).upper(), str(item["product"]).upper())
            ] = status
    return result


def _product_review_counts(
    view: Mapping[str, Any],
) -> dict[tuple[str, str], dict[str, int]]:
    rows = _review_queue(
        view["candidate"], _scope_current_field_keys(view["scope_currents"])
    )
    return {
        (row["exchange"], row["product"]): {
            "pending": max(
                0,
                row["candidate_fields"]
                + row["review_required_fields"]
                - row["scope_current_fields"],
            ),
            "extracted": row["candidate_fields"],
        }
        for row in rows
    }


def _product_status(
    coverage: str,
    pending_fields: int,
    extracted_fields: int,
) -> tuple[str, str]:
    if extracted_fields:
        return "review", f"\u5df2\u63d0\u53d6 {extracted_fields} \u9879"
    if pending_fields:
        return "review", f"\u65e0\u6570\u503c {pending_fields} \u9879"
    return coverage, _COVERAGE_LABELS.get(coverage, _UNKNOWN)


def _product_groups(
    store: ExchangeRuleArtifactStore,
    catalog_path: str | Path | None,
) -> list[dict[str, Any]]:
    catalog = _load_workbench_catalog(catalog_path)
    view = _artifact_view(store)
    coverage = _latest_product_coverage(view)
    review_counts = _product_review_counts(view)
    groups: list[dict[str, Any]] = []
    for exchange in catalog.exchanges:
        products = []
        for product in catalog.products_for_exchange(exchange):
            key = product.identity
            coverage_status = coverage.get(key, "unknown")
            counts = review_counts.get(
                key, {"pending": 0, "extracted": 0}
            )
            status_class, status_label = _product_status(
                coverage_status, counts["pending"], counts["extracted"]
            )
            products.append(
                {
                    "exchange": key[0],
                    "code": key[1],
                    "name_zh": product.name_zh,
                    "coverage": coverage_status,
                    "status_class": status_class,
                    "status_label": status_label,
                }
            )
        groups.append({"exchange": exchange, "products": products})
    return groups


def _catalog_product(
    catalog: FuturesCatalog,
    exchange: str,
    product: str,
) -> FuturesProduct:
    identity = (exchange.upper(), product.upper())
    for candidate in catalog.future_products:
        if candidate.identity == identity:
            return candidate
    raise WorkbenchNotFoundError("catalog product was not found")


_PRODUCT_QUERY_VALUE = re.compile(r"^[A-Z0-9]{1,32}$")


def _product_query(query: Mapping[str, list[str]]) -> tuple[str, str]:
    exchange = _single_filter(query, "exchange")
    product = _single_filter(query, "product")
    if exchange is None or product is None:
        raise WorkbenchRequestError("exchange and product are required")
    if not _PRODUCT_QUERY_VALUE.fullmatch(exchange):
        raise WorkbenchRequestError("exchange is invalid")
    if not _PRODUCT_QUERY_VALUE.fullmatch(product):
        raise WorkbenchRequestError("product is invalid")
    return exchange, product


def _selected_rule_payload(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    candidate = row.get("candidate")
    if isinstance(candidate, Mapping):
        return candidate
    scoped_current = row.get("scoped_current")
    if isinstance(scoped_current, Mapping):
        return scoped_current
    current = row.get("current")
    return current if isinstance(current, Mapping) else None


def _product_rule_fields(
    view: Mapping[str, Any],
    exchange: str,
    product: str,
) -> list[dict[str, Any]]:
    product_rows = [
        row
        for row in _rule_items(view)
        if row["exchange"] == exchange and row["product"] == product
    ]
    fields = []
    for category, field, label in _REQUIRED_RULE_FIELDS:
        matches = [
            row
            for row in product_rows
            if row["category"] == category and row["field"] == field
        ]
        matches.sort(
            key=lambda row: (
                row["candidate"] is not None,
                row["scope_current"],
                row["scoped_current"] is not None,
                row["current"] is not None,
                row["contract"] or "",
            ),
            reverse=True,
        )
        selected = matches[0] if matches else None
        payload = _selected_rule_payload(selected) if selected is not None else None
        value = payload.get("value") if payload is not None else None
        if value is None:
            display_status = _NO_VALUE
            status_class = "unknown"
            display_value = _NO_VALUE
        elif selected is not None and (
            selected["scope_current"]
            or selected["status"] == RuleStatus.CURRENT_APPROVED.value
        ):
            display_status = _APPROVED
            status_class = "approved"
            display_value = str(value)
        else:
            display_status = _EXTRACTED
            status_class = "review"
            display_value = str(value)
        fields.append(
            {
                "category": category,
                "field": field,
                "label": label,
                "status": display_status,
                "status_class": status_class,
                "value": display_value,
                "unit": payload.get("unit") if payload is not None else None,
                "contract": selected["contract"] if selected is not None else None,
            }
        )
    return fields


def _retained_content_length(
    store: ExchangeRuleArtifactStore,
    content_path: str,
) -> int | None:
    root = store.root.resolve()
    retained_path = (root / content_path).resolve()
    try:
        retained_path.relative_to(root)
    except ValueError as exc:
        raise WorkbenchError("retained document path escapes the artifact root") from exc
    try:
        return retained_path.stat().st_size
    except OSError:
        return None


def _document_retrieval_method(document: object) -> str | None:
    """Read the true retrieval method recorded inside the retained content.

    Direct structured-file downloads and stack scrapes share one content
    type, so the marker inside the JSON payload is the only discriminator.
    """

    content_type = str(getattr(document, "content_type", "") or "")
    if content_type.lower().split(";", 1)[0] != (
        "application/vnd.ak-trading.websearch-scrape+json"
    ):
        return None
    try:
        payload = json.loads(bytes(getattr(document, "content", b"")))
    except (AttributeError, TypeError, ValueError):
        return None
    if isinstance(payload, dict):
        method = payload.get("retrieval_method")
        if isinstance(method, str) and method.strip():
            return method.strip()
    return None


def _product_documents(
    view: Mapping[str, Any],
    exchange: str,
    product: str,
) -> list[dict[str, Any]]:
    document_ids = {
        item.source_document_id
        for snapshot in view["ordered_snapshots"]
        for item in snapshot.items
        if item.exchange.upper() == exchange
        and item.product.upper() == product
        and item.source_document_id is not None
    }
    documents = {}
    for snapshot in view["ordered_snapshots"]:
        for document in snapshot.documents:
            if document.document_id not in document_ids:
                continue
            documents[document.document_id] = {
                "document_id": document.document_id,
                "title": document.title,
                "url": document.url,
                "retrieval_method": _document_retrieval_method(document),
                "content_length": _retained_content_length(
                    view["store"], document.content_path
                ),
            }
    return sorted(
        documents.values(), key=lambda document: (document["title"], document["document_id"])
    )


def _issue_touches_product(text: str, exchange: str, product: str) -> bool:
    normalized = text.upper().strip()
    if normalized.startswith(f"{exchange}:"):
        return True
    if f"{exchange}/{product}" in normalized or f"{exchange}.{product}" in normalized:
        return True
    return re.search(rf"(?:^|[\s;,(]){re.escape(product)}:", normalized) is not None


def _product_issues(
    view: Mapping[str, Any],
    exchange: str,
    product: str,
) -> list[dict[str, str]]:
    runs = []
    if view["ordered_catalog_runs"]:
        runs.append(view["ordered_catalog_runs"][0])
    if view["ordered_runs"]:
        runs.append(view["ordered_runs"][0])
    issues: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for run in runs:
        for field, kind in (("errors", "error"), ("review_reasons", "review")):
            for value in run.get(field, []):
                text = str(value)
                key = (kind, text)
                if key in seen or not _issue_touches_product(
                    text, exchange, product
                ):
                    continue
                seen.add(key)
                issues.append({"kind": kind, "text": text})
    return issues


_WORKBENCH_CSS = """
:root { color-scheme:light; --ink:#20272c; --muted:#647078; --line:#d8ddde;
  --paper:#f4f6f5; --surface:#fff; --green:#17685b; --green-soft:#e2f0eb;
  --amber:#8a5500; --amber-soft:#fff0cf; --red:#9b3535; --red-soft:#f9e4e4;
  --blue:#245c83; --blue-soft:#e5eef5; }
* { box-sizing:border-box; letter-spacing:0; }
body { margin:0; background:var(--paper); color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
a { color:var(--blue); }
.topbar { background:#252d31; color:#fff; padding:18px clamp(16px,4vw,48px); }
.topbar-inner { max-width:1180px; margin:0 auto; display:flex; align-items:flex-end;
  justify-content:space-between; gap:20px; }
.eyebrow { color:#b9c7c9; font-size:12px; font-weight:700; margin:0 0 4px; }
h1 { margin:0; font-size:25px; line-height:1.2; }
.subtitle { color:#cbd4d5; margin:5px 0 0; }
.readonly { border:1px solid #77afa5; background:#174f47; color:#e7fffa;
  padding:4px 8px; font-size:12px; font-weight:700; white-space:nowrap; }
.back { display:inline-block; color:#d9e7e5; margin-bottom:8px; text-decoration:none; }
.back:hover,.back:focus { text-decoration:underline; }
main { max-width:1180px; margin:0 auto; padding:22px clamp(16px,4vw,48px) 46px; }
.exchange-group { border-top:1px solid var(--line); padding:18px 0 6px; }
.exchange-group:first-child { border-top:0; padding-top:0; }
.section-heading { display:flex; align-items:baseline; justify-content:space-between;
  gap:16px; margin-bottom:10px; }
.section-heading h2,.detail-section h2 { margin:0; font-size:16px; }
.count,.muted { color:var(--muted); font-size:12px; }
.product-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr));
  gap:8px; margin:0; padding:0; list-style:none; }
.product-link { min-height:66px; display:grid; grid-template-columns:minmax(0,1fr) auto;
  align-items:center; gap:10px; padding:10px 11px; border:1px solid var(--line);
  border-radius:4px; background:var(--surface); color:var(--ink); text-decoration:none; }
.product-link:hover,.product-link:focus { border-color:var(--green); outline:2px solid var(--green-soft);
  outline-offset:1px; }
.product-code { display:block; font:700 14px/1.3 ui-monospace,SFMono-Regular,Consolas,monospace; }
.product-name { display:block; color:var(--muted); margin-top:2px; overflow-wrap:anywhere; }
.status { display:inline-block; padding:3px 6px; font-size:12px; font-weight:700;
  white-space:nowrap; }
.status-review { color:var(--amber); background:var(--amber-soft); }
.status-approved,.status-processed { color:var(--green); background:var(--green-soft); }
.status-error,.status-unresolved { color:var(--red); background:var(--red-soft); }
.status-closed { color:var(--blue); background:var(--blue-soft); }
.status-unknown { color:var(--muted); background:#e9edee; }
.identity { display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding:0 0 18px;
  border-bottom:1px solid var(--line); }
.identity strong { font-size:17px; }
.detail-section { margin-top:24px; }
.detail-section > h2 { margin-bottom:10px; }
.table-wrap { overflow-x:auto; border:1px solid var(--line); background:var(--surface); }
table { width:100%; min-width:700px; border-collapse:collapse; }
th,td { padding:10px 11px; border-bottom:1px solid var(--line); text-align:left;
  vertical-align:top; }
th { color:#4f5c62; background:#edf0ef; font-size:12px; }
tbody tr:last-child td { border-bottom:0; }
.field-id,.mono { color:var(--muted); font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;
  overflow-wrap:anywhere; }
.rule-value { font-weight:700; }
.evidence-list,.issue-list { display:grid; gap:8px; margin:0; padding:0; list-style:none; }
.evidence-item,.issue-item { border:1px solid var(--line); border-radius:4px;
  background:var(--surface); padding:11px 12px; }
.evidence-title { font-weight:700; }
.evidence-url { display:block; margin-top:4px; overflow-wrap:anywhere; }
.evidence-meta { display:flex; flex-wrap:wrap; gap:8px 18px; color:var(--muted);
  font-size:12px; margin-top:7px; }
.issue-item.error { border-left:3px solid var(--red); }
.issue-item.review { border-left:3px solid var(--amber); }
.empty { color:var(--muted); margin:0; padding:12px 0; }
@media (max-width:560px) {
  .topbar-inner { align-items:flex-start; }
  h1 { font-size:21px; }
  .product-grid { grid-template-columns:1fr; }
  .product-link { min-height:60px; }
}
"""


def _page_document(
    *,
    title: str,
    heading: str,
    eyebrow: str,
    subtitle: str,
    content: str,
    back_href: str | None = None,
) -> str:
    back = (
        f'<a class="back" href="{escape(back_href, quote=True)}">'
        f'&larr; {escape("\u8fd4\u56de\u4ea7\u54c1\u5217\u8868")}</a>'
        if back_href is not None
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{_WORKBENCH_CSS}</style>
</head>
<body>
<header class="topbar"><div class="topbar-inner"><div>{back}<p class="eyebrow">{escape(eyebrow)}</p><h1>{escape(heading)}</h1><p class="subtitle">{escape(subtitle)}</p></div><span class="readonly">{escape("\u53ea\u8bfb")}</span></div></header>
<main>{content}</main>
</body>
</html>
"""


def _status_html(status_class: str, label: str) -> str:
    return (
        f'<span class="status status-{escape(status_class, quote=True)}">'
        f"{escape(label)}</span>"
    )


def _document_link_html(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return f'<span class="evidence-url mono">{escape(url)}</span>'
    return (
        f'<a class="evidence-url" href="{escape(url, quote=True)}" '
        f'target="_blank" rel="noreferrer">{escape(url)}</a>'
    )


def render_workbench_html(
    state: dict[str, Any],
    *,
    store: ExchangeRuleArtifactStore | None = None,
    catalog_path: str | Path | None = None,
) -> str:
    """Render the server-side exchange and product landing matrix."""

    active_store = store or ExchangeRuleArtifactStore(state["artifact_root"])
    groups = _product_groups(active_store, catalog_path)
    sections = []
    for group in groups:
        products = []
        for product in group["products"]:
            href = "/product?" + urlencode(
                {"exchange": product["exchange"], "product": product["code"]}
            )
            products.append(
                f'<li><a class="product-link" href="{escape(href, quote=True)}">'
                f'<span><span class="product-code">{escape(product["code"])}</span>'
                f'<span class="product-name">{escape(product["name_zh"])}</span></span>'
                f'{_status_html(product["status_class"], product["status_label"])}</a></li>'
            )
        sections.append(
            f'<section class="exchange-group" data-exchange="{escape(group["exchange"], quote=True)}">'
            f'<div class="section-heading"><h2>{escape(group["exchange"])}</h2>'
            f'<span class="count">{len(products)} {escape("\u4e2a\u4ea7\u54c1")}</span></div>'
            f'<ul class="product-grid">{"".join(products)}</ul></section>'
        )
    product_count = sum(len(group["products"]) for group in groups)
    return _page_document(
        title="\u4ea4\u6613\u6240\u89c4\u5219\u5de5\u4f5c\u53f0",
        heading="\u4ea4\u6613\u6240\u89c4\u5219\u5de5\u4f5c\u53f0",
        eyebrow="AK TRADING",
        subtitle=(
            f"{len(groups)} {escape("\u4e2a\u4ea4\u6613\u6240")} / "
            f"{product_count} {escape("\u4e2a\u4ea7\u54c1")}"
        ),
        content="".join(sections),
    )


def render_product_detail_html(
    store: ExchangeRuleArtifactStore,
    exchange: str,
    product: str,
    *,
    catalog_path: str | Path | None = None,
) -> str:
    """Render one catalog product's current rule and evidence detail."""

    catalog = _load_workbench_catalog(catalog_path)
    catalog_product = _catalog_product(catalog, exchange, product)
    exchange, product = catalog_product.identity
    view = _artifact_view(store)
    coverage = _latest_product_coverage(view).get((exchange, product), "unknown")
    counts = _product_review_counts(view).get((exchange, product), {"pending": 0, "extracted": 0})
    status_class, status_label = _product_status(
        coverage, counts["pending"], counts["extracted"]
    )
    fields = _product_rule_fields(view, exchange, product)
    documents = _product_documents(view, exchange, product)
    issues = _product_issues(view, exchange, product)

    field_rows = []
    for field in fields:
        unit = (
            f'<div class="muted">{escape(str(field["unit"]))}</div>'
            if field["unit"]
            else ""
        )
        contract = escape(field["contract"] or "\u5168\u90e8\u5408\u7ea6")
        field_rows.append(
            f'<tr><td><strong>{escape(field["label"])}</strong>'
            f'<div class="field-id">{escape(field["category"])} / {escape(field["field"])}</div></td>'
            f'<td>{contract}</td><td>{_status_html(field["status_class"], field["status"])}</td>'
            f'<td><span class="rule-value">{escape(field["value"])}</span>{unit}</td></tr>'
        )
    rules_html = (
        '<section class="detail-section"><h2>\u4ea4\u6613\u89c4\u5219</h2>'
        '<div class="table-wrap"><table><thead><tr>'
        '<th>\u89c4\u5219\u5b57\u6bb5</th><th>\u9002\u7528\u8303\u56f4</th>'
        '<th>\u72b6\u6001</th><th>\u503c</th></tr></thead>'
        f'<tbody>{"".join(field_rows)}</tbody></table></div></section>'
    )

    document_items = []
    for document in documents:
        method = document["retrieval_method"] or "\u672a\u8bb0\u5f55"
        length = (
            f'{document["content_length"]} bytes'
            if document["content_length"] is not None
            else _UNKNOWN
        )
        document_items.append(
            '<li class="evidence-item">'
            f'<div class="evidence-title">{escape(document["title"])}</div>'
            f'{_document_link_html(document["url"])}'
            '<div class="evidence-meta">'
            f'<span>\u83b7\u53d6\u65b9\u5f0f: <span class="mono">{escape(method)}</span></span>'
            f'<span>\u5185\u5bb9\u957f\u5ea6: {escape(length)}</span></div></li>'
        )
    documents_html = (
        '<section class="detail-section"><h2>\u8bc1\u636e\u6587\u6863</h2>'
        + (
            f'<ul class="evidence-list">{"".join(document_items)}</ul>'
            if document_items
            else '<p class="empty">\u6682\u65e0\u4fdd\u7559\u7684\u8bc1\u636e\u6587\u6863</p>'
        )
        + "</section>"
    )

    issue_items = []
    for issue in issues:
        label = "\u9694\u79bb\u9519\u8bef" if issue["kind"] == "error" else "\u5ba1\u6838\u539f\u56e0"
        issue_items.append(
            f'<li class="issue-item {escape(issue["kind"], quote=True)}">'
            f'<strong>{escape(label)}</strong><div>{escape(issue["text"])}</div></li>'
        )
    issues_html = (
        '<section class="detail-section"><h2>\u5ba1\u6838\u4e0e\u9519\u8bef</h2>'
        + (
            f'<ul class="issue-list">{"".join(issue_items)}</ul>'
            if issue_items
            else '<p class="empty">\u6682\u65e0\u4e0e\u8be5\u4ea7\u54c1\u76f8\u5173\u7684\u5ba1\u6838\u539f\u56e0\u6216\u9519\u8bef</p>'
        )
        + "</section>"
    )
    identity_html = (
        '<div class="identity">'
        f'<strong>{escape(exchange)} / {escape(product)}</strong>'
        f'<span>{escape(catalog_product.name_zh)}</span>'
        f'{_status_html(status_class, status_label)}</div>'
    )
    return _page_document(
        title=f"{product} {catalog_product.name_zh}",
        heading=f"{product} {catalog_product.name_zh}",
        eyebrow=exchange,
        subtitle="\u4ea7\u54c1\u89c4\u5219\u8be6\u60c5",
        content=identity_html + rules_html + documents_html + issues_html,
        back_href="/",
    )


def make_workbench_handler(
    store: ExchangeRuleArtifactStore,
    *,
    catalog_path: str | Path | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Create a request handler bound to one local artifact root."""

    class WorkbenchHandler(BaseHTTPRequestHandler):
        server_version = "AKExchangeRulesWorkbench/1"

        def _send(self, status_code: int, content_type: str, body: bytes) -> None:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            request = urlparse(self.path)
            path = request.path
            try:
                if path == "/api/state":
                    body = json.dumps(
                        build_workbench_state(store), ensure_ascii=True, allow_nan=False
                    ).encode("utf-8")
                    self._send(200, "application/json; charset=utf-8", body)
                elif path.startswith("/api/"):
                    resource = path.removeprefix("/api/")
                    body = json.dumps(
                        build_workbench_resource(
                            store,
                            resource,
                            parse_qs(request.query, keep_blank_values=True),
                        ),
                        ensure_ascii=True,
                        allow_nan=False,
                    ).encode("utf-8")
                    self._send(200, "application/json; charset=utf-8", body)
                elif path in {"/", "/index.html"}:
                    body = render_workbench_html(
                        build_workbench_state(store),
                        store=store,
                        catalog_path=catalog_path,
                    ).encode("utf-8")
                    self._send(200, "text/html; charset=utf-8", body)
                elif path == "/product":
                    exchange, product = _product_query(
                        parse_qs(request.query, keep_blank_values=True)
                    )
                    body = render_product_detail_html(
                        store,
                        exchange,
                        product,
                        catalog_path=catalog_path,
                    ).encode("utf-8")
                    self._send(200, "text/html; charset=utf-8", body)
                elif path == "/healthz":
                    self._send(200, "text/plain; charset=utf-8", b"ok\n")
                else:
                    self._send(404, "text/plain; charset=utf-8", b"not found\n")
            except WorkbenchNotFoundError:
                self._send(
                    404,
                    "text/plain; charset=utf-8",
                    b"product not found\n",
                )
            except WorkbenchRequestError as exc:
                # Escape defensively: exception text must never carry raw markup
                # even if a future validator starts echoing user input.
                body = f"invalid workbench request: {escape(str(exc))}\n".encode("utf-8")
                self._send(400, "text/plain; charset=utf-8", body)
            except (OSError, WorkbenchError, ExchangeRuleArtifactError) as exc:
                body = f"workbench unavailable: {type(exc).__name__}: {exc}\n".encode("utf-8")
                self._send(503, "text/plain; charset=utf-8", body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return WorkbenchHandler


def serve_workbench(
    artifact_root: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    once: bool = False,
    catalog_path: str | Path | None = None,
) -> None:
    """Serve the read-only workbench until interrupted, or one request in test mode."""

    if host != "127.0.0.1":
        raise WorkbenchError("workbench host must remain local-only and bind to 127.0.0.1")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise WorkbenchError("workbench port must be between 1 and 65535")
    server = ThreadingHTTPServer(
        (host, port),
        make_workbench_handler(
            ExchangeRuleArtifactStore(artifact_root), catalog_path=catalog_path
        ),
    )
    try:
        if once:
            server.handle_request()
        else:
            server.serve_forever()
    finally:
        server.server_close()


def _parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Serve the local exchange-rule workbench")
    parser.add_argument("--artifact-root", type=Path, default=root / "data" / "exchange_rules")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        serve_workbench(args.artifact_root, host=args.host, port=args.port, once=args.once)
    except (OSError, WorkbenchError, ValueError) as exc:
        print("EXCHANGE_RULES_WORKBENCH_FAIL", type(exc).__name__, str(exc)[:500])
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
