"""C20 frozen contracts for the AK offline benchmark harness (NF20).

This module is the single authoritative, importable definition of the C20
contract surface frozen by work order
``docs/devplan-next-frontier-20260924-r1/work-orders/NF20-benchmark-harness.md``:

* version identities (H20.1),
* statistics methods (H20.5): nearest-rank quantiles with exact integer
  arithmetic and unscaled MAD,
* path containment / forbidden-root rules (H20.2, boundaries doc),
* case-manifest / suite / source-map / baseline-policy validation (H20.2),
* the workload protocol (H20.3 / H20.6),
* environment capture allow-list (H20.1; never dumps environ/argv/credentials).

Any change to constants or semantics in this module is a contract change and
requires a new C20 version string plus re-freeze by the NF20 owner.  The
human-readable mirror of this file is ``docs/benchmarks/README.md`` plus
``docs/benchmarks/result-schema.json``.

C20-1.0.1 (NF20 r2 integration window, 2026-09-24): the only semantic change
versus C20-1.0.0 is the NF20-HARD-r1 finding F01 repair in the runner - in
formal mode every batch's ``verify()`` status is propagated into
``measurement.batches[].verify_status`` and ANY batch whose frozen rule
returned FAIL marks the case INVALID even when its digest matches the other
batches.  Smoke semantics, budgets, statistics, digest rules, comparison
matching and promotion gates are unchanged.  Records written under
C20-1.0.0 remain loadable; cross-version comparison is INCOMPARABLE by the
existing ``contract_version`` match key.

This module imports only the Python standard library.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# H20.1 - frozen version identities
# ---------------------------------------------------------------------------

CONTRACT_VERSION = "C20-1.0.1"
HARNESS_VERSION = "nf20-harness/1.0.1"

RESULT_SCHEMA_VERSION = "ak-benchmark-result/1.0.0"
CASE_MANIFEST_SCHEMA = "ak-benchmark-case-manifest/1.0.0"
SOURCE_MAP_SCHEMA = "ak-benchmark-source-map/1.0.0"
SUITE_SCHEMA = "ak-benchmark-suite/1.0.0"
BASELINE_POLICY_SCHEMA = "ak-benchmark-baseline-policy/1.0.0"
BASELINE_META_SCHEMA = "ak-benchmark-baseline-meta/1.0.0"
COMPARE_REPORT_SCHEMA = "ak-benchmark-compare-report/1.0.0"
DRIFT_REPORT_SCHEMA = "ak-benchmark-drift-report/1.0.0"

UNKNOWN = "UNKNOWN"

# ---------------------------------------------------------------------------
# Frozen enums / budgets (H20.3, H20.4, H20.5)
# ---------------------------------------------------------------------------

TIMER_NAME = "time.perf_counter_ns"

PROFILE_SMOKE = "smoke"
PROFILE_FORMAL = "formal"
PROFILES = (PROFILE_SMOKE, PROFILE_FORMAL)

CASE_STATUS_OK = "OK"
CASE_STATUS_INVALID = "INVALID"
CASE_STATUS_TIMEOUT = "TIMEOUT"
CASE_STATUS_ERROR = "ERROR"
CASE_STATUS_DRIFT = "DRIFT"
CASE_STATUSES = (
    CASE_STATUS_OK,
    CASE_STATUS_INVALID,
    CASE_STATUS_TIMEOUT,
    CASE_STATUS_ERROR,
    CASE_STATUS_DRIFT,
)

VERDICT_REGRESSION = "REGRESSION"
VERDICT_NO_REGRESSION = "NO_REGRESSION"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"
VERDICT_INCOMPARABLE = "INCOMPARABLE"
VERDICT_NO_BASELINE = "NO_BASELINE"
VERDICT_INVALID = "INVALID"
VERDICTS = (
    VERDICT_REGRESSION,
    VERDICT_NO_REGRESSION,
    VERDICT_INCONCLUSIVE,
    VERDICT_INCOMPARABLE,
    VERDICT_NO_BASELINE,
    VERDICT_INVALID,
)

SAMPLE_MODE_PER_OPERATION = "per_operation"
SAMPLE_MODE_AMORTIZED = "amortized"
SAMPLE_MODES = (SAMPLE_MODE_PER_OPERATION, SAMPLE_MODE_AMORTIZED)

RESET_METHOD_NONE = "none"
RESET_METHOD_BATCH_RESET = "batch_reset"
RESET_METHODS = (RESET_METHOD_NONE, RESET_METHOD_BATCH_RESET)

ORIGIN_SYNTHETIC = "SYNTHETIC"
ORIGIN_TEST_SIMULATED = "TEST_SIMULATED"
MANIFEST_ORIGINS = (ORIGIN_SYNTHETIC,)

PROVENANCE_KINDS = ("WORKTREE_SNAPSHOT", "RELEASE_MANIFEST")

# H20.4 budgets.  Smoke: per case <= 30 s, whole suite <= 180 s.  Formal
# defaults: >= 5 batches, >= 20 samples per batch, case cap 600 s.
SMOKE_CASE_BUDGET_S = 30.0
SMOKE_SUITE_BUDGET_S = 180.0
FORMAL_CASE_BUDGET_DEFAULT_S = 600.0
FORMAL_MIN_BATCHES = 5
FORMAL_MIN_OPS_PER_BATCH = 20

# H20.5: p95 requires at least 100 actual operation samples.
P95_MIN_SAMPLES = 100
FORMAL_MIN_TOTAL_SAMPLES = FORMAL_MIN_BATCHES * FORMAL_MIN_OPS_PER_BATCH

# Frozen digest normalization vocabulary (H20.6).  A case may only declare
# normalizations from this list; normalization never removes business state,
# ordering, or rejection reasons.
DIGEST_NORMALIZATIONS = ("run_id", "absolute_time", "elapsed_ns", "temp_path")

# H20.1 environment field allow-list.  Nothing else may be captured; in
# particular no environment variables, no process command lines, no
# credentials are ever read or recorded by the harness.
ENVIRONMENT_KEYS = (
    "python_version",
    "python_implementation",
    "os",
    "platform",
    "machine_arch",
    "cpu_brand",
    "cpu_count",
    "host_id",
    "timer",
    "pythonhashseed",
    "process_count",
    "dependencies",
)

# Fields of ``measurement`` (manifest) whose mismatch between baseline and
# candidate makes the two runs INCOMPARABLE (H20.7 measurement scope).
MEASUREMENT_SPEC_MATCH_FIELDS = (
    "sample_mode",
    "units",
    "units_per_operation",
    "min_operations",
    "batch_size",
    "reset_method",
    "includes_io",
)

# Environment fields whose mismatch makes two runs INCOMPARABLE (H20.7
# environment match).  Source hashes are deliberately absent: comparing a
# changed source against its baseline is the purpose of the protocol.
ENVIRONMENT_MATCH_KEYS = (
    "python_version",
    "python_implementation",
    "os",
    "machine_arch",
    "cpu_count",
    "host_id",
    "timer",
    "dependencies",
)

CASE_ID_RE = re.compile(r"^[a-z0-9_]{1,32}/[a-z0-9_]{1,64}$")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class ContractError(Exception):
    """C20 validation failure (usage-class error, CLI exit code 2).

    Raised for malformed manifests / source maps / suites / policies /
    records and for path-containment violations.  Raised BEFORE any
    module-under-test is imported (H20.2).
    """


# ---------------------------------------------------------------------------
# Canonical JSON, hashing, digests (H20.1, H20.6)
# ---------------------------------------------------------------------------

def canonical_json_bytes(obj: Any) -> bytes:
    """Frozen canonical JSON encoding used for every digest in C20."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> Tuple[str, int]:
    """Return ``(sha256_hex, size_bytes)`` for a regular file."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def digest_of(obj: Any) -> str:
    """sha256 over the canonical JSON encoding of ``obj`` (H20.6)."""
    return sha256_bytes(canonical_json_bytes(obj))


def write_json_atomic(path: Path, obj: Any) -> None:
    """Deterministic-layout JSON write (sort_keys, indent 2, LF newlines)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# H20.5 frozen statistics: nearest-rank quantiles, unscaled MAD
# ---------------------------------------------------------------------------

def quantile_index_1based(num: int, den: int, n: int) -> int:
    """1-based nearest-rank index ``ceil(num*n/den)`` clamped to ``[1, n]``.

    Exact integer arithmetic - no floating point anywhere in quantile
    selection.  Frozen pairs: median ``(1, 2)``, p95 ``(19, 20)``.
    """
    if n <= 0:
        raise ContractError("quantile of empty sample list")
    if num < 0 or den <= 0 or num > den:
        raise ContractError("invalid quantile (num, den): %r" % ((num, den),))
    idx = -((-num * n) // den)  # ceil division on integers
    return max(1, min(n, idx))


def nearest_rank(sorted_values: Sequence[int], num: int, den: int) -> Tuple[int, int]:
    """Return ``(value, index_1based)`` of the nearest-rank quantile.

    ``sorted_values`` must be ascending and non-empty.
    """
    idx = quantile_index_1based(num, den, len(sorted_values))
    return int(sorted_values[idx - 1]), idx


def median_nearest_rank(values: Sequence[int]) -> Tuple[int, int]:
    return nearest_rank(sorted(values), 1, 2)


def mad_unscaled(values: Sequence[int]) -> int:
    """Unscaled median absolute deviation (median of |x - median|).

    Both medians use the frozen nearest-rank definition.
    """
    if not values:
        raise ContractError("MAD of empty sample list")
    med, _ = median_nearest_rank(values)
    deviations = sorted(abs(int(v) - med) for v in values)
    mad, _ = nearest_rank(deviations, 1, 2)
    return mad


def latency_stats(samples_ns: Sequence[int], sample_mode: str) -> Dict[str, Any]:
    """Frozen latency statistics block for a list of valid sample durations.

    * ``per_operation``: n, median, min, max, MAD, p95 (only when
      ``n >= P95_MIN_SAMPLES``, else ``null`` with an explicit note).
    * ``amortized``: NO latency statistics at all.  A single total divided by
      N is an amortization, not N latency samples (H20.5), so no quantiles
      are emitted for amortized measurements.
    """
    if sample_mode not in SAMPLE_MODES:
        raise ContractError("unknown sample_mode: %r" % (sample_mode,))
    if not samples_ns:
        raise ContractError("latency_stats of empty sample list")
    if sample_mode == SAMPLE_MODE_AMORTIZED:
        return {
            "n": 0,
            "median_ns": None,
            "p95_ns": None,
            "p95_note": "P95_NOT_DEFINED_FOR_AMORTIZED",
            "min_ns": None,
            "max_ns": None,
            "mad_ns": None,
        }
    ordered = sorted(int(v) for v in samples_ns)
    n = len(ordered)
    median, _ = nearest_rank(ordered, 1, 2)
    if n >= P95_MIN_SAMPLES:
        p95, _ = nearest_rank(ordered, 19, 20)
        p95_note = "P95_PRESENT_N%d" % n
    else:
        p95 = None
        p95_note = "P95_OMITTED_INSUFFICIENT_SAMPLES"
    return {
        "n": n,
        "median_ns": median,
        "p95_ns": p95,
        "p95_note": p95_note,
        "min_ns": ordered[0],
        "max_ns": ordered[-1],
        "mad_ns": mad_unscaled(ordered),
    }


def throughput_stats(samples_ns: Sequence[int], units: Sequence[int]) -> Dict[str, Any]:
    """Frozen throughput block.

    The denominator is exclusively the sum of units of VALID samples -
    i.e. explicitly successful completion units (H20.5).  Returns nulls when
    the denominator or the measured time is zero.
    """
    total_ns = sum(int(v) for v in samples_ns)
    total_units = sum(int(u) for u in units)
    if total_ns <= 0 or total_units <= 0:
        return {
            "total_units": total_units,
            "total_measured_ns": total_ns,
            "throughput_units_per_s": None,
            "throughput_denominator": "successful_operation_units",
            "throughput_note": "THROUGHPUT_OMITTED_ZERO_DENOMINATOR",
        }
    return {
        "total_units": total_units,
        "total_measured_ns": total_ns,
        "throughput_units_per_s": (total_units * 1e9) / total_ns,
        "throughput_denominator": "successful_operation_units",
        "throughput_note": "THROUGHPUT_PRESENT",
    }


# ---------------------------------------------------------------------------
# H20.2 path security: explicit inputs, containment, forbidden roots
# ---------------------------------------------------------------------------

FORBIDDEN_DIR_COMPONENTS = frozenset({".git", "maintenance_artifacts"})
FORBIDDEN_BASENAME_PREFIXES = (".env",)
FROZEN_EXTERNAL_SURFACE = ("d:/akf260920",)


def validate_relative_path(rel: str) -> str:
    """Validate a manifest-relative path string (forward slashes only).

    Rejects: empty, absolute paths (drive letters, leading ``/``), UNC
    paths, backslashes, ``..`` components, and forbidden components
    (``.git``, ``maintenance_artifacts``, ``.env*``).
    """
    if not isinstance(rel, str) or not rel:
        raise ContractError("path must be a non-empty string, got %r" % (rel,))
    if "\\" in rel:
        raise ContractError("path %r must use forward slashes only" % rel)
    if rel.startswith("/"):
        raise ContractError("path %r must be relative, not absolute" % rel)
    if re.match(r"^[A-Za-z]:", rel):
        raise ContractError("path %r must not carry a drive letter" % rel)
    if rel.startswith("//") or rel.startswith("\\\\"):
        raise ContractError("UNC path %r is forbidden" % rel)
    parts = rel.split("/")
    for part in parts:
        if part in ("", "."):
            raise ContractError("path %r has empty/dot component" % rel)
        if part == "..":
            raise ContractError("path %r escapes with '..'" % rel)
        if part.lower() in FORBIDDEN_DIR_COMPONENTS:
            raise ContractError(
                "path %r enters forbidden directory %r" % (rel, part)
            )
        if any(part.lower().startswith(p) for p in FORBIDDEN_BASENAME_PREFIXES):
            raise ContractError("path %r touches forbidden .env* file" % rel)
    return rel


def resolve_under_root(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root`` with full containment enforcement.

    The normalized (case-insensitive on Windows) resolved path must remain
    under the resolved root; symlink/junction escapes are caught by the
    ``realpath`` resolution.  Also refuses the frozen external surface
    (e.g. ``D:/AKF260920``) defensively even though containment already
    excludes it.
    """
    validate_relative_path(rel)
    root_resolved = Path(root).resolve()
    candidate = (root_resolved / rel).resolve()
    root_norm = os.path.normcase(str(root_resolved))
    cand_norm = os.path.normcase(str(candidate))
    if cand_norm != root_norm and not cand_norm.startswith(root_norm + os.sep):
        raise ContractError("path %r resolves outside root %s" % (rel, root_resolved))
    for frozen in FROZEN_EXTERNAL_SURFACE:
        if cand_norm.startswith(os.path.normcase(frozen)):
            raise ContractError("path %r touches frozen external surface" % rel)
    return candidate


# ---------------------------------------------------------------------------
# H20.1 environment capture (allow-list only)
# ---------------------------------------------------------------------------

def host_id() -> str:
    """Stable, non-secret host identity: truncated sha256 of the node name."""
    return sha256_bytes(platform.node().encode("utf-8"))[:16]


def capture_environment(
    depends_on: Sequence[str],
    pythonhashseed: str,
    process_count: int = 1,
) -> Dict[str, Any]:
    """Capture exactly the frozen allow-list of non-secret environment facts."""
    dependencies: Dict[str, str] = {}
    for name in depends_on:
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = UNKNOWN
    cpu_count = os.cpu_count()
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "os": "%s-%s" % (platform.system(), platform.release()),
        "platform": platform.platform(),
        "machine_arch": platform.machine(),
        "cpu_brand": platform.processor() or UNKNOWN,
        "cpu_count": cpu_count if isinstance(cpu_count, int) else UNKNOWN,
        "host_id": host_id(),
        "timer": TIMER_NAME,
        "pythonhashseed": pythonhashseed,
        "process_count": process_count,
        "dependencies": dependencies,
    }


# ---------------------------------------------------------------------------
# JSON loading
# ---------------------------------------------------------------------------

def load_json_file(path: Path, what: str) -> Any:
    if not path.is_file():
        raise ContractError("%s not found: %s" % (what, path))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContractError("%s is not valid JSON (%s): %s" % (what, path, exc)) from exc


# ---------------------------------------------------------------------------
# Case manifest validation (H20.2 / H20.3 / H20.4 / H20.6)
# ---------------------------------------------------------------------------

def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ContractError(message)


def _require_int(obj: Dict[str, Any], key: str, where: str, minimum: Optional[int] = None) -> int:
    _require(key in obj, "%s: missing required field %r" % (where, key))
    value = obj[key]
    _require(
        isinstance(value, int) and not isinstance(value, bool),
        "%s.%s must be an integer" % (where, key),
    )
    if minimum is not None:
        _require(value >= minimum, "%s.%s must be >= %d" % (where, key, minimum))
    return value


def _require_bool(obj: Dict[str, Any], key: str, where: str) -> bool:
    _require(key in obj, "%s: missing required field %r" % (where, key))
    value = obj[key]
    _require(isinstance(value, bool), "%s.%s must be a boolean" % (where, key))
    return value


def _require_str(obj: Dict[str, Any], key: str, where: str) -> str:
    _require(key in obj, "%s: missing required field %r" % (where, key))
    value = obj[key]
    _require(isinstance(value, str) and value, "%s.%s must be a non-empty string" % (where, key))
    return value


def validate_measurement_spec(spec: Dict[str, Any]) -> None:
    where = "measurement"
    _require(isinstance(spec, dict), "measurement must be an object")
    mode = _require_str(spec, "sample_mode", where)
    _require(mode in SAMPLE_MODES, "measurement.sample_mode must be one of %s" % (list(SAMPLE_MODES),))
    _require_str(spec, "units", where)
    _require_int(spec, "units_per_operation", where, minimum=1)
    _require_int(spec, "min_operations", where, minimum=1)
    _require_int(spec, "batch_size", where, minimum=1)
    reset = _require_str(spec, "reset_method", where)
    _require(reset in RESET_METHODS, "measurement.reset_method must be one of %s" % (list(RESET_METHODS),))
    _require_bool(spec, "includes_io", where)


def validate_resources(resources: Dict[str, Any]) -> None:
    """H20.2: real network and production DB entry points are always refused."""
    _require(isinstance(resources, dict), "resources must be an object")
    for key in ("network", "production_db", "temp_sqlite"):
        _require(key in resources, "resources: missing required field %r" % key)
    _require(resources["network"] is False, "resources.network must be false (offline harness)")
    _require(resources["production_db"] is False, "resources.production_db must be false (offline harness)")
    _require(isinstance(resources["temp_sqlite"], bool), "resources.temp_sqlite must be a boolean")


def validate_correctness(correctness: Dict[str, Any]) -> None:
    where = "correctness"
    _require(isinstance(correctness, dict), "correctness must be an object")
    kind = _require_str(correctness, "kind", where)
    _require(kind == "digest", "correctness.kind must be 'digest' (frozen r1 rule set)")
    expected = correctness.get("expected_digest")
    _require(
        expected is None or (isinstance(expected, str) and HEX64_RE.match(expected)),
        "correctness.expected_digest must be null or a 64-char lowercase hex digest",
    )
    normalizations = correctness.get("normalizations", [])
    _require(isinstance(normalizations, list), "correctness.normalizations must be a list")
    for item in normalizations:
        _require(
            item in DIGEST_NORMALIZATIONS,
            "correctness.normalizations entry %r not in frozen vocabulary %s"
            % (item, list(DIGEST_NORMALIZATIONS)),
        )


def validate_budget(budget: Dict[str, Any]) -> Dict[str, Any]:
    """Validate per-profile budgets; returns normalized budget dict."""
    _require(isinstance(budget, dict), "budget must be an object")
    smoke = budget.get("smoke")
    _require(isinstance(smoke, dict), "budget.smoke must be an object")
    _require_int(smoke, "warmup_operations", "budget.smoke", minimum=0)
    _require_int(smoke, "operations", "budget.smoke", minimum=1)
    smoke_timeout = _require_int(smoke, "case_timeout_s", "budget.smoke", minimum=1)
    _require(
        smoke_timeout <= SMOKE_CASE_BUDGET_S,
        "budget.smoke.case_timeout_s must be <= %g seconds (H20.4)" % SMOKE_CASE_BUDGET_S,
    )
    formal = budget.get("formal")
    _require(isinstance(formal, dict), "budget.formal must be an object")
    _require_int(formal, "warmup_operations", "budget.formal", minimum=0)
    batches = _require_int(formal, "batches", "budget.formal", minimum=1)
    ops = _require_int(formal, "operations_per_batch", "budget.formal", minimum=1)
    formal_timeout = _require_int(formal, "case_timeout_s", "budget.formal", minimum=1)
    normalized: Dict[str, Any] = {
        "smoke": {
            "warmup_operations": smoke["warmup_operations"],
            "operations": smoke["operations"],
            "case_timeout_s": smoke_timeout,
        },
        "formal": {
            "warmup_operations": formal["warmup_operations"],
            "batches": batches,
            "operations_per_batch": ops,
            "case_timeout_s": formal_timeout,
        },
    }
    # H20.4: fewer repetitions than the frozen minimum are allowed only with
    # an explicit pre-registration recorded in the manifest.
    if batches < FORMAL_MIN_BATCHES or ops < FORMAL_MIN_OPS_PER_BATCH:
        pre = formal.get("pre_registration")
        _require(
            isinstance(pre, dict) and isinstance(pre.get("reason"), str) and pre.get("reason")
            and isinstance(pre.get("registered_by"), str) and pre.get("registered_by"),
            "budget.formal below frozen minimum (%d batches x %d ops) requires "
            "formal.pre_registration {reason, registered_by}"
            % (FORMAL_MIN_BATCHES, FORMAL_MIN_OPS_PER_BATCH),
        )
        normalized["formal"]["pre_registration"] = {
            "reason": pre["reason"],
            "registered_by": pre["registered_by"],
        }
    return normalized


def validate_case_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Full structural validation of a case manifest. Returns it unchanged."""
    where = "case-manifest"
    _require(isinstance(manifest, dict), "case manifest must be a JSON object")
    schema = _require_str(manifest, "schema", where)
    _require(schema == CASE_MANIFEST_SCHEMA, "case manifest schema must be %s" % CASE_MANIFEST_SCHEMA)
    case_id = _require_str(manifest, "case_id", where)
    _require(bool(CASE_ID_RE.match(case_id)), "case_id must match 'domain/name' pattern: %r" % case_id)
    _require_str(manifest, "description", where)
    origin = _require_str(manifest, "origin", where)
    _require(origin in MANIFEST_ORIGINS, "origin must be one of %s" % (list(MANIFEST_ORIGINS),))
    _require_str(manifest, "domain", where)
    _require_str(manifest, "backend", where)
    _require_bool(manifest, "eligible_for_baseline", where)

    workload = manifest.get("workload")
    _require(isinstance(workload, dict), "workload must be an object")
    _require_str(workload, "source", "workload")
    entrypoint = workload.get("entrypoint", "build")
    _require(
        isinstance(entrypoint, str) and bool(IDENTIFIER_RE.match(entrypoint)),
        "workload.entrypoint must be a Python identifier",
    )

    measured = manifest.get("measured_modules", [])
    _require(isinstance(measured, list), "measured_modules must be a list")
    for name in measured:
        _require(
            isinstance(name, str) and bool(IDENTIFIER_RE.match(name)),
            "measured_modules entry %r must be a dotted identifier" % (name,),
        )

    fixtures = manifest.get("fixtures", [])
    _require(isinstance(fixtures, list), "fixtures must be a list")
    for fixture in fixtures:
        _require(isinstance(fixture, dict), "fixtures entries must be objects")
        _require_str(fixture, "path", "fixtures[]")
        _require_str(fixture, "role", "fixtures[]")

    params = manifest.get("params", {})
    _require(isinstance(params, dict), "params must be an object")
    _require(
        "seed" in params and isinstance(params["seed"], int) and not isinstance(params["seed"], bool),
        "params.seed must be an integer (frozen workload seed, H20.3)",
    )

    validate_measurement_spec(manifest.get("measurement", {}))
    validate_resources(manifest.get("resources", {}))
    validate_correctness(manifest.get("correctness", {}))
    manifest["budget"] = validate_budget(manifest.get("budget", {}))

    environment = manifest.get("environment", {})
    _require(isinstance(environment, dict), "environment must be an object")
    depends_on = environment.get("depends_on", [])
    _require(isinstance(depends_on, list), "environment.depends_on must be a list")
    for name in depends_on:
        _require(isinstance(name, str) and name, "environment.depends_on entries must be strings")

    for key in ("algorithm_version", "policy_version"):
        value = manifest.get(key, UNKNOWN)
        _require(isinstance(value, str) and value, "%s must be a non-empty string" % key)
    return manifest


# ---------------------------------------------------------------------------
# Source-map validation (H20.2: verify containment+hash BEFORE any import)
# ---------------------------------------------------------------------------

SOURCE_MAP_REQUIRED_FIELDS = (
    "logical_module",
    "source_relative_path",
    "sha256",
    "bytes",
    "owner",
    "release_id",
    "provenance_kind",
)


def validate_source_map(source_map: Dict[str, Any]) -> None:
    where = "source-map"
    _require(isinstance(source_map, dict), "source map must be a JSON object")
    schema = _require_str(source_map, "schema", where)
    _require(schema == SOURCE_MAP_SCHEMA, "source map schema must be %s" % SOURCE_MAP_SCHEMA)
    entries = source_map.get("modules")
    _require(isinstance(entries, list), "source map 'modules' must be a list")
    seen = set()
    for index, entry in enumerate(entries):
        _require(isinstance(entry, dict), "source map entry #%d must be an object" % index)
        for field in SOURCE_MAP_REQUIRED_FIELDS:
            _require(field in entry, "source map entry #%d missing field %r" % (index, field))
        _require(
            bool(IDENTIFIER_RE.match(str(entry["logical_module"]))),
            "source map entry #%d logical_module %r must be a dotted identifier" % (index, entry["logical_module"]),
        )
        _require(
            isinstance(entry["sha256"], str) and bool(HEX64_RE.match(entry["sha256"])),
            "source map entry #%d sha256 must be 64-char lowercase hex" % index,
        )
        _require(
            isinstance(entry["bytes"], int) and not isinstance(entry["bytes"], bool) and entry["bytes"] >= 0,
            "source map entry #%d bytes must be a non-negative integer" % index,
        )
        for field in ("owner", "release_id"):
            _require(isinstance(entry[field], str) and entry[field], "entry #%d %s must be non-empty" % (index, field))
        _require(
            entry["provenance_kind"] in PROVENANCE_KINDS,
            "source map entry #%d provenance_kind must be one of %s" % (index, list(PROVENANCE_KINDS)),
        )
        # path validation happens here too: containment errors abort the run
        # before any module-under-test can be imported (H20.2).
        validate_relative_path(str(entry["source_relative_path"]))
        logical = str(entry["logical_module"])
        _require(logical not in seen, "duplicate logical_module %r in source map" % logical)
        seen.add(logical)


# ---------------------------------------------------------------------------
# Suite validation
# ---------------------------------------------------------------------------

def validate_suite(suite: Dict[str, Any]) -> None:
    where = "suite"
    _require(isinstance(suite, dict), "suite must be a JSON object")
    schema = _require_str(suite, "schema", where)
    _require(schema == SUITE_SCHEMA, "suite schema must be %s" % SUITE_SCHEMA)
    _require_str(suite, "contract_version", where)
    cases = suite.get("cases")
    _require(isinstance(cases, list), "suite.cases must be a list")
    seen = set()
    for index, entry in enumerate(cases):
        _require(isinstance(entry, dict), "suite.cases[%d] must be an object" % index)
        _require_str(entry, "case_id", "suite.cases[%d]" % index)
        _require_str(entry, "manifest", "suite.cases[%d]" % index)
        _require_str(entry, "owner", "suite.cases[%d]" % index)
        _require_str(entry, "status", "suite.cases[%d]" % index)
        case_id = entry["case_id"]
        _require(case_id not in seen, "duplicate case_id %r in suite" % case_id)
        seen.add(case_id)


# ---------------------------------------------------------------------------
# Baseline policy validation (H20.8)
# ---------------------------------------------------------------------------

POLICY_METRICS = ("median_ns", "throughput")


def validate_baseline_policy(policy: Dict[str, Any]) -> None:
    where = "baseline-policy"
    _require(isinstance(policy, dict), "policy must be a JSON object")
    schema = _require_str(policy, "schema", where)
    _require(schema == BASELINE_POLICY_SCHEMA, "policy schema must be %s" % BASELINE_POLICY_SCHEMA)
    _require(
        policy.get("fallback_thresholds") is False,
        "policy.fallback_thresholds must be false: thresholds may never be "
        "inferred as defaults, only pre-registered per case (H20.8)",
    )
    cases = policy.get("cases")
    _require(isinstance(cases, dict), "policy.cases must be an object")
    for case_id, entry in cases.items():
        _require(
            isinstance(case_id, str) and bool(CASE_ID_RE.match(case_id)),
            "policy case key %r must match 'domain/name'" % (case_id,),
        )
        _require(isinstance(entry, dict), "policy.cases.%s must be an object" % case_id)
        metric = _require_str(entry, "metric", "policy.cases.%s" % case_id)
        _require(metric in POLICY_METRICS, "policy metric must be one of %s" % (list(POLICY_METRICS),))
        for key in ("relative_tolerance", "noise_cap_relative"):
            value = _require_int_but_float(entry, key, "policy.cases.%s" % case_id)
            _require(value > 0, "policy.cases.%s.%s must be > 0" % (case_id, key))
        floor = entry.get("absolute_floor_ns")
        if metric == "median_ns":
            _require(
                floor is None or (isinstance(floor, int) and not isinstance(floor, bool) and floor >= 0),
                "policy.cases.%s.absolute_floor_ns must be null or a non-negative integer" % case_id,
            )
        else:
            _require(floor is None, "absolute_floor_ns must be null for throughput metric cases")
        _require_str(entry, "sampling_version", "policy.cases.%s" % case_id)
        _require_str(entry, "registered_by", "policy.cases.%s" % case_id)
        cites = entry.get("baseline_evidence")
        _require(
            isinstance(cites, list) and len(cites) >= 2 and len(set(cites)) == len(cites),
            "policy.cases.%s.baseline_evidence must list >= 2 distinct baseline run ids (H20.8)" % case_id,
        )
        for run_id in cites:
            _require(isinstance(run_id, str) and run_id, "baseline_evidence entries must be strings")
        _require_str(entry, "baseline_version", "policy.cases.%s" % case_id)
        _require(
            isinstance(entry.get("baseline_version"), str)
            and re.match(r"^v[0-9]+$", str(entry["baseline_version"])),
            "policy.cases.%s.baseline_version must match 'v<N>'" % case_id,
        )


def _require_int_but_float(obj: Dict[str, Any], key: str, where: str) -> float:
    _require(key in obj, "%s: missing required field %r" % (where, key))
    value = obj[key]
    _require(
        isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0,
        "%s.%s must be a non-negative number" % (where, key),
    )
    return float(value)


# ---------------------------------------------------------------------------
# Workload protocol (H20.3 / H20.6)
# ---------------------------------------------------------------------------

WORKLOAD_ALWAYS_METHODS = ("setup", "teardown", "verify")


def validate_workload_object(workload: Any, sample_mode: str, reset_method: str) -> None:
    """Duck-type validation of a built workload object (C20 case interface).

    Required methods:
      * always: ``setup(ctx)``, ``teardown(ctx)``, ``verify(ctx) -> dict``
      * per_operation: ``operation(ctx) -> {"ok", "units", "reason"?}``
      * amortized: ``batch(ctx, n) -> {"ok", "units", "reason"?}``
      * reset_method == batch_reset: ``reset(ctx)``
    """
    for name in WORKLOAD_ALWAYS_METHODS:
        _require(
            callable(getattr(workload, name, None)),
            "workload object missing required callable %r" % name,
        )
    if sample_mode == SAMPLE_MODE_PER_OPERATION:
        _require(callable(getattr(workload, "operation", None)), "per_operation workload missing 'operation'")
    elif sample_mode == SAMPLE_MODE_AMORTIZED:
        _require(callable(getattr(workload, "batch", None)), "amortized workload missing 'batch'")
    if reset_method == RESET_METHOD_BATCH_RESET:
        _require(callable(getattr(workload, "reset", None)), "batch_reset workload missing 'reset'")


def validate_verification_result(verification: Any) -> Dict[str, Any]:
    """Validate the dict returned by ``Workload.verify`` (H20.6)."""
    _require(isinstance(verification, dict), "verify() must return a dict")
    status = verification.get("status")
    _require(status in ("PASS", "FAIL"), "verify() status must be 'PASS' or 'FAIL'")
    digest = verification.get("digest")
    _require(
        isinstance(digest, str) and bool(HEX64_RE.match(digest)),
        "verify() digest must be a 64-char lowercase hex string",
    )
    summary = verification.get("summary")
    _require(isinstance(summary, dict), "verify() summary must be a dict")
    reasons = verification.get("fail_reasons", [])
    _require(isinstance(reasons, list), "verify() fail_reasons must be a list")
    for reason in reasons:
        _require(isinstance(reason, str), "verify() fail_reasons entries must be strings")
    return verification


# ---------------------------------------------------------------------------
# Result-record structural validation (used when loading records for
# comparison / promotion / drift so garbage JSON can never enter the pipeline)
# ---------------------------------------------------------------------------

RESULT_REQUIRED_KEYS = (
    "schema_version",
    "contract_version",
    "run_id",
    "case_id",
    "domain",
    "backend",
    "scope",
    "origin",
    "status",
    "status_reason",
    "identity",
    "environment",
    "phases",
    "measurement",
    "correctness",
    "notes",
    "started_at",
    "finished_at",
)
RESULT_ALLOWED_KEYS = RESULT_REQUIRED_KEYS + ("budget_override",)


def validate_result_record(record: Any) -> None:
    _require(isinstance(record, dict), "result record must be a JSON object")
    for key in RESULT_REQUIRED_KEYS:
        _require(key in record, "result record missing required key %r" % key)
    extra = sorted(set(record.keys()) - set(RESULT_ALLOWED_KEYS))
    _require(not extra, "result record has unknown top-level keys: %s" % extra)
    _require(
        record["schema_version"] == RESULT_SCHEMA_VERSION,
        "result record schema_version must be %s" % RESULT_SCHEMA_VERSION,
    )
    _require(record["status"] in CASE_STATUSES, "result record status must be one of %s" % (list(CASE_STATUSES),))
    _require(record["scope"] in PROFILES, "result record scope must be one of %s" % (list(PROFILES),))
    _require(bool(CASE_ID_RE.match(str(record["case_id"]))), "result record case_id malformed")
    identity = record["identity"]
    _require(isinstance(identity, dict), "result record identity must be an object")
    for key in (
        "source_map",
        "case_manifest",
        "fixtures",
        "workload",
        "measured_modules",
        "contract_version",
        "algorithm_version",
        "policy_version",
        "workload_params",
        "measurement_spec",
        "eligible_for_baseline",
    ):
        _require(key in identity, "record.identity missing %r" % key)
    environment = record["environment"]
    _require(isinstance(environment, dict), "record.environment must be an object")
    env_extra = sorted(set(environment.keys()) - set(ENVIRONMENT_KEYS))
    _require(
        not env_extra,
        "record.environment has fields outside the frozen allow-list: %s "
        "(H20.1 forbids capturing anything else)" % env_extra,
    )
    _require(isinstance(record["measurement"], dict), "record.measurement must be an object")
    _require(isinstance(record["correctness"], dict), "record.correctness must be an object")


# ---------------------------------------------------------------------------
# Case context handed to workloads (H20.3)
# ---------------------------------------------------------------------------

class CaseContext:
    """Everything a workload may receive from the harness.

    ``params``/``seed`` are frozen by the case manifest; ``tmp_dir`` is a
    run-scoped temporary directory that the harness creates and removes;
    ``resources`` carries harness-provided resource handles (currently only
    ``temp_sqlite_path``).  The context deliberately exposes NO clock: the
    performance clock is harness-owned ``perf_counter_ns`` only, and any
    business clock/RNG must be fixture-controlled (H20.3).
    """

    def __init__(
        self,
        params: Dict[str, Any],
        seed: int,
        tmp_dir: Path,
        resources: Dict[str, Any],
        normalizations: List[str],
    ) -> None:
        self.params = params
        self.seed = seed
        self.tmp_dir = tmp_dir
        self.resources = resources
        self.normalizations = normalizations
