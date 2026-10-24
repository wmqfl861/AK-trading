"""Shared helpers for the NF20 benchmark-harness tests.

Everything here is TEST-only infrastructure:

* synthetic case/manifest/source-map builders writing into ``tmp_path``
  mini-roots (never into the repository tree);
* a CLI subprocess runner (cwd = repository root, fixed PYTHONHASHSEED);
* a frozen result-record builder for the comparison state-machine tests.

Simulated timing vectors built by ``build_result_record`` carry
``origin="TEST_SIMULATED"`` by default: they exist to prove the comparison
math and state machine deterministically (reviewer protocol) and are
structurally refused by baseline promotion, so they can never enter the real
benchmark store.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.benchmarks import contracts as C  # noqa: E402

# ---------------------------------------------------------------------------
# workload templates (SYNTHETIC, TEST)
# ---------------------------------------------------------------------------

WORKLOAD_OK = '''
"""TEST synthetic workload (TEST_SIMULATED context, never a real baseline)."""
import hashlib


class W:
    def __init__(self, ctx):
        self.ctx = ctx
        self.n = 0
        self.acc = hashlib.sha256()

    def setup(self, ctx):
        pass

    def operation(self, ctx):
        self.n += 1
        self.acc.update(b"%d;" % self.n)
        return {"ok": True, "units": 1}

    def teardown(self, ctx):
        pass

    def verify(self, ctx):
        return {
            "status": "PASS",
            "digest": self.acc.hexdigest(),
            "summary": {"n": self.n},
            "fail_reasons": [],
        }


def build(ctx):
    return W(ctx)
'''

WORKLOAD_SENTINEL = '''
"""TEST workload that records on import (proves import-order guarantees)."""
import hashlib

open(_IMPORT_SENTINEL, "w", encoding="utf-8").write("imported")


class W:
    def __init__(self, ctx):
        self.ctx = ctx
        self.n = 0
        self.acc = hashlib.sha256()

    def setup(self, ctx):
        pass

    def operation(self, ctx):
        self.n += 1
        self.acc.update(b"%d;" % self.n)
        return {"ok": True, "units": 1}

    def teardown(self, ctx):
        pass

    def verify(self, ctx):
        return {
            "status": "PASS",
            "digest": self.acc.hexdigest(),
            "summary": {"n": self.n},
            "fail_reasons": [],
        }


def build(ctx):
    return W(ctx)
'''

WORKLOAD_RAISE = WORKLOAD_OK.replace(
    'self.acc.update(b"%d;" % self.n)',
    'raise RuntimeError("TEST injected execution failure")',
)

WORKLOAD_SLOW = WORKLOAD_OK.replace(
    'self.acc.update(b"%d;" % self.n)',
    'import time as _t; _t.sleep(0.05); self.acc.update(b"%d;" % self.n)',
)

WORKLOAD_SNEAKY = '''
"""TEST workload: reports ok but performs only half the work (H20.6 probe)."""
import hashlib


class W:
    def __init__(self, ctx):
        self.ctx = ctx
        self.n = 0
        self.done = 0
        self.acc = hashlib.sha256()

    def setup(self, ctx):
        pass

    def operation(self, ctx):
        self.n += 1
        if self.n % 2 == 0:  # skip every second unit of work
            return {"ok": True, "units": 1}
        self.done += 1
        self.acc.update(b"%d;" % self.n)
        return {"ok": True, "units": 1}

    def teardown(self, ctx):
        pass

    def verify(self, ctx):
        ok = self.done == self.n  # an honest workload performs every reported op
        return {
            "status": "PASS" if ok else "FAIL",
            "digest": self.acc.hexdigest(),
            "summary": {"reported": self.n, "actually_done": self.done},
            "fail_reasons": []
            if ok
            else ["partial work: %d reported, %d performed" % (self.n, self.done)],
        }


def build(ctx):
    return W(ctx)
'''

WORKLOAD_FAILING_OPS = WORKLOAD_OK.replace(
    'return {"ok": True, "units": 1}',
    'return {"ok": self.n % 3 != 0, "units": 1, "reason": None if self.n % 3 != 0 else "test failure"}',
)

WORKLOAD_SOCKET = WORKLOAD_OK.replace(
    'self.acc.update(b"%d;" % self.n)',
    'import socket as _s; _s.socket(_s.AF_INET, _s.SOCK_STREAM); self.acc.update(b"%d;" % self.n)',
)

WORKLOAD_SELF_MODIFY = '''
"""TEST workload: modifies its own file during setup -> end-of-run DRIFT."""
import hashlib
from pathlib import Path


class W:
    def __init__(self, ctx):
        self.ctx = ctx
        self.n = 0
        self.acc = hashlib.sha256()

    def setup(self, ctx):
        with open(__file__, "a", encoding="utf-8") as handle:
            handle.write("\\n# self-modified during setup (TEST drift probe)\\n")

    def operation(self, ctx):
        self.n += 1
        self.acc.update(b"%d;" % self.n)
        return {"ok": True, "units": 1}

    def teardown(self, ctx):
        pass

    def verify(self, ctx):
        return {
            "status": "PASS",
            "digest": self.acc.hexdigest(),
            "summary": {"n": self.n},
            "fail_reasons": [],
        }


def build(ctx):
    return W(ctx)
'''

WORKLOAD_SQLITE = '''
"""TEST workload using the harness-provisioned temp SQLite resource."""
import hashlib
import sqlite3


class W:
    def __init__(self, ctx):
        self.ctx = ctx
        self.conn = None
        self.n = 0

    def setup(self, ctx):
        self.db_path = str(ctx.resources["temp_sqlite_path"])
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        self.conn.commit()

    def operation(self, ctx):
        self.n += 1
        self.conn.execute("INSERT INTO t (v) VALUES (?)", ("r%d" % self.n,))
        return {"ok": True, "units": 1}

    def teardown(self, ctx):
        self.conn.commit()
        self.conn.close()

    def verify(self, ctx):
        count = self.conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        digest = hashlib.sha256(("n=%d" % count).encode("utf-8")).hexdigest()
        return {
            "status": "PASS" if count == self.n else "FAIL",
            "digest": digest,
            "summary": {"rows": count, "db_path": self.db_path},
            "fail_reasons": [],
        }


def build(ctx):
    return W(ctx)
'''

WORKLOAD_REPORTS_OWN_TIME = WORKLOAD_OK.replace(
    'return {"ok": True, "units": 1}',
    'return {"ok": True, "units": 1, "duration_ns": 12345}',
)


def write_workload(root: Path, name: str, body: str, sentinel: Optional[Path] = None) -> str:
    """Write a workload module under ``<root>/workloads/``; returns rel path
    with forward slashes (C20 manifests require forward-slash paths)."""
    if sentinel is not None:
        body = body.replace("_IMPORT_SENTINEL", repr(str(sentinel)))
    (root / "workloads").mkdir(parents=True, exist_ok=True)
    rel = "workloads/%s.py" % name
    (root / rel).write_text(body, encoding="utf-8", newline="\n")
    return rel


# ---------------------------------------------------------------------------
# case manifest / source-map builders
# ---------------------------------------------------------------------------

def make_manifest(
    root: Path,
    case_id: str,
    workload_rel: str,
    *,
    origin: str = "SYNTHETIC",
    backend: str = "python-inprocess",
    eligible: bool = False,
    measurement: Optional[Dict[str, Any]] = None,
    resources: Optional[Dict[str, Any]] = None,
    expected_digest: Optional[str] = None,
    smoke: Optional[Dict[str, Any]] = None,
    formal: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    measured_modules: Optional[List[str]] = None,
    extra_fixture: Optional[str] = None,
) -> Tuple[Dict[str, Any], str]:
    meas = {
        "sample_mode": "per_operation",
        "units": "iterations",
        "units_per_operation": 1,
        "min_operations": 1,
        "batch_size": 1000,
        "reset_method": "none",
        "includes_io": False,
    }
    meas.update(measurement or {})
    res = {"network": False, "production_db": False, "temp_sqlite": False}
    res.update(resources or {})
    fixtures = [{"path": workload_rel, "role": "workload"}]
    if extra_fixture:
        fixtures.append({"path": extra_fixture, "role": "input"})
    manifest = {
        "schema": C.CASE_MANIFEST_SCHEMA,
        "case_id": case_id,
        "description": "TEST synthetic case (TEST_SIMULATED context)",
        "origin": origin,
        "domain": case_id.split("/")[0],
        "backend": backend,
        "eligible_for_baseline": eligible,
        "algorithm_version": "test-alg-1",
        "policy_version": "test-policy-1",
        "workload": {"source": workload_rel, "entrypoint": "build"},
        "measured_modules": measured_modules if measured_modules is not None else [],
        "fixtures": fixtures,
        "params": params or {"seed": 1},
        "measurement": meas,
        "resources": res,
        "environment": {"depends_on": []},
        "correctness": {"kind": "digest", "expected_digest": expected_digest, "normalizations": []},
        "budget": {
            "smoke": smoke or {"warmup_operations": 0, "operations": 10, "case_timeout_s": 30},
            "formal": formal
            or {
                "batches": 5,
                "warmup_operations": 0,
                "operations_per_batch": 20,
                "case_timeout_s": 600,
            },
        },
    }
    C.validate_case_manifest(manifest)
    (root / "cases").mkdir(parents=True, exist_ok=True)
    rel = "cases/%s.json" % case_id.replace("/", "__")
    (root / rel).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest, rel


def make_source_map(root: Path, entries: List[Tuple[str, str]]) -> str:
    """entries: [(logical_module, relative_path)]; hashes computed from disk."""
    modules = []
    for logical, rel in entries:
        data = (root / rel).read_bytes()
        modules.append(
            {
                "logical_module": logical,
                "source_relative_path": rel,
                "sha256": C.sha256_bytes(data),
                "bytes": len(data),
                "owner": "NF20-TEST",
                "release_id": "WORKTREE_SNAPSHOT",
                "provenance_kind": "WORKTREE_SNAPSHOT",
            }
        )
    source_map = {
        "schema": C.SOURCE_MAP_SCHEMA,
        "description": "TEST source map",
        "modules": modules,
    }
    rel = "source-map.json"
    (root / rel).write_text(
        json.dumps(source_map, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    return rel


def load_manifest(root: Path, rel: str) -> Dict[str, Any]:
    manifest = C.load_json_file(root / rel, "case manifest")
    return C.validate_case_manifest(manifest)


# ---------------------------------------------------------------------------
# CLI runner (subprocess; cwd = repository root)
# ---------------------------------------------------------------------------

def run_cli(
    args: List[str],
    *,
    env_extra: Optional[Dict[str, str]] = None,
    timeout: float = 180.0,
    cwd: Optional[Path] = None,
) -> Tuple[int, str, str]:
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "tools.benchmarks.cli"] + args,
        cwd=str(cwd or REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def load_case_record(output_root: Path, run_id: str, case_id: str) -> Dict[str, Any]:
    path = output_root / "runs" / run_id / "results" / ("%s.json" % case_id.replace("/", "__"))
    record = C.load_json_file(path, "case record")
    C.validate_result_record(record)
    return record


def only_run_id(output_root: Path) -> str:
    runs = sorted((output_root / "runs").iterdir())
    assert len(runs) == 1, "expected exactly one run dir, got %s" % [p.name for p in runs]
    return runs[0].name


# ---------------------------------------------------------------------------
# frozen result-record builder for comparison state-machine tests
# ---------------------------------------------------------------------------

_SHA_A = "a" * 64
_SHA_B = "b" * 64


def build_result_record(
    *,
    run_id: str = "run-cand-0001",
    case_id: str = "t/case1",
    status: str = "OK",
    scope: str = "formal",
    origin: str = C.ORIGIN_TEST_SIMULATED,
    backend: str = "python-inprocess",
    host_id: str = "host0123456789abcd",
    samples: Optional[List[int]] = None,
    median_ns: Optional[int] = None,
    throughput_units_per_s: Optional[float] = None,
    sample_mode: str = "per_operation",
    fixture_sha: str = _SHA_A,
    source_sha: str = _SHA_A,
    source_map_sha: str = _SHA_A,
    params: Optional[Dict[str, Any]] = None,
    measurement_spec: Optional[Dict[str, Any]] = None,
    measured_module: str = "t.prod.thing",
    eligible: bool = True,
    n: Optional[int] = None,
    measurement_over: Optional[Dict[str, Any]] = None,
    identity_over: Optional[Dict[str, Any]] = None,
    environment_over: Optional[Dict[str, Any]] = None,
    min_operations: int = 100,
    batch_size: int = 100,
    batch_count: int = 1,
    status_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a structurally valid result record with simulated timing.

    ``samples`` defaults to 100 identical 1_000_000 ns samples so median/MAD
    are deterministic; pass explicit vectors for hand-computed expectations.
    """
    if samples is None:
        samples = [1_000_000] * 100
    stats = C.latency_stats(samples, sample_mode)
    units = [1] * len(samples)
    throughput = C.throughput_stats(samples, units)
    spec = {
        "sample_mode": sample_mode,
        "units": "iterations",
        "units_per_operation": 1,
        "min_operations": min_operations,
        "batch_size": batch_size,
        "reset_method": "none",
        "includes_io": False,
    }
    spec.update(measurement_spec or {})
    identity = {
        "source_map": {"path": "source-map.json", "sha256": source_map_sha},
        "case_manifest": {"path": "cases/t__case1.json", "sha256": _SHA_A},
        "suite": None,
        "fixtures": [{"path": "fixtures/input.bin", "role": "input", "sha256": fixture_sha, "bytes": 10}],
        "workload": {"source": "workloads/w.py", "sha256": _SHA_A},
        "measured_modules": [
            {
                "logical_module": measured_module,
                "source_relative_path": "src/t/prod/thing.py",
                "sha256": source_sha,
                "release_id": "WORKTREE_SNAPSHOT",
                "provenance_kind": "WORKTREE_SNAPSHOT",
            }
        ],
        "contract_version": C.CONTRACT_VERSION,
        "algorithm_version": "test-alg-1",
        "policy_version": "test-policy-1",
        "workload_params": params or {"seed": 1},
        "measurement_spec": spec,
        "eligible_for_baseline": eligible,
    }
    identity.update(identity_over or {})
    environment = {
        "python_version": "3.12.10",
        "python_implementation": "CPython",
        "os": "Windows-11",
        "platform": "Windows-11-x86_64",
        "machine_arch": "AMD64",
        "cpu_brand": "TEST CPU",
        "cpu_count": 8,
        "host_id": host_id,
        "timer": C.TIMER_NAME,
        "pythonhashseed": "1",
        "process_count": 1,
        "dependencies": {},
    }
    environment.update(environment_over or {})
    measurement = {
        "sample_mode": sample_mode,
        "units": "iterations",
        "units_per_operation": 1,
        "includes_io": False,
        "reset_method": "none",
        "batch_size": batch_size,
        "planned_operations": sum(units),
        "executed_operations": sum(units),
        "batches": [
            {"index": i, "process": "subprocess", "sample_count": len(samples), "failure_count": 0,
             "digest": _SHA_A, "elapsed_ns": 1, "rc": 0, "timed_out": False}
            for i in range(batch_count)
        ],
        "samples_ns": samples,
        "failures": {"count": 0, "samples": [], "truncated": False},
        "n": stats["n"],
        "median_ns": stats["median_ns"],
        "p95_ns": stats["p95_ns"],
        "p95_note": stats["p95_note"],
        "min_ns": stats["min_ns"],
        "max_ns": stats["max_ns"],
        "mad_ns": stats["mad_ns"],
        "total_units": throughput["total_units"],
        "total_measured_ns": throughput["total_measured_ns"],
        "throughput_units_per_s": throughput["throughput_units_per_s"],
        "throughput_denominator": throughput["throughput_denominator"],
        "throughput_note": throughput["throughput_note"],
    }
    measurement.update(measurement_over or {})
    if median_ns is not None:
        measurement["median_ns"] = median_ns
    if throughput_units_per_s is not None:
        measurement["throughput_units_per_s"] = throughput_units_per_s
    if n is not None:
        measurement["n"] = n
    record = {
        "schema_version": C.RESULT_SCHEMA_VERSION,
        "contract_version": C.CONTRACT_VERSION,
        "run_id": run_id,
        "case_id": case_id,
        "domain": case_id.split("/")[0],
        "backend": backend,
        "scope": scope,
        "origin": origin,
        "status": status,
        "status_reason": status_reason,
        "identity": identity,
        "environment": environment,
        "phases": {"setup_ns": 1, "warmup_ns": 1, "measured_ns": 1, "teardown_ns": 1},
        "measurement": measurement,
        "correctness": {
            "kind": "digest",
            "status": "PASS",
            "digest": _SHA_A,
            "expected_digest": None,
            "summary": {},
            "fail_reasons": [],
            "normalizations": [],
            "repeat_consistent": True,
        },
        "notes": ["TEST_SIMULATED record built by tests/benchmarks/conftest.py"],
        "started_at": "2026-09-24T00:00:00Z",
        "finished_at": "2026-09-24T00:00:01Z",
    }
    C.validate_result_record(record)
    return record


def write_run(
    output_root: Path,
    run_id: str,
    records: List[Dict[str, Any]],
    *,
    profile: str = "formal",
) -> Path:
    """Write a minimal run directory holding the given case records."""
    run_dir = output_root / "runs" / run_id
    (run_dir / "results").mkdir(parents=True, exist_ok=True)
    for record in records:
        (run_dir / "results" / ("%s.json" % record["case_id"].replace("/", "__"))).write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
    run_summary = {
        "schema": "ak-benchmark-run-summary/1.0.0",
        "run_id": run_id,
        "profile": profile,
        "cases": [{"case_id": r["case_id"], "status": r["status"]} for r in records],
    }
    (run_dir / "run.json").write_text(
        json.dumps(run_summary, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return run_dir


def write_policy(
    path: Path,
    cases: Dict[str, Dict[str, Any]],
    *,
    fallback_false: bool = True,
) -> Path:
    policy = {
        "schema": C.BASELINE_POLICY_SCHEMA,
        "fallback_thresholds": not fallback_false,
        "cases": cases,
    }
    path.write_text(
        json.dumps(policy, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    return path


def default_policy_entry(**overrides: Any) -> Dict[str, Any]:
    entry = {
        "metric": "median_ns",
        "relative_tolerance": 0.10,
        "noise_cap_relative": 0.02,
        "absolute_floor_ns": 100_000,
        "sampling_version": "test-sampling-1",
        "registered_by": "nf20-test",
        "baseline_evidence": ["run-b1", "run-b2"],
        "baseline_version": "v1",
    }
    entry.update(overrides)
    return entry


@pytest.fixture()
def mini_root(tmp_path: Path) -> Path:
    (tmp_path / "workloads").mkdir()
    (tmp_path / "cases").mkdir()
    return tmp_path
