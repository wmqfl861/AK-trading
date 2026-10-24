from datetime import date, datetime, timezone

import pytest

from ak_trading.exchange_rule_artifacts import ApprovalRecord
from ak_trading.exchange_rule_artifacts import ExchangeRuleArtifactError
from ak_trading.exchange_rules import RuleSnapshot, RuleStatus
from ak_trading.persistence.exchange_rules import (
    ExchangeRulePersistenceError,
    _ordered_snapshots,
    persist_exchange_rule_artifacts,
)


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


def _snapshot(status: RuleStatus, parent_snapshot_id: str | None = None) -> RuleSnapshot:
    return RuleSnapshot.create(
        target_trading_day=date(2026, 8, 10),
        collected_at=NOW,
        status=status,
        origin="test",
        documents=(),
        items=(),
        review_reason="test snapshot" if status in {RuleStatus.CANDIDATE, RuleStatus.REVIEW_REQUIRED} else None,
        parent_snapshot_id=parent_snapshot_id,
    )


def test_parent_snapshots_are_ordered_before_children():
    parent = _snapshot(RuleStatus.CANDIDATE)
    child = _snapshot(RuleStatus.CURRENT_APPROVED, parent.snapshot_id)

    assert [item.snapshot_id for item in _ordered_snapshots((child, parent))] == [
        parent.snapshot_id,
        child.snapshot_id,
    ]


def test_missing_parent_is_rejected_before_database_call():
    missing_parent_id = "a" * 64
    child = _snapshot(RuleStatus.CURRENT_APPROVED, missing_parent_id)

    with pytest.raises(ExchangeRulePersistenceError, match="parent is missing"):
        persist_exchange_rule_artifacts(None, snapshots=(child,))


def test_current_approval_requires_complete_import_set():
    candidate = _snapshot(RuleStatus.CANDIDATE)
    approved = _snapshot(RuleStatus.CURRENT_APPROVED, candidate.snapshot_id)
    approval = ApprovalRecord.create(
        candidate_snapshot_id=candidate.snapshot_id,
        approved_snapshot_id=approved.snapshot_id,
        decision="approve",
        reviewer="reviewer",
        reason="checked",
        decided_at=NOW,
    )

    with pytest.raises(ExchangeRulePersistenceError, match="approval import set"):
        persist_exchange_rule_artifacts(
            None,
            snapshots=(candidate, approved),
            current_approval=approval,
        )

    with pytest.raises(ExchangeRulePersistenceError, match="snapshot import set"):
        persist_exchange_rule_artifacts(
            None,
            snapshots=(candidate,),
            approvals=(approval,),
            current_approval=approval,
        )
