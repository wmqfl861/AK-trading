"""NF20 comparison-protocol tests: H20.7/H20.8 verdict state machine proven
with DETERMINISTIC SIMULATED timing vectors (no real timing), plus baseline
promotion guards, policy validation, report rendering and drift checks.

Every record built here carries ``origin=TEST_SIMULATED`` unless a test
explicitly needs a promotable SYNTHETIC record; promotion structurally
refuses TEST_SIMULATED, so simulated vectors can never enter a real
baseline store (work-order acceptance: simulated timing stays TEST).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import conftest as helpers
from conftest import default_policy_entry, write_policy, write_run

from tools.benchmarks import compare
from tools.benchmarks import contracts as C

CASE = "t/case1"
_SHA_ACTUAL_FIXTURE = None  # filled per-test from disk


def _promote_baseline(tmp_path: Path, record: dict) -> compare.BaselineStore:
    store = compare.BaselineStore(tmp_path / "baselines")
    store.promote(
        case_id=record["case_id"],
        record=record,
        source_run_id=record["run_id"],
        source_run_dir=tmp_path,
        promoter="nf20-test",
    )
    return store


def _verdict(
    tmp_path: Path,
    candidate: dict,
    policy_entry: dict,
    *,
    baseline: dict = None,
    citation_problems=None,
    pre_promote: bool = True,
):
    if baseline is None:
        baseline = helpers.build_result_record(
            run_id="run-base-0001", origin=C.ORIGIN_SYNTHETIC
        )
    store_dir = tmp_path / "baselines"
    if store_dir.exists():
        # keep independent stores per call within one test
        store_dir = tmp_path / ("baselines-%d" % len(list(tmp_path.iterdir())))
    store = compare.BaselineStore(store_dir)
    if pre_promote:
        store.promote(
            case_id=baseline["case_id"], record=baseline,
            source_run_id=baseline["run_id"], source_run_dir=tmp_path,
            promoter="nf20-test",
        )
    return compare.verdict_for_case(candidate, store, policy_entry, citation_problems)


# ---------------------------------------------------------------------------
# H20.8 verdict arithmetic with hand vectors (median metric)
# ---------------------------------------------------------------------------

class TestVerdictVectors:
    def test_regression(self, tmp_path):
        cand = helpers.build_result_record(samples=[1_500_000] * 100)
        verdict = _verdict(tmp_path, cand, default_policy_entry())
        assert verdict["verdict"] == C.VERDICT_REGRESSION
        assert verdict["rel_change"] == pytest.approx(0.5)

    def test_improvement_is_no_regression_not_speedup_claim(self, tmp_path):
        cand = helpers.build_result_record(samples=[900_000] * 100)
        verdict = _verdict(tmp_path, cand, default_policy_entry())
        assert verdict["verdict"] == C.VERDICT_NO_REGRESSION
        assert "not" in verdict["reason"] and "claimed" in verdict["reason"]

    def test_within_tolerance_but_above_noise(self, tmp_path):
        cand = helpers.build_result_record(samples=[1_050_000] * 100)
        verdict = _verdict(tmp_path, cand, default_policy_entry())
        assert verdict["verdict"] == C.VERDICT_NO_REGRESSION

    def test_within_noise_is_inconclusive(self, tmp_path):
        cand = helpers.build_result_record(samples=[1_010_000] * 100)
        verdict = _verdict(tmp_path, cand, default_policy_entry())
        assert verdict["verdict"] == C.VERDICT_INCONCLUSIVE
        assert "WITHIN_NOISE" in verdict["reason"]

    def test_absolute_floor_guards_small_case(self, tmp_path):
        cand = helpers.build_result_record(samples=[1_200_000] * 100)
        entry = default_policy_entry(absolute_floor_ns=300_000)  # delta 200k below floor
        verdict = _verdict(tmp_path, cand, entry)
        assert verdict["verdict"] == C.VERDICT_NO_REGRESSION

    def test_insufficient_samples(self, tmp_path):
        cand = helpers.build_result_record(samples=[1_500_000] * 50)  # n=50 < 100
        verdict = _verdict(tmp_path, cand, default_policy_entry())
        assert verdict["verdict"] == C.VERDICT_INCONCLUSIVE
        assert "INSUFFICIENT_SAMPLES" in verdict["reason"]

    def test_zero_denominator_is_invalid(self, tmp_path):
        base = helpers.build_result_record(
            run_id="run-base-0001", origin=C.ORIGIN_SYNTHETIC, median_ns=0
        )
        cand = helpers.build_result_record(samples=[1_500_000] * 100)
        verdict = _verdict(tmp_path, cand, default_policy_entry(), baseline=base)
        assert verdict["verdict"] == C.VERDICT_INVALID
        assert "ZERO_DENOMINATOR" in verdict["reason"]

    def test_metric_unavailable(self, tmp_path):
        base = helpers.build_result_record(
            run_id="run-base-0001", origin=C.ORIGIN_SYNTHETIC, sample_mode="amortized",
            batch_count=5, measurement_over={"throughput_units_per_s": 100.0},
        )
        cand = helpers.build_result_record(
            sample_mode="amortized", batch_count=5,
            measurement_over={"throughput_units_per_s": 50.0},
        )
        entry = default_policy_entry(metric="median_ns", min_samples=5)  # owner misregistration
        verdict = _verdict(tmp_path, cand, entry, baseline=base)
        assert verdict["verdict"] == C.VERDICT_INVALID
        assert "METRIC_UNAVAILABLE" in verdict["reason"]

    def test_throughput_metric_regressions(self, tmp_path):
        base = helpers.build_result_record(
            run_id="run-base-0001", origin=C.ORIGIN_SYNTHETIC, sample_mode="amortized",
            batch_count=5, measurement_over={"throughput_units_per_s": 100.0},
        )
        entry = default_policy_entry(
            metric="throughput", absolute_floor_ns=None, min_samples=5
        )
        slower = helpers.build_result_record(
            sample_mode="amortized", batch_count=5,
            measurement_over={"throughput_units_per_s": 50.0},
        )
        assert _verdict(tmp_path, slower, entry, baseline=base)["verdict"] == C.VERDICT_REGRESSION
        within_noise = helpers.build_result_record(
            sample_mode="amortized", batch_count=5,
            measurement_over={"throughput_units_per_s": 99.5},
        )
        noise_verdict = _verdict(tmp_path, within_noise, entry, baseline=base)
        assert noise_verdict["verdict"] == C.VERDICT_INCONCLUSIVE
        faster = helpers.build_result_record(
            sample_mode="amortized", batch_count=5,
            measurement_over={"throughput_units_per_s": 150.0},
        )
        assert _verdict(tmp_path, faster, entry, baseline=base)["verdict"] == C.VERDICT_NO_REGRESSION


# ---------------------------------------------------------------------------
# H20.7 matching - INCOMPARABLE reasons
# ---------------------------------------------------------------------------

class TestMatching:
    def test_cross_host_incomparable(self, tmp_path):
        cand = helpers.build_result_record(host_id="hostffffffffffff99")
        verdict = _verdict(tmp_path, cand, default_policy_entry())
        assert verdict["verdict"] == C.VERDICT_INCOMPARABLE
        assert any("HOST_MISMATCH" in r for r in verdict["match_reasons"])

    def test_cross_backend_incomparable(self, tmp_path):
        cand = helpers.build_result_record(backend="sqlite-temp")
        verdict = _verdict(tmp_path, cand, default_policy_entry())
        assert verdict["verdict"] == C.VERDICT_INCOMPARABLE
        assert any("BACKEND_MISMATCH" in r for r in verdict["match_reasons"])

    def test_tampered_fixture_incomparable(self, tmp_path):
        cand = helpers.build_result_record(fixture_sha="b" * 64)
        verdict = _verdict(tmp_path, cand, default_policy_entry())
        assert verdict["verdict"] == C.VERDICT_INCOMPARABLE
        assert any("FIXTURE_MISMATCH" in r for r in verdict["match_reasons"])

    def test_workload_params_incomparable(self, tmp_path):
        cand = helpers.build_result_record(params={"seed": 999})
        verdict = _verdict(tmp_path, cand, default_policy_entry())
        assert verdict["verdict"] == C.VERDICT_INCOMPARABLE
        assert any("WORKLOAD_PARAMS_MISMATCH" in r for r in verdict["match_reasons"])

    def test_measurement_scope_incomparable(self, tmp_path):
        cand = helpers.build_result_record(measurement_spec={"batch_size": 50})
        verdict = _verdict(tmp_path, cand, default_policy_entry())
        assert verdict["verdict"] == C.VERDICT_INCOMPARABLE
        assert any("MEASUREMENT_SCOPE_MISMATCH" in r for r in verdict["match_reasons"])

    def test_cross_profile_incomparable(self, tmp_path):
        cand = helpers.build_result_record(scope="smoke")
        verdict = _verdict(tmp_path, cand, default_policy_entry())
        assert verdict["verdict"] == C.VERDICT_INCOMPARABLE
        assert any("SCOPE_PROFILE_MISMATCH" in r for r in verdict["match_reasons"])

    def test_dependency_version_incomparable(self, tmp_path):
        cand = helpers.build_result_record(
            environment_over={"dependencies": {"numpy": "2.1.0"}}
        )
        verdict = _verdict(tmp_path, cand, default_policy_entry())
        assert verdict["verdict"] == C.VERDICT_INCOMPARABLE
        assert any("ENV_MISMATCH(dependencies)" in r for r in verdict["match_reasons"])

    def test_contract_version_incomparable(self, tmp_path):
        cand = helpers.build_result_record(
            identity_over={"contract_version": "C20-0.9.0"}
        )
        verdict = _verdict(tmp_path, cand, default_policy_entry())
        assert verdict["verdict"] == C.VERDICT_INCOMPARABLE
        assert any("CONTRACT_VERSION_MISMATCH" in r for r in verdict["match_reasons"])

    def test_source_hash_difference_is_comparable(self, tmp_path):
        """H20.7 counterexample: differing source hashes must NOT yield
        INCOMPARABLE - that difference is the purpose of the comparison."""
        cand = helpers.build_result_record(
            samples=[1_050_000] * 100,  # above noise, within tolerance
            source_sha="b" * 64,
            source_map_sha="c" * 64,
        )
        verdict = _verdict(tmp_path, cand, default_policy_entry())
        assert verdict["match_reasons"] == []
        assert verdict["verdict"] == C.VERDICT_NO_REGRESSION
        base_sha = verdict["baseline"]["measured_modules"][0]["sha256"]
        cand_sha = verdict["candidate"]["measured_modules"][0]["sha256"]
        assert base_sha != cand_sha  # both sides preserved separately
        assert verdict["baseline"]["source_map_sha256"] != verdict["candidate"]["source_map_sha256"]

    def test_baseline_not_ok_incomparable(self, tmp_path):
        # a non-OK baseline cannot enter the store via promote; place it
        # directly to prove the comparison side rejects it too
        base = helpers.build_result_record(
            run_id="run-base-0001", origin=C.ORIGIN_SYNTHETIC, status="TIMEOUT"
        )
        store_dir = tmp_path / "baselines"
        target = store_dir / "t__case1" / "v1"
        target.mkdir(parents=True)
        C.write_json_atomic(target / "result.json", base)
        C.write_json_atomic(
            target / "meta.json",
            {"schema": C.BASELINE_META_SCHEMA, "baseline_version": "v1",
             "case_id": CASE, "source_run_id": "run-base-0001", "promoter": "nf20-test"},
        )
        store = compare.BaselineStore(store_dir)
        verdict = compare.verdict_for_case(
            helpers.build_result_record(), store, default_policy_entry(), None
        )
        assert verdict["verdict"] == C.VERDICT_INCOMPARABLE
        assert "BASELINE_NOT_OK" in verdict["reason"]


# ---------------------------------------------------------------------------
# H20.7/H20.8 NO_BASELINE / policy gates / candidate gating
# ---------------------------------------------------------------------------

class TestVerdictGates:
    def test_no_baseline_when_store_empty(self, tmp_path):
        cand = helpers.build_result_record()
        store = compare.BaselineStore(tmp_path / "empty-store")
        verdict = compare.verdict_for_case(cand, store, default_policy_entry(), None)
        assert verdict["verdict"] == C.VERDICT_NO_BASELINE
        assert "NO_BASELINE_RECORD" in verdict["reason"]

    def test_pinned_version_missing(self, tmp_path):
        base = helpers.build_result_record(run_id="run-base-0001", origin=C.ORIGIN_SYNTHETIC)
        # promote once -> v1 exists; policy pins v2 (absent)
        store = _promote_baseline(tmp_path, base)
        entry = default_policy_entry(baseline_version="v2")
        verdict = compare.verdict_for_case(
            helpers.build_result_record(), store, entry, None
        )
        assert verdict["verdict"] == C.VERDICT_NO_BASELINE
        assert "PINNED_VERSION_MISSING" in verdict["reason"]

    def test_policy_not_preregistered_is_inconclusive(self, tmp_path):
        base = helpers.build_result_record(run_id="run-base-0001", origin=C.ORIGIN_SYNTHETIC)
        store = _promote_baseline(tmp_path, base)
        verdict = compare.verdict_for_case(helpers.build_result_record(), store, None, None)
        assert verdict["verdict"] == C.VERDICT_INCONCLUSIVE
        assert "POLICY_NOT_PREREGISTERED" in verdict["reason"]

    def test_policy_entry_invalid_citations(self, tmp_path):
        base = helpers.build_result_record(run_id="run-base-0001", origin=C.ORIGIN_SYNTHETIC)
        store = _promote_baseline(tmp_path, base)
        verdict = compare.verdict_for_case(
            helpers.build_result_record(), store, default_policy_entry(),
            ["cited baseline run run-x has no record for t/case1"],
        )
        assert verdict["verdict"] == C.VERDICT_INCONCLUSIVE
        assert "POLICY_ENTRY_INVALID" in verdict["reason"]

    @pytest.mark.parametrize("bad_status", ["INVALID", "TIMEOUT", "ERROR", "DRIFT"])
    def test_candidate_not_ok_is_invalid(self, tmp_path, bad_status):
        cand = helpers.build_result_record(status=bad_status, status_reason=None)
        verdict = _verdict(tmp_path, cand, default_policy_entry())
        assert verdict["verdict"] == C.VERDICT_INVALID
        assert "CANDIDATE_NOT_OK_%s" % bad_status in verdict["reason"]


# ---------------------------------------------------------------------------
# H20.8 policy file validation
# ---------------------------------------------------------------------------

class TestPolicyValidation:
    def test_fallback_thresholds_must_be_false(self, tmp_path):
        path = write_policy(tmp_path / "p.json", {}, fallback_false=False)
        with pytest.raises(C.ContractError, match="fallback"):
            compare.load_policy(path)

    def test_single_citation_rejected(self, tmp_path):
        entry = default_policy_entry(baseline_evidence=["run-b1"])
        path = write_policy(tmp_path / "p.json", {CASE: entry})
        with pytest.raises(C.ContractError, match="baseline_evidence"):
            compare.load_policy(path)

    def test_missing_responsibility_rejected(self, tmp_path):
        entry = default_policy_entry()
        del entry["registered_by"]
        path = write_policy(tmp_path / "p.json", {CASE: entry})
        with pytest.raises(C.ContractError, match="registered_by"):
            compare.load_policy(path)

    def test_duplicate_citations_rejected(self, tmp_path):
        entry = default_policy_entry(baseline_evidence=["run-b1", "run-b1"])
        path = write_policy(tmp_path / "p.json", {CASE: entry})
        with pytest.raises(C.ContractError, match="baseline_evidence"):
            compare.load_policy(path)

    def test_citations_validated_against_runs_root(self, tmp_path):
        entry = default_policy_entry()  # cites run-b1/run-b2 which do not exist
        problems = compare.validate_policy_citations(
            {"cases": {CASE: entry}}, tmp_path / "runs-root"
        )
        assert CASE in problems and len(problems[CASE]) == 2

    def test_throughput_metric_rejects_absolute_floor(self, tmp_path):
        entry = default_policy_entry(metric="throughput", absolute_floor_ns=100)
        path = write_policy(tmp_path / "p.json", {CASE: entry})
        with pytest.raises(C.ContractError, match="absolute_floor_ns"):
            compare.load_policy(path)


# ---------------------------------------------------------------------------
# baseline promotion guards (H20.8 / H20.9: manual, never automatic)
# ---------------------------------------------------------------------------

class TestPromotion:
    def _promotable(self, **kw):
        return helpers.build_result_record(
            run_id="run-promotable-0001", origin=C.ORIGIN_SYNTHETIC, **kw
        )

    def test_ok_formal_synthetic_promotes_to_v1(self, tmp_path):
        store = _promote_baseline(tmp_path, self._promotable())
        assert store.versions(CASE) == [1]
        meta = C.load_json_file(
            tmp_path / "baselines" / "t__case1" / "v1" / "meta.json", "meta"
        )
        assert meta["promoter"] == "nf20-test"
        assert "policy" in meta["policy_note"]

    def test_refuses_test_simulated_origin(self, tmp_path):
        record = helpers.build_result_record(origin=C.ORIGIN_TEST_SIMULATED)
        store = compare.BaselineStore(tmp_path / "baselines")
        with pytest.raises(C.ContractError, match="TEST_SIMULATED"):
            store.promote(CASE, record, "run-x", tmp_path, "nf20-test")

    def test_refuses_smoke_scope(self, tmp_path):
        record = self._promotable(scope="smoke")
        store = compare.BaselineStore(tmp_path / "baselines")
        with pytest.raises(C.ContractError, match="formal"):
            store.promote(CASE, record, "run-x", tmp_path, "nf20-test")

    def test_refuses_not_ok(self, tmp_path):
        record = self._promotable(status="INVALID")
        store = compare.BaselineStore(tmp_path / "baselines")
        with pytest.raises(C.ContractError, match="only OK"):
            store.promote(CASE, record, "run-x", tmp_path, "nf20-test")

    def test_refuses_not_eligible_case(self, tmp_path):
        record = self._promotable(eligible=False)
        store = compare.BaselineStore(tmp_path / "baselines")
        with pytest.raises(C.ContractError, match="eligible_for_baseline"):
            store.promote(CASE, record, "run-x", tmp_path, "nf20-test")

    def test_refuses_missing_promoter(self, tmp_path):
        store = compare.BaselineStore(tmp_path / "baselines")
        with pytest.raises(C.ContractError, match="promoter"):
            store.promote(CASE, self._promotable(), "run-x", tmp_path, "  ")

    def test_second_promotion_creates_v2_never_overwrites(self, tmp_path):
        store = _promote_baseline(tmp_path, self._promotable())
        second = helpers.build_result_record(
            run_id="run-promotable-0002", origin=C.ORIGIN_SYNTHETIC
        )
        version = store.promote(CASE, second, "run-promotable-0002", tmp_path, "nf20-test")
        assert version == 2
        first = C.load_json_file(
            tmp_path / "baselines" / "t__case1" / "v1" / "result.json", "v1"
        )
        assert first["run_id"] == "run-promotable-0001"  # v1 untouched

    def test_policy_pin_not_auto_swapped_by_new_promotion(self, tmp_path):
        """H20.8 counterexample: an (even authorized) new baseline version must
        not silently change what comparisons run against."""
        base = self._promotable()
        store = _promote_baseline(tmp_path, base)
        cand = helpers.build_result_record(samples=[1_000_000] * 100)
        entry = default_policy_entry()  # pins v1
        first = compare.verdict_for_case(cand, store, entry, None)
        assert first["baseline"]["baseline_version"] == "v1"
        newer = helpers.build_result_record(
            run_id="run-promotable-0002", origin=C.ORIGIN_SYNTHETIC,
            median_ns=5_000_000,
        )
        store.promote(CASE, newer, "run-promotable-0002", tmp_path, "nf20-test")
        after = compare.verdict_for_case(cand, store, entry, None)
        assert after["baseline"]["baseline_version"] == "v1"  # pin honored
        assert after["verdict"] == first["verdict"]


# ---------------------------------------------------------------------------
# compare_run end-to-end (report files + exit codes)
# ---------------------------------------------------------------------------

class TestCompareRun:
    def _setup(self, tmp_path, cand_samples, entry=None, base_samples=None):
        base_samples = base_samples or [1_000_000] * 100
        base = helpers.build_result_record(
            run_id="run-base-0001", origin=C.ORIGIN_SYNTHETIC, samples=base_samples
        )
        store = _promote_baseline(tmp_path, base)
        cand = helpers.build_result_record(run_id="run-cand-0001", samples=cand_samples)
        run_dir = write_run(tmp_path / "runs-root", "run-cand-0001", [cand])
        policy = write_policy(
            tmp_path / "policy.json", {CASE: entry or default_policy_entry()}
        )
        return run_dir, tmp_path / "baselines", policy

    def test_regression_report_rc3(self, tmp_path):
        run_dir, store, policy = self._setup(tmp_path, [1_500_000] * 100)
        out = tmp_path / "report-out"
        rc, report = compare.compare_run(run_dir, store, policy, out)
        assert rc == 3
        assert report["summary"][C.VERDICT_REGRESSION] == 1
        assert (out / "report.json").is_file() and (out / "report.md").is_file()
        markdown = (out / "report.md").read_text(encoding="utf-8")
        assert CASE in markdown and "REGRESSION" in markdown
        assert "source hashes" in markdown or "source" in markdown.lower()

    def test_no_baseline_report_rc0(self, tmp_path):
        cand = helpers.build_result_record()
        run_dir = write_run(tmp_path / "runs-root", "run-cand-0001", [cand])
        policy = write_policy(tmp_path / "policy.json", {})
        rc, report = compare.compare_run(
            run_dir, tmp_path / "empty-store", policy, tmp_path / "report-out"
        )
        assert rc == 0  # NO_BASELINE is informational, never a pass or failure
        assert report["summary"][C.VERDICT_NO_BASELINE] == 1

    def test_refuses_existing_output_dir(self, tmp_path):
        run_dir, store, policy = self._setup(tmp_path, [1_000_000] * 100)
        out = tmp_path / "report-out"
        out.mkdir()
        with pytest.raises(C.ContractError, match="already exists"):
            compare.compare_run(run_dir, store, policy, out)

    def test_garbage_record_rejected_on_load(self, tmp_path):
        run_dir = tmp_path / "runs" / "run-bad"
        (run_dir / "results").mkdir(parents=True)
        (run_dir / "results" / "x.json").write_text("not json", encoding="utf-8")
        (run_dir / "run.json").write_text("{}", encoding="utf-8")
        with pytest.raises(C.ContractError):
            compare.load_run(run_dir)


# ---------------------------------------------------------------------------
# drift (H20.9/H20.10)
# ---------------------------------------------------------------------------

def _drift_record(root: Path):
    """Materialize a record whose identity matches real files under root."""
    (root / "fixtures").mkdir(exist_ok=True)
    (root / "workloads").mkdir(exist_ok=True)
    (root / "cases").mkdir(exist_ok=True)
    (root / "src" / "t" / "prod").mkdir(parents=True, exist_ok=True)
    (root / "fixtures" / "input.bin").write_bytes(b"fixture-data")
    (root / "workloads" / "w.py").write_text("# workload\n", encoding="utf-8", newline="\n")
    (root / "cases" / "t__case1.json").write_text("{}", encoding="utf-8", newline="\n")
    (root / "source-map.json").write_text("{}", encoding="utf-8", newline="\n")
    (root / "src" / "t" / "prod" / "thing.py").write_text("v1\n", encoding="utf-8", newline="\n")
    return helpers.build_result_record(
        run_id="run-drift-0001",
        origin=C.ORIGIN_SYNTHETIC,
        identity_over={
            "case_manifest": {
                "path": "cases/t__case1.json",
                "sha256": C.sha256_bytes(b"{}"),
            },
            "source_map": {
                "path": "source-map.json",
                "sha256": C.sha256_bytes(b"{}"),
            },
            "fixtures": [
                {
                    "path": "fixtures/input.bin",
                    "role": "input",
                    "sha256": C.sha256_bytes(b"fixture-data"),
                    "bytes": 12,
                }
            ],
            "workload": {
                "source": "workloads/w.py",
                "sha256": C.sha256_bytes(b"# workload\n"),
            },
            "measured_modules": [
                {
                    "logical_module": "t.prod.thing",
                    "source_relative_path": "src/t/prod/thing.py",
                    "sha256": C.sha256_bytes(b"v1\n"),
                    "release_id": "WORKTREE_SNAPSHOT",
                    "provenance_kind": "WORKTREE_SNAPSHOT",
                }
            ],
        },
    )


class TestDrift:
    def test_no_drift_when_invariants_unchanged(self, tmp_path):
        report = compare.drift_check(tmp_path, _drift_record(tmp_path))
        assert report["status"] == "NO_DRIFT"

    def test_changed_fixture_is_drift(self, tmp_path):
        record = _drift_record(tmp_path)
        (tmp_path / "fixtures" / "input.bin").write_bytes(b"tampered")
        report = compare.drift_check(tmp_path, record)
        assert report["status"] == "DRIFT"
        assert any(item["state"] == "CHANGED" for item in report["invariant_items"])

    def test_missing_fixture_is_drift(self, tmp_path):
        record = _drift_record(tmp_path)
        (tmp_path / "fixtures" / "input.bin").unlink()
        report = compare.drift_check(tmp_path, record)
        assert report["status"] == "DRIFT"
        assert any(item["state"] == "MISSING" for item in report["invariant_items"])

    def test_source_change_is_informational_not_drift(self, tmp_path):
        record = _drift_record(tmp_path)
        (tmp_path / "src" / "t" / "prod" / "thing.py").write_text("v2 candidate\n")
        report = compare.drift_check(tmp_path, record)
        assert report["status"] == "NO_DRIFT"  # source evolution is expected
        assert any(
            item["state"] == "INFO_SOURCE_CHANGED" for item in report["source_items"]
        )

    def test_cli_drift_exit_codes(self, tmp_path):
        record = _drift_record(tmp_path)
        run_dir = write_run(tmp_path / "runs-root", "run-drift-0001", [record])
        out1 = tmp_path / "drift1.json"
        rc1, stdout1, _ = helpers.run_cli(
            ["drift", "--run", str(run_dir), "--out", str(out1), "--root", str(tmp_path)]
        )
        assert rc1 == 0 and "NO_DRIFT" in stdout1
        (tmp_path / "workloads" / "w.py").write_text("# workload changed\n", encoding="utf-8")
        rc2, _, _ = helpers.run_cli(
            ["drift", "--run", str(run_dir), "--out", str(tmp_path / "drift2.json"),
             "--root", str(tmp_path)]
        )
        assert rc2 == 3
        report = json.loads((tmp_path / "drift2.json").read_text(encoding="utf-8"))
        assert report["cases"][0]["status"] == "DRIFT"


# ---------------------------------------------------------------------------
# real core-case smoke through the full compare entry (informational only)
# ---------------------------------------------------------------------------

class TestRealCoreCompare:
    def test_compare_core_smoke_run_yields_no_baseline(self, tmp_path):
        """End-to-end: a REAL core smoke run compared against the shipped
        empty policy/store must report NO_BASELINE per case (rc 0, honest
        first-round semantics - no baselines established yet)."""
        rc, out, err = helpers.run_cli(
            [
                "run",
                "--source-map", "tests/benchmarks/fixtures/core/source-map.json",
                "--suite", "tools/benchmarks/suite.json",
                "--case", "core/counter_digest",
                "--case", "core/amortized_rows",
                "--case", "core/sqlite_temp_write",
                "--output-root", str(tmp_path / "out"),
                "--profile", "smoke",
            ],
            timeout=170,
        )
        assert rc == 0, err
        run_dir = tmp_path / "out" / "runs" / helpers.only_run_id(tmp_path / "out")
        (tmp_path / "baselines").mkdir()
        rc_cmp, stdout, _ = helpers.run_cli(
            [
                "compare", "--candidate", str(run_dir),
                "--baseline-store", str(tmp_path / "baselines"),
                "--policy", "docs/benchmarks/baseline-policy.json",
                "--out", str(tmp_path / "report"),
            ],
            timeout=60,
        )
        assert rc_cmp == 0
        assert "NO_BASELINE=3" in stdout
        report = json.loads((tmp_path / "report" / "report.json").read_text(encoding="utf-8"))
        assert report["summary"]["NO_BASELINE"] == 3
