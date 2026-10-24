import importlib.util
from datetime import date, datetime
from pathlib import Path

import pytest

from ak_trading.exchange_rule_artifacts import (
    CollectionLockedError,
    ExchangeRuleArtifactStore,
    build_catalog_collection_run,
)
from ak_trading.exchange_rule_collection import collect_exchange_rules
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


def _load_script(name: str):
    script = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_exchange_rule_cli_{name}", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _collect_args(artifact_root: Path) -> list[str]:
    arguments = [
        "--artifact-root",
        str(artifact_root),
        "--reference",
        str(REFERENCE),
        "--registry",
        str(REGISTRY),
        "--legacy-three-products",
        "--now",
        NOW.isoformat(),
        "--trading-day",
        "2026-07-29",
    ]
    for exchange, path in FIXTURES.items():
        arguments.extend(("--fixture", f"{exchange}={path}"))
    return arguments


def _collect_candidate(artifact_root: Path):
    result = collect_exchange_rules(
        store=ExchangeRuleArtifactStore(artifact_root),
        source_registry_path=REGISTRY,
        reference_path=REFERENCE,
        now=NOW,
        trading_day=date(2026, 7, 29),
        fixture_paths=FIXTURES,
    )
    assert result.snapshot is not None
    return result.snapshot


def _store_catalog_run(store: ExchangeRuleArtifactStore, candidate) -> str:
    catalog_id = "d" * 64
    store.store_catalog_run(
        build_catalog_collection_run(
            target_trading_day=candidate.target_trading_day,
            started_at=NOW,
            completed_at=NOW,
            catalog_id=catalog_id,
            exchange_statuses={"SHFE": "collected"},
            expected_futures=(("SHFE", "RB"),),
            processed_futures=(("SHFE", "RB"),),
            closed_futures=(),
            excluded_options=(),
            snapshot_id=candidate.snapshot_id,
            errors=(),
            review_reasons=(),
            document_ids=(document.document_id for document in candidate.documents),
            previous_snapshot_id=None,
        )
    )
    return catalog_id


def test_collect_cli_returns_review_required_and_prefixed_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    module = _load_script("collect_exchange_rules")

    assert module.main(_collect_args(tmp_path / "review-required")) == 1

    output = capsys.readouterr().out
    assert output.startswith("EXCHANGE_RULES_RUN ")
    assert "EXCHANGE_RULES_CHANGE " in output
    assert '"status": "review_required"' in output


def test_collect_cli_returns_zero_for_configured_closed_day(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    module = _load_script("collect_exchange_rules")
    arguments = [
        "--artifact-root",
        str(tmp_path / "closed-day"),
        "--reference",
        str(REFERENCE),
        "--registry",
        str(REGISTRY),
        "--legacy-three-products",
        "--now",
        "2026-09-30T20:30:00+08:00",
        "--trading-day",
        "2026-10-01",
    ]

    assert module.main(arguments) == 0

    output = capsys.readouterr().out
    assert output.startswith("EXCHANGE_RULES_RUN ")
    assert '"status": "skipped_closed_day"' in output


def test_collect_cli_reports_validation_and_lock_failures(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_script("collect_exchange_rules")

    assert module.main(["--now", "not-an-iso-datetime"]) == 2
    assert capsys.readouterr().out.startswith("EXCHANGE_RULES_FAIL ValueError")

    class LockedCollection:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise CollectionLockedError("another collector owns the lock")

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr(module, "CollectionLock", LockedCollection)
    assert module.main(["--artifact-root", str(tmp_path / "locked"), "--now", NOW.isoformat()]) == 2
    output = capsys.readouterr().out
    assert output.startswith("EXCHANGE_RULES_FAIL CollectionLockedError")
    assert "another collector owns the lock" in output


def test_review_cli_rejects_candidate_without_changing_current(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    module = _load_script("review_exchange_rules")
    artifact_root = tmp_path / "reject"
    candidate = _collect_candidate(artifact_root)

    arguments = [
        "--artifact-root",
        str(artifact_root),
        "--snapshot",
        candidate.snapshot_id,
        "--reviewer",
        "reviewer-secret",
        "--reason",
        "reason-secret",
        "--now",
        NOW.isoformat(),
        "reject",
    ]
    assert module.main(arguments) == 0

    output = capsys.readouterr().out
    assert output.startswith("EXCHANGE_RULES_REJECTED ")
    assert "reviewer-secret" not in output
    assert "reason-secret" not in output
    assert ExchangeRuleArtifactStore(artifact_root).current_snapshot_id() is None


def test_review_cli_approves_candidate_and_emits_no_reviewer_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    module = _load_script("review_exchange_rules")
    artifact_root = tmp_path / "approve"
    candidate = _collect_candidate(artifact_root)

    arguments = [
        "--artifact-root",
        str(artifact_root),
        "--snapshot",
        candidate.snapshot_id,
        "--reviewer",
        "reviewer-secret",
        "--reason",
        "reason-secret",
        "--now",
        NOW.isoformat(),
        "approve",
    ]
    assert module.main(arguments) == 0

    output = capsys.readouterr().out
    assert output.startswith("EXCHANGE_RULES_APPROVED ")
    assert "reviewer-secret" not in output
    assert "reason-secret" not in output
    store = ExchangeRuleArtifactStore(artifact_root)
    assert store.current_snapshot_id() is not None


def test_review_cli_reports_missing_candidate_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    module = _load_script("review_exchange_rules")
    arguments = [
        "--artifact-root",
        str(tmp_path / "missing"),
        "--snapshot",
        "a" * 64,
        "--reviewer",
        "reviewer",
        "--reason",
        "missing candidate",
        "--now",
        NOW.isoformat(),
        "reject",
    ]

    assert module.main(arguments) == 2

    output = capsys.readouterr().out
    assert output.startswith("EXCHANGE_RULES_REVIEW_FAIL ExchangeRuleArtifactError")


def test_scope_review_cli_approves_explicit_candidate_field(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    module = _load_script("review_exchange_rules")
    artifact_root = tmp_path / "scope-approve"
    candidate = _collect_candidate(artifact_root)
    catalog_id = _store_catalog_run(ExchangeRuleArtifactStore(artifact_root), candidate)

    arguments = [
        "--artifact-root",
        str(artifact_root),
        "--snapshot",
        candidate.snapshot_id,
        "--reviewer",
        "reviewer-secret",
        "--reason",
        "reason-secret",
        "--now",
        NOW.isoformat(),
        "scope-approve",
        "--catalog-id",
        catalog_id,
        "--scope-kind",
        "contract",
        "--exchange",
        "SHFE",
        "--product",
        "RB",
        "--contract",
        "SHFE.rb2610",
        "--field",
        "fee:open",
    ]

    assert module.main(arguments) == 0

    output = capsys.readouterr().out
    assert output.startswith("EXCHANGE_RULE_SCOPE_APPROVED ")
    assert "reviewer-secret" not in output
    assert "reason-secret" not in output


def test_scope_review_cli_refuses_unconfirmed_or_unresolved_whole_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    module = _load_script("review_exchange_rules")
    artifact_root = tmp_path / "scope-all"
    candidate = _collect_candidate(artifact_root)
    catalog_id = _store_catalog_run(ExchangeRuleArtifactStore(artifact_root), candidate)
    arguments = [
        "--artifact-root",
        str(artifact_root),
        "--snapshot",
        candidate.snapshot_id,
        "--reviewer",
        "reviewer",
        "--reason",
        "review",
        "--now",
        NOW.isoformat(),
        "scope-approve",
        "--catalog-id",
        catalog_id,
        "--scope-kind",
        "contract",
        "--exchange",
        "SHFE",
        "--product",
        "RB",
        "--contract",
        "SHFE.rb2610",
        "--all-scope-fields",
    ]

    assert module.main(arguments) == 2
    assert "requires --confirm-all-scope-fields" in capsys.readouterr().out

    arguments.append("--confirm-all-scope-fields")
    assert module.main(arguments) == 2
    assert "refuses scopes containing review-required fields" in capsys.readouterr().out
