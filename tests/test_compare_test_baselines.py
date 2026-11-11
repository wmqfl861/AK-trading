"""Unit tests for scripts/compare_test_baselines.py (K03).

All capture records used here are SYNTHETIC parser fixtures: they exercise the
descriptor validator, the offline-launcher stdout parser and the baseline
comparator. They are never historical evidence. The two launcher output
layouts (live forwarding and post-hoc dump) are both covered because the real
retained captures use different launcher generations.

The tests import ak_trading only to assert that the product code under test
runs from this tree's src directory (the offline launcher pins PYTHONPATH to
<root>/src ahead of any installed distribution).
"""
from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "compare_test_baselines.py"

sys.path.insert(0, str(ROOT / "scripts"))
import compare_test_baselines as ctb  # noqa: E402


STATS_PREFIX = ctb.STATS_PREFIX


# ---------------------------------------------------------------------------
# Synthetic capture fixture builders
# ---------------------------------------------------------------------------

def _stats(nodeids, *, failed=(), errors=(), collection_errors=(), collected_files=None,
           deselected=0, skipped=0, passed=None):
    selected = list(nodeids)
    failed = list(failed)
    files = collected_files if collected_files is not None else sorted({n.split("::")[0] for n in selected})
    setup_errors = {e[: -len(" (setup)")] for e in errors if e.endswith("(setup)")}
    ran = len(selected)
    if passed is None:
        passed = ran - len(failed) - skipped - len(setup_errors)
    return {
        "selected": len(selected),
        "deselected": deselected,
        "collected": len(selected) + deselected,
        "ran": ran,
        "passed": passed,
        "failed": len(failed),
        "skipped": skipped,
        "errors": len(errors),
        "selected_nodeids": selected,
        "failed_nodeids": failed,
        "error_nodeids": list(errors),
        "collection_errors": list(collection_errors),
        "collected_files": files,
    }


def _live_layout(domains, *, command="python scripts/test_offline.py (2 pytest arguments forwarded; guard before collection)"):
    """Current launcher: live forwarding between running/exit markers."""
    lines = [f"Offline command: {command}"]
    lines.append(f"Offline plan: {len(domains)} domain(s): " + ", ".join(d["name"] for d in domains))
    lines.append(r"Offline evidence: C:\fake\evidence")
    for domain in domains:
        lines.append(f"--- offline domain {domain['name']}: running ---")
        lines.append("....                                    [100%]")
        lines.append(f"Offline selection: {domain['stats']['selected']} selected, 0 deselected (mandatory exclusions cannot be overridden)")
        lines.append(STATS_PREFIX + json.dumps(domain["stats"]))
        lines.append(f"=== offline domain {domain['name']}: exit {domain['exit']} ===")
    lines.extend(_summary_block(domains))
    return "\n".join(lines) + "\n"


def _dump_layout(domains):
    """Historical launcher: exit marker first, accumulated child output after."""
    lines = ["Offline command: python scripts/test_offline.py (2 pytest arguments forwarded; guard before collection)"]
    lines.append(f"Offline plan: {len(domains)} domain(s): " + ", ".join(d["name"] for d in domains))
    for domain in domains:
        lines.append(f"=== offline domain {domain['name']}: exit {domain['exit']} ===")
        lines.append("....                                    [100%]")
        lines.append(STATS_PREFIX + json.dumps(domain["stats"]))
    lines.extend(_summary_block(domains))
    return "\n".join(lines) + "\n"


def _summary_block(domains):
    lines = [
        f"Offline summary: {len(domains)} domain(s) planned, {len(domains)} launched, "
        "0 skipped-empty, 0 unrun, 0 launcher-failed"
    ]
    selected_total = len({n for d in domains for n in d["stats"]["selected_nodeids"]})
    deselected = sum(d["stats"]["deselected"] for d in domains)
    ran = sum(d["stats"]["ran"] for d in domains)
    passed = sum(d["stats"]["passed"] for d in domains)
    failed = len({n for d in domains for n in d["stats"]["failed_nodeids"]})
    skipped = sum(d["stats"]["skipped"] for d in domains)
    errors = len({n for d in domains for n in d["stats"]["error_nodeids"]})
    coll = len({e for d in domains for e in d["stats"]["collection_errors"] if e.endswith(".py")})
    for domain in domains:
        s = domain["stats"]
        lines.append(
            f"Offline domain {domain['name']}: exit {domain['exit']}, selected {s['selected']}, "
            f"deselected {s['deselected']}, ran {s['ran']}, passed {s['passed']}, "
            f"failed {s['failed']}, skipped {s['skipped']}, errors {s['errors']}, "
            f"collection_errors {len(s['collection_errors'])}"
        )
    error_files = sorted({e for d in domains for e in d["stats"]["collection_errors"] if e.endswith(".py")})
    if error_files:
        lines.append("Offline collection error files: " + ", ".join(error_files))
    lines.append(
        f"Offline totals: selected {selected_total} (+{deselected} deselected), ran {ran}, "
        f"passed {passed}, failed {failed}, skipped {skipped}, errors {errors}, "
        f"collection_error_files {coll}, unresolved_files 0"
    )
    lines.append(f"Offline overall exit: {ctb.aggregate_domain_exits([d['exit'] for d in domains])}")
    return lines


def _write_gzip(gz_path: Path, data: bytes) -> None:
    with gzip.open(gz_path, "xb") as compressed:
        compressed.write(data)


def _ref(path: Path) -> dict:
    return {"path": str(path).replace("\\", "/"), "sha256": ctb.sha256_file(path)}


def _make_capture(directory: Path, capture_id: str, *, stdout_text: str, child_exit: int,
                  stderr_text: str = "warning: Making stdin inheritable failed\n",
                  python: str = "C:/synthetic/venv/Scripts/python.exe",
                  omit_result: bool = False, termination=None, supervisor_error=None,
                  bool_child_exit: bool = False):
    directory.mkdir(parents=True, exist_ok=False)
    argv = [python, "-B", "-X", "utf8", "scripts/test_offline.py", "-q", "tests/test_synthetic.py"]
    request = {
        "started": "2026-11-10T00:00:00+00:00",
        "argv": argv,
        "cwd": str(directory.parent / "tree").replace("\\", "/"),
        "wall_seconds": 7200,
        "memory_available": 32_000_000_000,
        "C_free": 50_000_000_000,
        "D_free": 10_000_000_000,
    }
    (directory / "request.json").write_text(json.dumps(request, indent=2), encoding="utf-8", newline="\n")
    result = {
        "job_readback": {"flags": 8704, "job_memory_limit": 8589934592},
        "limit": 8589934592, "deadline_s": 7200, "termination": termination,
        "child_exit": (True if bool_child_exit else child_exit),
        "supervisor_error": supervisor_error, "child_pid": 4242,
        "job_pids_after_wait": 0, "job_pids_final": 0, "job_pids_after_terminate": 0,
        "wall_s": 6.0, "sampled_peak_tree_rss_bytes": 20_000_000,
    }
    if not omit_result:
        (directory / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8", newline="\n")
    stdout_bytes = stdout_text.encode("utf-8")
    (directory / "stdout.txt").write_bytes(stdout_bytes)
    _write_gzip(directory / "stdout.txt.gz", stdout_bytes)
    stderr_bytes = stderr_text.encode("utf-8")
    (directory / "stderr.txt").write_bytes(stderr_bytes)
    _write_gzip(directory / "stderr.txt.gz", stderr_bytes)
    return request


def _make_environment(path: Path, python_executable: str = "C:/synthetic/venv/Scripts/python.exe"):
    payload = {
        "python": "3.13.15 (synthetic)",
        "executable": python_executable,
        "packages": [["pytest", "9.2.0"], ["pluggy", "1.7.0"]],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8", newline="\n")
    return payload


def _make_harness(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    refs = {}
    for name, payload in (
        ("launcher", b"launcher-bytes"),
        ("offline_guard", b"guard-bytes"),
        ("measure", b"measure-bytes"),
        ("job_guard", b"job-guard-bytes"),
    ):
        target = directory / f"{name}.py"
        target.write_bytes(payload)
        refs[name] = _ref(target)
    return refs


def _make_import_evidence(path: Path, tree: Path, revision: str, launcher_sha: str,
                          established_by="retained-launch-records"):
    payload = {
        "schema": "import-evidence-v1",
        "tree": str(tree).replace("\\", "/"),
        "revision": revision,
        "launcher_sha256": launcher_sha,
        "established_by": established_by,
        "recorded_at": "2026-11-10T00:00:00+00:00",
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8", newline="\n")
    return payload


def _make_descriptor(base: Path, capture_id: str, *, kind: str, nodeids, failed=(), errors=(),
                     collection_errors=(), expected_files=None, expected_domains=("normal",),
                     revision="synthetic-rev-1", child_exit=0, stdout_text=None, layout="live",
                     env_file: Path | None = None, harness_dir: Path | None = None,
                     omit_result=False, termination=None, supervisor_error=None,
                     bool_child_exit=False, python="C:/synthetic/venv/Scripts/python.exe",
                     snapshot_bytes=b"snapshot-bytes-1", skipped=0):
    tree = base / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    capture_dir = base / capture_id
    stats = _stats(nodeids, failed=failed, errors=errors, collection_errors=collection_errors, skipped=skipped)
    exit_code = 1 if (failed or errors or collection_errors) else (0 if nodeids else 5)
    if stdout_text is None:
        domain = {"name": "normal", "exit": exit_code, "stats": stats}
        stdout_text = (_live_layout if layout == "live" else _dump_layout)([domain])
    _make_capture(
        capture_dir, capture_id, stdout_text=stdout_text, child_exit=child_exit,
        omit_result=omit_result, termination=termination, supervisor_error=supervisor_error,
        bool_child_exit=bool_child_exit, python=python,
    )
    env_path = env_file or (base / "env.json")
    if not env_path.exists():
        _make_environment(env_path, python)
    harness_dir = harness_dir or (base / "harness")
    harness = _make_harness(harness_dir)
    snapshot = base / f"snapshot-{revision}.zip"
    if not snapshot.exists():
        snapshot.write_bytes(snapshot_bytes)
    evidence_path = base / f"import-evidence-{capture_id}.json"
    _make_import_evidence(evidence_path, tree, revision, harness["launcher"]["sha256"])
    files = expected_files if expected_files is not None else sorted({n.split("::")[0] for n in nodeids})
    return {
        "schema_version": "offline-capture-set-v1",
        "scope": "historical-forward" if kind == "historical-forward" else "full-suite-windows",
        "members": [{
            "kind": kind,
            "capture_id": capture_id,
            "request": _ref(capture_dir / "request.json"),
            "result": {"path": str(capture_dir / "result.json").replace("\\", "/"), "sha256": "0" * 64}
            if omit_result else _ref(capture_dir / "result.json"),
            "stdout": {"raw": _ref(capture_dir / "stdout.txt"), "gzip": _ref(capture_dir / "stdout.txt.gz")},
            "stderr": {"raw": _ref(capture_dir / "stderr.txt"), "gzip": _ref(capture_dir / "stderr.txt.gz")},
            "source": {
                "root": str(tree).replace("\\", "/"),
                "revision": revision,
                "snapshot": _ref(snapshot),
                "import_evidence": _ref(evidence_path),
            },
            "environment": _ref(env_path),
            "harness": harness,
            "selection": {"expected_files": files, "expected_domains": list(expected_domains)},
        }],
    }


NODE_A = "tests/test_synthetic.py::test_alpha"
NODE_B = "tests/test_synthetic.py::test_beta"
NODE_C = "tests/test_synthetic.py::test_gamma"
NODE_BROKEN = "tests/test_broken.py::test_fixed"


def _record(base: Path, name: str, *, kind="historical-forward", **kwargs) -> Path:
    descriptor_path = base / f"{name}-descriptor.json"
    descriptor_path.write_text(
        json.dumps(_make_descriptor(base, name, kind=kind, **kwargs), indent=2),
        encoding="utf-8", newline="\n",
    )
    output = base / f"{name}-baseline.json"
    code = ctb.main(["record", "--capture", str(descriptor_path), "--output", str(output)])
    assert code == 0, f"record failed: {code}"
    return output


def _invalid_record(base: Path, name: str, *, kind="historical-forward", **kwargs):
    descriptor_path = base / f"{name}-descriptor.json"
    descriptor_path.write_text(
        json.dumps(_make_descriptor(base, name, kind=kind, **kwargs), indent=2),
        encoding="utf-8", newline="\n",
    )
    output = base / f"{name}-baseline.json"
    return ctb.main(["record", "--capture", str(descriptor_path), "--output", str(output)])


# ---------------------------------------------------------------------------
# Descriptor / JSON strictness
# ---------------------------------------------------------------------------

def test_product_import_origin_is_this_tree() -> None:
    import ak_trading

    origin = Path(ak_trading.__file__).resolve()
    assert origin.is_relative_to((ROOT / "src").resolve()), origin


def test_record_success_and_canonical_roundtrip(tmp_path: Path) -> None:
    baseline = _record(tmp_path, "synthetic01", nodeids=[NODE_A, NODE_B], revision="rev-base")
    text = baseline.read_text(encoding="utf-8")
    payload = ctb.loads_strict(text, "baseline")
    assert payload["schema"] == "test-baseline-v1"
    assert payload["scope"] == "historical-forward"
    assert payload["population"]["selected_total"] == 2
    member = payload["members"][0]
    assert member["kind"] == "historical-forward"
    assert member["capture_id"] == "synthetic01"
    assert member["parent"]["overall_exit"] == 0
    assert member["parent"]["output_layout"] == "live-forwarding"
    assert member["domains"][0]["stats"]["selected_nodeids"] == [NODE_A, NODE_B]
    # Canonical JSON: dumping the parsed payload reproduces the file bytes.
    assert json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n" == text


def test_record_accepts_post_hoc_dump_layout(tmp_path: Path) -> None:
    baseline = _record(tmp_path, "dump01", nodeids=[NODE_A], layout="dump")
    payload = ctb.loads_strict(baseline.read_text(encoding="utf-8"), "baseline")
    assert payload["members"][0]["parent"]["output_layout"] == "post-hoc-dump"


def test_record_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    descriptor = tmp_path / "dup.json"
    descriptor.write_text('{"schema_version": "offline-capture-set-v1", "schema_version": "offline-capture-set-v1", "scope": "historical-forward", "members": []}', encoding="utf-8")
    assert ctb.main(["record", "--capture", str(descriptor), "--output", str(tmp_path / "b.json")]) == 2


def test_record_rejects_truncated_json(tmp_path: Path) -> None:
    descriptor = tmp_path / "truncated.json"
    descriptor.write_text('{"schema_version": "offline-capture-set-v1", "scope":', encoding="utf-8")
    assert ctb.main(["record", "--capture", str(descriptor), "--output", str(tmp_path / "b.json")]) == 2


def test_record_rejects_malformed_stats_line(tmp_path: Path) -> None:
    stats = _stats([NODE_A])
    text = _live_layout([{"name": "normal", "exit": 0, "stats": stats}])
    text = text.replace(STATS_PREFIX + json.dumps(stats), STATS_PREFIX + "{not json")
    baseline = _invalid_record(tmp_path, "badstats01", nodeids=[NODE_A], stdout_text=text, child_exit=0)
    assert baseline == 2


def test_record_rejects_duplicate_stats_lines(tmp_path: Path) -> None:
    stats = _stats([NODE_A])
    block = STATS_PREFIX + json.dumps(stats)
    text = _live_layout([{"name": "normal", "exit": 0, "stats": stats}]).replace(block, block + "\n" + block)
    assert _invalid_record(tmp_path, "dupstats01", nodeids=[NODE_A], stdout_text=text, child_exit=0) == 2


def test_record_rejects_truncated_stream(tmp_path: Path) -> None:
    stats = _stats([NODE_A])
    text = _live_layout([{"name": "normal", "exit": 0, "stats": stats}])
    truncated = text[: text.index("=== offline domain normal: exit 0 ===")]
    assert _invalid_record(tmp_path, "truncstream01", nodeids=[NODE_A], stdout_text=truncated, child_exit=0) == 2


# ---------------------------------------------------------------------------
# Terminal / guard reconciliation
# ---------------------------------------------------------------------------

def test_record_rejects_missing_result_file(tmp_path: Path) -> None:
    assert _invalid_record(tmp_path, "pending01", nodeids=[NODE_A], omit_result=True) == 2


def test_record_rejects_timeout_termination(tmp_path: Path) -> None:
    assert _invalid_record(
        tmp_path, "timeout01", nodeids=[NODE_A],
        termination={"reason": "deadline", "deadline_s": 7200},
    ) == 2


def test_record_rejects_supervisor_error(tmp_path: Path) -> None:
    assert _invalid_record(
        tmp_path, "supervisor01", nodeids=[NODE_A], supervisor_error="job object failure",
    ) == 2


def test_record_rejects_boolean_child_exit(tmp_path: Path) -> None:
    assert _invalid_record(tmp_path, "boolexit01", nodeids=[NODE_A], bool_child_exit=True) == 2


def test_record_rejects_child_exit_overall_mismatch(tmp_path: Path) -> None:
    # Launcher says exit 1 (one failure) but the guard recorded child_exit 0.
    assert _invalid_record(
        tmp_path, "exitmismatch01", nodeids=[NODE_A, NODE_B], failed=[NODE_B], child_exit=0
    ) == 2


def test_record_preserves_native_exit_one(tmp_path: Path) -> None:
    baseline = _record(tmp_path, "native01", nodeids=[NODE_A, NODE_B], failed=[NODE_B], child_exit=1)
    payload = ctb.loads_strict(baseline.read_text(encoding="utf-8"), "baseline")
    member = payload["members"][0]
    assert member["parent"]["overall_exit"] == 1
    assert member["parent"]["guard"]["child_exit"] == 1
    assert member["domains"][0]["exit_code"] == 1


# ---------------------------------------------------------------------------
# Completeness and launch gaps
# ---------------------------------------------------------------------------

def _summary_with(domains, *, unrun=0, empty=0, launcher_failed=0, unresolved=0, interrupted=False):
    lines = [
        f"Offline summary: {len(domains) + unrun + empty + launcher_failed} domain(s) planned, "
        f"{len(domains)} launched, {empty} skipped-empty, {unrun} unrun, {launcher_failed} launcher-failed"
    ]
    for domain in domains:
        s = domain["stats"]
        lines.append(
            f"Offline domain {domain['name']}: exit {domain['exit']}, selected {s['selected']}, "
            f"deselected {s['deselected']}, ran {s['ran']}, passed {s['passed']}, "
            f"failed {s['failed']}, skipped {s['skipped']}, errors {s['errors']}, "
            f"collection_errors {len(s['collection_errors'])}"
        )
    lines.append(
        f"Offline totals: selected {len({n for d in domains for n in d['stats']['selected_nodeids']})} "
        f"(+0 deselected), ran {sum(d['stats']['ran'] for d in domains)}, "
        f"passed {sum(d['stats']['passed'] for d in domains)}, "
        f"failed {len({n for d in domains for n in d['stats']['failed_nodeids']})}, "
        f"skipped {sum(d['stats']['skipped'] for d in domains)}, "
        f"errors {len({n for d in domains for n in d['stats']['error_nodeids']})}, "
        f"collection_error_files 0, unresolved_files {unresolved}"
    )
    if unresolved:
        lines.append("Offline unresolved files: tests/test_synthetic.py")
    if interrupted:
        lines.append("Offline interrupted; domains not run: pending")
    lines.append(f"Offline overall exit: {ctb.aggregate_domain_exits([d['exit'] for d in domains])}")
    return lines


def test_record_rejects_unrun_domain(tmp_path: Path) -> None:
    domain = {"name": "normal", "exit": 0, "stats": _stats([NODE_A])}
    lines = ["Offline command: x", "Offline plan: 2 domain(s): normal, pending",
             "Offline evidence: C:/e", "--- offline domain normal: running ---",
             STATS_PREFIX + json.dumps(domain["stats"]),
             "=== offline domain normal: exit 0 ==="]
    lines.extend(_summary_with([domain], unrun=1))
    text = "\n".join(lines) + "\n"
    assert _invalid_record(tmp_path, "unrun01", nodeids=[NODE_A], stdout_text=text, child_exit=0) == 2


def test_record_rejects_interrupted_run(tmp_path: Path) -> None:
    domain = {"name": "normal", "exit": 0, "stats": _stats([NODE_A])}
    lines = ["Offline command: x", "Offline plan: 2 domain(s): normal, pending",
             "--- offline domain normal: running ---",
             STATS_PREFIX + json.dumps(domain["stats"]),
             "=== offline domain normal: exit 0 ==="]
    lines.extend(_summary_with([domain], unrun=1, interrupted=True))
    lines[-1] = "Offline overall exit: 130"
    text = "\n".join(lines) + "\n"
    assert _invalid_record(tmp_path, "interrupted01", nodeids=[NODE_A], stdout_text=text, child_exit=130) == 2


def test_record_rejects_unresolved_files(tmp_path: Path) -> None:
    domain = {"name": "normal", "exit": 0, "stats": _stats([NODE_A])}
    lines = ["Offline command: x", "Offline plan: 1 domain(s): normal",
             "--- offline domain normal: running ---",
             STATS_PREFIX + json.dumps(domain["stats"]),
             "=== offline domain normal: exit 0 ==="]
    lines.extend(_summary_with([domain], unresolved=1))
    text = "\n".join(lines) + "\n"
    assert _invalid_record(tmp_path, "unresolved01", nodeids=[NODE_A], stdout_text=text, child_exit=0) == 2


def test_record_rejects_missing_domain(tmp_path: Path) -> None:
    assert _invalid_record(
        tmp_path, "missingdomain01", nodeids=[NODE_A], expected_domains=("normal", "datapipeline-x"),
    ) == 2


def test_record_allows_and_records_collection_errors(tmp_path: Path) -> None:
    nodeids = [NODE_A, NODE_B]
    baseline = _record(
        tmp_path, "coll01", nodeids=nodeids, failed=[NODE_B],
        collection_errors=["tests/test_broken.py"],
        expected_files=["tests/test_broken.py", "tests/test_synthetic.py"],
        child_exit=1,
    )
    payload = ctb.loads_strict(baseline.read_text(encoding="utf-8"), "baseline")
    stats = payload["members"][0]["domains"][0]["stats"]
    assert stats["collection_errors"] == ["tests/test_broken.py"]
    assert "tests/test_broken.py" in payload["members"][0]["resolved_files"]


def test_teardown_error_coexists_with_passed_call(tmp_path: Path) -> None:
    # A passed call phase and a teardown error on the same nodeid must both
    # count: the validator may not impose a mutually exclusive equation.
    ctb.validate_stats({
        "selected": 1, "deselected": 0, "collected": 1, "ran": 1, "passed": 1,
        "failed": 0, "skipped": 0, "errors": 1,
        "selected_nodeids": [NODE_A],
        "failed_nodeids": [],
        "error_nodeids": [f"{NODE_A} (teardown)"],
        "collection_errors": [],
        "collected_files": ["tests/test_synthetic.py"],
    }, "unit")
    baseline = _record(
        tmp_path, "teardown01", nodeids=[NODE_A], errors=[f"{NODE_A} (teardown)"], child_exit=1,
    )
    payload = ctb.loads_strict(baseline.read_text(encoding="utf-8"), "baseline")
    stats = payload["members"][0]["domains"][0]["stats"]
    assert stats["passed"] == 1 and stats["errors"] == 1


def test_record_rejects_stats_count_mismatch(tmp_path: Path) -> None:
    stats = _stats([NODE_A, NODE_B], failed=[NODE_B])
    stats["failed"] = 2  # inconsistent with failed_nodeids
    text = _live_layout([{"name": "normal", "exit": 1, "stats": stats}])
    assert _invalid_record(
        tmp_path, "countmismatch01", nodeids=[NODE_A, NODE_B], stdout_text=text, child_exit=1,
        expected_files=["tests/test_synthetic.py"],
    ) == 2


def test_record_rejects_invalid_count_equation(tmp_path: Path) -> None:
    # A mutually exclusive equation would reject passed-call + teardown-error;
    # instead assert that dropping the call outcome entirely is rejected.
    stats = _stats([NODE_A])
    stats["passed"] = stats["passed"] - 1  # ran 1, but no call outcome and no setup error
    text = _live_layout([{"name": "normal", "exit": 0, "stats": stats}])
    assert _invalid_record(
        tmp_path, "equation01", nodeids=[NODE_A], stdout_text=text, child_exit=0,
    ) == 2


def test_record_rejects_env_argv_interpreter_mismatch(tmp_path: Path) -> None:
    _make_environment(tmp_path / "env.json")  # default synthetic interpreter
    assert _invalid_record(
        tmp_path, "envargv01", nodeids=[NODE_A],
        python="C:/other/venv/Scripts/python.exe",
    ) == 2


def test_record_rejects_artifact_hash_mismatch(tmp_path: Path) -> None:
    descriptor = _make_descriptor(tmp_path, "hash01", kind="historical-forward", nodeids=[NODE_A])
    descriptor["members"][0]["request"]["sha256"] = "0" * 64
    path = tmp_path / "hash01-descriptor.json"
    path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8", newline="\n")
    assert ctb.main(["record", "--capture", str(path), "--output", str(tmp_path / "h.json")]) == 2


def test_gzip_only_stream_with_retained_content_hash(tmp_path: Path) -> None:
    descriptor = _make_descriptor(tmp_path, "gzonly01", kind="historical-forward", nodeids=[NODE_A])
    stdout_ref = descriptor["members"][0]["stdout"]
    raw_path = Path(stdout_ref["raw"]["path"])
    content = raw_path.read_bytes()
    stdout_ref["raw"] = None
    stdout_ref["raw_content_sha256"] = ctb.sha256_bytes(content)
    raw_path.unlink()
    path = tmp_path / "gzonly01-descriptor.json"
    path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8", newline="\n")
    out = tmp_path / "gzonly01-baseline.json"
    assert ctb.main(["record", "--capture", str(path), "--output", str(out)]) == 0


def test_gzip_only_stream_rejects_wrong_content_hash(tmp_path: Path) -> None:
    descriptor = _make_descriptor(tmp_path, "gzbad01", kind="historical-forward", nodeids=[NODE_A])
    stdout_ref = descriptor["members"][0]["stdout"]
    Path(stdout_ref["raw"]["path"]).unlink()
    stdout_ref["raw"] = None
    stdout_ref["raw_content_sha256"] = "a" * 64
    path = tmp_path / "gzbad01-descriptor.json"
    path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8", newline="\n")
    assert ctb.main(["record", "--capture", str(path), "--output", str(tmp_path / "g.json")]) == 2


def test_record_rejects_gzip_raw_inequality(tmp_path: Path) -> None:
    descriptor = _make_descriptor(tmp_path, "gzne01", kind="historical-forward", nodeids=[NODE_A])
    stdout_ref = descriptor["members"][0]["stdout"]
    corrupted = b"different bytes\n"
    with (tmp_path / "corrupt.gz").open("xb") as raw_handle:
        with gzip.open(raw_handle, "wb") as compressed:
            compressed.write(corrupted)
    stdout_ref["gzip"] = _ref(tmp_path / "corrupt.gz")
    path = tmp_path / "gzne01-descriptor.json"
    path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8", newline="\n")
    assert ctb.main(["record", "--capture", str(path), "--output", str(tmp_path / "g.json")]) == 2


# ---------------------------------------------------------------------------
# Aggregate-level rules
# ---------------------------------------------------------------------------

FULL_KIND_POPULATIONS = {
    "full_suite": ["tests/test_suite.py::test_one", "tests/test_suite.py::test_two"],
    "duplicate_probe": ["docs/probes/test_dup.py::test_dup[0]"],
    "confirmation_probe": ["docs/probes/test_conf.py::test_conf[0]"],
    "range_probe": ["docs/probes/test_range.py::test_range[0]"],
}


def _full_descriptor(base: Path, name: str, *, failing=None, revision="full-rev-1", env_file=None):
    failing = failing or {}
    members = []
    harness_dir = base / f"{name}-harness"
    harness = _make_harness(harness_dir)
    tree = base / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    env_path = env_file or (base / f"{name}-env.json")
    if not env_path.exists():
        _make_environment(env_path)
    snapshot = base / f"{name}-snapshot.zip"
    snapshot.write_bytes(f"{name}-{revision}".encode("ascii"))
    for index, (kind, nodeids) in enumerate(FULL_KIND_POPULATIONS.items()):
        capture_id = f"{name}-{kind}"
        failed = sorted(failing.get(kind, ()))
        stats = _stats(nodeids, failed=failed)
        exit_code = 1 if failed else 0
        capture_dir = base / capture_id
        text = _live_layout([{"name": "normal", "exit": exit_code, "stats": stats}])
        _make_capture(capture_dir, capture_id, stdout_text=text, child_exit=exit_code)
        evidence = base / f"{capture_id}-evidence.json"
        _make_import_evidence(evidence, tree, revision, harness["launcher"]["sha256"])
        members.append({
            "kind": kind,
            "capture_id": capture_id,
            "request": _ref(capture_dir / "request.json"),
            "result": _ref(capture_dir / "result.json"),
            "stdout": {"raw": _ref(capture_dir / "stdout.txt"), "gzip": _ref(capture_dir / "stdout.txt.gz")},
            "stderr": {"raw": _ref(capture_dir / "stderr.txt"), "gzip": _ref(capture_dir / "stderr.txt.gz")},
            "source": {
                "root": str(tree).replace("\\", "/"),
                "revision": revision,
                "snapshot": _ref(snapshot),
                "import_evidence": _ref(evidence),
            },
            "environment": _ref(env_path),
            "harness": harness,
            "selection": {
                "expected_files": sorted({n.split("::")[0] for n in nodeids}),
                "expected_domains": ["normal"],
            },
        })
    return {
        "schema_version": "offline-capture-set-v1",
        "scope": "full-suite-windows",
        "members": members,
    }


def _record_full(base: Path, name: str, **kwargs) -> Path:
    path = base / f"{name}-descriptor.json"
    path.write_text(json.dumps(_full_descriptor(base, name, **kwargs), indent=2), encoding="utf-8", newline="\n")
    output = base / f"{name}-baseline.json"
    assert ctb.main(["record", "--capture", str(path), "--output", str(output)]) == 0
    return output


def test_full_scope_requires_exactly_four_kinds(tmp_path: Path) -> None:
    descriptor = _full_descriptor(tmp_path, "three01")
    descriptor["members"] = descriptor["members"][:3]
    path = tmp_path / "three01.json"
    path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8", newline="\n")
    assert ctb.main(["record", "--capture", str(path), "--output", str(tmp_path / "b.json")]) == 2


def test_full_scope_rejects_duplicate_kinds(tmp_path: Path) -> None:
    descriptor = _full_descriptor(tmp_path, "dupkind01")
    descriptor["members"][1]["kind"] = "full_suite"
    path = tmp_path / "dupkind01.json"
    path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8", newline="\n")
    assert ctb.main(["record", "--capture", str(path), "--output", str(tmp_path / "b.json")]) == 2


def test_full_scope_rejects_overlapping_populations(tmp_path: Path) -> None:
    descriptor = _full_descriptor(tmp_path, "overlap01")
    # Make the duplicate probe select a nodeid already in full_suite.
    duplicate = descriptor["members"][1]
    nodeids = ["tests/test_suite.py::test_one"]
    stats = _stats(nodeids)
    text = _live_layout([{"name": "normal", "exit": 0, "stats": stats}])
    capture_dir = tmp_path / "overlap01-duplicate_probe-b"
    _make_capture(capture_dir, "overlap01-duplicate_probe-b", stdout_text=text, child_exit=0)
    duplicate["request"] = _ref(capture_dir / "request.json")
    duplicate["result"] = _ref(capture_dir / "result.json")
    duplicate["stdout"] = {"raw": _ref(capture_dir / "stdout.txt"), "gzip": _ref(capture_dir / "stdout.txt.gz")}
    duplicate["stderr"] = {"raw": _ref(capture_dir / "stderr.txt"), "gzip": _ref(capture_dir / "stderr.txt.gz")}
    duplicate["selection"]["expected_files"] = ["tests/test_suite.py"]
    path = tmp_path / "overlap01.json"
    path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8", newline="\n")
    assert ctb.main(["record", "--capture", str(path), "--output", str(tmp_path / "b.json")]) == 2


def test_full_scope_rejects_member_environment_mismatch(tmp_path: Path) -> None:
    other_env = tmp_path / "other-env.json"
    _make_environment(other_env, "C:/synthetic-2/venv/Scripts/python.exe")
    descriptor = _full_descriptor(tmp_path, "envmix01")
    descriptor["members"][2]["environment"] = _ref(other_env)
    path = tmp_path / "envmix01.json"
    path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8", newline="\n")
    assert ctb.main(["record", "--capture", str(path), "--output", str(tmp_path / "b.json")]) == 2


# ---------------------------------------------------------------------------
# Comparison semantics
# ---------------------------------------------------------------------------

def _compare(tmp_path: Path, baseline: Path, candidate: Path, output=None):
    arguments = ["compare", "--baseline", str(baseline), "--candidate", str(candidate)]
    if output is not None:
        arguments += ["--output", str(output)]
    return ctb.main(arguments)


def test_compare_no_added_failures_returns_zero_and_preserves_native_exit(tmp_path: Path) -> None:
    baseline = _record(tmp_path, "cb", nodeids=[NODE_A, NODE_B], failed=[NODE_B], child_exit=1, revision="rev-1")
    candidate = _record(tmp_path, "cc", nodeids=[NODE_A, NODE_B], failed=[NODE_B], child_exit=1, revision="rev-2")
    diff_path = tmp_path / "diff.json"
    assert _compare(tmp_path, baseline, candidate, diff_path) == 0
    diff = ctb.loads_strict(diff_path.read_text(encoding="utf-8"), "diff")
    member = diff["members"][0]
    assert member["native"]["baseline_overall_exit"] == 1
    assert member["native"]["candidate_overall_exit"] == 1
    assert member["failures"]["added_failed"] == []
    assert diff["exit"] == 0


def test_compare_added_failure_returns_one(tmp_path: Path) -> None:
    baseline = _record(tmp_path, "ab", nodeids=[NODE_A, NODE_B], revision="rev-1")
    candidate = _record(tmp_path, "ac", nodeids=[NODE_A, NODE_B], failed=[NODE_A], child_exit=1, revision="rev-2")
    diff_path = tmp_path / "diff.json"
    assert _compare(tmp_path, baseline, candidate, diff_path) == 1
    diff = ctb.loads_strict(diff_path.read_text(encoding="utf-8"), "diff")
    assert diff["members"][0]["failures"]["added_failed"] == [NODE_A]
    assert diff["summary"]["added_failed_total"] == 1


def test_compare_same_count_different_node_is_regression(tmp_path: Path) -> None:
    baseline = _record(tmp_path, "sb", nodeids=[NODE_A, NODE_B, NODE_C], failed=[NODE_A], child_exit=1)
    candidate = _record(tmp_path, "sc", nodeids=[NODE_A, NODE_B, NODE_C], failed=[NODE_B], child_exit=1)
    diff_path = tmp_path / "diff.json"
    assert _compare(tmp_path, baseline, candidate, diff_path) == 1
    diff = ctb.loads_strict(diff_path.read_text(encoding="utf-8"), "diff")
    failures = diff["members"][0]["failures"]
    assert failures["added_failed"] == [NODE_B]
    assert failures["resolved_failed"] == [NODE_A]


def test_compare_population_loss_is_error(tmp_path: Path) -> None:
    baseline = _record(tmp_path, "pb", nodeids=[NODE_A, NODE_B])
    candidate = _record(tmp_path, "pc", nodeids=[NODE_A])
    assert _compare(tmp_path, baseline, candidate) == 2


def test_compare_population_addition_is_recorded_not_exit_one(tmp_path: Path) -> None:
    baseline = _record(tmp_path, "addb", nodeids=[NODE_A])
    candidate = _record(tmp_path, "addc", nodeids=[NODE_A, NODE_B])
    diff_path = tmp_path / "diff.json"
    assert _compare(tmp_path, baseline, candidate, diff_path) == 0
    diff = ctb.loads_strict(diff_path.read_text(encoding="utf-8"), "diff")
    assert diff["members"][0]["population"]["added_nodeids"] == [NODE_B]


def test_compare_environment_mismatch_is_error(tmp_path: Path) -> None:
    baseline = _record(tmp_path, "eb", nodeids=[NODE_A])
    other_env = tmp_path / "full-env.json"
    _make_environment(other_env, "C:/full-env/Scripts/python.exe")
    candidate_descriptor = _make_descriptor(
        tmp_path, "ec", kind="historical-forward", nodeids=[NODE_A], env_file=other_env,
        python="C:/full-env/Scripts/python.exe",
    )
    path = tmp_path / "ec.json"
    path.write_text(json.dumps(candidate_descriptor, indent=2), encoding="utf-8", newline="\n")
    assert ctb.main(["compare", "--baseline", str(baseline), "--candidate-capture", str(path)]) == 2


def test_compare_missing_kind_is_error(tmp_path: Path) -> None:
    baseline = _record_full(tmp_path, "fb")
    candidate = _record(tmp_path, "fc", nodeids=[NODE_A])  # historical scope
    assert _compare(tmp_path, baseline, candidate) == 2


def test_compare_added_collection_error_returns_one(tmp_path: Path) -> None:
    baseline = _record(
        tmp_path, "kcb", nodeids=[NODE_A, NODE_B],
        expected_files=["tests/test_synthetic.py"],
    )
    candidate = _record(
        tmp_path, "kcc", nodeids=[NODE_A, NODE_B],
        collection_errors=["tests/test_new_broken.py"],
        expected_files=["tests/test_new_broken.py", "tests/test_synthetic.py"], child_exit=1,
    )
    diff_path = tmp_path / "diff.json"
    assert _compare(tmp_path, baseline, candidate, diff_path) == 1
    diff = ctb.loads_strict(diff_path.read_text(encoding="utf-8"), "diff")
    assert diff["members"][0]["failures"]["added_collection_errors"] == ["tests/test_new_broken.py"]


def test_compare_resolved_collection_error_is_reviewable_not_failure(tmp_path: Path) -> None:
    # tests/test_broken.py failed collection in the baseline and collects
    # successfully (with a selected test) in the candidate.
    baseline = _record(
        tmp_path, "rcb", nodeids=[NODE_A], collection_errors=["tests/test_broken.py"],
        expected_files=["tests/test_broken.py", "tests/test_synthetic.py"], child_exit=1,
    )
    candidate = _record(
        tmp_path, "rcc", nodeids=[NODE_A, NODE_BROKEN],
        expected_files=["tests/test_broken.py", "tests/test_synthetic.py"],
    )
    diff_path = tmp_path / "diff.json"
    assert _compare(tmp_path, baseline, candidate, diff_path) == 0
    diff = ctb.loads_strict(diff_path.read_text(encoding="utf-8"), "diff")
    failures = diff["members"][0]["failures"]
    assert failures["resolved_collection_errors"] == ["tests/test_broken.py"]
    assert failures["added_collection_errors"] == []


def test_json_and_raw_comparison_produce_the_same_delta(tmp_path: Path) -> None:
    baseline = _record(tmp_path, "rtb", nodeids=[NODE_A, NODE_B], revision="rev-1")
    candidate_json = _record(tmp_path, "rtc", nodeids=[NODE_A, NODE_B], failed=[NODE_B], child_exit=1, revision="rev-2")
    candidate_descriptor = _make_descriptor(
        tmp_path, "rtc2", kind="historical-forward", nodeids=[NODE_A, NODE_B], failed=[NODE_B],
        child_exit=1, revision="rev-2",
    )
    descriptor_path = tmp_path / "rtc2.json"
    descriptor_path.write_text(json.dumps(candidate_descriptor, indent=2), encoding="utf-8", newline="\n")
    diff_json = tmp_path / "from-json-diff.json"
    diff_raw = tmp_path / "from-raw-diff.json"
    assert ctb.main(["compare", "--baseline", str(baseline), "--candidate", str(candidate_json), "--output", str(diff_json)]) == 1
    assert ctb.main(["compare", "--baseline", str(baseline), "--candidate-capture", str(descriptor_path), "--output", str(diff_raw)]) == 1
    left = ctb.loads_strict(diff_json.read_text(encoding="utf-8"), "d1")
    right = ctb.loads_strict(diff_raw.read_text(encoding="utf-8"), "d2")
    for diff in (left, right):
        diff.pop("generated_at")
        diff["candidate"].pop("recorded_at")
        diff["baseline"].pop("recorded_at")
    assert left == right


def test_compare_rejects_tampered_baseline(tmp_path: Path) -> None:
    baseline = _record(tmp_path, "tb", nodeids=[NODE_A, NODE_B], failed=[NODE_B], child_exit=1)
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    payload["members"][0]["domains"][0]["stats"]["passed"] += 1  # breaks the count equation
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
    candidate = _record(tmp_path, "tc", nodeids=[NODE_A, NODE_B], failed=[NODE_B], child_exit=1)
    assert _compare(tmp_path, tampered, candidate) == 2


def test_compare_skip_count_change_is_a_finding(tmp_path: Path) -> None:
    baseline = _record(tmp_path, "skb", nodeids=[NODE_A, NODE_B])
    candidate_descriptor = _make_descriptor(
        tmp_path, "skc", kind="historical-forward", nodeids=[NODE_A, NODE_B], child_exit=0, skipped=1,
    )
    path = tmp_path / "skc.json"
    path.write_text(json.dumps(candidate_descriptor, indent=2), encoding="utf-8", newline="\n")
    diff_path = tmp_path / "sk-diff.json"
    assert ctb.main(["compare", "--baseline", str(baseline), "--candidate-capture", str(path), "--output", str(diff_path)]) == 0
    diff = ctb.loads_strict(diff_path.read_text(encoding="utf-8"), "diff")
    assert any("skipped count changed" in finding for finding in diff["identity_findings"])


# ---------------------------------------------------------------------------
# CLI behavior (exact native exit codes in a capturing harness)
# ---------------------------------------------------------------------------

def _run_cli(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", "-X", "utf8", str(SCRIPT), *arguments],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


def test_cli_record_then_compare_exit_codes(tmp_path: Path) -> None:
    base = tmp_path
    descriptor = _make_descriptor(base, "cli01", kind="historical-forward", nodeids=[NODE_A, NODE_B])
    descriptor_path = base / "cli01.json"
    descriptor_path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8", newline="\n")
    baseline = base / "cli01-baseline.json"
    completed = _run_cli("record", "--capture", str(descriptor_path), "--output", str(baseline))
    assert completed.returncode == 0, completed.stderr

    failing = _make_descriptor(base, "cli02", kind="historical-forward", nodeids=[NODE_A, NODE_B], failed=[NODE_A], child_exit=1)
    failing_path = base / "cli02.json"
    failing_path.write_text(json.dumps(failing, indent=2), encoding="utf-8", newline="\n")
    diff = base / "diff.json"
    completed = _run_cli("compare", "--baseline", str(baseline), "--candidate-capture", str(failing_path), "--output", str(diff))
    assert completed.returncode == 1, (completed.stdout, completed.stderr)
    diff_payload = ctb.loads_strict(diff.read_text(encoding="utf-8"), "diff")
    assert diff_payload["summary"]["added_failed_total"] == 1

    completed = _run_cli("compare", "--baseline", str(baseline), "--candidate", str(base / "missing.json"))
    assert completed.returncode == 2
    assert "INVALID INPUT" in completed.stderr


def test_cli_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    descriptor = _make_descriptor(tmp_path, "ow01", kind="historical-forward", nodeids=[NODE_A])
    descriptor_path = tmp_path / "ow01.json"
    descriptor_path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8", newline="\n")
    output = tmp_path / "existing.json"
    output.write_text("{}", encoding="utf-8")
    completed = _run_cli("record", "--capture", str(descriptor_path), "--output", str(output))
    assert completed.returncode == 2
    assert output.read_text(encoding="utf-8") == "{}"
    assert not list(tmp_path.glob("*.tmp-*")), "temporary artifacts must not survive"


# ---------------------------------------------------------------------------
# F1/F2 repair regressions (review r4-k03-real-capture-review02)
# ---------------------------------------------------------------------------

def test_f1_zero_selected_collection_error_domain_records_native_one(tmp_path: Path) -> None:
    """P5: a --continue-on-collection-errors domain whose only file failed
    collection legitimately completes selected=0 / CE>=1 / native 1."""
    baseline = _record(
        tmp_path, "p5ce", nodeids=[], collection_errors=["tests/test_broken.py"],
        expected_files=["tests/test_broken.py"], child_exit=1,
    )
    payload = ctb.loads_strict(baseline.read_text(encoding="utf-8"), "baseline")
    domain = payload["members"][0]["domains"][0]
    assert domain["exit_code"] == 1
    assert domain["stats"]["selected"] == 0
    assert domain["stats"]["collection_errors"] == ["tests/test_broken.py"]
    assert payload["members"][0]["parent"]["overall_exit"] == 1
    assert payload["members"][0]["parent"]["guard"]["child_exit"] == 1
    assert "tests/test_broken.py" in payload["members"][0]["resolved_files"]


def test_f1_zero_selected_ce_baseline_loads_and_self_compares(tmp_path: Path) -> None:
    """P5 loaded-JSON path: the recorded baseline re-validates and compares
    against itself with zero delta while preserving native exit 1."""
    baseline = _record(
        tmp_path, "p5self", nodeids=[], collection_errors=["tests/test_broken.py"],
        expected_files=["tests/test_broken.py"], child_exit=1,
    )
    diff_path = tmp_path / "p5-self-diff.json"
    assert ctb.main(["compare", "--baseline", str(baseline), "--candidate", str(baseline),
                     "--output", str(diff_path)]) == 0
    diff = ctb.loads_strict(diff_path.read_text(encoding="utf-8"), "diff")
    member = diff["members"][0]
    assert member["failures"]["added_failed"] == []
    assert member["native"]["baseline_overall_exit"] == 1
    assert member["native"]["candidate_overall_exit"] == 1


def test_f1_zero_selected_no_ce_native_zero_stays_invalid(tmp_path: Path) -> None:
    """P6b: selected 0 with no recorded failure identities and native 0 is a
    genuinely inconsistent empty domain and stays rejected."""
    stats = _stats([])
    text = _live_layout([{"name": "normal", "exit": 0, "stats": stats}])
    assert _invalid_record(tmp_path, "p6b", nodeids=[], stdout_text=text, child_exit=0) == 2


def test_f1_zero_selected_no_ce_native_five_records(tmp_path: Path) -> None:
    """P7: a legitimately empty domain (no tests, nothing recorded, exit 5)
    remains recordable."""
    baseline = _record(tmp_path, "p7empty", nodeids=[], child_exit=5)
    payload = ctb.loads_strict(baseline.read_text(encoding="utf-8"), "baseline")
    assert payload["members"][0]["domains"][0]["exit_code"] == 5
    assert payload["members"][0]["parent"]["overall_exit"] == 5


def test_f2_loaded_baseline_inconsistent_parent_counts_rejected(tmp_path: Path) -> None:
    """P8: planned/launched/domain-count disagreements must be rejected on the
    loaded-JSON path; the old chained comparison admitted both variants."""
    baseline = _record(tmp_path, "p8base", nodeids=[NODE_A])
    original = json.loads(baseline.read_text(encoding="utf-8"))
    other = _record(tmp_path, "p8other", nodeids=[NODE_A])
    for field, value in (("planned", 2), ("launched", 2)):
        tampered = json.loads(json.dumps(original))
        tampered["members"][0]["parent"][field] = value
        path = tmp_path / f"p8-{field}.json"
        path.write_text(json.dumps(tampered, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
        assert ctb.main(["compare", "--baseline", str(other), "--candidate", str(path)]) == 2


def test_f2_raw_record_inconsistent_parent_counts_rejected(tmp_path: Path) -> None:
    """P8 raw path: a plan line promising two domains with only one captured
    section (planned 2 / launched 1) is rejected as inconsistent counts."""
    stats = _stats([NODE_A])
    lines = [
        "Offline command: x",
        "Offline plan: 2 domain(s): normal, ghost",
        "--- offline domain normal: running ---",
        STATS_PREFIX + json.dumps(stats),
        "=== offline domain normal: exit 0 ===",
        "Offline summary: 2 domain(s) planned, 1 launched, 0 skipped-empty, 0 unrun, 0 launcher-failed",
        "Offline domain normal: exit 0, selected 1, deselected 0, ran 1, passed 1, failed 0, "
        "skipped 0, errors 0, collection_errors 0",
        "Offline totals: selected 1 (+0 deselected), ran 1, passed 1, failed 0, skipped 0, "
        "errors 0, collection_error_files 0, unresolved_files 0",
        "Offline overall exit: 0",
    ]
    text = "\n".join(lines) + "\n"
    assert _invalid_record(tmp_path, "p8raw", nodeids=[NODE_A], stdout_text=text, child_exit=0) == 2


def test_f1_f2_cli_exact_exit_codes(tmp_path: Path) -> None:
    """CLI-level probes with exact native codes: P5 records (0), P6b invalid
    (2), P7 records (0), P8 tampered baseline rejected (2)."""
    base = tmp_path
    p5 = _make_descriptor(base, "clip5", kind="historical-forward", nodeids=[],
                          collection_errors=["tests/test_broken.py"],
                          expected_files=["tests/test_broken.py"], child_exit=1)
    p5_path = base / "clip5.json"
    p5_path.write_text(json.dumps(p5, indent=2), encoding="utf-8", newline="\n")
    p5_out = base / "clip5-baseline.json"
    completed = _run_cli("record", "--capture", str(p5_path), "--output", str(p5_out))
    assert completed.returncode == 0, completed.stderr

    stats = _stats([])
    p6b_text = _live_layout([{"name": "normal", "exit": 0, "stats": stats}])
    p6b = _make_descriptor(base, "clip6b", kind="historical-forward", nodeids=[],
                           stdout_text=p6b_text, child_exit=0)
    p6b_path = base / "clip6b.json"
    p6b_path.write_text(json.dumps(p6b, indent=2), encoding="utf-8", newline="\n")
    completed = _run_cli("record", "--capture", str(p6b_path), "--output", str(base / "clip6b-out.json"))
    assert completed.returncode == 2
    assert "nothing selected and nothing recorded" in completed.stderr

    p7 = _make_descriptor(base, "clip7", kind="historical-forward", nodeids=[], child_exit=5)
    p7_path = base / "clip7.json"
    p7_path.write_text(json.dumps(p7, indent=2), encoding="utf-8", newline="\n")
    completed = _run_cli("record", "--capture", str(p7_path), "--output", str(base / "clip7-out.json"))
    assert completed.returncode == 0, completed.stderr

    tampered = json.loads(p5_out.read_text(encoding="utf-8"))
    tampered["members"][0]["parent"]["planned"] = 2
    tampered_path = base / "clip8.json"
    tampered_path.write_text(json.dumps(tampered, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
    completed = _run_cli("compare", "--baseline", str(p5_out), "--candidate", str(tampered_path))
    assert completed.returncode == 2
    assert "parent counts inconsistent" in completed.stderr
