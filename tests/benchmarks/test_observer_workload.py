"""NF23 observer benchmark workload tests (work order H23 acceptance).

Scope: ``tests/benchmarks/test_observer_workload.py`` only (NF23 exclusive
write set).  These tests are the development-seat self-verification for the
observer in-process benchmark workloads delivered under contract C20-1.0.0:

* all ten workload entrypoints conform to the C20 protocol, verify PASS, and
  produce deterministic digests across fresh builds;
* SYNTHETIC payload shapes match the frozen spec (fixed byte counts, symbol
  count, tick counts, legal boundaries at MAX_BODY_BYTES / MAX_TICKS);
* the seven H23 counterexample classes are probed against the *production*
  contracts (protocol / service / connection) and against the workload's own
  frozen verification rules - each probe injects one fault and asserts it is
  detected;
* manifests + source map are structurally valid and hash-bind the worktree;
* a real harness smoke run (CLI, NetworkGuard active) reproduces the frozen
  expected digests.

These are SYNTHETIC in-process tests: no sockets, no ports 17778/17779, no
deployed observer, no client, no credentials.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.benchmarks import contracts as C  # noqa: E402
from tools.benchmarks import runner  # noqa: E402

from ak_trading.infiny_bridge.connection import ConnectionMonitor  # noqa: E402
from ak_trading.infiny_bridge.protocol import (  # noqa: E402
    MAX_BODY_BYTES,
    MAX_TICKS,
    ProtocolViolation,
    normalize_payload,
    parse_json_body,
)
from ak_trading.infiny_bridge.service import BridgeService  # noqa: E402

WORKLOAD_PATH = REPO_ROOT / "tools" / "benchmarks" / "workloads" / "observer.py"
SPEC_REL = "tools/benchmarks/fixtures/observer/payload-spec.json"
SOURCE_MAP_REL = "tools/benchmarks/fixtures/observer/source-map.json"
SEED = 20260924

ALL_CASES: List[Tuple[str, str]] = [
    ("observer/parse_accept_typical_inprocess", "build_parse_accept"),
    ("observer/parse_reject_malformed_inprocess", "build_parse_reject"),
    ("observer/parse_max_legal_inprocess", "build_parse_max_legal"),
    ("observer/normalize_accept_typical_inprocess", "build_normalize_accept"),
    ("observer/normalize_reject_violations_inprocess", "build_normalize_reject"),
    ("observer/normalize_max_legal_inprocess", "build_normalize_max_legal"),
    ("observer/analyze_accept_typical_inprocess_hold_double", "build_analyze_accept"),
    ("observer/analyze_replay_duplicate_inprocess_hold_double", "build_analyze_replay"),
    ("observer/heartbeat_typical_inprocess_hold_double", "build_heartbeat"),
    ("observer/status_snapshot_inprocess_hold_double", "build_status_snapshot"),
]

FLAGSHIP_REL = "tools/benchmarks/cases/observer.json"
CASE_DIR_REL = "tools/benchmarks/fixtures/observer/cases"


def _load_workload_module():
    spec = importlib.util.spec_from_file_location("nf23_observer_workload", WORKLOAD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WL = _load_workload_module()


class _Ctx:
    """Minimal stand-in for ``contracts.CaseContext`` (same attribute surface)."""

    def __init__(self, tmp_dir: Path, case_id: str) -> None:
        self.params = {"seed": SEED, "case": case_id, "payload_spec": SPEC_REL}
        self.seed = SEED
        self.tmp_dir = tmp_dir
        self.resources = {"temp_sqlite_path": None}
        self.normalizations = []


def _run_workload(case_id: str, entrypoint: str, tmp_dir: Path, ops: int, warmup: int = 3):
    ctx = _Ctx(tmp_dir, case_id)
    workload = getattr(WL, entrypoint)(ctx)
    C.validate_workload_object(workload, "per_operation", "none")
    workload.setup(ctx)
    try:
        for _ in range(warmup):
            assert workload.operation(ctx)["ok"]
        for _ in range(ops):
            assert workload.operation(ctx)["ok"]
        verification = workload.verify(ctx)
    finally:
        workload.teardown(ctx)
    return verification


# ---------------------------------------------------------------------------
# protocol conformance + determinism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id,entrypoint", ALL_CASES)
def test_workload_conforms_passes_and_is_deterministic(
    case_id: str, entrypoint: str, tmp_path: Path
) -> None:
    first = _run_workload(case_id, entrypoint, tmp_path / "a", ops=12)
    second = _run_workload(case_id, entrypoint, tmp_path / "b", ops=12)
    assert first["status"] == "PASS", first["fail_reasons"]
    assert second["status"] == "PASS", second["fail_reasons"]
    assert first["digest"] == second["digest"]
    C.validate_verification_result(first)


# ---------------------------------------------------------------------------
# SYNTHETIC payload shapes and legal boundaries
# ---------------------------------------------------------------------------


def _spec() -> Dict[str, Any]:
    return json.loads((REPO_ROOT / SPEC_REL).read_text(encoding="utf-8"))


def test_payload_shapes_and_boundaries() -> None:
    spec = _spec()
    assert spec["shared"]["symbol_count_per_payload"] == 1

    typical = WL.build_typical_payload(spec, SEED, "parse_accept_typical_inprocess")
    body = json.dumps(typical, separators=(",", ":")).encode("utf-8")
    assert len(body) == len(json.dumps(WL.build_typical_payload(
        spec, SEED, "parse_accept_typical_inprocess"), separators=(",", ":")).encode("utf-8"))
    assert 1000 < len(body) < MAX_BODY_BYTES  # fixed, meaningful, sub-boundary
    assert len(typical["ticks"]) == spec["cases"]["parse_accept_typical_inprocess"]["ticks"]
    parsed = parse_json_body(body)
    assert parsed["exchange"] == "SHFE" and parsed["instrument_id"] == "rb2610"

    max_body = WL.build_padded_max_body(spec, SEED, "parse_max_legal_inprocess")
    assert len(max_body) == MAX_BODY_BYTES  # exact byte boundary, still legal
    assert parse_json_body(max_body)["nf23_padding"]

    max_dict = WL.build_max_legal_dict(spec, SEED, "normalize_max_legal_inprocess")
    assert len(max_dict["ticks"]) == MAX_TICKS  # exact tick-count boundary
    normalized = normalize_payload(dict(max_dict))
    assert normalized.blocked_reason is None and normalized.tick_count == MAX_TICKS


def test_reject_flavors_hit_the_declared_protocol_reasons() -> None:
    spec = _spec()
    for flavor, body in WL.PARSE_REJECT_BODIES.items():
        with pytest.raises(ProtocolViolation) as excinfo:
            parse_json_body(body)
        assert excinfo.value.reason == flavor
    for flavor, payload in WL.build_normalize_reject_payloads(
        spec, SEED, "normalize_reject_violations_inprocess"
    ):
        with pytest.raises(ProtocolViolation) as excinfo:
            normalize_payload(dict(payload))
        assert excinfo.value.reason == flavor


# ---------------------------------------------------------------------------
# H23 counterexample probes (one injected fault each, detection asserted)
# ---------------------------------------------------------------------------


def _accept_workload(tmp_path: Path, ops: int = 4):
    ctx = _Ctx(tmp_path, "observer/analyze_accept_typical_inprocess_hold_double")
    workload = WL.build_analyze_accept(ctx)
    workload.setup(ctx)
    for _ in range(ops):
        assert workload.operation(ctx)["ok"]
    return ctx, workload


def test_probe_1_wrong_epoch_is_refused_and_regression_is_flagged(tmp_path: Path) -> None:
    # (a) the connection contract refuses requests from a superseded epoch
    monitor = ConnectionMonitor()
    monitor.start("inst-1", "epoch-1")
    good = {"instance_id": "inst-1", "session_epoch": "epoch-1",
            "request_id": "r1", "sequence": 1}
    assert monitor.note_request(good) is True
    wrong_epoch = dict(good, session_epoch="epoch-2", request_id="r2", sequence=2)
    assert monitor.note_request(wrong_epoch) is False  # stale epoch never revives

    # (b) a regressed (wrong-epoch) business clock marks receipts not
    # clock-valid in the public status, while responses stay hold
    ctx, workload = _accept_workload(tmp_path)
    try:
        workload.clock.rewind(steps=5)
        payload = dict(workload.template)
        payload["batch_id"] = "probe-epoch-regress"
        response = workload.service.analyze(payload, source_header="nf23-probe")
        assert response == {"signal": "hold", "reason": "observing"}
        status = workload.service.status()
        assert status["recent_receipt"]["transport"]["clock_valid"] is False
    finally:
        workload.teardown(ctx)


def test_probe_2_expired_payload_is_blocked_not_accepted(tmp_path: Path) -> None:
    ctx, workload = _accept_workload(tmp_path)
    try:
        base = workload.clock.now() - timedelta(seconds=workload.clock.steps * 0 + 0)
        payload = dict(workload.template)
        payload["batch_id"] = "probe-stale"
        stale_at = (base - timedelta(seconds=120)).isoformat()
        payload["ticks"] = [dict(tick, time=stale_at) for tick in workload.template["ticks"]]
        response = workload.service.analyze(payload, source_header="nf23-probe")
        assert response == {"signal": "hold", "reason": "stale_tick"}
        counters = workload.service.status()["counters"]
        assert counters["blocked_requests"] >= 1
        assert counters["stale_requests"] >= 1
        # accounting: this response is NOT a clean accept
        assert response["reason"] != "observing"
    finally:
        workload.teardown(ctx)


def test_probe_3_duplicate_batch_is_a_cache_hit_not_business_processing(tmp_path: Path) -> None:
    verification = _run_workload(
        "observer/analyze_replay_duplicate_inprocess_hold_double",
        "build_analyze_replay",
        tmp_path,
        ops=7,
    )
    assert verification["status"] == "PASS", verification["fail_reasons"]
    summary = verification["summary"]
    assert summary["evaluator_calls"] == 1  # only the setup seed ever evaluates
    assert summary["counters"]["replays"] == summary["ops"]
    assert summary["accepted"] == summary["ops"]


def test_probe_4_buy_sell_is_blocked_and_accounting_flags_it(tmp_path: Path) -> None:
    # production contract: a candidate direction never crosses the boundary
    class BuyEvaluatorDouble:
        name = "buy_double"

        def evaluate(self, payload):
            return {"candidate_signal": "buy", "reason": "probe"}

        def status(self):
            return {"name": self.name, "signal": "buy"}

    ctx = _Ctx(tmp_path, "observer/analyze_accept_typical_inprocess_hold_double")
    workload = WL.build_analyze_accept(ctx)
    workload.setup(ctx)
    try:
        clock = workload.clock
        service = BridgeService(
            evaluator=BuyEvaluatorDouble().evaluate,
            allowed_symbols=["SHFE.rb2610"],
            audit_log=tmp_path / "probe-buy-audit.jsonl",
            instance_id="nf23-probe-buy",
            clock=clock,
            evaluator_workers=1,
        )
        try:
            payload = dict(workload.template)
            payload["batch_id"] = "probe-buy"
            response = service.analyze(payload, source_header="nf23-probe")
            assert response == {"signal": "hold", "reason": "candidate_blocked"}
            assert response["signal"] not in ("buy", "sell")
        finally:
            service.close()

        # workload accounting: such a response is not a clean accept
        workload._record(999, True, response=response)
        assert workload._rejected == 1
        verification = workload.verify(ctx)
        assert verification["status"] == "FAIL"
        assert any("REJECTION_MIXED" in r for r in verification["fail_reasons"])
    finally:
        workload.teardown(ctx)


def test_probe_5_one_unaccounted_request_fails_verify(tmp_path: Path) -> None:
    ctx, workload = _accept_workload(tmp_path, ops=5)
    try:
        payload = dict(workload.template)
        payload["batch_id"] = "probe-missing"
        assert workload.service.analyze(payload, source_header="nf23-probe")[
            "signal"
        ] == "hold"
        verification = workload.verify(ctx)
        assert verification["status"] == "FAIL"
        assert any("MISSING_REQUEST" in r for r in verification["fail_reasons"])
    finally:
        workload.teardown(ctx)


def test_probe_6_audit_write_failure_keeps_hold_responses_and_is_detected(
    tmp_path: Path,
) -> None:
    class BrokenAuditHandle:
        def write(self, *_args):
            raise OSError("probe: audit write failure")

        def flush(self):
            raise OSError("probe: audit flush failure")

        def close(self):
            return None

    ctx, workload = _accept_workload(tmp_path, ops=5)
    try:
        workload.service._audit_handle = BrokenAuditHandle()
        for _ in range(3):
            result = workload.operation(ctx)  # responses must remain hold
            assert result["ok"]
        counters = workload.service.status()["counters"]
        assert counters["responses"] == counters["requests"]  # receipt path unaffected
        verification = workload.verify(ctx)
        assert verification["status"] == "FAIL"
        assert any("AUDIT_LINES_MISMATCH" in r for r in verification["fail_reasons"])
    finally:
        workload.teardown(ctx)


def test_probe_7_rejection_mixed_into_accept_accounting_fails_verify(
    tmp_path: Path,
) -> None:
    ctx, workload = _accept_workload(tmp_path, ops=3)
    try:
        # inject the classic mixing fault: record a rejection as accepted
        workload._record(998, True, reason="stale_tick")
        verification = workload.verify(ctx)
        assert verification["status"] == "FAIL"
        assert any("REJECTION_MIXED" in r for r in verification["fail_reasons"])
        assert verification["summary"]["accepted"] == 3
        assert verification["summary"]["rejected"] == 1
    finally:
        workload.teardown(ctx)


# ---------------------------------------------------------------------------
# manifest / source-map integrity (explicit inputs, programmatic hashes)
# ---------------------------------------------------------------------------


def _manifest_paths() -> List[Path]:
    paths = [REPO_ROOT / FLAGSHIP_REL]
    for case_id, _ in ALL_CASES:
        rel = "%s/%s.json" % (CASE_DIR_REL, case_id.split("/", 1)[1])
        candidate = REPO_ROOT / rel
        if candidate not in paths:
            paths.append(candidate)
    flagship = REPO_ROOT / FLAGSHIP_REL
    if flagship not in paths:
        paths.append(flagship)
    return [p for p in paths if p.is_file()]


def test_manifests_validate_and_expected_digests_frozen() -> None:
    paths = _manifest_paths()
    assert len(paths) == 10
    seen = set()
    for path in paths:
        manifest = C.validate_case_manifest(C.load_json_file(path, "case manifest"))
        assert manifest["origin"] == "SYNTHETIC"
        assert manifest["domain"] == "observer"
        assert manifest["backend"] == "python-inprocess"
        assert manifest["measurement"]["sample_mode"] == "per_operation"
        assert manifest["correctness"]["expected_digest"], path
        assert manifest["resources"] == {
            "network": False, "production_db": False, "temp_sqlite": False,
        }
        seen.add(manifest["case_id"])
    assert seen == {case_id for case_id, _ in ALL_CASES}


def test_source_map_hashes_match_worktree_and_binds_measured_modules() -> None:
    source_map = C.load_json_file(REPO_ROOT / SOURCE_MAP_REL, "source map")
    C.validate_source_map(source_map)
    runner.verify_source_map_hashes(REPO_ROOT, source_map)  # raises on drift
    logical = {m["logical_module"] for m in source_map["modules"]}
    for case_id, entrypoint in ALL_CASES:
        path = REPO_ROOT / (
            FLAGSHIP_REL
            if case_id == "observer/analyze_accept_typical_inprocess_hold_double"
            else "%s/%s.json" % (CASE_DIR_REL, case_id.split("/", 1)[1])
        )
        manifest = C.load_json_file(path, "case manifest")
        for module in manifest["measured_modules"]:
            assert module in logical, (case_id, module)
        assert callable(getattr(WL, entrypoint))


def test_workload_file_hash_matches_manifest_fixture() -> None:
    for case_id, _ in ALL_CASES:
        path = REPO_ROOT / (
            FLAGSHIP_REL
            if case_id == "observer/analyze_accept_typical_inprocess_hold_double"
            else "%s/%s.json" % (CASE_DIR_REL, case_id.split("/", 1)[1])
        )
        manifest = C.load_json_file(path, "case manifest")
        workload_rel = manifest["workload"]["source"]
        spec_fixture = manifest["fixtures"][0]
        assert workload_rel == "tools/benchmarks/workloads/observer.py"
        assert spec_fixture["path"] == SPEC_REL
        runner.hash_relative(REPO_ROOT, workload_rel)
        runner.hash_relative(REPO_ROOT, spec_fixture["path"])


# ---------------------------------------------------------------------------
# real-harness smoke subset (CLI, frozen digests, NetworkGuard active)
# ---------------------------------------------------------------------------


def test_cli_smoke_subset_reproduces_frozen_digests(tmp_path: Path) -> None:
    import subprocess

    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "20260924"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = [
        sys.executable, "-B", "-m", "tools.benchmarks.cli", "run",
        "--source-map", SOURCE_MAP_REL,
        "--output-root", str(tmp_path / "out"),
        "--profile", "smoke",
        "--case-manifest",
        "tools/benchmarks/fixtures/observer/cases/parse_accept_typical_inprocess.json",
        "--case-manifest",
        "tools/benchmarks/fixtures/observer/cases/heartbeat_typical_inprocess_hold_double.json",
        "--run-id", "nf23-pytest-subset",
    ]
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    run_dir = tmp_path / "out" / "runs" / "nf23-pytest-subset"
    summary = C.load_json_file(run_dir / "run.json", "run summary")
    assert all(entry["status"] == "OK" for entry in summary["cases"])
    for case_id in (
        "observer/parse_accept_typical_inprocess",
        "observer/heartbeat_typical_inprocess_hold_double",
    ):
        record = C.load_json_file(
            run_dir / "results" / ("%s.json" % case_id.replace("/", "__")), "case record"
        )
        C.validate_result_record(record)
        assert record["status"] == "OK", record["status_reason"]
        assert record["correctness"]["status"] == "PASS"
        manifest_path = REPO_ROOT / ("%s/%s.json" % (
            CASE_DIR_REL, case_id.split("/", 1)[1]
        ))
        manifest = C.load_json_file(manifest_path, "case manifest")
        assert record["correctness"]["digest"] == manifest["correctness"]["expected_digest"]
        assert record["measurement"]["n"] >= 100
        assert record["measurement"]["p95_ns"] is not None  # 200 samples > p95 floor
