"""NF20 benchmark runner - executes C20 cases under frozen budgets.

Implements the execution half of the C20 contract:

* H20.2  explicit inputs: source-map / case manifests / suite are hashed and
  containment-checked BEFORE the module-under-test is imported; network and
  production DB entry points are refused (declaration gate + runtime socket
  guard); cleanup touches only paths this run created.
* H20.3  timing: the only performance clock is ``time.perf_counter_ns``,
  harness-owned; phases are split (setup / warmup / measured / teardown);
  PYTHONHASHSEED and the workload seed are fixed and recorded before any
  subprocess starts; workload-reported durations are never used as samples.
* H20.4  budgets: smoke per-case <= 30 s and suite <= 180 s; formal runs use
  independent subprocess batches (default >= 5 x >= 20 samples) with a
  per-case wall cap; TIMEOUT keeps raw data and is never retried in-run.
* H20.6  correctness first: frozen digest verification runs before any
  result can be ranked OK; failed work is INVALID, never silently dropped.
  C20-1.0.1 (NF20-HARD-r1 finding F01): in formal mode every batch's
  verify() PASS/FAIL status is propagated into ``measurement.batches[]``
  (``verify_status``) and ANY batch whose frozen rule returned FAIL marks
  the whole case INVALID - a later batch may FAIL on a predicate the digest
  does not cover while emitting the same digest as the other batches.
* Q51-W02 (C6 cross-batch carrier, D02 paras 4-5, 2026-09-26): formal-mode
  ``correctness.summary`` now carries ``batch_verifications`` - one entry
  per planned batch holding ``batch_index`` plus the COMPLETE ``verify``
  object that batch's child process produced (``verify.summary.sqlite_facts``
  are the C6 raw facts; trace facts such as process ids, ``id(dbapi
  connection)`` and temp paths stay verbatim and are never required to be
  equal across processes), or an explicit ``missing_reason`` when the batch
  produced no verify object (not started / killed / child failed / verify
  not run) - never a default empty object.  The summary keys OUTSIDE the
  carrier remain, by frozen semantics, ONLY the first available batch's
  verify summary (backward compatibility) and must not be read as
  whole-round facts; this scope is annotated via ``sqlite_facts_scope``
  whenever sqlite facts are present.  A workload verify() summary may not
  use the reserved carrier keys - collisions fail the case closed (ERROR).
  Smoke semantics are unchanged: smoke records carry no carrier.
* H20.10 end-of-run identity re-hash: shared-worktree drift ends the attempt
  with a preserved DRIFT result.

Standard library only.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from tools.benchmarks import contracts as C

RUN_SUMMARY_SCHEMA = "ak-benchmark-run-summary/1.0.0"
BATCH_PARTIAL_SCHEMA = "ak-benchmark-batch-partial/1.0.0"

MAX_STORED_FAILURE_SAMPLES = 20
TRACEBACK_TAIL_LINES = 15

# Q51-W02 (C6 carrier): keys the harness itself writes into the formal-mode
# correctness.summary.  A workload verify() summary that already uses one of
# these names is a carrier collision and fails the case closed (ERROR) -
# harness carrier facts and workload facts may never silently override each
# other.
RESERVED_SUMMARY_KEYS = (
    "batch_verifications",
    "batch_verifications_semantics",
    "sqlite_facts_scope",
)

# Q51-W02 (D02 para 4): coarse, explicit cause vocabulary for
# batch_verifications entries whose batch produced no verify object
# ("缺失项明确记录缺失原因").  Each entry carries exactly one of these
# strings in ``missing_reason``; the per-batch execution note in
# ``measurement.batches[]`` keeps the authoritative detail.
MISSING_VERIFY_REASONS = {
    "not_started": "BATCH_NOT_STARTED_CASE_DEADLINE",
    "killed_no_partial": "VERIFY_MISSING_BATCH_KILLED_NO_PARTIAL",
    "child_failed_no_partial": "VERIFY_MISSING_BATCH_CHILD_FAILED_NO_PARTIAL",
    "not_run_in_partial": "VERIFY_NOT_RUN_IN_BATCH_PARTIAL",
    "unclassified": "VERIFY_MISSING_UNCLASSIFIED",
}


class HarnessExecutionError(Exception):
    """Execution-class failure -> case status ERROR (run exit code 3)."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_run_id() -> str:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = os.urandom(4).hex()
    return "run-%s-%s" % (stamp, suffix)


def case_slug(case_id: str) -> str:
    return case_id.replace("/", "__")


class Deadline:
    """Wall-clock budget on ``time.monotonic`` (used for budgets only;
    performance samples always come from ``perf_counter_ns``)."""

    def __init__(self, budget_s: float) -> None:
        self._start = time.monotonic()
        self._budget_s = float(budget_s)

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def remaining(self) -> float:
        return self._budget_s - self.elapsed()

    def expired(self) -> bool:
        return self.remaining() <= 0


# ---------------------------------------------------------------------------
# H20.2 network guard - offline harness: refuse real sockets during case
# execution (workloads may only use explicitly declared temp SQLite files).
# ---------------------------------------------------------------------------

def _network_refused(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("network disabled by NF20 benchmark harness (H20.2 offline-only)")


class NetworkGuard:
    """Patches ``socket.socket``/``create_connection``/``getaddrinfo`` for the
    duration of case execution and restores them afterwards."""

    _targets = ("socket", "create_connection", "getaddrinfo")

    def __enter__(self) -> "NetworkGuard":
        self._saved: Dict[str, Any] = {}
        for name in self._targets:
            self._saved[name] = getattr(socket, name)
            setattr(socket, name, _network_refused)
        return self

    def __exit__(self, *exc: Any) -> None:
        for name, original in self._saved.items():
            setattr(socket, name, original)


# ---------------------------------------------------------------------------
# identity hashing (all of it happens BEFORE importing the workload)
# ---------------------------------------------------------------------------

def hash_relative(root: Path, rel: str) -> Dict[str, Any]:
    path = C.resolve_under_root(root, rel)
    if not path.is_file():
        raise C.ContractError("input file missing: %s" % rel)
    sha, size = C.sha256_file(path)
    return {"path": rel, "sha256": sha, "bytes": size}


def verify_source_map_hashes(root: Path, source_map: Dict[str, Any]) -> None:
    """Verify every source-map entry (containment + sha256 + size).

    Runs before anything under test is imported; any mismatch aborts the
    whole run (usage-class, rc 2): nothing has been measured yet, so there
    is no raw data to preserve.  Mid-run drift is detected separately by
    the end-of-run re-hash (H20.10).
    """
    for entry in source_map["modules"]:
        rel = str(entry["source_relative_path"])
        path = C.resolve_under_root(root, rel)
        if not path.is_file():
            raise C.ContractError(
                "SOURCE_MISSING: %s (logical %s)" % (rel, entry["logical_module"])
            )
        sha, size = C.sha256_file(path)
        if sha != entry["sha256"] or size != entry["bytes"]:
            raise C.ContractError(
                "SOURCE_HASH_MISMATCH: %s (logical %s): expected %s/%d B, found "
                "%s/%d B - worktree drifted from the recorded source map"
                % (rel, entry["logical_module"], entry["sha256"], entry["bytes"], sha, size)
            )


def build_case_identity(
    root: Path,
    manifest: Dict[str, Any],
    manifest_path: str,
    manifest_sha: str,
    source_map: Dict[str, Any],
    source_map_path: str,
    source_map_sha: str,
    suite_info: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Assemble the frozen H20.1 identity block (everything hashed, nothing
    imported).  Raises ContractError on missing/tampered inputs."""
    fixtures: List[Dict[str, Any]] = []
    for fixture in manifest.get("fixtures", []):
        rel = str(fixture["path"])
        entry = hash_relative(root, rel)
        entry["role"] = fixture["role"]
        fixtures.append(entry)
    workload_entry = hash_relative(root, str(manifest["workload"]["source"]))

    by_logical = {str(e["logical_module"]): e for e in source_map["modules"]}
    measured: List[Dict[str, Any]] = []
    for logical in manifest.get("measured_modules", []):
        if logical not in by_logical:
            raise C.ContractError(
                "measured module %r not present in the provided source map "
                "(H20.2 explicit-input binding)" % logical
            )
        entry = by_logical[logical]
        measured.append(
            {
                "logical_module": logical,
                "source_relative_path": str(entry["source_relative_path"]),
                "sha256": entry["sha256"],
                "release_id": entry["release_id"],
                "provenance_kind": entry["provenance_kind"],
            }
        )

    return {
        "source_map": {"path": source_map_path, "sha256": source_map_sha},
        "case_manifest": {"path": manifest_path, "sha256": manifest_sha},
        "suite": suite_info,
        "fixtures": fixtures,
        "workload": {
            "source": manifest["workload"]["source"],
            "sha256": workload_entry["sha256"],
        },
        "measured_modules": measured,
        "contract_version": C.CONTRACT_VERSION,
        "algorithm_version": manifest.get("algorithm_version", C.UNKNOWN),
        "policy_version": manifest.get("policy_version", C.UNKNOWN),
        "workload_params": manifest.get("params", {}),
        "measurement_spec": manifest["measurement"],
        "eligible_for_baseline": bool(manifest["eligible_for_baseline"]),
    }


INVARIANT_KINDS = ("case_manifest", "suite", "source_map", "fixture", "workload")


def drift_recheck(root: Path, identity: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]]]:
    """End-of-run H20.10 re-hash of every invariant input.

    Source-map *entries* are informational only: measuring a candidate whose
    sources differ from a baseline's is the purpose of the protocol.  The
    invariant set is the case manifest, the suite, the fixtures, the
    workload file and the source-map file itself.
    """
    problems: List[Dict[str, Any]] = []
    checks: List[Tuple[str, str, str]] = []
    cm = identity["case_manifest"]
    checks.append(("case_manifest", cm["path"], cm["sha256"]))
    if identity.get("suite"):
        checks.append(("suite", identity["suite"]["path"], identity["suite"]["sha256"]))
    checks.append(("source_map", identity["source_map"]["path"], identity["source_map"]["sha256"]))
    for fixture in identity["fixtures"]:
        checks.append(("fixture", fixture["path"], fixture["sha256"]))
    checks.append(("workload", identity["workload"]["source"], identity["workload"]["sha256"]))
    for kind, rel, expected_sha in checks:
        try:
            info = hash_relative(root, rel)
        except C.ContractError as exc:
            problems.append({"kind": kind, "path": rel, "state": "MISSING", "detail": str(exc)})
            continue
        if info["sha256"] != expected_sha:
            problems.append(
                {
                    "kind": kind,
                    "path": rel,
                    "state": "CHANGED",
                    "detail": "expected %s found %s" % (expected_sha, info["sha256"]),
                }
            )
    return (not problems), problems


# ---------------------------------------------------------------------------
# workload loading (only called after every hash check passed)
# ---------------------------------------------------------------------------

_MODULE_CACHE: Dict[str, Any] = {}


def load_workload(root: Path, manifest: Dict[str, Any]) -> Any:
    """Import the workload module from its file path (no sys.path mutation)
    and return the callable named by ``workload.entrypoint`` (default build)."""
    rel = str(manifest["workload"]["source"])
    path = C.resolve_under_root(root, rel)
    entrypoint = str(manifest["workload"].get("entrypoint", "build"))
    module = _MODULE_CACHE.get(str(path))
    if module is None:
        mod_name = "benchmarks_workload.%s" % re.sub(r"[^0-9A-Za-z_]", "_", rel)
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise HarnessExecutionError("cannot create import spec for workload %s" % rel)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - recorded honestly
            raise HarnessExecutionError("workload import failed (%s): %s" % (rel, exc)) from exc
        _MODULE_CACHE[str(path)] = module
    factory = getattr(module, entrypoint, None)
    if not callable(factory):
        raise HarnessExecutionError(
            "workload module %s has no callable entrypoint %r" % (rel, entrypoint)
        )
    return factory


def make_case_context(manifest: Dict[str, Any], tmp_dir: Path) -> C.CaseContext:
    resources: Dict[str, Any] = {
        "temp_sqlite_path": (tmp_dir / "temp_case.sqlite")
        if manifest["resources"].get("temp_sqlite")
        else None
    }
    return C.CaseContext(
        params=manifest.get("params", {}),
        seed=int(manifest["params"]["seed"]),
        tmp_dir=tmp_dir,
        resources=resources,
        normalizations=list(manifest["correctness"].get("normalizations", [])),
    )


# ---------------------------------------------------------------------------
# in-process phase execution (shared by smoke runs and formal batch children)
# ---------------------------------------------------------------------------

def _validate_operation_result(result: Any, spec: Dict[str, Any]) -> Tuple[bool, int, Optional[str]]:
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        raise HarnessExecutionError(
            "operation/batch result must be a dict with boolean 'ok', got %r" % (result,)
        )
    units = result.get("units", spec["units_per_operation"])
    if not isinstance(units, int) or isinstance(units, bool) or units < 0:
        raise HarnessExecutionError("operation 'units' must be a non-negative integer")
    reason = result.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise HarnessExecutionError("operation 'reason' must be a string or null")
    return result["ok"], units, reason


def execute_inprocess(
    workload: Any,
    ctx: C.CaseContext,
    spec: Dict[str, Any],
    warmup_operations: int,
    operations: int,
    deadline: Deadline,
) -> Dict[str, Any]:
    """Run setup -> warmup -> measured -> verify -> teardown for one process.

    Phase order and semantics (frozen):

    * ``setup`` runs once.
    * warmup runs once per process, is never timed into samples.
    * measured: for ``per_operation`` cases each ``operation(ctx)`` call is
      individually timed; batches of ``batch_size`` operations are separated
      by ``reset(ctx)`` when ``reset_method == batch_reset``.  For
      ``amortized`` cases ``batch(ctx, n)`` is called with ``n =
      batch_size`` (or the remainder) and the whole call is one sample;
      ``operations`` counts planned units.
    * ``verify`` is skipped when the deadline hit (correctness NOT_RUN).
    * ``teardown`` always runs (its own failure is only a note).
    """
    out: Dict[str, Any] = {
        "phases": {"setup_ns": 0, "warmup_ns": 0, "measured_ns": 0, "teardown_ns": 0},
        "samples": [],
        "sample_units": [],
        "failures": [],
        "failure_count": 0,
        "executed": 0,
        "planned": operations,
        "verify": None,
        "timed_out": False,
        "exception": None,
        "notes": [],
    }
    amortized = spec["sample_mode"] == C.SAMPLE_MODE_AMORTIZED

    def record_failure(batch_index: int, reason: Optional[str]) -> None:
        out["failure_count"] += 1
        if len(out["failures"]) < MAX_STORED_FAILURE_SAMPLES:
            out["failures"].append({"batch_index": batch_index, "reason": reason or "operation_failed"})

    try:
        _, out["phases"]["setup_ns"] = _phase_ns(lambda: workload.setup(ctx))

        # ---- warmup (never sampled) ----
        def _warmup() -> None:
            if warmup_operations <= 0:
                return
            if deadline.expired():
                out["timed_out"] = True
                return
            if amortized:
                result = workload.batch(ctx, warmup_operations)
                ok, _units, reason = _validate_operation_result(result, spec)
                if not ok:
                    out["notes"].append("WARMUP_BATCH_REPORTED_NOT_OK: %s" % (reason or "",))
                return
            for _ in range(warmup_operations):
                if deadline.expired():
                    out["timed_out"] = True
                    return
                result = workload.operation(ctx)
                ok, _units, reason = _validate_operation_result(result, spec)
                if not ok:
                    out["notes"].append("WARMUP_OPERATION_REPORTED_NOT_OK: %s" % (reason or "",))

        _, out["phases"]["warmup_ns"] = _phase_ns(_warmup)

        # ---- measured ----
        def _measured() -> None:
            if out["timed_out"]:
                return
            if amortized:
                batch_index = 0
                remaining = operations
                while remaining > 0:
                    if deadline.expired():
                        out["timed_out"] = True
                        return
                    if spec["reset_method"] == C.RESET_METHOD_BATCH_RESET:
                        workload.reset(ctx)
                    n = min(int(spec["batch_size"]), remaining)
                    t0 = time.perf_counter_ns()
                    result = workload.batch(ctx, n)
                    t1 = time.perf_counter_ns()
                    ok, units, reason = _validate_operation_result(result, spec)
                    out["executed"] += units
                    if ok:
                        out["samples"].append(t1 - t0)
                        out["sample_units"].append(units)
                    else:
                        record_failure(batch_index, reason)
                        return  # failed batch aborts the measured phase
                    remaining -= n
                    batch_index += 1
                return
            done = 0
            batch_index = 0
            while done < operations:
                if deadline.expired():
                    out["timed_out"] = True
                    return
                if spec["reset_method"] == C.RESET_METHOD_BATCH_RESET:
                    workload.reset(ctx)
                in_batch = 0
                while in_batch < int(spec["batch_size"]) and done < operations:
                    if deadline.expired():
                        out["timed_out"] = True
                        return
                    t0 = time.perf_counter_ns()
                    result = workload.operation(ctx)
                    t1 = time.perf_counter_ns()
                    ok, units, reason = _validate_operation_result(result, spec)
                    out["executed"] += 1
                    if ok:
                        out["samples"].append(t1 - t0)
                        out["sample_units"].append(units)
                    else:
                        record_failure(batch_index, reason)
                    done += 1
                    in_batch += 1
                batch_index += 1

        _, out["phases"]["measured_ns"] = _phase_ns(_measured)

        if (
            not out["timed_out"]
            and not out["exception"]
            and out["executed"] < operations
            and out["failure_count"] == 0
        ):
            raise HarnessExecutionError(
                "SAMPLE_SHORTFALL: executed %d of %d planned operations without "
                "a timeout or a recorded failure" % (out["executed"], operations)
            )

        # ---- correctness (H20.6) - skipped on timeout ----
        if not out["timed_out"]:
            verification, _ = _phase_ns(lambda: workload.verify(ctx))
            try:
                out["verify"] = C.validate_verification_result(verification)
            except C.ContractError as exc:
                raise HarnessExecutionError(
                    "verify() returned malformed result: %s" % exc
                ) from exc
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        out["exception"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback_tail": traceback.format_exc().splitlines()[-TRACEBACK_TAIL_LINES:],
        }
    finally:
        try:
            _, out["phases"]["teardown_ns"] = _phase_ns(lambda: workload.teardown(ctx))
        except Exception as exc:  # noqa: BLE001
            out["notes"].append("TEARDOWN_ERROR: %s: %s" % (type(exc).__name__, exc))
    return out


def _phase_ns(fn: Callable[[], Any]) -> Tuple[Any, int]:
    t0 = time.perf_counter_ns()
    value = fn()
    t1 = time.perf_counter_ns()
    if t1 < t0:
        raise HarnessExecutionError(
            "TIMER_REGRESSION: perf_counter_ns went backwards (%d -> %d)" % (t0, t1)
        )
    return value, t1 - t0


# ---------------------------------------------------------------------------
# formal profile: independent subprocess batches
# ---------------------------------------------------------------------------

def execute_formal_batches(
    root: Path,
    run_dir: Path,
    case_id: str,
    manifest_path: str,
    manifest: Dict[str, Any],
    source_map_path: str,
    deadline: Deadline,
    pythonhashseed: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Spawn one fresh Python process per batch (H20.4 independence).

    Returns ``(batch_records, partials)``.  A child killed on the case
    deadline surfaces as a timed-out batch record with whatever partial data
    was already flushed (raw data preserved, never retried in-run).
    """
    formal = manifest["budget"]["formal"]
    batches = int(formal["batches"])
    ops_per_batch = int(formal["operations_per_batch"])
    warmup = int(formal["warmup_operations"])
    tmp_dir = run_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    batch_records: List[Dict[str, Any]] = []
    partials: List[Dict[str, Any]] = []
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = pythonhashseed  # fixed BEFORE subprocess start (H20.3)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Make the harness package importable in the child regardless of --root:
    # this PYTHONPATH entry only exposes tools/benchmarks itself.  Workload
    # code is imported by absolute file path and measured production modules
    # come from the hash-verified source map; no measured code is ever
    # resolved through this path.
    harness_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = (
        harness_root + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else harness_root
    )

    for index in range(batches):
        if deadline.expired():
            for remaining in range(index, batches):
                batch_records.append(
                    {
                        "index": remaining,
                        "process": "subprocess",
                        "sample_count": 0,
                        "failure_count": 0,
                        "digest": None,
                        "verify_status": None,
                        "elapsed_ns": 0,
                        "timed_out": True,
                        "note": "CASE_DEADLINE_REACHED_BEFORE_BATCH",
                    }
                )
            break
        partial_path = tmp_dir / ("%s-batch-%02d.json" % (case_slug(case_id), index))
        cmd = [
            sys.executable, "-B", "-m", "tools.benchmarks.cli", "batch",
            "--case-manifest", manifest_path,
            "--source-map", source_map_path,
            "--partial-out", str(partial_path),
            "--batch-index", str(index),
            "--warmup-operations", str(warmup),
            "--operations", str(ops_per_batch),
            "--root", str(root),
        ]
        batch_t0 = time.perf_counter_ns()
        proc = subprocess.Popen(cmd, cwd=str(root), env=env)
        try:
            proc.wait(timeout=max(0.1, deadline.remaining()))
            rc: Optional[int] = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            rc = None
        elapsed = time.perf_counter_ns() - batch_t0
        partial: Optional[Dict[str, Any]] = None
        if partial_path.is_file():
            try:
                partial = C.load_json_file(partial_path, "batch partial")
            except C.ContractError:
                partial = None
        # C20-1.0.1 (F01): carry the batch's verify() PASS/FAIL status into
        # the batch record so the parent can fail the case even when the
        # digest string matches the other batches.  ``partial["verify"]`` is
        # None when the child's own deadline expired before verify ran
        # (a timed-out partial is still written); guard instead of crashing.
        partial_verify = (partial.get("verify") or {}) if partial else {}
        record: Dict[str, Any] = {
            "index": index,
            "process": "subprocess",
            "sample_count": len(partial["samples"]) if partial else 0,
            "failure_count": int(partial["failure_count"]) if partial else 0,
            "digest": partial_verify.get("digest"),
            "verify_status": partial_verify.get("status"),
            "elapsed_ns": elapsed,
            "rc": rc,
            "timed_out": rc is None,
        }
        if partial is not None:
            partials.append(partial)  # raw data preserved even for failed batches
        if rc is None:
            record["note"] = "BATCH_KILLED_ON_CASE_DEADLINE"
        elif rc != 0:
            record["note"] = "BATCH_FAILED_RC_%s" % rc
        batch_records.append(record)
    return batch_records, partials


# ---------------------------------------------------------------------------
# Q51-W02 (C6 carrier): whole-round per-batch verification evidence
# ---------------------------------------------------------------------------

def _classify_missing_verify(
    record: Dict[str, Any], partial: Optional[Dict[str, Any]]
) -> str:
    """Coarse, explicit cause for a batch that produced no verify object.

    ``partial`` is the batch's flushed partial JSON when one exists (its
    ``verify`` key is absent or None).  ``record`` is the batch record from
    ``measurement.batches[]`` and carries the authoritative execution note
    (killed on case deadline / child rc / never started).
    """
    if partial is not None:
        # A partial was flushed but carries no verify: the child's own
        # deadline expired before verify ran (a timed-out partial is still
        # written), or the child died between measured and verify.
        return MISSING_VERIFY_REASONS["not_run_in_partial"]
    note = str(record.get("note") or "")
    if "CASE_DEADLINE_REACHED_BEFORE_BATCH" in note:
        return MISSING_VERIFY_REASONS["not_started"]
    if record.get("timed_out"):
        return MISSING_VERIFY_REASONS["killed_no_partial"]
    rc = record.get("rc")
    if rc is not None and rc != 0:
        return MISSING_VERIFY_REASONS["child_failed_no_partial"]
    return MISSING_VERIFY_REASONS["unclassified"]


def _build_batch_verifications(
    batch_records: Sequence[Dict[str, Any]],
    partials: Sequence[Dict[str, Any]],
    planned_batches: int,
) -> List[Dict[str, Any]]:
    """Q51-W02 (D02 para 4) C6 cross-batch carrier: one entry per planned batch.

    Each entry is ``{"batch_index", "verify"}`` with the COMPLETE verify
    object that batch's child process produced - untouched: status, digest,
    summary (including ``sqlite_facts``), fail_reasons and any trace facts
    (process ids, ``id(dbapi_connection)``, temp paths) stay verbatim and
    are never normalized (D02 para 5: cross-process numeric equality is not
    required and must not be manufactured).  A batch without a verify object
    gets ``{"batch_index", "verify": None, "missing_reason": <explicit
    cause>}`` instead - never a default empty object (D02 para 3).

    Pairing: partials written by ``run_batch_child`` always carry
    ``batch_index``, so the real execution path pairs exactly by key.
    Partials from batch executors that omit ``batch_index`` are paired
    positionally with the earliest unpaired record that shows a verify hint
    (``digest`` or ``verify_status``), so verify FACTS are never dropped for
    legacy executor shapes; keyed data always wins over the fallback.
    Entries cover every planned batch index even when the executor returned
    no record for it (fail-visible, never fail-silent).
    """
    records_by_index: Dict[int, Dict[str, Any]] = {
        int(b["index"]): b for b in batch_records
    }
    keyed_verify: Dict[int, Dict[str, Any]] = {}
    keyed_partial: Dict[int, Dict[str, Any]] = {}
    unkeyed_with_verify: List[Dict[str, Any]] = []
    for partial in partials:
        verify = partial.get("verify")
        idx = partial.get("batch_index")
        if isinstance(idx, int) and not isinstance(idx, bool):
            keyed_partial[idx] = partial
            if verify is not None:
                keyed_verify[idx] = verify
        elif verify is not None:
            unkeyed_with_verify.append(verify)

    entries: List[Dict[str, Any]] = []
    indices = sorted(
        set(records_by_index) | set(range(max(0, int(planned_batches))))
    )
    unkeyed_iter = iter(unkeyed_with_verify)
    for index in indices:
        record = records_by_index.get(index)
        verify: Optional[Dict[str, Any]] = keyed_verify.get(index)
        partial_of_index: Optional[Dict[str, Any]] = keyed_partial.get(index)
        if verify is None and record is not None:
            hint = (
                record.get("digest") is not None
                or record.get("verify_status") is not None
            )
            if hint:
                for candidate in unkeyed_iter:  # take the next unkeyed verify
                    verify = candidate
                    break
        if verify is not None:
            entries.append({"batch_index": index, "verify": verify})
        else:
            reason = (
                _classify_missing_verify(record, partial_of_index)
                if record is not None
                else MISSING_VERIFY_REASONS["unclassified"]
            )
            entries.append(
                {"batch_index": index, "verify": None, "missing_reason": reason}
            )
    return entries


# ---------------------------------------------------------------------------
# case record assembly
# ---------------------------------------------------------------------------

def _empty_measurement(spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "sample_mode": spec["sample_mode"],
        "units": spec["units"],
        "units_per_operation": spec["units_per_operation"],
        "includes_io": spec["includes_io"],
        "reset_method": spec["reset_method"],
        "batch_size": spec["batch_size"],
        "planned_operations": 0,
        "executed_operations": 0,
        "batches": [],
        "samples_ns": [],
        "failures": {"count": 0, "samples": [], "truncated": False},
        "n": 0,
        "median_ns": None,
        "p95_ns": None,
        "p95_note": "NO_SAMPLES",
        "min_ns": None,
        "max_ns": None,
        "mad_ns": None,
        "total_units": 0,
        "total_measured_ns": 0,
        "throughput_units_per_s": None,
        "throughput_denominator": "successful_operation_units",
        "throughput_note": "NO_SAMPLES",
    }


def _finalize_measurement(
    spec: Dict[str, Any],
    planned: int,
    executed: int,
    samples_ns: Sequence[int],
    sample_units: Sequence[int],
    failures: Sequence[Dict[str, Any]],
    failure_count: int,
    batch_records: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    measurement = _empty_measurement(spec)
    measurement["planned_operations"] = planned
    measurement["executed_operations"] = executed
    measurement["batches"] = list(batch_records)
    measurement["samples_ns"] = [int(s) for s in samples_ns]
    stored = list(failures[:MAX_STORED_FAILURE_SAMPLES])
    measurement["failures"] = {
        "count": int(failure_count),
        "samples": stored,
        "truncated": failure_count > len(stored),
    }
    if samples_ns:
        measurement.update(C.latency_stats(samples_ns, spec["sample_mode"]))
        throughput = C.throughput_stats(samples_ns, sample_units)
        measurement["total_units"] = throughput["total_units"]
        measurement["total_measured_ns"] = throughput["total_measured_ns"]
        measurement["throughput_units_per_s"] = throughput["throughput_units_per_s"]
        measurement["throughput_denominator"] = throughput["throughput_denominator"]
        measurement["throughput_note"] = throughput["throughput_note"]
    return measurement


def _base_record(
    run_id: str,
    manifest: Dict[str, Any],
    profile: str,
    identity: Optional[Dict[str, Any]],
    environment: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "schema_version": C.RESULT_SCHEMA_VERSION,
        "contract_version": C.CONTRACT_VERSION,
        "run_id": run_id,
        "case_id": manifest["case_id"],
        "domain": manifest["domain"],
        "backend": manifest["backend"],
        "scope": profile,
        "origin": manifest["origin"],
        "status": C.CASE_STATUS_ERROR,
        "status_reason": None,
        "identity": identity,
        "environment": environment,
        "phases": {"setup_ns": 0, "warmup_ns": 0, "measured_ns": 0, "teardown_ns": 0},
        "measurement": _empty_measurement(manifest["measurement"]),
        "correctness": {
            "kind": "digest",
            "status": "NOT_RUN",
            "digest": None,
            "expected_digest": manifest["correctness"].get("expected_digest"),
            "summary": {},
            "fail_reasons": [],
            "normalizations": manifest["correctness"].get("normalizations", []),
            "repeat_consistent": None,
        },
        "notes": [],
        "started_at": utc_now_iso(),
        "finished_at": utc_now_iso(),
    }


def run_single_case(
    root: Path,
    run_dir: Path,
    run_id: str,
    profile: str,
    manifest_path: str,
    manifest: Dict[str, Any],
    source_map: Dict[str, Any],
    source_map_path: str,
    source_map_sha: str,
    suite_info: Optional[Dict[str, Any]],
    ambient_pythonhashseed: str,
    allow_budget_override: bool,
    case_deadline_budget_s: Optional[float] = None,
    batch_executor: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Execute one case end-to-end and return its frozen result record.

    ``case_deadline_budget_s`` optionally tightens (never loosens) the
    manifest case budget - the smoke suite uses it to enforce the 180 s
    whole-suite cap (H20.4).
    """
    case_id = manifest["case_id"]
    spec = manifest["measurement"]
    budget = manifest["budget"][profile]
    notes: List[str] = []

    manifest_sha = C.sha256_file(C.resolve_under_root(root, manifest_path))[0]
    identity = build_case_identity(
        root, manifest, manifest_path, manifest_sha,
        source_map, source_map_path, source_map_sha, suite_info,
    )

    case_timeout_s = float(budget["case_timeout_s"])
    if profile == C.PROFILE_FORMAL and case_timeout_s > C.FORMAL_CASE_BUDGET_DEFAULT_S:
        if not allow_budget_override:
            raise C.ContractError(
                "budget.formal.case_timeout_s=%g exceeds the frozen default cap "
                "%g s; pass --allow-budget-override to record an explicit "
                "pre-run resource adjustment (H20.4)"
                % (case_timeout_s, C.FORMAL_CASE_BUDGET_DEFAULT_S)
            )
        notes.append(
            "BUDGET_OVERRIDE: formal case_timeout_s=%gs (> default %gs), "
            "authorized via --allow-budget-override"
            % (case_timeout_s, C.FORMAL_CASE_BUDGET_DEFAULT_S)
        )
    if case_deadline_budget_s is not None:
        case_timeout_s = min(case_timeout_s, float(case_deadline_budget_s))
    deadline = Deadline(case_timeout_s)

    started_at = utc_now_iso()
    tmp_dir = run_dir / "tmp" / case_slug(case_id)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    planned = 0
    executed = 0
    samples: List[int] = []
    sample_units: List[int] = []
    failures: List[Dict[str, Any]] = []
    failure_count = 0
    batch_records: List[Dict[str, Any]] = []
    phases = {"setup_ns": 0, "warmup_ns": 0, "measured_ns": 0, "teardown_ns": 0}
    verification: Optional[Dict[str, Any]] = None
    exception: Optional[Dict[str, Any]] = None
    timed_out = False
    pythonhashseed = ambient_pythonhashseed
    process_count = 1
    # C20-1.0.1 (F01): indices of formal batches whose frozen verify() rule
    # returned FAIL (empty for smoke - the smoke path is unchanged).
    batch_verify_failures: List[int] = []
    failed_batch_reasons: List[str] = []
    # Q51-W02 (C6 carrier): per-planned-batch verification evidence for the
    # final correctness.summary (formal mode only; stays empty for smoke).
    batch_verifications: List[Dict[str, Any]] = []

    try:
        if profile == C.PROFILE_SMOKE:
            planned = int(budget["operations"])
            workload_factory = load_workload(root, manifest)
            ctx = make_case_context(manifest, tmp_dir)
            try:
                workload = workload_factory(ctx)
            except Exception as exc:  # noqa: BLE001
                raise HarnessExecutionError("workload build failed: %s" % exc) from exc
            C.validate_workload_object(workload, spec["sample_mode"], spec["reset_method"])
            with NetworkGuard():
                partial = execute_inprocess(
                    workload, ctx, spec,
                    int(budget["warmup_operations"]), planned, deadline,
                )
            phases = partial["phases"]
            samples = partial["samples"]
            sample_units = partial["sample_units"]
            failures = partial["failures"]
            failure_count = partial["failure_count"]
            executed = partial["executed"]
            verification = partial["verify"]
            exception = partial["exception"]
            timed_out = partial["timed_out"]
            notes.extend(partial["notes"])
            batch_records = [
                {
                    "index": 0,
                    "process": "inprocess",
                    "sample_count": len(samples),
                    "failure_count": failure_count,
                    "digest": verification.get("digest") if verification else None,
                    "elapsed_ns": phases["measured_ns"],
                    "timed_out": timed_out,
                }
            ]
            if ambient_pythonhashseed == "UNSET":
                notes.append(
                    "SMOKE_INPROCESS_HASHSEED_UNSET: set PYTHONHASHSEED in the "
                    "invoking shell for reproducibility (formal batches always "
                    "fix it)"
                )
        else:
            planned = int(budget["batches"]) * int(budget["operations_per_batch"])
            pythonhashseed = str(int(manifest["params"]["seed"]))
            executor = batch_executor or execute_formal_batches
            batch_records, partials = executor(
                root=root,
                run_dir=run_dir,
                case_id=case_id,
                manifest_path=manifest_path,
                manifest=manifest,
                source_map_path=source_map_path,
                deadline=deadline,
                pythonhashseed=pythonhashseed,
            )
            process_count = 1  # batches are sequential, never concurrent
            notes.append(
                "FORMAL_BATCHES_SUBPROCESS: %d sequential batches, PYTHONHASHSEED "
                "fixed to the workload seed per H20.3" % int(budget["batches"])
            )
            for partial in partials:
                samples.extend(int(s) for s in partial["samples"])
                sample_units.extend(int(u) for u in partial["sample_units"])
                failures.extend(partial["failures"])
                failure_count += int(partial["failure_count"])
                executed += int(partial["executed"])
                for key in ("setup_ns", "warmup_ns", "measured_ns", "teardown_ns"):
                    phases[key] += int(partial["phases"][key])
                if verification is None and partial.get("verify"):
                    verification = partial["verify"]
            # Q51-W02 (C6 carrier): the top-level selection above stays
            # first-available by frozen semantics (annotated in the final
            # summary); the carrier below keeps EVERY batch's complete
            # verify object so the whole round's readback facts survive the
            # end-of-run cleanup of the batch partials (D02 para 4).
            for partial in partials:
                v = partial.get("verify")
                if v is None:
                    continue
                summary = v.get("summary") or {}
                if not isinstance(summary, dict):
                    continue  # malformed summaries already failed validation
                clash = sorted(set(summary) & set(RESERVED_SUMMARY_KEYS))
                if clash:
                    raise HarnessExecutionError(
                        "C6_CARRIER_KEY_COLLISION: workload verify() summary "
                        "uses reserved harness carrier key(s) %s" % clash
                    )
            batch_verifications = _build_batch_verifications(
                batch_records, partials, int(budget["batches"])
            )
            # C20-1.0.1 (F01): any batch whose frozen verify rule returned
            # FAIL invalidates the case even when its digest equals the
            # digests of the other batches.
            for b in batch_records:
                if b.get("verify_status") == "FAIL":
                    batch_verify_failures.append(int(b["index"]))
            for partial in partials:
                v = partial.get("verify")
                if v and v.get("status") == "FAIL":
                    failed_batch_reasons.extend(
                        str(r) for r in v.get("fail_reasons", [])
                    )
            timed_out = any(bool(b.get("timed_out")) for b in batch_records)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # ---- H20.10 end-of-run drift re-check (invariants only) ----
    drift_ok, drift_problems = drift_recheck(root, identity)
    if not drift_ok:
        notes.append("DRIFT_PROBLEMS: %s" % C.canonical_json_bytes(drift_problems).decode("utf-8"))

    measurement = _finalize_measurement(
        spec, planned, executed, samples, sample_units, failures,
        failure_count, batch_records,
    )

    # Q51-W02 (D02 para 4): the top-level summary keeps the first available
    # batch's verify summary keys (frozen backward compatibility) - its
    # scope is ONLY that batch and must not masquerade as whole-round; the
    # batch_verifications carrier added in formal mode is the whole-round
    # evidence surface from which C6 regenerates the full coverage list.
    top_summary: Dict[str, Any] = (
        dict(verification.get("summary", {})) if verification else {}
    )
    if profile == C.PROFILE_FORMAL:
        top_summary["batch_verifications"] = batch_verifications
        top_summary["batch_verifications_semantics"] = (
            "one entry per planned formal batch: batch_index plus the COMPLETE "
            "verify object that batch's child process returned "
            "(verify.summary.sqlite_facts are the C6 raw facts; trace facts "
            "stay verbatim and are never compared across processes) or an "
            "explicit missing_reason; summary keys outside this carrier "
            "reflect ONLY the first available batch's verify"
        )
        if "sqlite_facts" in top_summary:
            top_summary["sqlite_facts_scope"] = (
                "first_available_batch_only - NOT whole-round; whole-round "
                "facts live in batch_verifications[].verify.summary.sqlite_facts"
            )
    correctness: Dict[str, Any] = {
        "kind": "digest",
        "status": "NOT_RUN",
        "digest": verification.get("digest") if verification else None,
        "expected_digest": manifest["correctness"].get("expected_digest"),
        "summary": top_summary,
        "fail_reasons": list(verification["fail_reasons"]) if verification else [],
        "normalizations": manifest["correctness"].get("normalizations", []),
        "repeat_consistent": None,
    }
    if profile == C.PROFILE_FORMAL and batch_records:
        digests = [b.get("digest") for b in batch_records if b.get("digest")]
        correctness["repeat_consistent"] = len(digests) == len(batch_records) and len(set(digests)) == 1

    # ---- frozen status state machine ----
    status = C.CASE_STATUS_OK
    status_reason: Optional[str] = None
    if exception is not None:
        status = C.CASE_STATUS_ERROR
        status_reason = "%s: %s" % (exception["type"], exception["message"])
    elif timed_out:
        status = C.CASE_STATUS_TIMEOUT
        status_reason = "CASE_DEADLINE_REACHED; raw samples preserved"
    elif not drift_ok:
        status = C.CASE_STATUS_DRIFT
        status_reason = "INPUT_IDENTITY_DRIFT_AFTER_RUN"
    elif planned and executed < planned:
        status = C.CASE_STATUS_ERROR
        status_reason = "SAMPLE_SHORTFALL: %d/%d" % (executed, planned)
    elif samples and all(s == 0 for s in samples):
        status = C.CASE_STATUS_ERROR
        status_reason = "TIMER_ZERO_SAMPLES: perf_counter_ns returned 0 for every sample"
    elif verification is None:
        status = C.CASE_STATUS_ERROR
        status_reason = "VERIFY_NOT_RUN"
    elif verification["status"] == "FAIL":
        status = C.CASE_STATUS_INVALID
        status_reason = "VERIFY_FAILED: %s" % "; ".join(verification["fail_reasons"])[:300]
    elif batch_verify_failures:
        # C20-1.0.1 (F01): ANY formal batch whose frozen verify() rule returned
        # FAIL invalidates the case even when its digest equals the digests of
        # the other batches (a predicate the digest does not cover).  Smoke is
        # unaffected: the branch only ever fills in formal mode.
        status = C.CASE_STATUS_INVALID
        detail = "; ".join(failed_batch_reasons)[:300] if failed_batch_reasons else "no fail_reasons reported"
        status_reason = (
            "BATCH_VERIFY_FAILED: formal batch(es) %s returned verify FAIL while "
            "emitting the same digest as the other batches (%s)"
            % (batch_verify_failures, detail)
        )
    elif failure_count > 0:
        status = C.CASE_STATUS_INVALID
        status_reason = "OPERATION_FAILURES_RECORDED: %d failed operations" % failure_count
    elif profile == C.PROFILE_SMOKE:
        expected = manifest["correctness"].get("expected_digest")
        if expected is not None and verification["digest"] != expected:
            status = C.CASE_STATUS_INVALID
            status_reason = "DIGEST_MISMATCH: expected %s got %s" % (expected, verification["digest"])
    elif correctness["repeat_consistent"] is False:
        status = C.CASE_STATUS_INVALID
        status_reason = "DIGEST_INCONSISTENT_ACROSS_BATCHES"
    if correctness["status"] == "NOT_RUN" and verification is not None:
        correctness["status"] = "FAIL" if status == C.CASE_STATUS_INVALID else "PASS"

    environment = C.capture_environment(
        manifest.get("environment", {}).get("depends_on", []),
        pythonhashseed,
        process_count,
    )

    record = _base_record(run_id, manifest, profile, identity, environment)
    record["status"] = status
    record["status_reason"] = status_reason
    record["started_at"] = started_at
    record["finished_at"] = utc_now_iso()
    record["phases"] = phases
    record["measurement"] = measurement
    record["correctness"] = correctness
    record["notes"] = notes
    if (
        profile == C.PROFILE_FORMAL
        and float(budget["case_timeout_s"]) > C.FORMAL_CASE_BUDGET_DEFAULT_S
    ):
        record["budget_override"] = {
            "formal_case_timeout_s": float(budget["case_timeout_s"]),
            "authorized": bool(allow_budget_override),
        }
    C.validate_result_record(record)
    result_path = run_dir / "results" / ("%s.json" % case_slug(case_id))
    C.write_json_atomic(result_path, record)
    return record


# ---------------------------------------------------------------------------
# batch child entry (invoked by cli `batch`; one formal batch per process)
# ---------------------------------------------------------------------------

def run_batch_child(
    root: Path,
    manifest_path: str,
    source_map_path: str,
    partial_out: Path,
    batch_index: int,
    warmup_operations: int,
    operations: int,
) -> int:
    """Validate inputs, execute one batch in-process, write the partial JSON."""
    source_map_path_resolved = Path(source_map_path).resolve()
    manifest_path_resolved = Path(manifest_path).resolve()
    source_map = C.load_json_file(source_map_path_resolved, "source map")
    C.validate_source_map(source_map)
    verify_source_map_hashes(root, source_map)
    manifest = C.load_json_file(manifest_path_resolved, "case manifest")
    C.validate_case_manifest(manifest)
    manifest_rel = _to_rel(root, manifest_path_resolved)
    identity = build_case_identity(
        root, manifest, manifest_rel,
        C.sha256_file(manifest_path_resolved)[0],
        source_map, _to_rel(root, source_map_path_resolved),
        C.sha256_file(source_map_path_resolved)[0], None,
    )
    workload_factory = load_workload(root, manifest)
    tmp_dir = partial_out.parent / ("batch-tmp-%02d" % batch_index)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ctx = make_case_context(manifest, tmp_dir)
    try:
        workload = workload_factory(ctx)
        C.validate_workload_object(
            workload, manifest["measurement"]["sample_mode"],
            manifest["measurement"]["reset_method"],
        )
        deadline = Deadline(float(manifest["budget"]["formal"]["case_timeout_s"]))
        with NetworkGuard():
            partial = execute_inprocess(
                workload, ctx, manifest["measurement"],
                warmup_operations, operations, deadline,
            )
        partial["batch_index"] = batch_index
        partial["schema"] = BATCH_PARTIAL_SCHEMA
        partial["identity_ok"] = True
        C.write_json_atomic(partial_out, partial)
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# suite orchestration
# ---------------------------------------------------------------------------

def run_suite(
    root: Path,
    output_root: Path,
    profile: str,
    source_map_path: Path,
    suite_path: Optional[Path],
    case_ids: Sequence[str],
    manifest_paths: Sequence[Path],
    run_id_arg: Optional[str],
    allow_budget_override: bool,
    ambient_pythonhashseed: str,
    suite_budget_s: float = C.SMOKE_SUITE_BUDGET_S,
) -> Tuple[int, str]:
    """Full run orchestration. Returns ``(exit_code, run_id)``.

    Every input is validated and hashed BEFORE the run directory is created,
    so invalid inputs leave no artifacts behind and import nothing (H20.2).
    """
    # ---- phase 1: validate everything, import nothing under test ----
    if run_id_arg is not None and not C.RUN_ID_RE.match(run_id_arg):
        raise C.ContractError("--run-id must match %s" % C.RUN_ID_RE.pattern)
    source_map = C.load_json_file(source_map_path.resolve(), "source map")
    C.validate_source_map(source_map)
    verify_source_map_hashes(root, source_map)
    source_map_rel = _to_rel(root, source_map_path)
    source_map_sha = C.sha256_file(source_map_path.resolve())[0]

    suite: Optional[Dict[str, Any]] = None
    suite_info: Optional[Dict[str, Any]] = None
    if suite_path is not None:
        suite = C.load_json_file(suite_path.resolve(), "suite")
        C.validate_suite(suite)
        suite_info = {
            "path": _to_rel(root, suite_path),
            "sha256": C.sha256_file(suite_path.resolve())[0],
        }

    selected: List[Tuple[str, Path]] = []
    if suite is not None:
        for entry in suite["cases"]:
            if not case_ids or entry["case_id"] in set(case_ids):
                selected.append((entry["case_id"], _resolve_rel(root, entry["manifest"])))
    for mp in manifest_paths:
        selected.append(("<from-manifest>", Path(mp).resolve()))
    if not selected:
        raise C.ContractError("no cases selected: provide --suite/--case or --case-manifest")

    loaded: List[Tuple[str, str, Path, Dict[str, Any]]] = []
    seen_case_ids: Dict[str, str] = {}
    for case_id_hint, manifest_path in selected:
        manifest = C.load_json_file(manifest_path, "case manifest")
        C.validate_case_manifest(manifest)
        if (
            profile == C.PROFILE_FORMAL
            and float(manifest["budget"]["formal"]["case_timeout_s"]) > C.FORMAL_CASE_BUDGET_DEFAULT_S
            and not allow_budget_override
        ):
            raise C.ContractError(
                "budget.formal.case_timeout_s of case %r exceeds the frozen cap "
                "%g s and --allow-budget-override was not given (H20.4: resource "
                "adjustments are recorded before the run)"
                % (manifest["case_id"], C.FORMAL_CASE_BUDGET_DEFAULT_S)
            )
        real_id = manifest["case_id"]
        if case_id_hint != "<from-manifest>" and case_id_hint != real_id:
            raise C.ContractError(
                "suite entry case_id %r != manifest case_id %r" % (case_id_hint, real_id)
            )
        if real_id in seen_case_ids:
            raise C.ContractError("duplicate case id %r across selected manifests" % real_id)
        seen_case_ids[real_id] = str(manifest_path)
        manifest_rel = _to_rel(root, manifest_path)
        # hash-and-contain every referenced fixture/workload now (pre-import)
        build_case_identity(
            root, manifest, manifest_rel,
            C.sha256_file(manifest_path)[0],
            source_map, source_map_rel, source_map_sha, suite_info,
        )
        loaded.append((real_id, manifest_rel, manifest_path, manifest))
    if case_ids:
        missing = set(case_ids) - set(seen_case_ids)
        if missing:
            raise C.ContractError("unknown --case ids not in suite/manifests: %s" % sorted(missing))

    # ---- phase 2: run directory (created only after full validation) ----
    run_id = run_id_arg or generate_run_id()
    run_dir = output_root / "runs" / run_id
    if run_dir.exists():
        raise C.ContractError(
            "duplicate output directory %s already exists; runs are immutable "
            "and never overwritten" % run_dir
        )
    (run_dir / "results").mkdir(parents=True, exist_ok=True)

    suite_deadline = Deadline(suite_budget_s) if profile == C.PROFILE_SMOKE else None
    case_summaries: List[Dict[str, Any]] = []
    statuses: List[str] = []
    run_started_at = utc_now_iso()

    for case_id, manifest_rel, manifest_path, manifest in loaded:
        budget_s: Optional[float] = None
        if suite_deadline is not None and suite_deadline.remaining() <= 0:
            record = _base_record(
                run_id, manifest, C.PROFILE_SMOKE, None,
                C.capture_environment([], ambient_pythonhashseed, 1),
            )
            record["status_reason"] = (
                "SUITE_BUDGET_EXCEEDED: smoke suite budget %.0fs exhausted before "
                "case start" % suite_budget_s
            )
            record["correctness"]["fail_reasons"] = [record["status_reason"]]
            record["notes"] = ["case never started; no workload code was imported"]
            record["identity"] = _minimal_identity(manifest, manifest_rel)
            C.validate_result_record(record)
            C.write_json_atomic(run_dir / "results" / ("%s.json" % case_slug(case_id)), record)
            statuses.append(record["status"])
            case_summaries.append(_summary_entry(record, manifest_rel, 0.0))
            continue
        if suite_deadline is not None:
            budget_s = suite_deadline.remaining()

        t0 = time.monotonic()
        try:
            record = run_single_case(
                root=root,
                run_dir=run_dir,
                run_id=run_id,
                profile=profile,
                manifest_path=manifest_rel,
                manifest=manifest,
                source_map=source_map,
                source_map_path=source_map_rel,
                source_map_sha=source_map_sha,
                suite_info=suite_info,
                ambient_pythonhashseed=ambient_pythonhashseed,
                allow_budget_override=allow_budget_override,
                case_deadline_budget_s=budget_s,
            )
        except (HarnessExecutionError, C.ContractError) as exc:
            # post-start per-case failure: record honestly, keep the suite alive
            record = _base_record(
                run_id, manifest, profile, _minimal_identity(manifest, manifest_rel),
                C.capture_environment([], ambient_pythonhashseed, 1),
            )
            record["status_reason"] = "%s: %s" % (type(exc).__name__, str(exc)[:400])
            record["notes"] = ["per-case failure recorded by the run orchestrator"]
            C.validate_result_record(record)
            C.write_json_atomic(run_dir / "results" / ("%s.json" % case_slug(case_id)), record)
        elapsed = time.monotonic() - t0
        statuses.append(record["status"])
        case_summaries.append(_summary_entry(record, manifest_rel, elapsed))

    all_ok = bool(statuses) and all(s == C.CASE_STATUS_OK for s in statuses)
    run_summary = {
        "schema": RUN_SUMMARY_SCHEMA,
        "contract_version": C.CONTRACT_VERSION,
        "harness_version": C.HARNESS_VERSION,
        "run_id": run_id,
        "profile": profile,
        "origin": (
            "SYNTHETIC" if all(m["origin"] == "SYNTHETIC" for _, _, _, m in loaded) else "MIXED"
        ),
        "source_map": {"path": source_map_rel, "sha256": source_map_sha},
        "suite": suite_info,
        "suite_budget_s": suite_budget_s if profile == C.PROFILE_SMOKE else None,
        "cases": case_summaries,
        "all_ok": all_ok,
        "started_at": run_started_at,
        "finished_at": utc_now_iso(),
        "environment": C.capture_environment([], ambient_pythonhashseed, 1),
        "note": "environment variables / argv / credentials are never recorded (H20.1)",
    }
    C.write_json_atomic(run_dir / "run.json", run_summary)
    shutil.rmtree(run_dir / "tmp", ignore_errors=True)
    return (0 if all_ok else 3, run_id)


def _minimal_identity(manifest: Dict[str, Any], manifest_rel: str) -> Dict[str, Any]:
    return {
        "source_map": {"path": None, "sha256": None},
        "case_manifest": {"path": manifest_rel, "sha256": None},
        "suite": None,
        "fixtures": [],
        "workload": {"source": manifest["workload"]["source"], "sha256": None},
        "measured_modules": [],
        "contract_version": C.CONTRACT_VERSION,
        "algorithm_version": manifest.get("algorithm_version", C.UNKNOWN),
        "policy_version": manifest.get("policy_version", C.UNKNOWN),
        "workload_params": manifest.get("params", {}),
        "measurement_spec": manifest["measurement"],
        "eligible_for_baseline": bool(manifest["eligible_for_baseline"]),
    }


def _summary_entry(record: Dict[str, Any], manifest_rel: str, elapsed_s: float) -> Dict[str, Any]:
    return {
        "case_id": record["case_id"],
        "manifest": {
            "path": manifest_rel,
            "sha256": (record["identity"] or {}).get("case_manifest", {}).get("sha256"),
        },
        "status": record["status"],
        "status_reason": record["status_reason"],
        "result_file": "results/%s.json" % case_slug(record["case_id"]),
        "elapsed_s": round(elapsed_s, 3),
        "planned_operations": record["measurement"]["planned_operations"],
        "executed_operations": record["measurement"]["executed_operations"],
        "sample_count": record["measurement"]["n"],
        "started_at": record["started_at"],
    }


def _resolve_rel(root: Path, rel: str) -> Path:
    return C.resolve_under_root(root, rel)


def _to_rel(root: Path, path: Path) -> str:
    resolved_root = root.resolve()
    resolved = Path(path).resolve()
    norm_root = os.path.normcase(str(resolved_root))
    norm = os.path.normcase(str(resolved))
    if norm == norm_root or not norm.startswith(norm_root + os.sep):
        raise C.ContractError("path %s is outside root %s" % (path, root))
    return str(resolved.relative_to(resolved_root)).replace("\\", "/")
