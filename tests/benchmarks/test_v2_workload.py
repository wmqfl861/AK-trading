"""NF21 v2 workload tests (own scope only; run with --confcutdir=tests/benchmarks).

Covers the seven NF21 case workloads directly (determinism, pre-registered
counts, prefix invariance, honest INSUFFICIENT retention) plus the frozen
artifacts (manifest/fixture binding, source-map vs worktree) and one CLI
smoke for the fastest case to prove end-to-end C20 integration.

Never collects or modifies anything outside the NF21 v2 domain subtree.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.benchmarks import contracts as C  # noqa: E402

WORKLOAD = "tools/benchmarks/workloads/v2.py"
CASES_DIR = REPO_ROOT / "tools" / "benchmarks" / "cases" / "v2"
FIXTURES = REPO_ROOT / "tools" / "benchmarks" / "fixtures" / "v2"

CASE_ORDER = [
    "feature_prefix_cold",
    "feature_prefix_hot",
    "decision_replay_wait_only",
    "decision_replay_filled",
    "validation_forecasts",
    "validation_bootstrap",
    "validation_replay_metrics",
]


def load_manifest(case: str) -> dict:
    manifest = C.load_json_file(CASES_DIR / ("%s.json" % case), "case manifest")
    return C.validate_case_manifest(manifest)


def load_workload():
    path = (REPO_ROOT / WORKLOAD).resolve()
    spec = importlib.util.spec_from_file_location("nf21_v2_workload_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_ctx(manifest: dict, tmp_dir: Path) -> C.CaseContext:
    return C.CaseContext(
        params=manifest["params"],
        seed=int(manifest["params"]["seed"]),
        tmp_dir=tmp_dir,
        resources={"temp_sqlite_path": None},
        normalizations=list(manifest["correctness"].get("normalizations", [])),
    )


def run_workload(manifest: dict, tmp_dir: Path, warmup: int, operations: int) -> dict:
    module = load_workload()
    ctx = make_ctx(manifest, tmp_dir)
    workload = module.build(ctx)
    C.validate_workload_object(
        workload, manifest["measurement"]["sample_mode"],
        manifest["measurement"]["reset_method"])
    workload.setup(ctx)
    for _ in range(warmup):
        workload.operation(ctx)
    for _ in range(operations):
        result = workload.operation(ctx)
        assert result["ok"], result
    verification = workload.verify(ctx)
    C.validate_verification_result(verification)
    workload.teardown(ctx)
    return verification


# ---------------------------------------------------------------------------
# frozen artifacts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASE_ORDER)
def test_manifests_validate_and_bind_fixtures(case: str) -> None:
    manifest = load_manifest(case)
    assert manifest["origin"] == "SYNTHETIC"
    assert manifest["case_id"] == "v2/%s" % case
    assert manifest["resources"] == {
        "network": False, "production_db": False, "temp_sqlite": False}
    for fixture in manifest["fixtures"]:
        assert fixture["role"] in ("input", "workload")
        path = REPO_ROOT / fixture["path"]
        assert path.is_file(), fixture["path"]
    # every manifest's frozen expected digest must be a real digest now
    assert C.HEX64_RE.match(manifest["correctness"]["expected_digest"])


def test_manifest_params_agree_with_frozen_fixture_jsons() -> None:
    fixtures = {
        name: json.loads((FIXTURES / ("%s.json" % name)).read_text(encoding="utf-8"))
        for name in ("feature_prefix", "decision_replay", "validation")
    }
    for case in CASE_ORDER:
        manifest = load_manifest(case)
        params = manifest["params"]
        matched = False
        for name, fixture in fixtures.items():
            shared = {k: v for k, v in fixture.items() if k in params}
            if shared and shared == {k: params[k] for k in shared}:
                matched = True
        assert matched, "case %s params bind no frozen fixture JSON" % case


def test_source_map_matches_worktree() -> None:
    source_map = C.load_json_file(FIXTURES / "source-map.json", "source map")
    C.validate_source_map(source_map)
    logical = set()
    for entry in source_map["modules"]:
        path = REPO_ROOT / entry["source_relative_path"]
        sha, size = C.sha256_file(path)
        assert sha == entry["sha256"] and size == entry["bytes"], entry["source_relative_path"]
        logical.add(entry["logical_module"])
    for case in CASE_ORDER:
        for measured in load_manifest(case)["measured_modules"]:
            assert measured in logical, (case, measured)


# ---------------------------------------------------------------------------
# feature prefix workloads
# ---------------------------------------------------------------------------

def test_feature_prefix_cold_deterministic_and_ready(tmp_path: Path) -> None:
    manifest = load_manifest("feature_prefix_cold")
    first = run_workload(manifest, tmp_path / "a", warmup=0, operations=6)
    second = run_workload(manifest, tmp_path / "b", warmup=0, operations=6)
    assert first["status"] == second["status"] == "PASS"
    assert first["digest"] == second["digest"]
    assert first["summary"]["statuses"] == ["READY"]
    assert first["summary"]["ready_values"] > 0
    assert sorted(first["summary"]["tiers"]) == [90, 150, 300]


def test_feature_prefix_hot_deterministic_and_cache_independent(tmp_path: Path) -> None:
    manifest = load_manifest("feature_prefix_hot")
    first = run_workload(manifest, tmp_path / "a", warmup=4, operations=12)
    second = run_workload(manifest, tmp_path / "b", warmup=4, operations=12)
    assert first["status"] == second["status"] == "PASS"
    assert first["digest"] == second["digest"]
    # the verify-stage cold-rebuild probe ran and matched
    assert first["summary"]["cache_identity_checks"] >= 1


def test_feature_snapshot_prefix_invariance_counterexample(tmp_path: Path) -> None:
    """Appending future bars (visible only after the decision cutoff) must
    not change the snapshot identity at the same decision (prefix-
    invariance probe from the M2 causality contract)."""
    from decimal import Decimal

    from ak_trading.v2.contracts import MarketBar

    module = load_workload()
    manifest = load_manifest("feature_prefix_cold")
    ctx = make_ctx(manifest, tmp_path)
    workload = module.build(ctx)
    workload.setup(ctx)
    factors = workload.factors
    minute = 60_000_000_000
    length = 150
    decision = workload.session.start_nano + length * minute
    baseline = factors.build_feature_snapshot(
        "RB2610", workload.bars[:length], decision, workload.specs, None, None,
        workload.rule, (workload.session,))
    template = workload.bars[0]
    extreme = tuple(
        MarketBar(
            bar_id="RB2610|future|%d" % i, contract_id="RB2610",
            raw_symbol="rb2610", product="SHFE.RB", duration_seconds=60,
            datetime_nano=workload.session.start_nano + (length + i) * minute,
            start_nano=workload.session.start_nano + (length + i) * minute,
            end_nano=workload.session.start_nano + (length + i + 1) * minute,
            trading_day=workload.session.trading_day,
            session_id=workload.session.session_id,
            available_at_nano=workload.session.start_nano + (length + i + 1) * minute,
            imported_at_nano=None, open=Decimal("9999"), high=Decimal("9999"),
            low=Decimal("9999"), close=Decimal("9999"),
            volume=Decimal("1000000000"), open_oi=Decimal(1), close_oi=Decimal(1),
            complete=True, expected_minutes=1, actual_minutes=1,
            source="synthetic-nf21-future", quality=template.quality)
        for i in range(20))
    extended = factors.build_feature_snapshot(
        "RB2610", workload.bars + extreme, decision, workload.specs, None, None,
        workload.rule, (workload.session,))
    assert extended.snapshot_id == baseline.snapshot_id
    assert extended.available_at_nano == baseline.available_at_nano


# ---------------------------------------------------------------------------
# decision replay workloads
# ---------------------------------------------------------------------------

def test_decision_replay_wait_only_honests_counts(tmp_path: Path) -> None:
    manifest = load_manifest("decision_replay_wait_only")
    verification = run_workload(manifest, tmp_path, warmup=1, operations=3)
    assert verification["status"] == "PASS"
    expected = verification["summary"]["expected_counts"]
    assert expected["fills"] == 0 and expected["decisions"] == 5
    assert verification["summary"]["statuses"] == ["COMPLETE"]


def test_decision_replay_filled_selection_risk_fills_and_account(tmp_path: Path) -> None:
    manifest = load_manifest("decision_replay_filled")
    first = run_workload(manifest, tmp_path / "a", warmup=1, operations=3)
    second = run_workload(manifest, tmp_path / "b", warmup=1, operations=3)
    assert first["status"] == second["status"] == "PASS"
    assert first["digest"] == second["digest"]
    expected = first["summary"]["expected_counts"]
    assert int(expected["fills"]) >= 2
    assert expected["fees_cny"] != "0"
    assert expected["terminal_cash_cny"] != "100000"
    assert expected["reconciliation_status"] == "PASS"
    # the replay module surface is restored after operations (no leaking patch)
    import ak_trading.v2.decision.replay as replay_module
    assert callable(replay_module.build_strategy_context)
    assert callable(replay_module.strategy_registry_v2)


# ---------------------------------------------------------------------------
# validation workloads
# ---------------------------------------------------------------------------

def test_validation_bootstrap_deterministic_interval(tmp_path: Path) -> None:
    manifest = load_manifest("validation_bootstrap")
    first = run_workload(manifest, tmp_path / "a", warmup=5, operations=10)
    second = run_workload(manifest, tmp_path / "b", warmup=5, operations=10)
    assert first["status"] == second["status"] == "PASS"
    assert first["digest"] == second["digest"]
    assert first["summary"]["replicates"] == 1000
    low, high = first["summary"]["ci"]
    assert low <= high


def test_validation_forecasts_keeps_honest_statuses(tmp_path: Path) -> None:
    manifest = load_manifest("validation_forecasts")
    first = run_workload(manifest, tmp_path / "a", warmup=1, operations=2)
    second = run_workload(manifest, tmp_path / "b", warmup=1, operations=2)
    assert first["status"] == second["status"] == "PASS"
    assert first["digest"] == second["digest"]
    # synthetic data must NOT claim statistical PASS everywhere
    assert "INSUFFICIENT" in first["summary"]["statuses"]
    assert first["summary"]["bootstrap_replicates"] == 1000


def test_validation_replay_metrics_retains_insufficient(tmp_path: Path) -> None:
    manifest = load_manifest("validation_replay_metrics")
    verification = run_workload(manifest, tmp_path, warmup=2, operations=5)
    assert verification["status"] == "PASS"
    # one closed synthetic episode keeps the production INSUFFICIENT verdict
    assert verification["summary"]["status"] == "INSUFFICIENT"
    assert verification["summary"]["episodes"] >= 1


# ---------------------------------------------------------------------------
# end-to-end CLI (single fast case; full-suite smoke runs live in evidence)
# ---------------------------------------------------------------------------

def test_cli_smoke_validation_bootstrap_case(tmp_path: Path) -> None:
    manifest = load_manifest("validation_bootstrap")
    env = {k: v for k, v in __import__("os").environ.items()}
    env["PYTHONHASHSEED"] = str(manifest["params"]["seed"])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "tools.benchmarks.cli", "run",
         "--source-map", "tools/benchmarks/fixtures/v2/source-map.json",
         "--output-root", str(tmp_path / "out"),
         "--profile", "smoke",
         "--case-manifest", "tools/benchmarks/cases/v2/validation_bootstrap.json",
         "--run-id", "nf21-test-cli-smoke"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    record_path = (tmp_path / "out" / "runs" / "nf21-test-cli-smoke" / "results"
                   / "v2__validation_bootstrap.json")
    record = C.load_json_file(record_path, "case record")
    C.validate_result_record(record)
    assert record["status"] == "OK"
    assert record["correctness"]["digest"] == manifest["correctness"]["expected_digest"]
    assert record["measurement"]["n"] == manifest["budget"]["smoke"]["operations"]
