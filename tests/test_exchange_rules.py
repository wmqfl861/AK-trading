import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ak_trading.exchange_rule_artifacts import (
    ExchangeRuleArtifactError,
    ExchangeRuleArtifactStore,
    build_catalog_collection_run,
)
from ak_trading.exchange_rule_collection import (
    ExchangeRuleCollectionError,
    bootstrap_legacy_reference,
    collect_exchange_rules,
)
from ak_trading.exchange_rules import (
    FeeBasis,
    FetchedDocument,
    LockCalculationMethod,
    LockMarginRule,
    RuleCategory,
    RuleItem,
    RuleScope,
    RuleScopeKind,
    RuleSnapshot,
    RuleStatus,
    RuleValueKind,
    SourceDocument,
    canonical_sha256,
    compute_same_contract_lock_margin,
    diff_snapshots,
    estimate_fee,
    estimate_margin_rate,
)
from ak_trading.market_reference import SHANGHAI_TZ


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "config" / "market_reference.json"
REGISTRY = ROOT / "config" / "exchange_rule_sources.json"
FIXTURES = {
    "SHFE": ROOT / "tests" / "fixtures_exchange_rules_shfe.json",
    "CZCE": ROOT / "tests" / "fixtures_exchange_rules_czce.json",
    "DCE": ROOT / "tests" / "fixtures_exchange_rules_dce.json",
}
NOW = datetime(2026, 7, 28, 20, 30, tzinfo=SHANGHAI_TZ)


def _document() -> SourceDocument:
    fetched = FetchedDocument(
        url="https://www.shfe.com.cn/fixture/rules.json",
        title="SHFE rules",
        content=b"official rule evidence",
        content_type="text/plain",
        retrieved_at=NOW,
    )
    return SourceDocument.from_fetched(
        fetched,
        exchange="SHFE",
        content_path="documents/00/evidence.txt",
    )


def _fee_item(document: SourceDocument, *, status: RuleStatus = RuleStatus.CANDIDATE) -> RuleItem:
    return RuleItem(
        exchange="SHFE",
        product="RB",
        contract="SHFE.rb2610",
        category=RuleCategory.FEE,
        field="open",
        value_kind=RuleValueKind.DECIMAL,
        status=status,
        value=Decimal("3"),
        unit=FeeBasis.CNY_PER_LOT.value,
        applies_to="SHFE.rb2610",
        effective_from=date(2026, 8, 1),
        effective_through=None,
        source_document_id=document.document_id,
        source_url=document.url,
        excerpt="Exchange fee is CNY 3 per lot.",
    )


def _snapshot(item: RuleItem) -> RuleSnapshot:
    return RuleSnapshot.create(
        target_trading_day=date(2026, 7, 29),
        collected_at=NOW,
        status=RuleStatus.CANDIDATE,
        origin="test",
        documents=(_document(),),
        items=(item,),
        review_reason="test candidate",
    )


def _catalog_run_for(snapshot: RuleSnapshot, *, catalog_id: str = "a" * 64) -> dict[str, object]:
    return build_catalog_collection_run(
        target_trading_day=snapshot.target_trading_day,
        started_at=NOW,
        completed_at=NOW,
        catalog_id=catalog_id,
        exchange_statuses={"SHFE": "collected"},
        expected_futures=(("SHFE", "RB"),),
        processed_futures=(("SHFE", "RB"),),
        closed_futures=(),
        excluded_options=(),
        snapshot_id=snapshot.snapshot_id,
        errors=(),
        review_reasons=(),
        document_ids=(document.document_id for document in snapshot.documents),
        previous_snapshot_id=None,
    )


def test_legacy_catalog_run_is_normalized_without_rewriting(tmp_path: Path):
    document = _document()
    snapshot = _snapshot(replace(_fee_item(document), contract=None, applies_to="RB"))
    canonical = _catalog_run_for(snapshot)
    legacy_without_id = dict(canonical)
    legacy_without_id.pop("run_id")
    legacy_without_id["collected_futures"] = legacy_without_id.pop("processed_futures")
    legacy = dict(legacy_without_id)
    legacy["run_id"] = canonical_sha256(legacy_without_id)

    store = ExchangeRuleArtifactStore(tmp_path / "exchange_rules")
    path = store.catalog_runs_root / f"{legacy['run_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    original_bytes = (
        json.dumps(legacy, ensure_ascii=False, indent=4, sort_keys=False).encode("utf-8")
        + b"\n"
    )
    path.write_bytes(original_bytes)

    runs = store.iter_catalog_runs()

    assert len(runs) == 1
    assert runs[0]["run_id"] == legacy["run_id"]
    assert runs[0]["processed_futures"] == [{"exchange": "SHFE", "product": "RB"}]
    assert "collected_futures" not in runs[0]
    assert path.read_bytes() == original_bytes

    tampered_without_id = dict(legacy)
    tampered_without_id.pop("run_id")
    tampered_without_id["review_reasons"] = ["semantic tampering"]
    tampered = dict(tampered_without_id)
    tampered["run_id"] = canonical_sha256(tampered_without_id)
    with pytest.raises(ExchangeRuleArtifactError, match="canonical coverage content"):
        store.store_catalog_run(tampered)
    assert not (store.catalog_runs_root / f"{tampered['run_id']}.json").exists()


    assert estimate_fee(Decimal("3")) == Decimal("3.01")
    assert estimate_fee(Decimal("3"), lots=2) == Decimal("3.02")
    assert estimate_margin_rate(Decimal("0.10")) == Decimal("0.13000000")


def test_same_contract_lock_calculation_requires_explicit_formula():
    rule = LockMarginRule(
        rate=Decimal("0.5"),
        price_basis="settlement_price",
        formula_text="larger side only",
        calculation_method=LockCalculationMethod.LARGER_SIDE_ONLY,
        status=RuleStatus.CANDIDATE,
    )
    assert compute_same_contract_lock_margin(
        rule,
        long_lots=3,
        short_lots=2,
        settlement_price=Decimal("3000"),
        contract_size=Decimal("10"),
    ) == Decimal("45000.00")

    unknown = LockMarginRule(
        rate=None,
        price_basis=None,
        formula_text="official text requires review",
        calculation_method=LockCalculationMethod.UNKNOWN,
        status=RuleStatus.REVIEW_REQUIRED,
    )
    assert compute_same_contract_lock_margin(
        unknown,
        long_lots=3,
        short_lots=2,
        settlement_price=Decimal("3000"),
        contract_size=Decimal("10"),
    ) is None


def test_snapshot_round_trip_and_foreign_source_rejection():
    document = _document()
    snapshot = _snapshot(_fee_item(document))
    restored = RuleSnapshot.from_dict(snapshot.as_dict())
    assert restored.as_dict() == snapshot.as_dict()

    raw = snapshot.as_dict()
    raw["items"][0]["source_document_id"] = "a" * 64
    content = dict(raw)
    content.pop("snapshot_id")
    content.pop("content_sha256")
    raw["snapshot_id"] = canonical_sha256(content)
    raw["content_sha256"] = raw["snapshot_id"]
    with pytest.raises(ValueError, match="outside the snapshot"):
        RuleSnapshot.from_dict(raw)


def test_diff_distinguishes_value_status_and_evidence_changes():
    document = _document()
    original = _snapshot(_fee_item(document))
    status_changed = _snapshot(
        replace(_fee_item(document), status=RuleStatus.CURRENT_APPROVED)
    )
    evidence_changed = _snapshot(
        replace(_fee_item(document), excerpt="Updated official excerpt")
    )

    assert diff_snapshots(original, status_changed)[0].change_type == "review_status_changed"
    assert diff_snapshots(original, evidence_changed)[0].change_type == "evidence_changed"


def test_bootstrap_is_one_time_and_not_current(tmp_path: Path):
    store = ExchangeRuleArtifactStore(tmp_path / "exchange_rules")
    first = bootstrap_legacy_reference(
        store=store,
        reference_path=REFERENCE,
        now=NOW,
        trading_day=date(2026, 7, 29),
    )
    assert first.run["source_registry_sha256"] is None
    assert store.current_snapshot_id() is None
    assert first.snapshot is not None
    assert first.snapshot.status is RuleStatus.LEGACY_BASELINE

    with pytest.raises(ExchangeRuleCollectionError, match="one-time"):
        bootstrap_legacy_reference(
            store=store,
            reference_path=REFERENCE,
            now=NOW,
            trading_day=date(2026, 7, 29),
        )


def test_candidate_rejection_and_approval_keep_immutable_trail(tmp_path: Path):
    store = ExchangeRuleArtifactStore(tmp_path / "exchange_rules")
    result = collect_exchange_rules(
        store=store,
        source_registry_path=REGISTRY,
        reference_path=REFERENCE,
        now=NOW,
        trading_day=date(2026, 7, 29),
        fixture_paths=FIXTURES,
    )
    assert result.snapshot is not None
    candidate = result.snapshot
    assert candidate.status is RuleStatus.REVIEW_REQUIRED

    rejection = store.reject_snapshot(
        candidate,
        reviewer="reviewer",
        reason="needs official field review",
        decided_at=NOW,
    )
    assert rejection.decision == "reject"
    assert store.current_snapshot_id() is None

    approved, approval, _ = store.approve_snapshot(
        candidate,
        reviewer="reviewer",
        reason="reviewed retained evidence",
        decided_at=datetime(2026, 7, 28, 21, 0, tzinfo=SHANGHAI_TZ),
    )
    assert approval.decision == "approve"
    assert approval.candidate_snapshot_id == candidate.snapshot_id
    assert approval.approved_snapshot_id == approved.snapshot_id
    assert approved.snapshot_id != candidate.snapshot_id
    assert store.current_snapshot_id() == approved.snapshot_id
    assert any(item.status is RuleStatus.REVIEW_REQUIRED for item in approved.items)
    assert store.load_snapshot(candidate.snapshot_id).as_dict() == candidate.as_dict()


def test_current_pointer_rejects_missing_approval_record(tmp_path: Path):
    store = ExchangeRuleArtifactStore(tmp_path / "exchange_rules")
    document = _document()
    snapshot = _snapshot(_fee_item(document))
    store.store_snapshot(snapshot)
    store.current_path.parent.mkdir(parents=True, exist_ok=True)
    store.current_path.write_text(
        '{"schema_version": 1, "snapshot_id": "' + snapshot.snapshot_id + '", '
        '"approval_record_id": "' + "b" * 64 + '"}',
        encoding="utf-8",
    )
    with pytest.raises(ExchangeRuleArtifactError, match="approval"):
        store.current_snapshot_id()


def test_scope_approval_keeps_field_level_current_trail(tmp_path: Path):
    document = _document()
    product_item = replace(_fee_item(document), contract=None, applies_to="RB")
    contract_item = _fee_item(document)
    snapshot = RuleSnapshot.create(
        target_trading_day=date(2026, 7, 29),
        collected_at=NOW,
        status=RuleStatus.CANDIDATE,
        origin="test_catalog",
        documents=(document,),
        items=(product_item, contract_item),
        review_reason="test catalog candidate",
    )
    store = ExchangeRuleArtifactStore(tmp_path / "exchange_rules")
    catalog_id = "a" * 64
    store.store_catalog_run(_catalog_run_for(snapshot, catalog_id=catalog_id))
    scope = RuleScope(RuleScopeKind.PRODUCT, "SHFE", "RB")

    first, _ = store.approve_scope_fields(
        snapshot,
        scope=scope,
        field_keys=(product_item.identity_key,),
        reviewer="reviewer",
        reason="verified product fee",
        decided_at=NOW,
        catalog_id=catalog_id,
    )
    second, _ = store.approve_scope_fields(
        snapshot,
        scope=scope,
        field_keys=(contract_item.identity_key,),
        reviewer="reviewer",
        reason="verified contract fee",
        decided_at=datetime(2026, 7, 28, 21, 0, tzinfo=SHANGHAI_TZ),
        catalog_id=catalog_id,
    )

    pointer = store.scope_current(scope)
    assert pointer is not None
    assert [entry["approval_record_id"] for entry in pointer["field_approvals"]] == [
        first.record_id,
        second.record_id,
    ]
    assert store.current_snapshot_id() is None


def test_scope_approval_rejects_review_required_field_and_time_regression(tmp_path: Path):
    document = _document()
    candidate_item = replace(_fee_item(document), contract=None, applies_to="RB")
    unresolved_item = replace(
        _fee_item(document),
        field="close",
        contract=None,
        applies_to="RB",
        status=RuleStatus.REVIEW_REQUIRED,
        value_kind=RuleValueKind.UNKNOWN,
        value=None,
        unit=None,
    )
    snapshot = RuleSnapshot.create(
        target_trading_day=date(2026, 7, 29),
        collected_at=NOW,
        status=RuleStatus.REVIEW_REQUIRED,
        origin="test_catalog",
        documents=(document,),
        items=(candidate_item, unresolved_item),
        review_reason="one field requires review",
    )
    store = ExchangeRuleArtifactStore(tmp_path / "exchange_rules")
    catalog_id = "b" * 64
    store.store_catalog_run(_catalog_run_for(snapshot, catalog_id=catalog_id))
    scope = RuleScope(RuleScopeKind.PRODUCT, "SHFE", "RB")

    with pytest.raises(ExchangeRuleArtifactError, match="deterministic candidate"):
        store.approve_scope_fields(
            snapshot,
            scope=scope,
            field_keys=(unresolved_item.identity_key,),
            reviewer="reviewer",
            reason="must not approve unknown",
            decided_at=NOW,
            catalog_id=catalog_id,
        )

    store.approve_scope_fields(
        snapshot,
        scope=scope,
        field_keys=(candidate_item.identity_key,),
        reviewer="reviewer",
        reason="verified candidate",
        decided_at=NOW,
        catalog_id=catalog_id,
    )
    with pytest.raises(ExchangeRuleArtifactError, match="later than the current"):
        store.approve_scope_fields(
            snapshot,
            scope=scope,
            field_keys=(candidate_item.identity_key,),
            reviewer="reviewer",
            reason="backdated approval",
            decided_at=NOW,
            catalog_id=catalog_id,
        )


def test_scope_decisions_require_matching_catalog_run(tmp_path: Path):
    document = _document()
    snapshot = _snapshot(replace(_fee_item(document), contract=None, applies_to="RB"))
    store = ExchangeRuleArtifactStore(tmp_path / "exchange_rules")
    scope = RuleScope(RuleScopeKind.PRODUCT, "SHFE", "RB")

    with pytest.raises(ExchangeRuleArtifactError, match="not linked"):
        store.reject_scope_fields(
            snapshot,
            scope=scope,
            field_keys=(snapshot.items[0].identity_key,),
            reviewer="reviewer",
            reason="no catalog run",
            decided_at=NOW,
            catalog_id="c" * 64,
        )
