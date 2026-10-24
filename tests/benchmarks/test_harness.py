"""NF20 harness tests: C20 statistics, path security, import gating, runner
status machine, budgets, correctness-first rules.

All fixtures are TEST synthetic material inside ``tmp_path`` mini-roots; no
repository file outside this suite's write set is read or written.  Workload
timings produced here are TEST evidence only and never enter a real
benchmark store (origin SYNTHETIC cases built here declare
eligible_for_baseline=false).
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

import conftest as helpers
from conftest import (
    WORKLOAD_FAILING_OPS,
    WORKLOAD_OK,
    WORKLOAD_RAISE,
    WORKLOAD_SELF_MODIFY,
    WORKLOAD_SENTINEL,
    WORKLOAD_SLOW,
    WORKLOAD_SOCKET,
    WORKLOAD_SQLITE,
    make_manifest,
    make_source_map,
    run_cli,
)
from conftest import WORKLOAD_SNEAKY  # noqa: E402  (kept separate for clarity)

from tools.benchmarks import contracts as C
from tools.benchmarks import runner

REPO_ROOT = helpers.REPO_ROOT


# ---------------------------------------------------------------------------
# H20.5 frozen statistics - deterministic hand vectors (no timing involved)
# ---------------------------------------------------------------------------

class TestStatistics:
    def test_nearest_rank_quantile_vectors(self):
        # median (num=1, den=2): ceil(n/2), 1-based
        assert C.quantile_index_1based(1, 2, 4) == 2
        assert C.quantile_index_1based(1, 2, 5) == 3
        assert C.quantile_index_1based(1, 2, 1) == 1
        values, idx = C.nearest_rank([10, 20, 30, 40], 1, 2)
        assert (values, idx) == (20, 2)
        # p95 (num=19, den=20): ceil(19n/20)
        assert C.quantile_index_1based(19, 20, 100) == 95
        assert C.quantile_index_1based(19, 20, 101) == 96
        assert C.quantile_index_1based(19, 20, 20) == 19
        assert C.quantile_index_1based(19, 20, 1) == 1  # clamped
        one_to_hundred = list(range(1, 101))
        assert C.nearest_rank(one_to_hundred, 1, 2)[0] == 50
        assert C.nearest_rank(one_to_hundred, 19, 20)[0] == 95

    def test_median_and_mad_hand_vectors(self):
        median, idx = C.median_nearest_rank([10, 20, 30, 40])
        assert (median, idx) == (20, 2)
        # |x-20| = [10, 0, 10, 10] -> sorted [0, 10, 10, 10] -> idx 2 -> 10
        assert C.mad_unscaled([10, 20, 30, 40]) == 10
        assert C.median_nearest_rank([1, 2, 3, 4, 5]) == (3, 3)
        # deviations [2,1,0,1,2] -> sorted [0,1,1,2,2] -> idx 3 -> 1
        assert C.mad_unscaled([1, 2, 3, 4, 5]) == 1

    def test_p95_gate_at_100_samples(self):
        slow = list(range(1, 101))  # n = 100 -> p95 emitted (95th value)
        stats = C.latency_stats(slow, "per_operation")
        assert stats["p95_ns"] == 95 and stats["p95_note"].startswith("P95_PRESENT")
        shorter = list(range(1, 100))  # n = 99 -> omitted, never invented
        stats99 = C.latency_stats(shorter, "per_operation")
        assert stats99["p95_ns"] is None
        assert stats99["p95_note"] == "P95_OMITTED_INSUFFICIENT_SAMPLES"
        assert stats99["median_ns"] == 50  # median still reported

    def test_amortized_has_no_latency_quantiles(self):
        stats = C.latency_stats([5_000_000, 6_000_000], "amortized")
        assert stats["n"] == 0
        for key in ("median_ns", "p95_ns", "min_ns", "max_ns", "mad_ns"):
            assert stats[key] is None
        assert stats["p95_note"] == "P95_NOT_DEFINED_FOR_AMORTIZED"

    def test_throughput_zero_denominator_guard(self):
        zeroed = C.throughput_stats([0, 0], [5, 5])
        assert zeroed["throughput_units_per_s"] is None
        assert zeroed["throughput_note"] == "THROUGHPUT_OMITTED_ZERO_DENOMINATOR"
        no_units = C.throughput_stats([100, 100], [0, 0])
        assert no_units["throughput_units_per_s"] is None
        ok = C.throughput_stats([100, 100], [1, 1])
        assert ok["throughput_units_per_s"] == pytest.approx(1e7)
        assert ok["throughput_denominator"] == "successful_operation_units"

    def test_digest_is_canonical_and_order_independent(self):
        assert C.digest_of({"b": 1, "a": 2}) == C.digest_of({"a": 2, "b": 1})
        assert C.digest_of({"a": 1}) != C.digest_of({"a": 2})


# ---------------------------------------------------------------------------
# H20.2 path security
# ---------------------------------------------------------------------------

class TestPathSecurity:
    @pytest.mark.parametrize(
        "bad",
        [
            "..",
            "a/../b",
            "/abs/path.py",
            "C:/x.py",
            "C:\\x.py",
            "\\\\server\\share\\x.py",
            "a\\b.py",
            ".git/config",
            "sub/.git/objects",
            "maintenance_artifacts/x.json",
            ".env",
            ".env.local",
            "conf/.env.production",
            "",
        ],
    )
    def test_rejected_relative_paths(self, bad):
        with pytest.raises(C.ContractError):
            C.validate_relative_path(bad)

    def test_valid_relative_path(self):
        assert C.validate_relative_path("a/b/c.py") == "a/b/c.py"

    def test_containment_escape_rejected(self, tmp_path):
        (tmp_path / "sub").mkdir()
        # a path that would escape via symlink/.. is caught before resolution
        with pytest.raises(C.ContractError):
            C.validate_relative_path("sub/../../escape.py")
        inside = C.resolve_under_root(tmp_path, "sub/x.py")
        assert inside == (tmp_path / "sub" / "x.py").resolve()

    def test_forbidden_dir_components_rejected(self, tmp_path):
        (tmp_path / ".git").mkdir()
        with pytest.raises(C.ContractError):
            C.resolve_under_root(tmp_path, ".git/config")

    def test_source_map_entries_path_validated(self, tmp_path):
        bad_map = {
            "schema": C.SOURCE_MAP_SCHEMA,
            "modules": [
                {
                    "logical_module": "x.y",
                    "source_relative_path": "../escape.py",
                    "sha256": "0" * 64,
                    "bytes": 1,
                    "owner": "t",
                    "release_id": "t",
                    "provenance_kind": "WORKTREE_SNAPSHOT",
                }
            ],
        }
        with pytest.raises(C.ContractError):
            C.validate_source_map(bad_map)


# ---------------------------------------------------------------------------
# H20.1 environment capture allow-list
# ---------------------------------------------------------------------------

class TestEnvironmentCapture:
    def test_capture_keys_exactly_frozen(self):
        env = C.capture_environment(["pytest", "not-a-real-dist-xyz"], "0", 1)
        assert set(env) == set(C.ENVIRONMENT_KEYS)
        assert env["timer"] == C.TIMER_NAME
        assert env["pythonhashseed"] == "0"
        assert env["dependencies"]["not-a-real-dist-xyz"] == C.UNKNOWN
        assert env["dependencies"]["pytest"]  # real distribution resolves

    def test_record_environment_allowlist_enforced(self):
        good = helpers.build_result_record()
        good["environment"]["HOME"] = "/home/x"  # outside the frozen allow-list
        with pytest.raises(C.ContractError, match="allow-list"):
            C.validate_result_record(good)


# ---------------------------------------------------------------------------
# result-record structural validation
# ---------------------------------------------------------------------------

class TestResultRecordValidation:
    def test_golden_record_validates(self):
        record = helpers.build_result_record()
        C.validate_result_record(record)

    def test_missing_required_key_rejected(self):
        record = helpers.build_result_record()
        del record["status"]
        with pytest.raises(C.ContractError, match="status"):
            C.validate_result_record(record)

    def test_unknown_top_level_key_rejected(self):
        record = helpers.build_result_record()
        record["argv"] = ["python", "-m", "secret"]
        with pytest.raises(C.ContractError, match="argv"):
            C.validate_result_record(record)


# ---------------------------------------------------------------------------
# H20.2 CLI gating: help / invalid input return before any import
# ---------------------------------------------------------------------------

class TestCliGating:
    def test_help_returns_zero(self):
        rc, out, err = run_cli(["--help"])
        assert rc == 0
        assert "run" in out and "compare" in out
        rc2, _, _ = run_cli(["run", "--help"])
        assert rc2 == 0

    def test_missing_required_arg_is_usage_error(self):
        rc, _, err = run_cli(["run", "--source-map", "x.json"])
        assert rc == 2

    def test_invalid_input_returns_before_import(self, mini_root, tmp_path):
        sentinel = tmp_path / "import-sentinel.txt"
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_SENTINEL, sentinel=sentinel)
        make_manifest(mini_root, "t/case1", workload_rel)
        sm_rel = make_source_map(mini_root, [("t.w", workload_rel)])
        assert not sentinel.exists()
        # nonexistent manifest: must fail without importing the workload
        rc, out, err = run_cli(
            [
                "run", "--source-map", str(mini_root / sm_rel),
                "--case-manifest", str(mini_root / "cases" / "missing.json"),
                "--output-root", str(tmp_path / "out"), "--profile", "smoke",
                "--root", str(mini_root),
            ]
        )
        assert rc == 2
        assert not sentinel.exists()
        assert not (tmp_path / "out" / "runs").exists()  # no artifacts left

    def test_source_hash_mismatch_aborts_before_import(self, mini_root, tmp_path):
        sentinel = tmp_path / "import-sentinel.txt"
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_SENTINEL, sentinel=sentinel)
        make_manifest(mini_root, "t/case1", workload_rel)
        sm_rel = make_source_map(mini_root, [("t.w", workload_rel)])
        out = tmp_path / "out"
        base_args = [
            "run", "--source-map", str(mini_root / sm_rel),
            "--case-manifest", str(mini_root / "cases" / "t__case1.json"),
            "--output-root", str(out), "--profile", "smoke", "--root", str(mini_root),
        ]
        # control: untampered run imports the workload and succeeds
        rc, _, err = run_cli(base_args)
        assert rc == 0, err
        assert sentinel.exists()
        # tamper the workload AFTER the source map recorded its hash
        with open(mini_root / workload_rel, "a", encoding="utf-8") as handle:
            handle.write("\n# tampered\n")
        sentinel.unlink()
        rc, _, err = run_cli(base_args)
        assert rc == 2
        assert "SOURCE_HASH_MISMATCH" in err
        assert not sentinel.exists()  # H20.2: never imported despite valid args

    def test_manifest_fixture_missing_aborts_without_artifacts(self, mini_root, tmp_path):
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_OK)
        make_manifest(mini_root, "t/case1", workload_rel, extra_fixture="fixtures/missing.bin")
        sm_rel = make_source_map(mini_root, [])
        out = tmp_path / "out"
        rc, _, err = run_cli(
            [
                "run", "--source-map", str(mini_root / sm_rel),
                "--case-manifest", str(mini_root / "cases" / "t__case1.json"),
                "--output-root", str(out), "--profile", "smoke", "--root", str(mini_root),
            ]
        )
        assert rc == 2
        assert "missing" in err
        assert not out.exists() or not (out / "runs").exists()

    def test_measured_module_must_be_in_source_map(self, mini_root, tmp_path):
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_OK)
        make_manifest(mini_root, "t/case1", workload_rel, measured_modules=["prod.missing"])
        sm_rel = make_source_map(mini_root, [])
        rc, _, err = run_cli(
            [
                "run", "--source-map", str(mini_root / sm_rel),
                "--case-manifest", str(mini_root / "cases" / "t__case1.json"),
                "--output-root", str(tmp_path / "out"), "--profile", "smoke",
                "--root", str(mini_root),
            ]
        )
        assert rc == 2
        assert "not present in the provided source map" in err

    def test_no_case_selected(self, mini_root, tmp_path):
        sm_rel = make_source_map(mini_root, [])
        rc, _, err = run_cli(
            [
                "run", "--source-map", str(mini_root / sm_rel),
                "--output-root", str(tmp_path / "out"), "--profile", "smoke",
                "--root", str(mini_root),
            ]
        )
        assert rc == 2 and ("no cases selected" in err or "requires --suite" in err)

    def test_unknown_case_id(self, mini_root, tmp_path):
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_OK)
        make_manifest(mini_root, "t/case1", workload_rel)
        sm_rel = make_source_map(mini_root, [])
        rc, _, err = run_cli(
            [
                "run", "--source-map", str(mini_root / sm_rel),
                "--case-manifest", str(mini_root / "cases" / "t__case1.json"),
                "--case", "t/other", "--output-root", str(tmp_path / "out"),
                "--profile", "smoke", "--root", str(mini_root),
            ]
        )
        assert rc == 2 and "unknown --case" in err

    def test_duplicate_case_id_across_manifests(self, mini_root, tmp_path):
        w1 = helpers.write_workload(mini_root, "w1", WORKLOAD_OK)
        w2 = helpers.write_workload(mini_root, "w2", WORKLOAD_OK)
        make_manifest(mini_root, "t/same", w1)
        manifest2 = json.loads((mini_root / "cases" / "t__same.json").read_text(encoding="utf-8"))
        manifest2["workload"]["source"] = w2  # same case_id, different workload
        (mini_root / "cases" / "t__same2.json").write_text(
            json.dumps(manifest2, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        sm_rel = make_source_map(mini_root, [])
        rc, _, err = run_cli(
            [
                "run", "--source-map", str(mini_root / sm_rel),
                "--case-manifest", str(mini_root / "cases" / "t__same.json"),
                "--case-manifest", str(mini_root / "cases" / "t__same2.json"),
                "--output-root", str(tmp_path / "out"), "--profile", "smoke",
                "--root", str(mini_root),
            ]
        )
        assert rc == 2 and "duplicate case id" in err


# ---------------------------------------------------------------------------
# runner status machine (H20.4 / H20.6) via real CLI smoke runs
# ---------------------------------------------------------------------------

def _smoke_run(mini_root, tmp_path, workload_body, *, manifest_kwargs=None, tag="t"):
    workload_rel = helpers.write_workload(mini_root, "w", workload_body)
    manifest, manifest_rel = make_manifest(mini_root, "t/case1", workload_rel, **(manifest_kwargs or {}))
    sm_rel = make_source_map(mini_root, [])
    out = tmp_path / ("out-%s" % tag)
    rc, stdout, stderr = run_cli(
        [
            "run", "--source-map", str(mini_root / sm_rel),
            "--case-manifest", str(mini_root / manifest_rel),
            "--output-root", str(out), "--profile", "smoke", "--root", str(mini_root),
        ]
    )
    record = helpers.load_case_record(out, helpers.only_run_id(out), "t/case1")
    return rc, record, stderr


class TestRunnerStatusMachine:
    def test_ok_case(self, mini_root, tmp_path):
        rc, record, err = _smoke_run(mini_root, tmp_path, WORKLOAD_OK)
        assert rc == 0, err
        assert record["status"] == "OK"
        assert record["measurement"]["n"] == 10
        assert record["measurement"]["median_ns"] > 0
        assert record["measurement"]["p95_note"] == "P95_OMITTED_INSUFFICIENT_SAMPLES"
        assert record["correctness"]["status"] == "PASS"

    def test_exception_in_workload_is_error(self, mini_root, tmp_path):
        rc, record, _ = _smoke_run(mini_root, tmp_path, WORKLOAD_RAISE)
        assert rc == 3
        assert record["status"] == "ERROR"
        assert "TEST injected execution failure" in record["status_reason"]

    def test_timeout_preserves_raw_samples(self, mini_root, tmp_path):
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_SLOW)
        make_manifest(
            mini_root, "t/case1", workload_rel,
            smoke={"warmup_operations": 0, "operations": 100, "case_timeout_s": 1},
        )
        sm_rel = make_source_map(mini_root, [])
        out = tmp_path / "out-timeout"
        rc, _, err = run_cli(
            [
                "run", "--source-map", str(mini_root / sm_rel),
                "--case-manifest", str(mini_root / "cases" / "t__case1.json"),
                "--output-root", str(out), "--profile", "smoke", "--root", str(mini_root),
            ]
        )
        assert rc == 3
        record = helpers.load_case_record(out, helpers.only_run_id(out), "t/case1")
        assert record["status"] == "TIMEOUT"
        assert record["measurement"]["n"] > 0  # raw samples preserved
        assert len(record["measurement"]["samples_ns"]) == record["measurement"]["n"]

    def test_partial_work_is_invalid(self, mini_root, tmp_path):
        rc, record, _ = _smoke_run(mini_root, tmp_path, WORKLOAD_SNEAKY)
        assert rc == 3
        assert record["status"] == "INVALID"
        assert "partial work" in record["status_reason"]
        # INVALID results keep their samples but correctness is FAIL
        assert record["correctness"]["status"] == "FAIL"

    def test_operation_failures_are_invalid_and_recorded(self, mini_root, tmp_path):
        rc, record, _ = _smoke_run(mini_root, tmp_path, WORKLOAD_FAILING_OPS)
        assert rc == 3
        assert record["status"] == "INVALID"
        assert "OPERATION_FAILURES_RECORDED" in record["status_reason"]
        assert record["measurement"]["failures"]["count"] > 0
        # failed ops are NOT silently dropped: n + failures == planned
        n = record["measurement"]["n"]
        failures = record["measurement"]["failures"]["count"]
        assert n + failures == record["measurement"]["planned_operations"]

    def test_wrong_digest_is_invalid(self, mini_root, tmp_path):
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_OK)
        make_manifest(
            mini_root, "t/case1", workload_rel,
            expected_digest="0" * 64,  # deliberately wrong frozen digest
        )
        sm_rel = make_source_map(mini_root, [])
        out = tmp_path / "out-digest"
        rc, _, err = run_cli(
            [
                "run", "--source-map", str(mini_root / sm_rel),
                "--case-manifest", str(mini_root / "cases" / "t__case1.json"),
                "--output-root", str(out), "--profile", "smoke", "--root", str(mini_root),
            ]
        )
        assert rc == 3
        record = helpers.load_case_record(out, helpers.only_run_id(out), "t/case1")
        assert record["status"] == "INVALID"
        assert "DIGEST_MISMATCH" in record["status_reason"]

    def test_self_modifying_workload_is_drift(self, mini_root, tmp_path):
        rc, record, _ = _smoke_run(mini_root, tmp_path, WORKLOAD_SELF_MODIFY)
        assert rc == 3
        assert record["status"] == "DRIFT"
        assert "DRIFT" in record["status_reason"] or "drift" in record["status_reason"].lower()
        assert record["measurement"]["n"] == 10  # raw data preserved (H20.10)

    def test_temp_sqlite_resource_provisioned(self, mini_root, tmp_path):
        rc, record, err = _smoke_run(
            mini_root, tmp_path, WORKLOAD_SQLITE,
            manifest_kwargs={"resources": {"network": False, "production_db": False, "temp_sqlite": True}},
        )
        assert rc == 0, err
        assert record["status"] == "OK"
        db_path = record["correctness"]["summary"]["db_path"]
        assert db_path.endswith(".sqlite")  # harness-provisioned path was used

    def test_workload_reported_duration_never_used_as_sample(self, mini_root, tmp_path):
        rc, record, _ = _smoke_run(
            mini_root, tmp_path,
            helpers.WORKLOAD_REPORTS_OWN_TIME,
        )
        assert rc == 0
        samples = record["measurement"]["samples_ns"]
        assert samples and 12345 not in samples  # harness clock only (H20.3)

    def test_environment_recorded_and_allowlisted(self, mini_root, tmp_path):
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_OK)
        make_manifest(mini_root, "t/case1", workload_rel)
        (mini_root / "cases" / "t__case1.json").write_text(
            json.dumps(
                {
                    **json.loads((mini_root / "cases" / "t__case1.json").read_text(encoding="utf-8")),
                    "environment": {"depends_on": ["pytest", "not-a-real-dist-xyz"]},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        sm_rel = make_source_map(mini_root, [])
        out = tmp_path / "out-env"
        rc, _, err = run_cli(
            [
                "run", "--source-map", str(mini_root / sm_rel),
                "--case-manifest", str(mini_root / "cases" / "t__case1.json"),
                "--output-root", str(out), "--profile", "smoke", "--root", str(mini_root),
            ]
        )
        assert rc == 0, err
        record = helpers.load_case_record(out, helpers.only_run_id(out), "t/case1")
        assert set(record["environment"]) == set(C.ENVIRONMENT_KEYS)
        assert record["environment"]["pythonhashseed"] == "0"
        assert record["environment"]["dependencies"]["not-a-real-dist-xyz"] == C.UNKNOWN
        text = json.dumps(record)
        assert "PATH" not in text and "USERNAME" not in text  # no environ dump

    def test_duplicate_run_dir_refused(self, mini_root, tmp_path):
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_OK)
        make_manifest(mini_root, "t/case1", workload_rel)
        sm_rel = make_source_map(mini_root, [])
        out = tmp_path / "out-dup"
        args = [
            "run", "--source-map", str(mini_root / sm_rel),
            "--case-manifest", str(mini_root / "cases" / "t__case1.json"),
            "--output-root", str(out), "--profile", "smoke", "--root", str(mini_root),
            "--run-id", "run-fixed-0001",
        ]
        rc, _, _ = run_cli(args)
        assert rc == 0
        rc2, _, err2 = run_cli(args)  # same run-id -> refuse, never overwrite
        assert rc2 == 2 and "already exists" in err2

    def test_cleanup_only_own_tmp(self, mini_root, tmp_path):
        rc, record, err = _smoke_run(mini_root, tmp_path, WORKLOAD_OK, tag="clean")
        assert rc == 0, err
        run_dir = tmp_path / "out-clean" / "runs" / record["run_id"]
        assert not (run_dir / "tmp").exists()  # run-scoped tmp removed
        assert list((tmp_path / "out-clean" / "runs").iterdir())  # run dir kept


# ---------------------------------------------------------------------------
# in-process probes: timer regression, network guard
# ---------------------------------------------------------------------------

class _MiniWorkload:
    def __init__(self):
        self.n = 0

    def setup(self, ctx):
        pass

    def operation(self, ctx):
        self.n += 1
        return {"ok": True, "units": 1}

    def teardown(self, ctx):
        pass

    def verify(self, ctx):
        return {
            "status": "PASS",
            "digest": "f" * 64,
            "summary": {"n": self.n},
            "fail_reasons": [],
        }


class _SocketWorkload(_MiniWorkload):
    def operation(self, ctx):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # never connects
        return {"ok": True, "units": 1}


def _ctx(tmp_path):
    return C.CaseContext(
        params={"seed": 1}, seed=1, tmp_dir=tmp_path / "casetmp",
        resources={"temp_sqlite_path": None}, normalizations=[],
    )


_SPEC = {
    "sample_mode": "per_operation",
    "units": "iterations",
    "units_per_operation": 1,
    "min_operations": 1,
    "batch_size": 100,
    "reset_method": "none",
    "includes_io": False,
}


class TestInProcessProbes:
    def test_timer_regression_detected(self, tmp_path, monkeypatch):
        state = {"t": 1_000_000}
        backwards = lambda: state.__setitem__("t", state["t"] - 1) or state["t"]  # noqa: E731
        monkeypatch.setattr(runner.time, "perf_counter_ns", backwards)
        partial = runner.execute_inprocess(
            _MiniWorkload(), _ctx(tmp_path), _SPEC, 0, 3, runner.Deadline(10)
        )
        assert partial["exception"] is not None
        assert "TIMER_REGRESSION" in partial["exception"]["message"]

    def test_network_guard_blocks_socket(self, tmp_path):
        with runner.NetworkGuard():
            partial = runner.execute_inprocess(
                _SocketWorkload(), _ctx(tmp_path), _SPEC, 0, 3, runner.Deadline(10)
            )
        assert partial["exception"] is not None
        assert "network disabled" in partial["exception"]["message"]
        # guard restored afterwards
        assert socket.socket is not runner._network_refused

    def test_zero_duration_all_samples_is_error(self, mini_root, tmp_path):
        # injected via a fake formal batch executor (deterministic zeros)
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_OK)
        manifest, manifest_rel = make_manifest(
            mini_root, "t/case1", workload_rel,
            formal={
                "batches": 2, "operations_per_batch": 2, "warmup_operations": 0,
                "case_timeout_s": 600,
                "pre_registration": {"reason": "TEST small batches", "registered_by": "nf20-test"},
            },
        )
        manifest = helpers.load_manifest(mini_root, manifest_rel)
        sm_rel = make_source_map(mini_root, [])
        source_map = C.load_json_file(mini_root / sm_rel, "source map")

        def fake_executor(**kwargs):
            partial = {
                "samples": [0, 0], "sample_units": [1, 1], "failures": [],
                "failure_count": 0, "executed": 2, "planned": 2,
                "verify": {"status": "PASS", "digest": "e" * 64, "summary": {}, "fail_reasons": []},
                "phases": {"setup_ns": 1, "warmup_ns": 1, "measured_ns": 1, "teardown_ns": 1},
            }
            batches = [
                {"index": i, "process": "subprocess", "sample_count": 2, "failure_count": 0,
                 "digest": "e" * 64, "elapsed_ns": 1, "rc": 0, "timed_out": False}
                for i in range(2)
            ]
            return batches, [partial, partial]

        record = runner.run_single_case(
            root=mini_root, run_dir=tmp_path / "rundir", run_id="run-zero-0001",
            profile="formal", manifest_path=manifest_rel, manifest=manifest,
            source_map=source_map, source_map_path=sm_rel,
            source_map_sha=C.sha256_file(mini_root / sm_rel)[0],
            suite_info=None, ambient_pythonhashseed="0",
            allow_budget_override=False, batch_executor=fake_executor,
        )
        assert record["status"] == "ERROR"
        assert "TIMER_ZERO_SAMPLES" in record["status_reason"]

    def test_sample_shortfall_is_error(self, mini_root, tmp_path):
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_OK)
        manifest, manifest_rel = make_manifest(
            mini_root, "t/case1", workload_rel,
            formal={
                "batches": 2, "operations_per_batch": 2, "warmup_operations": 0,
                "case_timeout_s": 600,
                "pre_registration": {"reason": "TEST small batches", "registered_by": "nf20-test"},
            },
        )
        manifest = helpers.load_manifest(mini_root, manifest_rel)
        sm_rel = make_source_map(mini_root, [])
        source_map = C.load_json_file(mini_root / sm_rel, "source map")

        def short_executor(**kwargs):
            partial = {
                "samples": [11, 12], "sample_units": [1, 1], "failures": [],
                "failure_count": 0, "executed": 2, "planned": 2,
                "verify": {"status": "PASS", "digest": "d" * 64, "summary": {}, "fail_reasons": []},
                "phases": {"setup_ns": 1, "warmup_ns": 1, "measured_ns": 1, "teardown_ns": 1},
            }
            other = dict(partial, samples=[13], sample_units=[1], executed=1)
            batches = [
                {"index": 0, "process": "subprocess", "sample_count": 2, "failure_count": 0,
                 "digest": "d" * 64, "elapsed_ns": 1, "rc": 0, "timed_out": False},
                {"index": 1, "process": "subprocess", "sample_count": 1, "failure_count": 0,
                 "digest": "d" * 64, "elapsed_ns": 1, "rc": 1, "timed_out": False,
                 "note": "BATCH_FAILED_RC_1"},
            ]
            return batches, [partial, other]  # executed 3 of planned 4

        record = runner.run_single_case(
            root=mini_root, run_dir=tmp_path / "rundir", run_id="run-short-0001",
            profile="formal", manifest_path=manifest_rel, manifest=manifest,
            source_map=source_map, source_map_path=sm_rel,
            source_map_sha=C.sha256_file(mini_root / sm_rel)[0],
            suite_info=None, ambient_pythonhashseed="0",
            allow_budget_override=False, batch_executor=short_executor,
        )
        assert record["status"] == "ERROR"
        assert "SAMPLE_SHORTFALL" in record["status_reason"]

    def test_formal_digest_inconsistency_is_invalid(self, mini_root, tmp_path):
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_OK)
        manifest, manifest_rel = make_manifest(
            mini_root, "t/case1", workload_rel,
            formal={
                "batches": 2, "operations_per_batch": 2, "warmup_operations": 0,
                "case_timeout_s": 600,
                "pre_registration": {"reason": "TEST small batches", "registered_by": "nf20-test"},
            },
        )
        manifest = helpers.load_manifest(mini_root, manifest_rel)
        sm_rel = make_source_map(mini_root, [])
        source_map = C.load_json_file(mini_root / sm_rel, "source map")

        def inconsistent_executor(**kwargs):
            def partial(digest):
                return {
                    "samples": [10, 11], "sample_units": [1, 1], "failures": [],
                    "failure_count": 0, "executed": 2, "planned": 2,
                    "verify": {"status": "PASS", "digest": digest, "summary": {}, "fail_reasons": []},
                    "phases": {"setup_ns": 1, "warmup_ns": 1, "measured_ns": 1, "teardown_ns": 1},
                }
            batches = [
                {"index": 0, "process": "subprocess", "sample_count": 2, "failure_count": 0,
                 "digest": "1" * 64, "elapsed_ns": 1, "rc": 0, "timed_out": False},
                {"index": 1, "process": "subprocess", "sample_count": 2, "failure_count": 0,
                 "digest": "2" * 64, "elapsed_ns": 1, "rc": 0, "timed_out": False},
            ]
            return batches, [partial("1" * 64), partial("2" * 64)]

        record = runner.run_single_case(
            root=mini_root, run_dir=tmp_path / "rundir", run_id="run-dig-0001",
            profile="formal", manifest_path=manifest_rel, manifest=manifest,
            source_map=source_map, source_map_path=sm_rel,
            source_map_sha=C.sha256_file(mini_root / sm_rel)[0],
            suite_info=None, ambient_pythonhashseed="0",
            allow_budget_override=False, batch_executor=inconsistent_executor,
        )
        assert record["status"] == "INVALID"
        assert "DIGEST_INCONSISTENT_ACROSS_BATCHES" in record["status_reason"]
        assert record["correctness"]["repeat_consistent"] is False

    def test_formal_later_batch_verify_fail_same_digest_is_invalid(self, mini_root, tmp_path):
        """C20-1.0.1 (NF20-HARD-r1 F01 counterexample): a LATER formal batch
        whose frozen verify() rule returns FAIL while emitting the SAME digest
        as the other batches (a hold-constraint predicate the digest does not
        cover) must mark the case INVALID - not OK with correctness PASS."""
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_OK)
        manifest, manifest_rel = make_manifest(
            mini_root, "t/case1", workload_rel,
            formal={
                "batches": 2, "operations_per_batch": 2, "warmup_operations": 0,
                "case_timeout_s": 600,
                "pre_registration": {"reason": "TEST small batches", "registered_by": "nf20-test"},
            },
        )
        manifest = helpers.load_manifest(mini_root, manifest_rel)
        sm_rel = make_source_map(mini_root, [])
        source_map = C.load_json_file(mini_root / sm_rel, "source map")
        same_digest = "7" * 64

        def fail_later_executor(**kwargs):
            # batch 0 verifies PASS; batch 1 verifies FAIL on a predicate the
            # digest does not carry, yet returns the identical digest string.
            def partial(status, reasons):
                return {
                    "samples": [10, 11], "sample_units": [1, 1], "failures": [],
                    "failure_count": 0, "executed": 2, "planned": 2,
                    "verify": {
                        "status": status, "digest": same_digest,
                        "summary": {}, "fail_reasons": reasons,
                    },
                    "phases": {"setup_ns": 1, "warmup_ns": 1, "measured_ns": 1, "teardown_ns": 1},
                }

            batches = [
                {"index": 0, "process": "subprocess", "sample_count": 2, "failure_count": 0,
                 "digest": same_digest, "verify_status": "PASS", "elapsed_ns": 1, "rc": 0,
                 "timed_out": False},
                {"index": 1, "process": "subprocess", "sample_count": 2, "failure_count": 0,
                 "digest": same_digest, "verify_status": "FAIL", "elapsed_ns": 1, "rc": 0,
                 "timed_out": False},
            ]
            return batches, [
                partial("PASS", []),
                partial("FAIL", ["hold constraint violated on batch 1 (TEST F01 probe)"]),
            ]

        record = runner.run_single_case(
            root=mini_root, run_dir=tmp_path / "rundir", run_id="run-f01-0001",
            profile="formal", manifest_path=manifest_rel, manifest=manifest,
            source_map=source_map, source_map_path=sm_rel,
            source_map_sha=C.sha256_file(mini_root / sm_rel)[0],
            suite_info=None, ambient_pythonhashseed="0",
            allow_budget_override=False, batch_executor=fail_later_executor,
        )
        assert record["status"] == "INVALID"
        assert "BATCH_VERIFY_FAILED" in record["status_reason"]
        assert "batch 1" in record["status_reason"] or "hold constraint" in record["status_reason"]
        assert record["correctness"]["status"] == "FAIL"
        # digest equality is a fact about digests only - it must NOT rescue
        # the case, and repeat_consistent honestly stays True
        assert record["correctness"]["repeat_consistent"] is True
        # per-batch verify statuses are preserved in the raw record (F01 fix)
        statuses = [b.get("verify_status") for b in record["measurement"]["batches"]]
        assert statuses == ["PASS", "FAIL"]

    def test_formal_all_batches_pass_same_digest_stays_ok(self, mini_root, tmp_path):
        """F01 control: all batches PASS with the same digest remains OK."""
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_OK)
        manifest, manifest_rel = make_manifest(
            mini_root, "t/case1", workload_rel,
            formal={
                "batches": 2, "operations_per_batch": 2, "warmup_operations": 0,
                "case_timeout_s": 600,
                "pre_registration": {"reason": "TEST small batches", "registered_by": "nf20-test"},
            },
        )
        manifest = helpers.load_manifest(mini_root, manifest_rel)
        sm_rel = make_source_map(mini_root, [])
        source_map = C.load_json_file(mini_root / sm_rel, "source map")
        same_digest = "7" * 64

        def all_pass_executor(**kwargs):
            def partial():
                return {
                    "samples": [10, 11], "sample_units": [1, 1], "failures": [],
                    "failure_count": 0, "executed": 2, "planned": 2,
                    "verify": {"status": "PASS", "digest": same_digest, "summary": {}, "fail_reasons": []},
                    "phases": {"setup_ns": 1, "warmup_ns": 1, "measured_ns": 1, "teardown_ns": 1},
                }

            batches = [
                {"index": i, "process": "subprocess", "sample_count": 2, "failure_count": 0,
                 "digest": same_digest, "verify_status": "PASS", "elapsed_ns": 1, "rc": 0,
                 "timed_out": False}
                for i in range(2)
            ]
            return batches, [partial(), partial()]

        record = runner.run_single_case(
            root=mini_root, run_dir=tmp_path / "rundir", run_id="run-f01c-0001",
            profile="formal", manifest_path=manifest_rel, manifest=manifest,
            source_map=source_map, source_map_path=sm_rel,
            source_map_sha=C.sha256_file(mini_root / sm_rel)[0],
            suite_info=None, ambient_pythonhashseed="0",
            allow_budget_override=False, batch_executor=all_pass_executor,
        )
        assert record["status"] == "OK"
        assert record["correctness"]["status"] == "PASS"
        assert record["correctness"]["repeat_consistent"] is True


# ---------------------------------------------------------------------------
# H20.4 budgets: suite cap, pre-registration, override flag
# ---------------------------------------------------------------------------

class TestBudgets:
    def test_smoke_suite_budget_enforced(self, mini_root, tmp_path):
        slow_rel = helpers.write_workload(mini_root, "slow", WORKLOAD_SLOW)
        fast_rel = helpers.write_workload(mini_root, "fast", WORKLOAD_OK)
        make_manifest(
            mini_root, "t/slow", slow_rel,
            smoke={"warmup_operations": 0, "operations": 100, "case_timeout_s": 2},
        )
        make_manifest(mini_root, "t/fast", fast_rel)
        sm_rel = make_source_map(mini_root, [])
        rc, run_id = runner.run_suite(
            root=mini_root,
            output_root=tmp_path / "out",
            profile="smoke",
            source_map_path=mini_root / sm_rel,
            suite_path=None,
            case_ids=(),
            manifest_paths=[mini_root / "cases" / "t__slow.json", mini_root / "cases" / "t__fast.json"],
            run_id_arg="run-suite-budget-0001",
            allow_budget_override=False,
            ambient_pythonhashseed="0",
            suite_budget_s=1.2,  # tightened for a fast test; first case burns it
        )
        assert rc == 3
        slow = helpers.load_case_record(tmp_path / "out", run_id, "t/slow")
        fast = helpers.load_case_record(tmp_path / "out", run_id, "t/fast")
        assert slow["status"] == "TIMEOUT"
        assert fast["status"] == "ERROR"
        assert "SUITE_BUDGET_EXCEEDED" in fast["status_reason"]
        assert fast["measurement"]["planned_operations"] == 0  # never started

    def test_formal_below_minimum_needs_pre_registration(self, mini_root):
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_OK)
        # write a manifest with formal batches=2 and NO pre_registration
        # (make_manifest would refuse it, so write the JSON directly)
        manifest, _ = make_manifest(mini_root, "t/case1", workload_rel)
        manifest["budget"]["formal"]["batches"] = 2
        broken = mini_root / "cases" / "t__broken.json"
        broken.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
        rc, _, err = run_cli(
            ["validate-case", "--case-manifest", str(broken), "--root", str(mini_root)]
        )
        assert rc == 2 and "pre_registration" in err

    def test_formal_budget_override_gate(self, mini_root, tmp_path):
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_OK)
        formal = {
            "batches": 2, "operations_per_batch": 2, "warmup_operations": 0,
            "case_timeout_s": 900,  # above the frozen 600 s default
            "pre_registration": {"reason": "TEST small batches", "registered_by": "nf20-test"},
        }
        make_manifest(mini_root, "t/case1", workload_rel, formal=formal)
        sm_rel = make_source_map(mini_root, [])
        args = [
            "run", "--source-map", str(mini_root / sm_rel),
            "--case-manifest", str(mini_root / "cases" / "t__case1.json"),
            "--output-root", str(tmp_path / "out"), "--profile", "formal",
            "--root", str(mini_root), "--run-id", "run-ov",
        ]
        rc, _, err = run_cli(args)
        assert rc == 2 and "allow-budget-override" in err
        rc2, _, err2 = run_cli(args + ["--allow-budget-override"])
        assert rc2 == 0, err2
        record = helpers.load_case_record(tmp_path / "out", "run-ov", "t/case1")
        assert record["budget_override"]["authorized"] is True
        assert any("BUDGET_OVERRIDE" in note for note in record["notes"])

    def test_formal_subprocess_batches_real(self, mini_root, tmp_path):
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_OK)
        make_manifest(
            mini_root, "t/case1", workload_rel,
            formal={
                "batches": 2, "operations_per_batch": 5, "warmup_operations": 2,
                "case_timeout_s": 60,
                "pre_registration": {"reason": "TEST tiny batches", "registered_by": "nf20-test"},
            },
        )
        sm_rel = make_source_map(mini_root, [])
        out = tmp_path / "out-formal"
        rc, _, err = run_cli(
            [
                "run", "--source-map", str(mini_root / sm_rel),
                "--case-manifest", str(mini_root / "cases" / "t__case1.json"),
                "--output-root", str(out), "--profile", "formal", "--root", str(mini_root),
            ],
            timeout=120,
        )
        assert rc == 0, err
        record = helpers.load_case_record(out, helpers.only_run_id(out), "t/case1")
        assert record["measurement"]["n"] == 10
        assert all(b["process"] == "subprocess" for b in record["measurement"]["batches"])
        assert record["correctness"]["repeat_consistent"] is True
        assert record["environment"]["pythonhashseed"] == "1"  # == str(params.seed)
        # zero-duration single samples are honest coarse-timer artifacts and
        # are kept; only an ALL-zero record is a TIMER_ZERO error
        samples = record["measurement"]["samples_ns"]
        assert all(s >= 0 for s in samples) and any(s > 0 for s in samples)
        assert record["measurement"]["p95_note"].startswith("P95_OMITTED")  # n=10 < 100

    def test_validate_case_cli(self, mini_root):
        workload_rel = helpers.write_workload(mini_root, "w", WORKLOAD_OK)
        manifest, manifest_rel = make_manifest(mini_root, "t/case1", workload_rel)
        rc, out, _ = run_cli(
            ["validate-case", "--case-manifest", str(mini_root / manifest_rel), "--root", str(mini_root)]
        )
        assert rc == 0 and "VALID" in out
        broken = mini_root / "cases" / "broken.json"
        broken.write_text('{"schema": "wrong"}', encoding="utf-8")
        rc2, _, err2 = run_cli(["validate-case", "--case-manifest", str(broken), "--root", str(mini_root)])
        assert rc2 == 2


# ---------------------------------------------------------------------------
# core suite fixtures in the repository (real smoke boundary check)
# ---------------------------------------------------------------------------

class TestCoreSuite:
    def test_core_suite_smoke_via_cli(self, tmp_path):
        """Short REAL-timing smoke proving the execution boundary on the
        repository's frozen core cases (work order acceptance: reviewer runs
        a short real smoke; these timings are SYNTHETIC-case harness
        validation only, not performance baselines)."""
        rc, out, err = run_cli(
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
        assert "run " in out
        run_summary = json.loads(
            (
                tmp_path / "out" / "runs" / helpers.only_run_id(tmp_path / "out") / "run.json"
            ).read_text(encoding="utf-8")
        )
        assert run_summary["all_ok"] is True
        for case in run_summary["cases"]:
            assert case["status"] == "OK"
            assert case["elapsed_s"] < C.SMOKE_CASE_BUDGET_S  # H20.4 per-case budget
        total = sum(case["elapsed_s"] for case in run_summary["cases"])
        assert total < C.SMOKE_SUITE_BUDGET_S  # whole-suite budget

    def test_core_case_p95_present_for_counter(self, tmp_path):
        rc, out, err = run_cli(
            [
                "run",
                "--source-map", "tests/benchmarks/fixtures/core/source-map.json",
                "--suite", "tools/benchmarks/suite.json",
                "--case", "core/counter_digest",
                "--output-root", str(tmp_path / "out"),
                "--profile", "smoke",
            ],
            timeout=60,
        )
        assert rc == 0, err
        record = helpers.load_case_record(tmp_path / "out", helpers.only_run_id(tmp_path / "out"), "core/counter_digest")
        assert record["measurement"]["n"] == 200
        assert record["measurement"]["p95_ns"] is not None
        assert record["identity"]["eligible_for_baseline"] is False
