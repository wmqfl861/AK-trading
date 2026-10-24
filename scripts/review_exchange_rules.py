"""Review exchange-rule candidates without mutating source evidence."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from ak_trading.exchange_rule_artifacts import (
    CollectionLock,
    ExchangeRuleArtifactError,
    ExchangeRuleArtifactStore,
)
from ak_trading.exchange_rules import RuleScope, RuleScopeKind, RuleStatus
from ak_trading.market_reference import SHANGHAI_TZ


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_ROOT = ROOT / "data" / "exchange_rules"


def _aware_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI_TZ)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("--now must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError("--now must include a timezone offset")
    return parsed.astimezone(SHANGHAI_TZ)


def _add_common_review_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--snapshot", required=True, help="candidate snapshot SHA-256")
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--now", help=argparse.SUPPRESS)


def _add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--catalog-id", required=True, help="catalog SHA-256")
    parser.add_argument(
        "--scope-kind",
        choices=[kind.value for kind in RuleScopeKind],
        required=True,
    )
    parser.add_argument("--exchange", required=True)
    parser.add_argument("--product")
    parser.add_argument("--contract")
    parser.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="CATEGORY:FIELD|PRODUCT|CONTRACT|CATEGORY|FIELD",
        help=(
            "explicit field in the declared scope; product/contract scopes accept "
            "CATEGORY:FIELD, while full keys use - for a null contract"
        ),
    )
    parser.add_argument(
        "--all-scope-fields",
        action="store_true",
        help="select all deterministic candidate fields in the declared scope",
    )
    parser.add_argument(
        "--confirm-all-scope-fields",
        action="store_true",
        help="required together with --all-scope-fields",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Approve or reject an exchange-rule candidate"
    )
    _add_common_review_arguments(parser)
    subparsers = parser.add_subparsers(dest="decision", required=True)
    subparsers.add_parser("approve", help="create a legacy current-approved snapshot")
    subparsers.add_parser("reject", help="record a legacy rejection without changing current")

    for decision, help_text in (
        ("scope-approve", "approve explicit fields for one exchange, product, or contract scope"),
        ("scope-reject", "reject explicit fields for one exchange, product, or contract scope"),
    ):
        subparser = subparsers.add_parser(decision, help=help_text)
        _add_scope_arguments(subparser)
    return parser


def _scope_from_args(args: argparse.Namespace) -> RuleScope:
    return RuleScope(
        kind=RuleScopeKind(args.scope_kind),
        exchange=args.exchange.strip().upper(),
        product=args.product.strip().upper() if isinstance(args.product, str) else None,
        contract=args.contract.strip() if isinstance(args.contract, str) else None,
    )


def _requested_field_keys(
    args: argparse.Namespace,
    *,
    candidate,
    scope: RuleScope,
) -> tuple[tuple[str, str, str | None, str, str], ...]:
    if args.all_scope_fields:
        if not args.confirm_all_scope_fields:
            raise ValueError("--all-scope-fields requires --confirm-all-scope-fields")
        if args.field:
            raise ValueError("--field cannot be combined with --all-scope-fields")
        keys = tuple(
            item.identity_key
            for item in candidate.items
            if scope.contains(item) and item.status is RuleStatus.CANDIDATE
        )
        if not keys:
            raise ValueError("declared scope has no deterministic candidate fields")
        unresolved = [
            item.identity_key
            for item in candidate.items
            if scope.contains(item) and item.status is not RuleStatus.CANDIDATE
        ]
        if unresolved:
            raise ValueError(
                "--all-scope-fields refuses scopes containing review-required fields; "
                "use explicit --field selections"
            )
        return keys
    if args.confirm_all_scope_fields:
        raise ValueError("--confirm-all-scope-fields requires --all-scope-fields")
    if not args.field:
        raise ValueError("scope decisions require at least one --field")
    selected: list[tuple[str, str, str | None, str, str]] = []
    for raw_field in args.field:
        if "|" in raw_field:
            parts = raw_field.split("|")
            if len(parts) != 4:
                raise ValueError(
                    "full --field values must use PRODUCT|CONTRACT|CATEGORY|FIELD"
                )
            product, contract, category, field = (part.strip() for part in parts)
            if not product or not category or not field:
                raise ValueError(
                    "full --field values require product, category, and field"
                )
            selected.append(
                (
                    scope.exchange,
                    product.upper(),
                    None if contract == "-" else contract,
                    category,
                    field,
                )
            )
            continue
        if scope.kind is RuleScopeKind.EXCHANGE:
            raise ValueError(
                "exchange scope --field must use PRODUCT|CONTRACT|CATEGORY|FIELD"
            )
        category, separator, field = raw_field.partition(":")
        if not separator or not category.strip() or not field.strip() or ":" in field:
            raise ValueError("--field must use CATEGORY:FIELD")
        selected.append(
            (
                scope.exchange,
                scope.product or "",
                scope.contract,
                category.strip(),
                field.strip(),
            )
        )
    return tuple(selected)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        now = _aware_now(args.now)
        store = ExchangeRuleArtifactStore(args.artifact_root)
        with CollectionLock(store.lock_path):
            candidate = store.load_snapshot(args.snapshot)
            if args.decision == "approve":
                approved, record, pointer = store.approve_snapshot(
                    candidate,
                    reviewer=args.reviewer,
                    reason=args.reason,
                    decided_at=now,
                )
                print(
                    "EXCHANGE_RULES_APPROVED",
                    json.dumps(
                        {
                            "candidate_snapshot_id": candidate.snapshot_id,
                            "approved_snapshot_id": approved.snapshot_id,
                            "approval_record_id": record.record_id,
                            "current_pointer": str(pointer),
                        },
                        ensure_ascii=True,
                        sort_keys=True,
                    ),
                )
            elif args.decision == "reject":
                record = store.reject_snapshot(
                    candidate,
                    reviewer=args.reviewer,
                    reason=args.reason,
                    decided_at=now,
                )
                print(
                    "EXCHANGE_RULES_REJECTED",
                    json.dumps(
                        {
                            "candidate_snapshot_id": candidate.snapshot_id,
                            "approval_record_id": record.record_id,
                            "current_snapshot_id": store.current_snapshot_id(),
                        },
                        ensure_ascii=True,
                        sort_keys=True,
                    ),
                )
            else:
                scope = _scope_from_args(args)
                field_keys = _requested_field_keys(
                    args,
                    candidate=candidate,
                    scope=scope,
                )
                if args.decision == "scope-approve":
                    record, pointer = store.approve_scope_fields(
                        candidate,
                        scope=scope,
                        field_keys=field_keys,
                        reviewer=args.reviewer,
                        reason=args.reason,
                        decided_at=now,
                        catalog_id=args.catalog_id,
                    )
                    prefix = "EXCHANGE_RULE_SCOPE_APPROVED"
                    payload = {
                        "candidate_snapshot_id": candidate.snapshot_id,
                        "approval_record_id": record.record_id,
                        "catalog_id": record.catalog_id,
                        "field_count": len(record.field_keys),
                        "scope": record.scope.as_dict(),
                        "scope_current_pointer": str(pointer),
                    }
                else:
                    record = store.reject_scope_fields(
                        candidate,
                        scope=scope,
                        field_keys=field_keys,
                        reviewer=args.reviewer,
                        reason=args.reason,
                        decided_at=now,
                        catalog_id=args.catalog_id,
                    )
                    prefix = "EXCHANGE_RULE_SCOPE_REJECTED"
                    payload = {
                        "candidate_snapshot_id": candidate.snapshot_id,
                        "approval_record_id": record.record_id,
                        "catalog_id": record.catalog_id,
                        "field_count": len(record.field_keys),
                        "scope": record.scope.as_dict(),
                    }
                print(prefix, json.dumps(payload, ensure_ascii=True, sort_keys=True))
    except (ExchangeRuleArtifactError, OSError, ValueError) as exc:
        print("EXCHANGE_RULES_REVIEW_FAIL", type(exc).__name__, str(exc)[:500])
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
