"""DP08 release-verification tests (H08.3/H08.6/H08.7; SYNTHETIC, offline).

AUTHORITY: work-orders/DP08-integration.md, 04-validation-and-release.md
("verify_datapipeline_release.py --manifest <frozen-release.json> --output
<new-evidence-dir>").  The verifier is exercised end-to-end: manifest
building from the node freeze manifests (programmatic hashes only), clean
verification over an isolated copy of the released files, drift/missing
counterexamples, frozen-output refusal and the V15 canary gate (a RUN
claim without real canary evidence is refused - no canary may be inferred
from mocks).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "DP08"
_spec = importlib.util.spec_from_file_location("dp08_bootstrap_rel", _FIXTURES / "bootstrap.py")
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
_RESEARCH_UNUSED = _bootstrap.ensure_importable()  # integrated tree importable

_SCRIPT = _bootstrap.REPO_ROOT / "scripts" / "verify_datapipeline_release.py"
_spec2 = importlib.util.spec_from_file_location("dp08_release_verifier", _SCRIPT)
verifier = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(verifier)


@pytest.fixture(scope="module")
def built_manifest(tmp_path_factory) -> Path:
    """A release manifest built from the REAL node freeze manifests, plus a
    DP08 summary fixture so the DP08-owned matrix rows resolve."""
    out = tmp_path_factory.mktemp("release") / "release-manifest.json"
    summary = out.parent / "dp08-summary.json"
    summary.write_text(json.dumps({
        "pytest": "61 passed, 1 skipped",
        "exit_code": 0,
        "command": "python -B -m pytest tests/datapipeline/test_end_to_end.py "
                   "tests/datapipeline/test_boundaries.py tests/datapipeline/test_release.py",
        "log": "docs/datapipeline-evidence-20260923-r1/DP08/r1/pytest-run-r1.log",
    }), encoding="utf-8")
    rc = verifier.main([
        "--repo", str(_bootstrap.REPO_ROOT),
        "--build-manifest", str(out),
        "--dp08-summary", str(summary),
    ])
    assert rc == 0
    return out


@pytest.fixture(scope="module")
def fake_repo(built_manifest, tmp_path_factory) -> Path:
    """An isolated copy of every file the manifest lists, preserving the
    repo-relative layout (the verifier's --repo target for counterexamples;
    the real repository is never tampered with)."""
    root = tmp_path_factory.mktemp("fakerepo")
    manifest = json.loads(Path(built_manifest).read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        source = _bootstrap.REPO_ROOT / entry["path"]
        target = root / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    # the verifier also reads pytest logs for the matrix; copy them over
    for log_rel in verifier.NODE_PYTEST_LOGS.values():
        source = _bootstrap.REPO_ROOT / log_rel
        if source.is_file():
            target = root / log_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    return root


class TestBuildManifest:
    def test_manifest_covers_all_eight_owners_programmatically(self, built_manifest):
        manifest = json.loads(Path(built_manifest).read_text(encoding="utf-8"))
        nodes = {entry["node"] for entry in manifest["files"]}
        assert nodes == {"DP01", "DP02", "DP03", "DP04", "DP05", "DP06", "DP07", "DP08"}
        # every entry carries a 64-hex sha256 read from a node manifest
        for entry in manifest["files"]:
            assert len(entry["sha256"]) == 64
            int(entry["sha256"], 16)
        # DP08's own delivered write set is folded in (script/tests/runbook)
        dp08_paths = {e["path"] for e in manifest["files"] if e["node"] == "DP08"}
        assert "scripts/verify_datapipeline_release.py" in dp08_paths
        assert "docs/datapipeline-runbook.md" in dp08_paths
        # staging scaffolds are flagged, never presented as candidate files
        scaffolds = [e for e in manifest["files"] if e["role"] == "staging_scaffold"]
        assert scaffolds, "staging scaffolds must be visible and flagged"

    def test_v15_is_not_run_without_canary_evidence(self, built_manifest):
        manifest = json.loads(Path(built_manifest).read_text(encoding="utf-8"))
        assert manifest["v_matrix"]["V15"]["status"] == "NOT_RUN"
        # PG-required legs stay NOT_RUN (never laundered to green)
        assert manifest["v_matrix"]["V03"]["status"] == "NOT_RUN"
        assert manifest["v_matrix"]["V05"]["status"] == "NOT_RUN"

    def test_manifest_build_is_deterministic(self, tmp_path):
        outs = []
        for i in (1, 2):
            out = tmp_path / f"m{i}.json"
            assert verifier.main(["--repo", str(_bootstrap.REPO_ROOT),
                                  "--build-manifest", str(out)]) == 0
            outs.append(json.loads(out.read_text(encoding="utf-8")))
        assert outs[0]["files"] == outs[1]["files"]
        assert outs[0]["v_matrix"] == outs[1]["v_matrix"]


class TestVerifyManifest:
    def test_clean_verification_exit_zero(self, built_manifest, fake_repo, tmp_path):
        out = tmp_path / "verification"
        rc = verifier.main([
            "--repo", str(fake_repo),
            "--manifest", str(built_manifest),
            "--output", str(out),
        ])
        assert rc == 0
        report = json.loads((out / "verification-report.json").read_text(encoding="utf-8"))
        assert report["clean"] is True
        assert report["drift"] == [] and report["missing"] == []
        assert report["files_verified"] == report["manifest_files"]
        assert (out / "verification-summary.md").is_file()

    def test_tampered_file_is_detected_by_exact_path(self, built_manifest, fake_repo, tmp_path):
        manifest = json.loads(Path(built_manifest).read_text(encoding="utf-8"))
        victim = fake_repo / manifest["files"][10]["path"]
        original = victim.read_bytes()
        victim.write_bytes(original + b"\n# tampered (SYNTHETIC counterexample)\n")
        try:
            out = tmp_path / "verification"
            rc = verifier.main([
                "--repo", str(fake_repo),
                "--manifest", str(built_manifest),
                "--output", str(out),
            ])
            assert rc == 1
            report = json.loads((out / "verification-report.json").read_text(encoding="utf-8"))
            assert report["clean"] is False
            assert [d["path"] for d in report["drift"]] == [manifest["files"][10]["path"]]
        finally:
            victim.write_bytes(original)

    def test_missing_file_is_reported(self, built_manifest, fake_repo, tmp_path):
        manifest = json.loads(Path(built_manifest).read_text(encoding="utf-8"))
        victim = fake_repo / manifest["files"][20]["path"]
        backup = victim.read_bytes()
        victim.unlink()
        try:
            out = tmp_path / "verification"
            rc = verifier.main([
                "--repo", str(fake_repo),
                "--manifest", str(built_manifest),
                "--output", str(out),
            ])
            assert rc == 1
            report = json.loads((out / "verification-report.json").read_text(encoding="utf-8"))
            assert manifest["files"][20]["path"] in report["missing"]
        finally:
            victim.write_bytes(backup)

    def test_output_inside_frozen_face_is_refused(self, built_manifest, fake_repo):
        rc = verifier.main([
            "--repo", str(fake_repo),
            "--manifest", str(built_manifest),
            "--output", str(_bootstrap.REPO_ROOT / "docs" / "devplan-datapipeline-20260923-r1" / "DP01" / "r1"),
        ])
        assert rc == 1

    def test_v15_run_claim_without_canary_evidence_is_refused(self, built_manifest, fake_repo, tmp_path):
        """A manifest claiming V15 RUN with no injected canary evidence is
        a gate violation - the canary can never be inferred from mocks."""
        payload = json.loads(Path(built_manifest).read_text(encoding="utf-8"))
        payload["v_matrix"]["V15"] = {"owner": "DP08", "status": "RUN"}
        forged = tmp_path / "forged-manifest.json"
        forged.write_text(json.dumps(payload), encoding="utf-8")
        out = tmp_path / "verification"
        rc = verifier.main([
            "--repo", str(fake_repo), "--manifest", str(forged), "--output", str(out),
        ])
        assert rc == 1
        report = json.loads((out / "verification-report.json").read_text(encoding="utf-8"))
        assert any("V15" in err for err in report["gate_errors"])

    def test_v15_run_claim_backed_by_canary_evidence_passes_gate(self, built_manifest, fake_repo, tmp_path):
        payload = json.loads(Path(built_manifest).read_text(encoding="utf-8"))
        payload["v_matrix"]["V15"] = {"owner": "DP08", "status": "RUN"}
        forged = tmp_path / "forged-manifest.json"
        forged.write_text(json.dumps(payload), encoding="utf-8")
        canary = tmp_path / "canary-evidence.json"
        canary.write_text(json.dumps({"schema": "dp08-canary/1", "SYNTHETIC": False}), encoding="utf-8")
        out = tmp_path / "verification"
        rc = verifier.main([
            "--repo", str(fake_repo), "--manifest", str(forged), "--output", str(out),
            "--canary-evidence", str(canary),
        ])
        assert rc == 0
        report = json.loads((out / "verification-report.json").read_text(encoding="utf-8"))
        assert report["clean"] is True

    def test_dependency_pin_cross_check_flags_divergent_copies(self, tmp_path):
        """Two files pinned with the SAME hash must stay byte-identical; a
        divergent twin is reported (counterexample with a synthetic tree)."""
        root = tmp_path
        a = root / "a.py"; a.write_bytes(b"same")
        b = root / "b.py"; b.write_bytes(b"different")
        manifest = {
            "schema": verifier.RELEASE_SCHEMA,
            "files": [
                {"path": "a.py", "sha256": verifier._sha256(a), "node": "X", "role": "write_set"},
                {"path": "b.py", "sha256": verifier._sha256(a), "node": "Y", "role": "dependency_pin"},
            ],
            "v_matrix": {"V15": {"status": "NOT_RUN"}},
        }
        report = verifier.verify_manifest(manifest, root)
        assert report["dependency_pin_mismatch"]


class TestCLIShape:
    def test_verify_without_output_is_refused(self, built_manifest, fake_repo):
        assert verifier.main([
            "--repo", str(fake_repo), "--manifest", str(built_manifest),
        ]) == 1

    def test_missing_manifest_is_an_error(self, tmp_path):
        assert verifier.main([
            "--repo", str(tmp_path),
            "--manifest", str(tmp_path / "nope.json"),
            "--output", str(tmp_path / "out"),
        ]) == 1
