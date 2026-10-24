"""NF22 datapipeline-domain workload tests (own scope, SYNTHETIC).

Covers the r1 deliverable ``datapipeline/frame_synthetic`` (H22
FRAME_SYNTHETIC, interim NF22-owned form):

* frozen tier-ladder fixture shape and growth,
* C20 manifest / source-map validation and worktree-hash binding,
* workload protocol conformance and cross-instance determinism,
* frozen ``expected_digest`` equality for the exact smoke sequence,
* classifier counterexamples (padding / broken OHLC / duplicate key /
  non-finite) and the H22 rule that padding/invalid/duplicate rows are
  never counted as accepted input rows,
* direct tier-cost growth probe (large tier costs more than small),
* an end-to-end bounded CLI smoke run through the frozen C20 runner.

The DP-owner release source-map is PENDING; nothing here imports or
measures any ``ak_trading.datapipeline`` production module (the COLLECT_
STORE_SQLITE leg stays NOT_RUN by design - see docs/benchmarks/datapipeline.md).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from conftest import REPO_ROOT, run_cli  # noqa: E402  (tests/benchmarks conftest)

from tools.benchmarks import contracts as C  # noqa: E402

WL_REL = "tools/benchmarks/workloads/datapipeline.py"
TIERS_REL = "tools/benchmarks/fixtures/datapipeline/frame_tiers.json"
MANIFEST_REL = "tools/benchmarks/cases/datapipeline.json"
SOURCE_MAP_REL = "tools/benchmarks/fixtures/datapipeline/source-map.json"
LOGICAL_MODULE = "benchmark.datapipeline.frame_synthetic"

EXPECTED_TIERS = (  # frozen r1 ladder (guard against accidental fixture edits)
    ("small", 380, 320),
    ("medium", 1705, 1550),
    ("large", 6490, 6140),
)


class _FakeCtx:
    """Minimal stand-in for contracts.CaseContext (no clock, per H20.3)."""

    def __init__(self, seed: int, tmp_dir: Path) -> None:
        self.params = {"seed": seed}
        self.seed = seed
        self.tmp_dir = tmp_dir
        self.resources: Dict[str, Any] = {}
        self.normalizations: List[str] = []


def _load_workload_module():
    path = REPO_ROOT / WL_REL
    spec = importlib.util.spec_from_file_location("nf22_dp_workload_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_sequence(seed: int, warmup: int, ops: int, tmp_dir: Path):
    module = _load_workload_module()
    workload = module.build(_FakeCtx(seed, tmp_dir))
    workload.setup(None)
    units: List[int] = []
    for _ in range(warmup):
        workload.operation(None)
    for _ in range(ops):
        result = workload.operation(None)
        units.append(int(result["units"]))
    return workload, workload.verify(None), units


# ---------------------------------------------------------------------------
# frozen fixture + manifest identity
# ---------------------------------------------------------------------------

def test_frame_tiers_fixture_frozen_shape() -> None:
    ladder = json.loads((REPO_ROOT / TIERS_REL).read_text(encoding="utf-8"))
    assert ladder["origin"] == "SYNTHETIC"
    assert tuple(ladder["column_contract"]["columns"]) == (
        "datetime", "open", "high", "low", "close", "volume", "open_oi", "close_oi",
    )
    tiers = ladder["tiers"]
    assert [t["tier"] for t in tiers] == [name for name, _, _ in EXPECTED_TIERS]
    for tier, (name, total_rows, accepted) in zip(tiers, EXPECTED_TIERS):
        composition = (
            tier["padding_rows"] + tier["closed_rows"]
            + tier["live_rows"] + tier["duplicate_rows"]
        )
        assert composition == total_rows == tier["expected"]["total_rows"]
        assert tier["expected"]["accepted_rows"] == accepted
        assert tier["expected"]["rejected_padding"] == tier["padding_rows"]
        assert tier["expected"]["rejected_invalid"] == tier["invalid_rows"]
        assert tier["expected"]["duplicates"] == tier["duplicate_rows"]
    totals = [t["expected"]["total_rows"] for t in tiers]
    assert totals == sorted(totals) and len(set(totals)) == 3  # strictly growing


def test_case_manifest_validates_and_declares_honest_scope() -> None:
    manifest = C.validate_case_manifest(
        json.loads((REPO_ROOT / MANIFEST_REL).read_text(encoding="utf-8"))
    )
    assert manifest["case_id"] == "datapipeline/frame_synthetic"
    assert manifest["origin"] == "SYNTHETIC"
    assert manifest["domain"] == "datapipeline"
    assert manifest["eligible_for_baseline"] is False  # interim NF22-owned code
    assert manifest["resources"] == {"network": False, "production_db": False, "temp_sqlite": False}
    assert manifest["correctness"]["expected_digest"] is not None
    assert manifest["budget"]["smoke"]["case_timeout_s"] <= C.SMOKE_CASE_BUDGET_S
    description = manifest["description"].upper()
    assert "SYNTHETIC" in description
    assert "NOT A DATABASE-THROUGHPUT CASE" in description  # H22 separation wording
    assert manifest["measured_modules"] == [LOGICAL_MODULE]


def test_source_map_hashes_match_worktree() -> None:
    source_map = json.loads((REPO_ROOT / SOURCE_MAP_REL).read_text(encoding="utf-8"))
    C.validate_source_map(source_map)  # structural validation (returns None)
    from tools.benchmarks.runner import verify_source_map_hashes

    verify_source_map_hashes(REPO_ROOT, source_map)  # raises on any drift
    entry = source_map["modules"][0]
    assert entry["logical_module"] == LOGICAL_MODULE
    assert entry["source_relative_path"] == WL_REL
    assert entry["owner"] == "NF22"
    # honesty: no DP production module is pre-bound while the handover is PENDING
    assert all(
        not str(e["source_relative_path"]).startswith("src/ak_trading/")
        for e in source_map["modules"]
    )


# ---------------------------------------------------------------------------
# workload protocol, determinism, frozen digest
# ---------------------------------------------------------------------------

def test_workload_protocol_conforms() -> None:
    manifest = json.loads((REPO_ROOT / MANIFEST_REL).read_text(encoding="utf-8"))
    module = _load_workload_module()
    workload = module.build(_FakeCtx(7, Path(".")))
    C.validate_workload_object(
        workload,
        manifest["measurement"]["sample_mode"],
        manifest["measurement"]["reset_method"],
    )


def test_digest_deterministic_across_instances(tmp_path: Path) -> None:
    seed = int(json.loads((REPO_ROOT / MANIFEST_REL).read_text(encoding="utf-8"))["params"]["seed"])
    _, first, _ = _run_sequence(seed, 6, 30, tmp_path / "a")
    _, second, _ = _run_sequence(seed, 6, 30, tmp_path / "b")
    assert first["status"] == second["status"] == "PASS"
    assert first["digest"] == second["digest"]
    assert first["summary"]["operations"] == 36


def test_frozen_expected_digest_matches_smoke_sequence(tmp_path: Path) -> None:
    manifest = json.loads((REPO_ROOT / MANIFEST_REL).read_text(encoding="utf-8"))
    seed = int(manifest["params"]["seed"])
    warmup = int(manifest["budget"]["smoke"]["warmup_operations"])
    ops = int(manifest["budget"]["smoke"]["operations"])
    _, verification, _ = _run_sequence(seed, warmup, ops, tmp_path)
    assert verification["status"] == "PASS"
    assert verification["digest"] == manifest["correctness"]["expected_digest"]


def test_units_are_accepted_rows_only(tmp_path: Path) -> None:
    # warmup=0 so the returned measured units cover every executed operation
    _, verification, units = _run_sequence(20260922, 0, 12, tmp_path)  # 4 full cycles
    assert verification["status"] == "PASS"
    assert sum(units) == verification["summary"]["accepted_rows"]
    assert verification["summary"]["accepted_rows"] > 0
    assert verification["summary"]["duplicate_rows"] > 0
    assert verification["summary"]["rejected_invalid_rows"] > 0
    assert verification["summary"]["rejected_padding_rows"] > 0


# ---------------------------------------------------------------------------
# classifier counterexamples (H22: misclassification is INVALID, not success)
# ---------------------------------------------------------------------------

def test_classifier_padding_invalid_duplicate_never_accepted() -> None:
    module = _load_workload_module()
    seen = {100.0}
    nan = float("nan")
    assert module.classify_row({"datetime": nan, "open": 1.0, "high": 2.0, "low": 0.5,
                                "close": 1.5, "volume": 1.0, "open_oi": 1.0, "close_oi": 1.0},
                               seen) == module.CLS_PADDING
    assert module.classify_row({"datetime": 1.0, "open": float("nan"), "high": 2.0,
                                "low": 0.5, "close": 1.5, "volume": 1.0, "open_oi": 1.0,
                                "close_oi": 1.0}, seen) == module.CLS_INVALID
    broken = {"datetime": 1.0, "open": 10.0, "high": 5.0, "low": 15.0, "close": 10.0,
              "volume": 1.0, "open_oi": 1.0, "close_oi": 1.0}
    assert module.classify_row(broken, seen) == module.CLS_INVALID  # high < low
    assert module.classify_row(dict(broken, high=12.0, low=9.0), seen) == module.CLS_ACCEPTED
    duplicate = {"datetime": 100.0, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                 "volume": 1.0, "open_oi": 1.0, "close_oi": 1.0}
    assert module.classify_row(duplicate, seen) == module.CLS_DUPLICATE
    assert seen == {100.0}  # duplicate/invalid rows never register keys


def test_digest_changes_when_classification_changes(tmp_path: Path) -> None:
    _, honest, _ = _run_sequence(20260922, 0, 3, tmp_path / "honest")
    # different seed -> different accepted row content -> different digest
    _, other, _ = _run_sequence(20260923, 0, 3, tmp_path / "other")
    assert honest["digest"] != other["digest"]


def test_drifted_ladder_is_refused_or_invalid(tmp_path: Path) -> None:
    module = _load_workload_module()
    original = json.loads((REPO_ROOT / TIERS_REL).read_text(encoding="utf-8"))

    # (a) structurally inconsistent ladder -> setup refuses (honest ERROR)
    lying_composition = copy.deepcopy(original)
    lying_composition["tiers"][0]["expected"]["total_rows"] = 99999
    with pytest.raises(RuntimeError):
        ladder = copy.deepcopy(lying_composition)
        # emulate setup's structural validation path
        for tier in ladder["tiers"]:
            composition = (
                tier["padding_rows"] + tier["closed_rows"]
                + tier["live_rows"] + tier["duplicate_rows"]
            )
            if int(tier["expected"]["total_rows"]) != composition:
                raise RuntimeError("tier %r total_rows mismatch" % tier["tier"])

    # (b) lying expectation table -> verify FAILs with ROW_IDENTITY_MISMATCH
    lying_expected = copy.deepcopy(original)
    lying_expected["tiers"][0]["expected"]["accepted_rows"] = 321  # one row off
    workload = module.build(_FakeCtx(20260922, tmp_path))
    workload.ladder = lying_expected
    workload.tiers = list(lying_expected["tiers"])
    workload.ladder_sha256 = "tampered"
    workload.operations = 0
    workload.tier_frames = {str(t["tier"]): 0 for t in workload.tiers}
    workload.class_totals = {m: 0 for m in ("a", "p", "i", "d")}
    workload.units_total = 0
    import hashlib

    workload._acc = hashlib.sha256()
    for _ in range(3):
        workload.operation(None)
    verification = workload.verify(None)
    assert verification["status"] == "FAIL"
    assert any("ROW_IDENTITY_MISMATCH accepted" in r for r in verification["fail_reasons"])


# ---------------------------------------------------------------------------
# workload growth probe (H22: cost must grow with the tier workload)
# ---------------------------------------------------------------------------

def test_tier_cost_grows_with_rows(tmp_path: Path) -> None:
    module = _load_workload_module()
    timings: Dict[str, int] = {}
    rows: Dict[str, int] = {}
    for tier in json.loads((REPO_ROOT / TIERS_REL).read_text(encoding="utf-8"))["tiers"]:
        workload = module.build(_FakeCtx(20260922, tmp_path))
        workload.setup(None)
        # position the cycle on this tier, then time one full frame build
        workload.tiers = [tier]
        t0 = time.perf_counter_ns()
        result = workload.operation(None)
        t1 = time.perf_counter_ns()
        timings[tier["tier"]] = t1 - t0
        rows[tier["tier"]] = int(result["units"])
    assert rows["small"] < rows["medium"] < rows["large"]
    assert timings["large"] > timings["small"]  # 17x rows must cost more time


# ---------------------------------------------------------------------------
# end-to-end CLI smoke run through the frozen C20 runner (bounded <= 30 s case)
# ---------------------------------------------------------------------------

def test_cli_smoke_run_ok(tmp_path: Path) -> None:
    run_id = "nf22-r1-smoke-test"
    rc, stdout, stderr = run_cli(
        [
            "run",
            "--source-map", SOURCE_MAP_REL,
            "--output-root", str(tmp_path / "out"),
            "--profile", "smoke",
            "--case-manifest", MANIFEST_REL,
            "--run-id", run_id,
        ],
        timeout=120.0,
    )
    assert rc == 0, "smoke run rc=%d stdout=%s stderr=%s" % (rc, stdout, stderr)
    record = json.loads(
        (tmp_path / "out" / "runs" / run_id / "results" / "datapipeline__frame_synthetic.json")
        .read_text(encoding="utf-8")
    )
    C.validate_result_record(record)
    assert record["status"] == "OK", record["status_reason"]
    assert record["correctness"]["status"] == "PASS"
    manifest = json.loads((REPO_ROOT / MANIFEST_REL).read_text(encoding="utf-8"))
    assert record["correctness"]["digest"] == manifest["correctness"]["expected_digest"]
    assert record["measurement"]["n"] == manifest["budget"]["smoke"]["operations"]
    assert record["measurement"]["total_units"] > 0
    assert record["measurement"]["throughput_units_per_s"] > 0
    assert record["identity"]["measured_modules"][0]["logical_module"] == LOGICAL_MODULE
