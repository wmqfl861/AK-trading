"""M1-06 artifacts: canonical encoding, atomic publish, contract lock, evidence."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ak_trading.v2 import CONTRACT_VERSION
from ak_trading.v2 import artifacts as a
from ak_trading.v2 import contracts as c
from ak_trading.v2 import schemas as s
from ak_trading.v2.policy import research_policy

ROOT = Path(__file__).resolve().parents[1]
ZERO = "0" * 64
M = 60_000_000_000
H = 3_600_000_000_000


def nano(hour: int, minute: int = 0) -> int:
    moment = datetime(2026, 1, 9, hour, minute, tzinfo=timezone.utc) - timedelta(hours=8)
    delta = moment - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9


def quality() -> c.DataQuality:
    return c.DataQuality(status="READY", reasons=(), temporal_basis="observed",
                         input_hash=hashlib.sha256(b"artifacts-test").hexdigest())


def input_provenance() -> dict:
    return {
        "schema_version": "ak_probability_input_v2.1.0", "contract_version": CONTRACT_VERSION,
        "dataset_kind": "SYNTHETIC", "created_at_nano": nano(16),
        "request": {"contract_ids": ["X"], "raw_symbols": ["X"], "start_nano": 0,
                    "end_nano": nano(16), "as_of_nano": nano(16), "durations": [60, 86400],
                    "page_size": 5000, "maximum_rows": 200000, "maximum_bytes": 268435456},
        "read_only_transaction": {"isolation": "NOT_APPLICABLE", "read_only": True,
                                  "verified": False},
        "source_hashes": {}, "limitations": ["synthetic"],
    }


def test_canonical_encoding_and_publish_bundle(tmp_path: Path) -> None:
    payload = {"b": 1, "a": {"z": [1, 2], "y": None}, "c": "中文"}
    encoded = s.canonical_json_bytes(payload)
    assert encoded.endswith(b"\n") and not encoded.endswith(b"\n\n")
    assert encoded.count(b"\n") == 1
    assert encoded.decode("utf-8") == '{"a":{"y":null,"z":[1,2]},"b":1,"c":"中文"}\n'
    assert s.canonical_json_bytes(payload) == encoded
    with pytest.raises(s.SchemaError):
        s.canonical_json_bytes({"x": float("nan")})
    with pytest.raises(s.SchemaError):
        s.canonical_json_bytes({"x": float("inf")})
    with pytest.raises(s.SchemaError):
        s.parse_json_bytes(b'{"a":1,"a":2}')
    with pytest.raises(s.SchemaError):
        s.parse_json_bytes("﻿{}".encode("utf-8"))
    with pytest.raises(s.SchemaError):
        s.parse_json_bytes(b'{"x": NaN}')

    payloads = {"bars.csv": b"header\n", "availability.json": b"{}"}
    output = tmp_path / "bundle"
    manifest_sha = a.publish_bundle(output, payloads, input_provenance())
    assert manifest_sha == a.sha256_hex((output / "manifest.json").read_bytes())
    manifest = s.parse_json_bytes((output / "manifest.json").read_bytes())
    assert manifest["files"] == {name: a.sha256_hex(data) for name, data in payloads.items()}
    assert "manifest.json" not in manifest["files"]
    # Output directories must be new; payload names are validated before writing.
    with pytest.raises(a.ArtifactsError):
        a.publish_bundle(output, payloads, input_provenance())
    with pytest.raises(a.ArtifactsError):
        a.publish_bundle(tmp_path / "escape", {"../escape.csv": b"x"}, input_provenance())
    assert not (tmp_path / "escape").exists()
    with pytest.raises(a.ArtifactsError):
        a.publish_bundle(tmp_path / "abs", {"/abs.csv": b"x"}, input_provenance())
    with pytest.raises(a.ArtifactsError):
        a.publish_bundle(tmp_path / "reserved", {"manifest.json": b"x"}, input_provenance())
    # The caller cannot forge files or contract_version.
    forged = dict(input_provenance())
    forged["files"] = {"bars.csv": ZERO}
    with pytest.raises(a.ArtifactsError):
        a.publish_bundle(tmp_path / "forged", payloads, forged)
    wrong_version = dict(input_provenance(), contract_version="other")
    with pytest.raises(a.ArtifactsError):
        a.publish_bundle(tmp_path / "wrong", payloads, wrong_version)
    missing = {k: v for k, v in input_provenance().items() if k != "limitations"}
    with pytest.raises(a.ArtifactsError):
        a.publish_bundle(tmp_path / "missing", payloads, missing)

    # Unified M2-M5 run-manifest shape: control fields are lifted to the root,
    # contract_version/files are derived, never caller-filled.
    run_payloads = {"metrics.json": b"{}", "report.md": b"# x\n"}
    run_provenance = {
        "schema_version": "ak_probability_validation_run_v2.1.0",
        "created_at_nano": nano(16),
        "provenance": {
            "node": "M5", "dataset_kind": "SYNTHETIC",
            "input_hashes": ["1" * 64], "code_hashes": {"m5": "2" * 64},
            "parameter_hashes": {}, "model_hashes": {}, "policy_versions": {"m1": "v"},
            "seed": 7, "limitations": [],
            "previous_segment_manifest_sha256": None,
            "recovery_source_sha256": None, "ignored_tail_sha256": None,
        },
    }
    run_output = tmp_path / "run"
    run_sha = a.publish_bundle(run_output, run_payloads, run_provenance)
    run_manifest = s.parse_json_bytes((run_output / "manifest.json").read_bytes())
    assert set(run_manifest) == {"schema_version", "contract_version", "created_at_nano",
                                 "files", "provenance"}
    assert run_manifest["contract_version"] == CONTRACT_VERSION
    assert run_manifest["provenance"] == run_provenance["provenance"]
    assert run_sha == a.sha256_hex((run_output / "manifest.json").read_bytes())
    broken_run = {"schema_version": "x", "created_at_nano": 1}
    with pytest.raises(a.ArtifactsError):
        a.publish_bundle(tmp_path / "run2", run_payloads, broken_run)


def _stage_acceptance_world(tree: Path) -> bytes:
    """Stage a minimal frozen SYNTHETIC acceptance world inside ``tree``:
    six fixture attachments plus a complete F whose files table binds every
    src/ak_trading/v2 *.py present in the tree, both runners, every
    lock-required test file and the attachments themselves. Returns the F
    bytes actually written (T7 §5.1/§5.3 wiring)."""
    attachments = {
        "tests/fixtures/v2/ep1_t7/recipe.json": b'{"recipe": "ep1-t7-synthetic-v1"}\n',
        "tests/fixtures/v2/ep1_t7/minute_bars.jsonl": b'{"m": 1}\r\n{"m": 2}\n',
        "tests/fixtures/v2/ep1_t7/daily_bars.jsonl": b'{"d": 1}\n',
        "tests/fixtures/v2/ep1_t7/calendar.json": b'{"trading_days": []}\n',
        "tests/fixtures/v2/ep1_t7/rules.json": b'{"rules": []}\n',
        "tests/fixtures/v2/ep1_t7/costs.json": b'{"costs": {}}\n',
    }
    for name, payload in attachments.items():
        target = tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    listed = sorted({
        *attachments,
        *(p.relative_to(tree).as_posix()
          for p in (tree / "src/ak_trading/v2").rglob("*.py")),
        a.ACCEPTANCE_RUNNER_PATH,
        a.ACCEPTANCE_OFFLINE_RUNNER_PATH,
        *a.PUBLIC_TEST_FILES,
        *a.DOWNSTREAM_TEST_FILES,
    })
    entries = [{"path": name, "bytes": len((tree / name).read_bytes()),
                "sha256": a.sha256_hex((tree / name).read_bytes())}
               for name in listed]
    document = {
        "schema_version": a.ACCEPTANCE_INPUT_MANIFEST_SCHEMA_ID,
        "dataset_kind": "SYNTHETIC",
        "labels": ["SYNTHETIC"],
        "generation": {"recipe": "tests/fixtures/v2/ep1_t7/recipe.json", "seed": 7},
        "inputs": {
            "minute_bars": ["tests/fixtures/v2/ep1_t7/minute_bars.jsonl"],
            "daily_bars": ["tests/fixtures/v2/ep1_t7/daily_bars.jsonl"],
            "calendar": ["tests/fixtures/v2/ep1_t7/calendar.json"],
            "rules": ["tests/fixtures/v2/ep1_t7/rules.json"],
            "costs": ["tests/fixtures/v2/ep1_t7/costs.json"],
        },
        "splits": {"outer": {"train_days": 152}, "inner": {"fraction": 0.64}},
        "selection": {"rule": "FIRST_FEASIBLE_P1_OPEN_PER_WINDOW"},
        "bindings": [{"strategy_id": "strat-1", "exit_binding_sha256": ZERO}],
        "files": entries,
    }
    payload = s.canonical_json_bytes(document)
    target = tree / a.ACCEPTANCE_INPUT_MANIFEST_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return payload


def _mirror_lock_tree(tree: Path) -> Path:
    """Mirror every lock-bound file into a fresh temp tree, staging stubs for
    owner deliverables not yet on disk (the M3C P0 pattern, and the M5 T7
    evidence file until the fixture work order lands it), the two runners,
    and a complete synthetic acceptance world (F), so the full lock pipeline
    is exercised without writing other nodes' files."""
    import shutil
    for name in (*a.PUBLIC_CODE_FILES, *a.PUBLIC_TEST_FILES,
                 *a.DOWNSTREAM_TEST_FILES, *a.FIXTURE_FILES,
                 a.ACCEPTANCE_RUNNER_PATH, a.ACCEPTANCE_OFFLINE_RUNNER_PATH):
        if (ROOT / name).is_file():
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
    m3c_entries = {
        "tests/test_v2_m3c_registry.py": "test_p0_contract_registry_binding_and_parameters",
        "tests/test_v2_m3c_features.py": "test_p0_contract_feature_keys_scope_and_visibility",
        "tests/test_v2_m3c_state.py": "test_p0_contract_state_confirmation_roundtrip",
        "tests/test_v2_m3c_trailing.py": "test_p0_contract_trailing_spec_and_protection_record",
        "tests/test_v2_m3c_time_decay.py": "test_p0_contract_time_decay_p1_parameter_shape",
        "tests/test_v2_m3c_causality.py": "test_p0_contract_candidate_state_and_execution_time",
    }
    for name, entry in m3c_entries.items():
        target = tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        stub = (
            "def " + entry + "() -> None:" + "\n"
            "    raise NotImplementedError(\"M3C owner P0 deliverable\")" + "\n"
        )
        target.write_text(stub, encoding="utf-8")
    t7_evidence = "tests/test_v2_m5_t7_evidence.py"
    if not (tree / t7_evidence).is_file():
        target = tree / t7_evidence
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "def test_t7_evidence_owner_deliverable() -> None:\n"
            "    raise NotImplementedError(\"M5 owner T7 deliverable\")\n",
            encoding="utf-8")
    _stage_acceptance_world(tree)
    return tree


def test_build_and_verify_contract_lock_roundtrip(tmp_path: Path) -> None:
    # OQ-2/T7 §5.3: the lock assembly requires every bound file on disk. The
    # real tree is mirrored into a temp root with contract-level stubs for
    # owner deliverables not yet landed and a synthetic frozen acceptance
    # world (F), so the full lock pipeline — including the recursive fixture
    # table — is exercised without writing other nodes' files.
    import shutil
    tree = _mirror_lock_tree(tmp_path / "tree")
    lock = a.build_contract_lock(tree, "001", ZERO)
    assert lock["schema_version"] == "ak_probability_contract_lock_v2.1.0"
    assert lock["contract_version"] == CONTRACT_VERSION
    assert lock["document_version"] == "devplan-v2.1.7"
    assert set(lock["public_files"]) == set(a.PUBLIC_CODE_FILES)
    assert set(lock["required_tests"]) == set((*a.PUBLIC_TEST_FILES, *a.DOWNSTREAM_TEST_FILES))
    assert lock["required_tests"]["tests/test_v2_m3c_registry.py"] ==         ["test_p0_contract_registry_binding_and_parameters"]
    # required_check_ids registers every node's full H list plus applicable D
    # items (07 §R1 all-node inventory + 007 §9.2), not just M1's clauses.
    assert set(lock["required_check_ids"]) == {"M1", "M2", "M3A", "M3B", "M3C", "M4", "M5"}
    assert lock["required_check_ids"]["M1"] == [
        *(f"M1-H{index:02d}" for index in range(1, 11)), "D01", "D02", "D07"]
    assert lock["required_check_ids"]["M3C"] == [
        *(f"M3C-H{index:02d}" for index in range(1, 17)),
        "D01", "D02", "D04", "D05", "D06", "D08", "D09", "D10"]
    assert lock["required_check_ids"]["M2"] == [
        *(f"M2-H{index:02d}" for index in range(1, 9)), "D02"]
    assert lock["required_check_ids"]["M4"] == [
        *(f"M4-H{index:02d}" for index in range(1, 13)),
        "D01", "D02", "D04", "D05", "D06", "D07", "D08", "D09"]
    assert lock["required_check_ids"]["M5"] == [
        *(f"M5-H{index:02d}" for index in range(1, 13)),
        *(f"D{index:02d}" for index in range(1, 12))]
    assert lock["policy"] == dict(research_policy())
    assert "M3C" in lock["ownership"]
    assert "ak_trading.v2.contracts.exit_binding_content_hash" in lock["public_signatures"]
    lock_path = tmp_path / "contract-lock.json"
    lock_path.write_bytes(s.canonical_json_bytes(lock))
    a.verify_contract_lock(lock_path, tree)

    # T7 §5.1/§5.3: fixture_files[F] is H_F (UTF-8/LF-normalized manifest
    # bytes — the staged bytes are already LF); F is an M1-owned lock file;
    # and no F/E/R identity ever becomes a new lock root field.
    manifest_payload = (tree / a.ACCEPTANCE_INPUT_MANIFEST_PATH).read_bytes()
    assert lock["fixture_files"][a.ACCEPTANCE_INPUT_MANIFEST_PATH] == \
        a.sha256_hex(manifest_payload)
    assert lock["fixture_files"][a.ACCEPTANCE_INPUT_MANIFEST_PATH] == \
        a.acceptance_input_manifest_sha256(tree)
    assert a.ACCEPTANCE_INPUT_MANIFEST_PATH in lock["ownership"]["M1"]
    assert set(lock) == set(a._LOCK_KEYS)
    # H(R) wiring on the published-state composite quick check (§5.4 step 2:
    # a candidate lock's reviewer_report_sha256 points at R0's real bytes).
    report_path = tmp_path / "R0.md"
    report_path.write_bytes(b"R0 frozen-input check record\n")
    candidate = a.build_contract_lock(
        tree, "ep1-r1-candidate", a.sha256_hex(report_path.read_bytes()))
    candidate_path = tmp_path / "candidate-lock.json"
    candidate_path.write_bytes(s.canonical_json_bytes(candidate))
    a.verify_contract_lock_with_report(candidate_path, tree, report_path)
    tampered_report = tmp_path / "R0-tampered.md"
    tampered_report.write_bytes(b"R0 frozen-input check record\ndrift")
    with pytest.raises(a.ArtifactsError, match="reviewer_report_sha256_mismatch"):
        a.verify_contract_lock_with_report(candidate_path, tree, tampered_report)
    with pytest.raises(a.ArtifactsError, match="reviewer_report_not_readable"):
        a.verify_contract_lock_with_report(candidate_path, tree, tmp_path / "absent-R.md")

    # Tampering with any bound surface fails verification.
    def verify_with(mutate) -> None:
        mutated = json.loads(json.dumps(lock))
        mutate(mutated)
        path = tmp_path / "mutated-lock.json"
        path.write_bytes(s.canonical_json_bytes(mutated))
        with pytest.raises(a.ArtifactsError):
            a.verify_contract_lock(path, tree)

    verify_with(lambda document: document["public_files"].__setitem__(
        "src/ak_trading/v2/contracts.py", ZERO))
    verify_with(lambda document: document["fixture_files"].__setitem__(
        "tests/fixtures/v2/shared.json", ZERO))
    verify_with(lambda document: document["fixture_files"].__setitem__(
        a.ACCEPTANCE_INPUT_MANIFEST_PATH, ZERO))
    verify_with(lambda document: document["public_signatures"].__setitem__(
        "Horizon", ZERO))
    verify_with(lambda document: document["public_signatures"].pop(
        a.BUILD_CALIBRATION_EVIDENCE_DECLARATION_KEY))
    verify_with(lambda document: document["public_signatures"].__setitem__(
        a.BUILD_CALIBRATION_EVIDENCE_DECLARATION_KEY, ZERO))
    verify_with(lambda document: document["public_signatures"].pop(
        next(iter(a.M4_LEDGER_DECLARATIONS))))
    verify_with(lambda document: document["schemas"].__setitem__(
        s.BARS_SCHEMA_ID, ZERO))
    verify_with(lambda document: document["schemas"].pop(
        s.METRICS_SCHEMA_ID))
    verify_with(lambda document: document["schemas"].pop(
        s.SEASONALITY_SCHEMA_ID))
    verify_with(lambda document: document["required_tests"].__setitem__(
        "tests/test_v2_m1_clock.py", ["test_never_existed"]))
    verify_with(lambda document: document["required_check_ids"].pop("M2"))
    verify_with(lambda document: document["required_check_ids"].__setitem__(
        "M1", ["M1-H01", "D01"]))
    verify_with(lambda document: document["required_check_ids"].__setitem__(
        "M1", ["M1-H01", "D03"]))
    verify_with(lambda document: document["policy"].__setitem__("policy_version", "x"))
    verify_with(lambda document: document.__setitem__("reviewed_revision", ""))
    verify_with(lambda document: document.__setitem__("reviewer_report_sha256", "nothex"))

    # A lock whose code hash reflects live bytes drifts when a file changes.
    drifted = tmp_path / "drift"
    drifted.mkdir()
    mirrored = {name: (tree / name).read_bytes() for name in a.PUBLIC_CODE_FILES}
    for name, payload in mirrored.items():
        target = drifted / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    (drifted / "src/ak_trading/v2/policy.py").write_bytes(
        mirrored["src/ak_trading/v2/policy.py"] + b"\n# drift\n")
    with pytest.raises(a.ArtifactsError):
        a.verify_contract_lock(lock_path, drifted)

    # §5.3 item 2: the lock-side fixture check recurses into F's table — a
    # drifted attachment byte invalidates the lock even though H_F itself is
    # unchanged (never a bare unchecked index).
    drifted_fixture = tmp_path / "drifted-fixture"
    shutil.copytree(tree, drifted_fixture)
    (drifted_fixture / "tests/fixtures/v2/ep1_t7/minute_bars.jsonl").write_bytes(
        b'{"m": 9}\r\n{"m": 2}\n')
    with pytest.raises(a.ArtifactsError, match="attachment_hash_mismatch"):
        a.verify_contract_lock(lock_path, drifted_fixture)

    with pytest.raises(ValueError):
        a.build_contract_lock(ROOT, "", ZERO)
    with pytest.raises(ValueError):
        a.build_contract_lock(ROOT, "001", "not-a-hash")


def test_acceptance_input_manifest_freeze_drift_and_rejections(tmp_path: Path) -> None:
    # T7 §5.1/§5.3 wiring: F freeze semantics (H_F over UTF-8/LF-normalized
    # bytes; attachments over their original raw bytes), drift
    # counterexamples, circular-hash rejection and attachment escape
    # rejection, each against a fresh synthetic world.
    import shutil
    pristine = _mirror_lock_tree(tmp_path / "pristine")
    payload = (pristine / a.ACCEPTANCE_INPUT_MANIFEST_PATH).read_bytes()
    h_f = a.acceptance_input_manifest_sha256(pristine)
    assert h_f == a.sha256_hex(payload)  # staged F bytes are already LF
    assert a.verify_acceptance_input_manifest(pristine) == h_f
    assert a._fixture_hashes(pristine)[a.ACCEPTANCE_INPUT_MANIFEST_PATH] == h_f
    # A CRLF-flavored copy of the same manifest yields the same H_F; the
    # CRLF-bearing minute_bars.jsonl still verifies, proving attachments are
    # hashed over raw bytes without re-serialization.
    crlf = tmp_path / "crlf"
    shutil.copytree(pristine, crlf)
    (crlf / a.ACCEPTANCE_INPUT_MANIFEST_PATH).write_bytes(payload.replace(b"\n", b"\r\n"))
    assert a.acceptance_input_manifest_sha256(crlf) == h_f
    assert a.verify_acceptance_input_manifest(crlf) == h_f

    def rewrite_manifest(case: Path, mutate) -> None:
        document = json.loads((case / a.ACCEPTANCE_INPUT_MANIFEST_PATH).read_bytes())
        mutate(document)
        (case / a.ACCEPTANCE_INPUT_MANIFEST_PATH).write_bytes(
            s.canonical_json_bytes(document))

    cases = [
        # Drift counterexamples: bytes (same length, different content),
        # size, absence, unlisted v2 source, unlisted fixture file.
        (lambda t: (t / "tests/fixtures/v2/ep1_t7/minute_bars.jsonl").write_bytes(
            b'{"m": 9}\r\n{"m": 2}\n'),
         "attachment_hash_mismatch"),
        (lambda t: rewrite_manifest(t, lambda d: d["files"][0].__setitem__("bytes", 0)),
         "attachment_size_mismatch"),
        (lambda t: (t / "tests/fixtures/v2/ep1_t7/costs.json").unlink(),
         "attachment_missing"),
        (lambda t: (t / "src/ak_trading/v2/unlisted_module.py").write_bytes(b"X = 1\n"),
         "required_source_not_listed"),
        (lambda t: (t / "tests/fixtures/v2/ep1_t7/extra-unlisted.json").write_bytes(b"{}\n"),
         "unlisted_fixture_file"),
        # Circular-hash rejection: F never lists itself and never carries the
        # final lock hash, a future report hash or post-run readiness at any
        # depth; a future lock file is not an attachable input.
        (lambda t: rewrite_manifest(t, lambda d: d["files"].append(
            {"path": a.ACCEPTANCE_INPUT_MANIFEST_PATH, "bytes": 1, "sha256": ZERO})),
         "must_not_list_itself"),
        (lambda t: rewrite_manifest(t, lambda d: d.__setitem__("contract_lock_sha256", ZERO)),
         "root_fields_must_be_exactly"),
        (lambda t: rewrite_manifest(t, lambda d: d.pop("bindings")),
         "root_fields_must_be_exactly"),
        (lambda t: rewrite_manifest(t, lambda d: d["splits"].__setitem__(
            "contract_lock_sha256", ZERO)),
         "circular_field_forbidden"),
        (lambda t: rewrite_manifest(t, lambda d: d["selection"].__setitem__(
            "readiness", "PASS")),
         "circular_field_forbidden"),
        (lambda t: rewrite_manifest(t, lambda d: d["bindings"][0].__setitem__(
            "reviewer_report_sha256", ZERO)),
         "circular_field_forbidden"),
        (lambda t: rewrite_manifest(t, lambda d: d["files"].append(
            {"path": "data/research_runs/v2/contracts-2.1.5/contract-lock.ep1-r1.json",
             "bytes": 2, "sha256": ZERO})),
         "attachment_path_forbidden"),
        # Attachment escapes and non-canonical paths.
        (lambda t: rewrite_manifest(t, lambda d: d["files"].append(
            {"path": "../outside.txt", "bytes": 1, "sha256": ZERO})),
         "unsafe_file_name"),
        (lambda t: rewrite_manifest(t, lambda d: d["files"].append(
            {"path": "/etc/passwd", "bytes": 1, "sha256": ZERO})),
         "unsafe_file_name"),
        (lambda t: rewrite_manifest(t, lambda d: d["files"].append(
            {"path": "tests\\fixtures\\v2\\x.json", "bytes": 1, "sha256": ZERO})),
         "forward_slashes"),
        (lambda t: rewrite_manifest(t, lambda d: d["files"].append(
            {"path": "scripts/./research_probability_v2.py", "bytes": 1, "sha256": ZERO})),
         "path_not_canonical"),
        # Table discipline: duplicates, unsorted, bad hex, bool size, extra
        # entry field, dangling recipe/input references.
        (lambda t: rewrite_manifest(t, lambda d: d["files"].append(dict(d["files"][0]))),
         "duplicate_attachment"),
        (lambda t: rewrite_manifest(t, lambda d: d["files"].reverse()),
         "sorted_by_path"),
        (lambda t: rewrite_manifest(t, lambda d: d["files"][0].__setitem__("sha256", "A" * 64)),
         "entry_sha256_must_be"),
        (lambda t: rewrite_manifest(t, lambda d: d["files"][0].__setitem__("bytes", True)),
         "entry_bytes_must_be"),
        (lambda t: rewrite_manifest(t, lambda d: d["files"][0].__setitem__("extra", 1)),
         "entry_fields_must_be"),
        (lambda t: rewrite_manifest(t, lambda d: d["generation"].__setitem__(
            "recipe", "tests/fixtures/v2/ep1_t7/nope.json")),
         "reference_not_listed"),
        (lambda t: rewrite_manifest(t, lambda d: d["inputs"].__setitem__(
            "calendar", ["tests/fixtures/v2/ep1_t7/nope.json"])),
         "reference_not_listed"),
        # Schema rejection.
        (lambda t: rewrite_manifest(t, lambda d: d.__setitem__("schema_version", "other")),
         "schema_version_mismatch"),
        (lambda t: rewrite_manifest(t, lambda d: d.__setitem__("dataset_kind", "HISTORICAL_PIT")),
         "dataset_kind_must_be_synthetic"),
        (lambda t: rewrite_manifest(t, lambda d: d.__setitem__("labels", ["SCENARIO"])),
         "labels_must_include_synthetic"),
        (lambda t: rewrite_manifest(t, lambda d: d.__setitem__("labels", ["SYNTHETIC", "SYNTHETIC"])),
         "labels_must_be_unique"),
        # Broken manifest bytes and absence.
        (lambda t: (t / a.ACCEPTANCE_INPUT_MANIFEST_PATH).write_bytes(b"{nope"),
         "acceptance_manifest_invalid_json"),
        (lambda t: (t / a.ACCEPTANCE_INPUT_MANIFEST_PATH).write_bytes(b"\xff\xfe{}"),
         "not_utf8"),
        (lambda t: (t / a.ACCEPTANCE_INPUT_MANIFEST_PATH).unlink(),
         "acceptance_input_manifest_not_frozen"),
    ]
    for index, (mutate, expected) in enumerate(cases):
        case = tmp_path / f"reject-{index:02d}"
        shutil.copytree(pristine, case)
        mutate(case)
        with pytest.raises(a.ArtifactsError, match=expected):
            a.verify_acceptance_input_manifest(case)


def test_reviewer_report_and_evidence_quick_check_wiring(tmp_path: Path) -> None:
    # §5.3 item 5 wiring: H(R) binds the report's actual bytes to
    # lock.reviewer_report_sha256; H_E binds a report-cited evidence-package
    # identity to the evidence manifest's actual bytes (the evidence
    # manifest is never recursively self-hashed).
    report = tmp_path / "R0.md"
    report.write_bytes(b"R0 frozen-input check record\n")
    h_r = a.sha256_hex(report.read_bytes())
    assert a.verify_reviewer_report_binding(
        {"reviewer_report_sha256": h_r}, report) == h_r
    with pytest.raises(a.ArtifactsError, match="reviewer_report_sha256_mismatch"):
        a.verify_reviewer_report_binding({"reviewer_report_sha256": ZERO}, report)
    with pytest.raises(a.ArtifactsError, match="reviewer_report_sha256_must_be_64_hex"):
        a.verify_reviewer_report_binding({"reviewer_report_sha256": "nothex"}, report)
    with pytest.raises(a.ArtifactsError, match="reviewer_report_not_readable"):
        a.verify_reviewer_report_binding(
            {"reviewer_report_sha256": h_r}, tmp_path / "absent-report.md")
    evidence = tmp_path / "evidence-manifest.json"
    evidence.write_bytes(b'{"owner_cold_chain": true}\n')
    h_e = a.sha256_hex(evidence.read_bytes())
    assert a.verify_evidence_manifest_binding(h_e, evidence) == h_e
    with pytest.raises(a.ArtifactsError, match="evidence_manifest_sha256_mismatch"):
        a.verify_evidence_manifest_binding(ZERO, evidence)
    with pytest.raises(a.ArtifactsError,
                       match="cited_evidence_manifest_sha256_must_be_64_hex"):
        a.verify_evidence_manifest_binding("nothex", evidence)
    with pytest.raises(a.ArtifactsError, match="evidence_manifest_not_readable"):
        a.verify_evidence_manifest_binding(h_e, tmp_path / "absent-evidence.json")


EXIT_REGISTRY = "9" * 64


def _chain_exit_binding() -> c.ExitPolicyBinding:
    values = dict(strategy_id="strat-1", entry_spec_sha256="f" * 64,
                  exit_spec_sha256s=(EXIT_REGISTRY,), sizing_spec_sha256=None,
                  priority=("exit-legacy-m1",), legacy_rules_version="1.0.0")
    return c.ExitPolicyBinding(
        binding_sha256=c.exit_binding_content_hash(**values), **values)


def _exit_kwargs():
    return {"exit_registry_sha256": EXIT_REGISTRY,
            "exit_binding": _chain_exit_binding()}


def _strategy_spec() -> c.StrategySpec:
    return c.StrategySpec(
        strategy_id="strat-1", family="MARTINGALE", version="1.0.0", parameter_version="p1",
        code_sha256="f" * 64, mode="MINUTE", parameters={}, required_factor_keys=(),
        allowed_regimes=("ORDERED_RANGE",), allowed_flags=(),
        confidence_policy_id="confidence-martingale-v2.1.0", path_barrier_key="ATR_1",
        max_holding=c.Horizon(unit="TRADING_MINUTE", value=60),
        holding_policy=c.HoldingPolicy(True, True, False, 30, 5), max_layers=3,
        max_total_lots=7, validation_stage="RESEARCH")


def _qualification(model_hashes: dict[str, str]) -> c.StrategyQualification:
    return c.StrategyQualification(
        qualification_id="qual-1", strategy_id="strat-1", strategy_version="1.0.0",
        parameter_version="p1", code_sha256="f" * 64,
        exit_registry_sha256=EXIT_REGISTRY,
        exit_binding_sha256=_chain_exit_binding().binding_sha256,
        exit_spec_sha256s=(EXIT_REGISTRY,), product="SYN.rb",
        contract_ids=("SYN.rb2609",), mode="MINUTE",
        horizons=(c.Horizon(unit="TRADING_MINUTE", value=60),),
        macro_mode="PRICE_ONLY", research_policy_version="research-policy-v2.1.0",
        macro_policy_version="macro-risk-v2.1.0",
        cost_scenarios=("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5", "GAP", "NO_STOP"),
        model_hashes=model_hashes, experiment_sha256="9" * 64, statistics="PASS",
        report_sha256="7" * 64, review_sha256="8" * 64, issued_at_nano=1,
        valid_until_nano=1 + 30 * 24 * H, reasons=())


def _group_key(scenario: str) -> str:
    return json.dumps(["STRATEGY", "split-1", "3" * 64, "strat-1", "SYN.RB",
                       "SYN.rb2609", "MINUTE", "TRADING_MINUTE", 60, "PRICE_ONLY",
                       scenario])


def _other_scope_key() -> str:
    return json.dumps(["STRATEGY", "split-1", "3" * 64, "other-strat", "SHFE.CU",
                       "SHFE.cu2609", "MINUTE", "TRADING_MINUTE", 60, "PRICE_ONLY",
                       "BASE"])


SIX_KEYS = [_group_key(scenario) for scenario in
            ("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5", "GAP", "NO_STOP")]


def _evidence_chain(model_hashes: dict[str, str]):
    bundle_sha = a.sha256_hex(s.canonical_json_bytes(model_hashes))
    metrics = {
        "schema_version": "ak_probability_metrics_v2.1.4",
        "research_policy_version": "research-policy-v2.1.0",
        "macro_policy_version": "macro-risk-v2.1.0",
        "cost_scenarios": ["BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5", "GAP", "NO_STOP"],
        "required_group_keys": [*SIX_KEYS, _other_scope_key()],
        "model_bundles": {bundle_sha: model_hashes},
        "forecast_groups": [],
        "strategy_groups": [
            *({
                "group_key": key, "split_id": "split-1",
                "model_bundle_sha256": bundle_sha, "run_policy_sha256": "3" * 64,
                "routing_mode": "FIXED", "fixed_strategy_id": "strat-1",
                "account_id": "acc-1", "strategy_id": "strat-1", "product": "SYN.RB",
                "contract_id": "SYN.rb2609", "mode": "MINUTE",
                "horizon_unit": "TRADING_MINUTE", "horizon_value": 60,
                "macro_mode": "PRICE_ONLY", "cost_scenario": scenario,
                "episodes": 10, "blocks": 5, "admission_rate": 0.5,
                "no_trade_rate": 0.5, "net_pnl_cny": "100",
                "conservative_ev_cny": "10", "max_drawdown_ratio": 0.01,
                "cvar95_cny": "50", "maximum_stress_loss_ratio": 0.01,
                "margin_shortfall_cny": "0", "peak_margin_cny": "1000",
                "max_inventory": 3, "longest_recovery_days": 2,
                "status": "PASS", "reasons": [],
                "exit_binding_sha256": _chain_exit_binding().binding_sha256,
                "exit_spec_sha256s": [EXIT_REGISTRY],
                "fixed_exit_binding_sha256": _chain_exit_binding().binding_sha256,
            } for scenario, key in zip(("BASE", "FEE_X2", "SLIPPAGE_X2",
                                         "MARGIN_X1_5", "GAP", "NO_STOP"), SIX_KEYS)),
            {  # another scope's failing group: recorded, never blocking, never
               # authorizing this qualification
                "group_key": _other_scope_key(), "split_id": "split-1",
                "model_bundle_sha256": bundle_sha, "run_policy_sha256": "3" * 64,
                "routing_mode": "FIXED", "fixed_strategy_id": "other-strat",
                "account_id": "acc-1", "strategy_id": "other-strat",
                "product": "SHFE.CU", "contract_id": "SHFE.cu2609", "mode": "MINUTE",
                "horizon_unit": "TRADING_MINUTE", "horizon_value": 60,
                "macro_mode": "PRICE_ONLY", "cost_scenario": "BASE",
                "episodes": 10, "blocks": 5, "admission_rate": 0.5,
                "no_trade_rate": 0.5, "net_pnl_cny": "-100",
                "conservative_ev_cny": "-10", "max_drawdown_ratio": 0.9,
                "cvar95_cny": "500", "maximum_stress_loss_ratio": 0.9,
                "margin_shortfall_cny": "0", "peak_margin_cny": "1000",
                "max_inventory": 3, "longest_recovery_days": 2,
                "status": "FAIL", "reasons": ["insufficient"],
                "exit_binding_sha256": _chain_exit_binding().binding_sha256,
                "exit_spec_sha256s": [EXIT_REGISTRY],
                "fixed_exit_binding_sha256": None,
            },
        ],
        "exclusions": {}, "bootstrap": {"method": "TRADING_DAY_BLOCK", "replicates": 1000,
                                        "seed": 7},
        "exit_registry_sha256": EXIT_REGISTRY,
        "exit_bindings": {_chain_exit_binding().binding_sha256:
                          s.encode_record(_chain_exit_binding())},
        "exit_groups": [],
        "replay_summaries": [{
            "account_id": "acc-1", "initial_equity_cny": "1000000",
            "terminal_equity_cny": "1000600",
            "closed_episode_net_pnl_cny": "600",
            "open_episode_realized_net_pnl_cny": None,
            "terminal_unrealized_cny": None,
            "open_episode_ids": [], "pending_intent_ids": [],
            "reconciliation_status": "PASS", "status": "COMPLETE",
            "reasons": [], "split_id": "split-1",
            "run_policy_sha256": "3" * 64, "model_bundle_sha256": bundle_sha,
        }],
    }
    metrics_bytes = s.canonical_json_bytes(metrics)
    experiment = {
        "schema_version": "ak_probability_experiment_v2.1.1",
        "spec": {
            "experiment_id": "exp-1", "run_id": "run-1", "input_manifest_hashes": ["6" * 64],
            "contract_lock_sha256": "4" * 64, "source_sha256": "f" * 64,
            "parameter_sha256": "7" * 64, "split_specs": [], "holdout_id": "hold-1",
            "holdout_scope": {}, "run_policies": [],
            "required_group_keys": [*SIX_KEYS, _other_scope_key()],
            "seed": 7, "research_policy_version": "research-policy-v2.1.0",
            "macro_policy_version": "macro-risk-v2.1.0", "macro_mode": "PRICE_ONLY",
            "evaluation_as_of_nano": 1, "exit_registry_sha256": EXIT_REGISTRY,
            "exit_bindings": [s.encode_record(_chain_exit_binding())],
        },
    }
    experiment_bytes = s.canonical_json_bytes(experiment)
    review = {
        "schema_version": "ak_probability_qualification_review_v2.1.1",
        "scope": "OFFLINE_STATISTICS", "verdict": "PASS", "independent": True,
        "reviewer_id": "reviewer-1", "report_sha256": a.sha256_hex(metrics_bytes),
        "contract_lock_sha256": "4" * 64, "registry_sha256": "5" * 64,
        "experiment_sha256": a.sha256_hex(experiment_bytes), "model_hashes": model_hashes,
        "research_policy_version": "research-policy-v2.1.0",
        "macro_policy_version": "macro-risk-v2.1.0",
        "cost_scenarios": ["BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5", "GAP", "NO_STOP"],
        "source_hashes": {"code": "f" * 64},
        "exit_registry_sha256": EXIT_REGISTRY,
        "exit_binding_sha256s": [_chain_exit_binding().binding_sha256],
        "required_group_keys": [*SIX_KEYS, _other_scope_key()],
        "checks": {key: "PASS" for key in (
            "scope_registered", "groups_complete", "metrics_recomputed",
            "thresholds_unchanged", "holdout_once", "causality_passed",
            "source_hashes_match")},
        "reasons": [],
    }
    review_bytes = s.canonical_json_bytes(review)
    evidence = {
        a.sha256_hex(metrics_bytes): metrics_bytes,
        a.sha256_hex(review_bytes): review_bytes,
        a.sha256_hex(experiment_bytes): experiment_bytes,
    }
    qualification = dataclasses.replace(
        _qualification(model_hashes),
        report_sha256=a.sha256_hex(metrics_bytes),
        review_sha256=a.sha256_hex(review_bytes),
        experiment_sha256=a.sha256_hex(experiment_bytes))
    return evidence, qualification, metrics, review, experiment


def test_verify_qualification_evidence_chain() -> None:
    model_hashes = {"forecast": "1" * 64, "payoff": "2" * 64, "regime_reference": "8" * 64}
    evidence, qualification, metrics, review, experiment = _evidence_chain(model_hashes)
    spec = _strategy_spec()
    a.verify_qualification_evidence(qualification, spec, evidence, "4" * 64, "5" * 64,
                                     **_exit_kwargs())

    def expect_error(mutate) -> None:
        mutated_evidence, mutated_qualification = mutate()
        with pytest.raises(a.ArtifactsError):
            a.verify_qualification_evidence(mutated_qualification, spec, mutated_evidence,
                                            "4" * 64, "5" * 64, **_exit_kwargs())

    def mutate_review(field, value):
        def apply():
            ev, qual, met, rev, exp = _evidence_chain(model_hashes)
            rev = dict(rev)
            rev[field] = value
            rev_bytes = s.canonical_json_bytes(rev)
            evidence2 = dict(ev)
            evidence2[a.sha256_hex(rev_bytes)] = rev_bytes
            qual2 = dataclasses.replace(qual, review_sha256=a.sha256_hex(rev_bytes))
            return evidence2, qual2
        return apply

    expect_error(mutate_review("verdict", "FAIL"))
    expect_error(mutate_review("independent", False))
    expect_error(mutate_review("checks", {**review["checks"], "holdout_once": "FAIL"}))
    expect_error(mutate_review("scope", "FULL"))
    expect_error(mutate_review("contract_lock_sha256", "0" * 64))
    expect_error(mutate_review("registry_sha256", "0" * 64))
    expect_error(mutate_review("experiment_sha256", "0" * 64))
    expect_error(mutate_review("model_hashes", {**model_hashes, "payoff": "0" * 64}))

    # Dropping a pre-registered group from metrics (or review/experiment) fails.
    def mutate_metrics(field, value):
        def apply():
            ev, qual, met, rev, exp = _evidence_chain(model_hashes)
            met = json.loads(json.dumps(met))
            met[field] = value
            met_bytes = s.canonical_json_bytes(met)
            evidence2 = dict(ev)
            evidence2[a.sha256_hex(met_bytes)] = met_bytes
            qual2 = dataclasses.replace(qual, report_sha256=a.sha256_hex(met_bytes))
            review2 = dict(rev)
            review2["report_sha256"] = a.sha256_hex(met_bytes)
            review2_bytes = s.canonical_json_bytes(review2)
            evidence2[a.sha256_hex(review2_bytes)] = review2_bytes
            qual2 = dataclasses.replace(qual2, review_sha256=a.sha256_hex(review2_bytes))
            return evidence2, qual2
        return apply

    expect_error(mutate_metrics("required_group_keys", []))
    expect_error(mutate_metrics("strategy_groups", []))
    expect_error(mutate_metrics("model_bundles", {}))
    expect_error(mutate_metrics("cost_scenarios", ["BASE"]))

    # A scope group that exists but did not pass cannot authorize; another
    # scope's PASS rows never substitute (exact-key matching).
    def apply_failing():
        ev, qual, met, rev, exp = _evidence_chain(model_hashes)
        met = json.loads(json.dumps(met))
        met["strategy_groups"][0]["status"] = "FAIL"
        met_bytes = s.canonical_json_bytes(met)
        review2 = dict(rev)
        review2["report_sha256"] = a.sha256_hex(met_bytes)
        review2_bytes = s.canonical_json_bytes(review2)
        return ({**ev, a.sha256_hex(met_bytes): met_bytes,
                 a.sha256_hex(review2_bytes): review2_bytes},
                dataclasses.replace(
                    qual, report_sha256=a.sha256_hex(met_bytes),
                    review_sha256=a.sha256_hex(review2_bytes)))
    expect_error(apply_failing)

    def rebuild_with_metrics(ev, qual, rev, met):
        met_bytes = s.canonical_json_bytes(met)
        review2 = dict(rev)
        review2["report_sha256"] = a.sha256_hex(met_bytes)
        review2_bytes = s.canonical_json_bytes(review2)
        evidence2 = {**ev, a.sha256_hex(met_bytes): met_bytes,
                     a.sha256_hex(review2_bytes): review2_bytes}
        return evidence2, dataclasses.replace(
            qual, report_sha256=a.sha256_hex(met_bytes),
            review_sha256=a.sha256_hex(review2_bytes))

    # 变更申请-004: replay_summaries items carry the exact fixed projection.
    valid_summary = {
        "account_id": "acc-1", "initial_equity_cny": "1000000",
        "terminal_equity_cny": "1000060", "closed_episode_net_pnl_cny": "60",
        "open_episode_realized_net_pnl_cny": None, "terminal_unrealized_cny": None,
        "open_episode_ids": [], "pending_intent_ids": [],
        "reconciliation_status": "PASS", "status": "COMPLETE",
        "reasons": [], "split_id": "split-1",
        "run_policy_sha256": "3" * 64, "model_bundle_sha256": a.sha256_hex(
            s.canonical_json_bytes(model_hashes)),
    }
    incomplete_summary = {
        **valid_summary, "status": "INCOMPLETE",
        "reconciliation_status": "INCOMPLETE",
        "terminal_equity_cny": None, "closed_episode_net_pnl_cny": None,
        "open_episode_ids": ["ep-open"], "reasons": ["open_position"],
    }

    def apply_summary(summary):
        def apply():
            ev, qual, met, rev, exp = _evidence_chain(model_hashes)
            met = json.loads(json.dumps(met))
            met["replay_summaries"] = [summary]
            return rebuild_with_metrics(ev, qual, rev, met)
        return apply

    # A well-formed COMPLETE summary keeps the chain verifiable; a matching
    # INCOMPLETE replay cannot back a PASS qualification (F04).
    chain = apply_summary(valid_summary)()
    a.verify_qualification_evidence(chain[1], spec, chain[0], "4" * 64, "5" * 64,
                                     **_exit_kwargs())
    expect_error(apply_summary(incomplete_summary))
    for mutation in ({**valid_summary, "status": "PASS"},
                     {**valid_summary, "extra_field": 1},
                     {key: value for key, value in valid_summary.items()
                      if key != "split_id"},
                     {**valid_summary, "initial_equity_cny": 1000000}):
        expect_error(apply_summary(mutation))
    expect_error(mutate_metrics("replay_summaries", {}))
    expect_error(mutate_metrics("replay_summaries", None))
    # Cross-scope theft: our BASE row disappears; the other scope's PASS row
    # must not satisfy it.
    def apply_theft():
        ev, qual, met, rev, exp = _evidence_chain(model_hashes)
        met = json.loads(json.dumps(met))
        met["strategy_groups"] = [group for group in met["strategy_groups"]
                                  if group["group_key"] != SIX_KEYS[0]]
        return rebuild_with_metrics(ev, qual, rev, met)

    expect_error(apply_theft)

    # Missing scenario: the GAP row is absent while its key stays registered.
    def apply_missing_scenario():
        ev, qual, met, rev, exp = _evidence_chain(model_hashes)
        met = json.loads(json.dumps(met))
        met["strategy_groups"] = [group for group in met["strategy_groups"]
                                  if not (group["strategy_id"] == "strat-1"
                                          and group["cost_scenario"] == "GAP")]
        return rebuild_with_metrics(ev, qual, rev, met)

    expect_error(apply_missing_scenario)

    # A horizon never registered for this scope authorizes nothing.
    def apply_unregistered():
        ev, qual, _, _, _ = _evidence_chain(model_hashes)
        return ev, dataclasses.replace(
            qual, horizons=(c.Horizon(unit="TRADING_DAY", value=5),))

    unregistered = apply_unregistered()
    with pytest.raises(a.ArtifactsError):
        a.verify_qualification_evidence(unregistered[1], _strategy_spec(),
                                        unregistered[0], "4" * 64, "5" * 64,
                                        **_exit_kwargs())

    # The experiment must carry the same pre-registered set and source identity.
    def apply_experiment(field, value):
        def apply():
            ev, qual, met, rev, exp = _evidence_chain(model_hashes)
            exp = json.loads(json.dumps(exp))
            exp["spec"][field] = value
            exp_bytes = s.canonical_json_bytes(exp)
            evidence2 = dict(ev)
            evidence2[a.sha256_hex(exp_bytes)] = exp_bytes
            qual2 = dataclasses.replace(qual, experiment_sha256=a.sha256_hex(exp_bytes))
            return evidence2, qual2
        return apply

    expect_error(apply_experiment("required_group_keys", []))
    expect_error(apply_experiment("source_sha256", "0" * 64))
    expect_error(apply_experiment("research_policy_version", "other"))

    # Missing referenced files and spec mismatches are rejected.
    def apply_missing():
        ev, qual, _, _, _ = _evidence_chain(model_hashes)
        return {key: value for key, value in ev.items()
                if key != qual.experiment_sha256}, qual
    expect_error(apply_missing)

    ev, qual, _, _, _ = _evidence_chain(model_hashes)
    mismatched = dataclasses.replace(_strategy_spec(), code_sha256="e" * 64)
    with pytest.raises(a.ArtifactsError):
        a.verify_qualification_evidence(qual, mismatched, ev, "4" * 64, "5" * 64,
                                     **_exit_kwargs())
    uncovered = dataclasses.replace(
        qual, horizons=(c.Horizon(unit="TRADING_DAY", value=5),))
    with pytest.raises(a.ArtifactsError):
        a.verify_qualification_evidence(uncovered, _strategy_spec(), ev, "4" * 64, "5" * 64,
                                     **_exit_kwargs())
    with pytest.raises(a.ArtifactsError):
        tampered = {key: value + b" " for key, value in ev.items()}
        a.verify_qualification_evidence(qual, _strategy_spec(), tampered, "4" * 64, "5" * 64,
                                     **_exit_kwargs())
