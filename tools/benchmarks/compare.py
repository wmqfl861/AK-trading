"""NF20 comparison protocol - baseline policy, verdicts, promotion, drift.

Implements the comparison half of the C20 contract:

* H20.7  matching: workload / fixture / contract / backend / measurement
  scope / environment must match.  Baseline and candidate SOURCE hashes are
  saved separately and are deliberately NOT a match criterion - comparing a
  changed source against its baseline is the entire purpose.  Different host
  or semantic configuration -> INCOMPARABLE; missing baseline -> NO_BASELINE
  (never counts as a pass); matched but within pre-registered noise ->
  INCONCLUSIVE.
* H20.8  thresholds: first round only establishes baselines; per-case
  relative tolerance / absolute floor / noise cap / sampling version /
  responsibility are pre-registered by the NF20 owner after >= 2 independent
  baseline samples; no fallback defaults exist.  Verdicts: REGRESSION,
  NO_REGRESSION, INCONCLUSIVE, INCOMPARABLE (NO_BASELINE, INVALID included).
* H20.9  continuous entry: explicit CLI, JSON + Markdown reports, versioned
  baseline store, drift check; promotion is manual and never automatic.

Standard library only.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tools.benchmarks import contracts as C

DEFAULT_MIN_COMPARE_SAMPLES = C.FORMAL_MIN_TOTAL_SAMPLES


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# record / run loading
# ---------------------------------------------------------------------------

def _load_record(path: Path) -> Dict[str, Any]:
    record = C.load_json_file(path, "result record")
    C.validate_result_record(record)
    return record


def load_run(run_dir: Path) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Load ``run.json`` plus every per-case result record of a run."""
    run_summary = C.load_json_file(run_dir / "run.json", "run summary")
    records: Dict[str, Dict[str, Any]] = {}
    results_dir = run_dir / "results"
    if not results_dir.is_dir():
        raise C.ContractError("run directory has no results/: %s" % run_dir)
    for path in sorted(results_dir.glob("*.json")):
        record = _load_record(path)
        records[record["case_id"]] = record
    if not records:
        raise C.ContractError("run directory has no case records: %s" % run_dir)
    return run_summary, records


# ---------------------------------------------------------------------------
# baseline store (versioned, manual promotion only - H20.8/H20.9)
# ---------------------------------------------------------------------------

class BaselineStore:
    """``<dir>/<case_id>/v<N>/{result.json, meta.json}`` - immutable versions."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _case_dir(self, case_id: str) -> Path:
        if not C.CASE_ID_RE.match(case_id):
            raise C.ContractError("baseline store case id malformed: %r" % case_id)
        return self.root / case_id.replace("/", "__")

    def versions(self, case_id: str) -> List[int]:
        case_dir = self._case_dir(case_id)
        if not case_dir.is_dir():
            return []
        found = []
        for child in case_dir.iterdir():
            name = child.name
            if child.is_dir() and name.startswith("v") and name[1:].isdigit():
                if (child / "result.json").is_file() and (child / "meta.json").is_file():
                    found.append(int(name[1:]))
        return sorted(found)

    def get(self, case_id: str, version: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        path = self._case_dir(case_id) / ("v%d" % version)
        record = _load_record(path / "result.json")
        meta = C.load_json_file(path / "meta.json", "baseline meta")
        return record, meta

    def promote(
        self,
        case_id: str,
        record: Dict[str, Any],
        source_run_id: str,
        source_run_dir: Path,
        promoter: str,
    ) -> int:
        """Promote one OK formal case record to a NEW baseline version.

        Refuses (ContractError): non-OK records, smoke-scope records,
        TEST_SIMULATED origin, non eligible cases, missing promoter name.
        Never overwrites: a new ``v<N+1>`` directory is created.
        """
        if record["case_id"] != case_id:
            raise C.ContractError("record case_id %r != requested %r" % (record["case_id"], case_id))
        if record["status"] != C.CASE_STATUS_OK:
            raise C.ContractError(
                "refusing promotion: case status is %s (%s); only OK runs may "
                "become baselines" % (record["status"], record["status_reason"])
            )
        if record["scope"] != C.PROFILE_FORMAL:
            raise C.ContractError(
                "refusing promotion: scope is %r; baselines must be formal "
                "measurements (smoke only proves runnability)" % record["scope"]
            )
        if record["origin"] == C.ORIGIN_TEST_SIMULATED:
            raise C.ContractError(
                "refusing promotion: origin TEST_SIMULATED never enters the "
                "real baseline store (H20 acceptance: simulated timing stays TEST)"
            )
        if not record["identity"].get("eligible_for_baseline"):
            raise C.ContractError(
                "refusing promotion: case manifest declares eligible_for_baseline=false"
            )
        if not promoter or not promoter.strip():
            raise C.ContractError("refusing promotion: --promoter is required (H20.8 responsibility)")
        versions = self.versions(case_id)
        next_version = (versions[-1] + 1) if versions else 1
        target = self._case_dir(case_id) / ("v%d" % next_version)
        if target.exists():
            raise C.ContractError("refusing promotion: %s already exists" % target)
        target.mkdir(parents=True)
        meta = {
            "schema": C.BASELINE_META_SCHEMA,
            "baseline_version": "v%d" % next_version,
            "case_id": case_id,
            "source_run_id": source_run_id,
            "source_run_dir": str(source_run_dir),
            "promoted_at_utc": _utc_now_iso(),
            "promoter": promoter,
            "policy_note": (
                "promotion does not register thresholds and does not update "
                "the policy pin; threshold pre-registration after >= 2 "
                "independent baselines is a separate NF20-owner action (H20.8)"
            ),
        }
        C.write_json_atomic(target / "result.json", record)
        C.write_json_atomic(target / "meta.json", meta)
        return next_version


# ---------------------------------------------------------------------------
# policy loading / citation validation (H20.8)
# ---------------------------------------------------------------------------

def load_policy(path: Path) -> Tuple[Dict[str, Any], str]:
    policy = C.load_json_file(path.resolve(), "baseline policy")
    C.validate_baseline_policy(policy)
    sha = C.sha256_file(path.resolve())[0]
    return policy, sha


def validate_policy_citations(
    policy: Dict[str, Any],
    runs_root: Optional[Path],
) -> Dict[str, List[str]]:
    """Per-case citation problems.

    Structural requirements (>= 2 distinct run ids, etc.) are enforced by
    ``contracts.validate_baseline_policy``.  When ``runs_root`` is given,
    every cited run must exist and contain an OK record for the case; a
    violation invalidates the entry (its cases compare as INCONCLUSIVE).
    """
    problems: Dict[str, List[str]] = {}
    if runs_root is None:
        return problems
    for case_id, entry in policy["cases"].items():
        case_problems: List[str] = []
        for run_id in entry["baseline_evidence"]:
            run_dir = runs_root / "runs" / run_id
            result_file = run_dir / "results" / ("%s.json" % case_id.replace("/", "__"))
            if not run_dir.is_dir() or not result_file.is_file():
                case_problems.append("cited baseline run %s has no record for %s" % (run_id, case_id))
                continue
            record = _load_record(result_file)
            if record["status"] != C.CASE_STATUS_OK:
                case_problems.append(
                    "cited baseline run %s case %s has status %s" % (run_id, case_id, record["status"])
                )
        if case_problems:
            problems[case_id] = case_problems
    return problems


# ---------------------------------------------------------------------------
# matching (H20.7)
# ---------------------------------------------------------------------------

def match_records(candidate: Dict[str, Any], baseline: Dict[str, Any]) -> List[str]:
    """All reasons the two records are INCOMPARABLE.  Source hashes are
    intentionally absent from this list (H20.7)."""
    reasons: List[str] = []
    if candidate["case_id"] != baseline["case_id"]:
        reasons.append("CASE_ID_MISMATCH")
    if candidate["scope"] != C.PROFILE_FORMAL or baseline["scope"] != C.PROFILE_FORMAL:
        reasons.append("SCOPE_PROFILE_MISMATCH(%s vs %s)" % (candidate["scope"], baseline["scope"]))
    if candidate["backend"] != baseline["backend"]:
        reasons.append("BACKEND_MISMATCH(%s vs %s)" % (candidate["backend"], baseline["backend"]))
    cand_id, base_id = candidate["identity"], baseline["identity"]
    if cand_id["contract_version"] != base_id["contract_version"]:
        reasons.append(
            "CONTRACT_VERSION_MISMATCH(%s vs %s)"
            % (cand_id["contract_version"], base_id["contract_version"])
        )
    if C.canonical_json_bytes(cand_id["workload_params"]) != C.canonical_json_bytes(
        base_id["workload_params"]
    ):
        reasons.append("WORKLOAD_PARAMS_MISMATCH")
    cand_fix = sorted((f["path"], f["sha256"]) for f in cand_id["fixtures"])
    base_fix = sorted((f["path"], f["sha256"]) for f in base_id["fixtures"])
    if cand_fix != base_fix:
        reasons.append("FIXTURE_MISMATCH")
    for field in C.MEASUREMENT_SPEC_MATCH_FIELDS:
        if cand_id["measurement_spec"].get(field) != base_id["measurement_spec"].get(field):
            reasons.append("MEASUREMENT_SCOPE_MISMATCH(%s)" % field)
    for key in C.ENVIRONMENT_MATCH_KEYS:
        if candidate["environment"].get(key) != baseline["environment"].get(key):
            label = "HOST_MISMATCH" if key == "host_id" else "ENV_MISMATCH(%s)" % key
            reasons.append(label)
    return reasons


# ---------------------------------------------------------------------------
# verdict state machine (H20.7 / H20.8; order frozen)
# ---------------------------------------------------------------------------

def _side_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    identity = record["identity"]
    measured = [
        {"logical_module": m["logical_module"], "sha256": m["sha256"]}
        for m in identity["measured_modules"]
    ]
    measurement = record["measurement"]
    return {
        "run_id": record["run_id"],
        "status": record["status"],
        "scope": record["scope"],
        "backend": record["backend"],
        "source_map_sha256": identity["source_map"]["sha256"],
        "measured_modules": measured,
        "n": measurement["n"],
        "median_ns": measurement["median_ns"],
        "throughput_units_per_s": measurement["throughput_units_per_s"],
        "host_id": record["environment"]["host_id"],
    }


def _metric_values(record: Dict[str, Any], metric: str) -> Optional[float]:
    if metric == "median_ns":
        value = record["measurement"]["median_ns"]
    else:
        value = record["measurement"]["throughput_units_per_s"]
    if value is None:
        return None
    return float(value)


def _effective_sample_count(record: Dict[str, Any]) -> int:
    """Valid measurements available for comparison.

    ``per_operation``: the recorded ``n`` (latency samples).  ``amortized``:
    the number of completed (non-timed-out) batch measurements - batch
    totals are the only samples amortized cases produce (H20.5).
    """
    measurement = record["measurement"]
    if measurement.get("sample_mode") == C.SAMPLE_MODE_AMORTIZED:
        return len([b for b in measurement.get("batches", []) if not b.get("timed_out")])
    return int(measurement.get("n", 0) or 0)


def verdict_for_case(
    candidate: Dict[str, Any],
    store: BaselineStore,
    policy_entry: Optional[Dict[str, Any]],
    citation_problems: Optional[List[str]],
) -> Dict[str, Any]:
    verdict: Dict[str, Any] = {
        "case_id": candidate["case_id"],
        "verdict": None,
        "reason": None,
        "metric": policy_entry["metric"] if policy_entry else None,
        "baseline": None,
        "candidate": _side_summary(candidate),
        "delta": None,
        "rel_change": None,
        "match_reasons": [],
    }

    # (a) candidate must be OK - INVALID candidates never enter speed ranking
    if candidate["status"] != C.CASE_STATUS_OK:
        verdict["verdict"] = C.VERDICT_INVALID
        verdict["reason"] = "CANDIDATE_NOT_OK_%s: %s" % (candidate["status"], candidate["status_reason"])
        return verdict

    # (b) no baseline record at all
    versions = store.versions(candidate["case_id"])
    if not versions:
        verdict["verdict"] = C.VERDICT_NO_BASELINE
        verdict["reason"] = "NO_BASELINE_RECORD: baseline store has no version of this case"
        return verdict

    # (c) policy entry must be pre-registered
    if policy_entry is None:
        verdict["verdict"] = C.VERDICT_INCONCLUSIVE
        verdict["reason"] = (
            "POLICY_NOT_PREREGISTERED: first round establishes baselines only; "
            "the NF20 owner must pre-register thresholds after >= 2 independent "
            "baseline samples (H20.8)"
        )
        verdict["baseline"] = {"available_versions": ["v%d" % v for v in versions]}
        return verdict

    # (d) citation evidence must hold
    if citation_problems:
        verdict["verdict"] = C.VERDICT_INCONCLUSIVE
        verdict["reason"] = "POLICY_ENTRY_INVALID: %s" % "; ".join(citation_problems)[:300]
        return verdict

    # (e) pinned baseline version must exist
    pinned = str(policy_entry["baseline_version"])
    pinned_int = int(pinned[1:])
    if pinned_int not in versions:
        verdict["verdict"] = C.VERDICT_NO_BASELINE
        verdict["reason"] = "PINNED_VERSION_MISSING: policy pins %s, store has %s" % (
            pinned, ["v%d" % v for v in versions]
        )
        return verdict
    baseline_record, baseline_meta = store.get(candidate["case_id"], pinned_int)
    verdict["baseline"] = _side_summary(baseline_record)
    verdict["baseline"]["baseline_version"] = pinned
    verdict["baseline"]["source_run_id"] = baseline_meta.get("source_run_id")

    # (f) baseline itself must be OK
    if baseline_record["status"] != C.CASE_STATUS_OK:
        verdict["verdict"] = C.VERDICT_INCOMPARABLE
        verdict["reason"] = "BASELINE_NOT_OK_%s" % baseline_record["status"]
        return verdict

    # (g) match (scope/backend/contract/params/fixtures/measurement/env)
    reasons = match_records(candidate, baseline_record)
    verdict["match_reasons"] = reasons
    if reasons:
        verdict["verdict"] = C.VERDICT_INCOMPARABLE
        verdict["reason"] = "%s (+%d more)" % (reasons[0], max(0, len(reasons) - 1))
        return verdict

    # (h) sample sufficiency, zero denominators, metric availability
    metric = str(policy_entry["metric"])
    min_samples = int(policy_entry.get("min_samples", DEFAULT_MIN_COMPARE_SAMPLES))
    cand_count = _effective_sample_count(candidate)
    base_count = _effective_sample_count(baseline_record)
    if cand_count < min_samples or base_count < min_samples:
        verdict["verdict"] = C.VERDICT_INCONCLUSIVE
        verdict["reason"] = (
            "INSUFFICIENT_SAMPLES: need >= %d valid measurements on both sides "
            "(candidate %d, baseline %d)" % (min_samples, cand_count, base_count)
        )
        return verdict

    base_value = _metric_values(baseline_record, metric)
    cand_value = _metric_values(candidate, metric)
    if base_value is None or cand_value is None:
        verdict["verdict"] = C.VERDICT_INVALID
        verdict["reason"] = "METRIC_UNAVAILABLE: %s missing on one side" % metric
        return verdict
    if base_value <= 0:
        verdict["verdict"] = C.VERDICT_INVALID
        verdict["reason"] = "ZERO_DENOMINATOR: baseline %s is %s" % (metric, base_value)
        return verdict

    delta = cand_value - base_value
    if metric == "throughput":
        # lower throughput = slower: normalize so rel > 0 means candidate
        # slower on both metrics
        rel = (base_value - cand_value) / base_value
    else:
        rel = delta / base_value
    verdict["delta"] = delta
    verdict["rel_change"] = rel

    # (i) within pre-registered noise?
    noise_cap = float(policy_entry["noise_cap_relative"])
    if abs(rel) <= noise_cap:
        verdict["verdict"] = C.VERDICT_INCONCLUSIVE
        verdict["reason"] = "WITHIN_NOISE: |rel|=%.4f <= noise cap %.4f" % (abs(rel), noise_cap)
        return verdict

    # (j) threshold judgement (regression = candidate slower)
    tolerance = float(policy_entry["relative_tolerance"])
    floor = policy_entry.get("absolute_floor_ns")
    slower = rel > 0
    if metric == "median_ns" and floor is not None:
        exceeds = slower and rel > tolerance and delta > float(floor)
    else:
        exceeds = slower and rel > tolerance
    if exceeds:
        verdict["verdict"] = C.VERDICT_REGRESSION
        verdict["reason"] = (
            "candidate slower: rel=%.4f > tolerance %.4f%s"
            % (rel, tolerance, "" if floor is None else " and |delta| > absolute floor")
        )
    else:
        verdict["verdict"] = C.VERDICT_NO_REGRESSION
        verdict["reason"] = (
            "within tolerance (rel=%.4f, tolerance %.4f); improvement is not "
            "claimed (H20.8 first round)" % (rel, tolerance)
        )
    return verdict


# ---------------------------------------------------------------------------
# report assembly (H20.9)
# ---------------------------------------------------------------------------

def compare_run(
    candidate_run_dir: Path,
    baseline_store_dir: Path,
    policy_path: Path,
    out_dir: Path,
    runs_root: Optional[Path] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Compare every case of a candidate run. Returns ``(exit_code, report)``.

    Exit code 3 when any verdict is REGRESSION or INVALID; 0 otherwise
    (INCONCLUSIVE / INCOMPARABLE / NO_BASELINE are informational).
    """
    if out_dir.exists():
        raise C.ContractError("compare output directory already exists: %s" % out_dir)
    run_summary, records = load_run(candidate_run_dir)
    policy, policy_sha = load_policy(policy_path)
    citation_problems = validate_policy_citations(policy, runs_root)
    store = BaselineStore(baseline_store_dir)

    verdicts: List[Dict[str, Any]] = []
    for case_id in sorted(records):
        entry = policy["cases"].get(case_id)
        verdicts.append(
            verdict_for_case(records[case_id], store, entry, citation_problems.get(case_id))
        )

    summary = {verdict: 0 for verdict in C.VERDICTS}
    for verdict in verdicts:
        summary[verdict["verdict"]] += 1
    report = {
        "schema": C.COMPARE_REPORT_SCHEMA,
        "contract_version": C.CONTRACT_VERSION,
        "generated_at_utc": _utc_now_iso(),
        "candidate": {
            "run_id": run_summary.get("run_id"),
            "run_dir": str(candidate_run_dir),
            "profile": run_summary.get("profile"),
        },
        "baseline_store": {"dir": str(baseline_store_dir)},
        "policy": {"path": str(policy_path), "sha256": policy_sha},
        "cases": verdicts,
        "summary": summary,
        "note": (
            "source hashes of both sides are recorded per case and are not a "
            "match criterion (H20.7); NO_BASELINE is never a pass"
        ),
    }
    out_dir.mkdir(parents=True)
    C.write_json_atomic(out_dir / "report.json", report)
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8", newline="\n")
    bad = summary[C.VERDICT_REGRESSION] + summary[C.VERDICT_INVALID]
    return (3 if bad else 0), report


def render_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# Benchmark comparison report")
    lines.append("")
    lines.append(
        "- candidate run: `%s` (%s)" % (
            report["candidate"]["run_id"], report["candidate"]["run_dir"]
        )
    )
    lines.append("- baseline store: `%s`" % report["baseline_store"]["dir"])
    lines.append(
        "- policy: `%s` (sha256 `%s`)" % (report["policy"]["path"], report["policy"]["sha256"][:16] + "...")
    )
    lines.append("- generated: %s" % report["generated_at_utc"])
    lines.append("")
    lines.append("| case | verdict | reason | metric | baseline | candidate | rel |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for verdict in report["cases"]:
        metric = verdict.get("metric") or "-"
        base = verdict.get("baseline") or {}
        if verdict.get("rel_change") is not None:
            rel = "%+.2f%%" % (verdict["rel_change"] * 100)
        else:
            rel = "-"
        if metric == "median_ns":
            base_v = _fmt_ns(base.get("median_ns"))
            cand_v = _fmt_ns(verdict["candidate"].get("median_ns"))
        elif metric == "throughput":
            base_v = _fmt_thr(base.get("throughput_units_per_s"))
            cand_v = _fmt_thr(verdict["candidate"].get("throughput_units_per_s"))
        else:
            base_v = base.get("baseline_version") or "-"
            cand_v = "-"
        lines.append(
            "| `%s` | **%s** | %s | %s | %s | %s | %s |"
            % (verdict["case_id"], verdict["verdict"], verdict["reason"] or "-", metric, base_v, cand_v, rel)
        )
        base_sources = ",".join(m["sha256"][:8] for m in base.get("measured_modules", [])) or "-"
        cand_sources = ",".join(
            m["sha256"][:8] for m in verdict["candidate"].get("measured_modules", [])
        ) or "-"
        lines.append(
            "  - sources: baseline `%s` vs candidate `%s` (saved separately; "
            "difference is expected, not INCOMPARABLE)" % (base_sources, cand_sources)
        )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    summary = report["summary"]
    lines.append(
        "- REGRESSION: %d, NO_REGRESSION: %d, INCONCLUSIVE: %d, INCOMPARABLE: %d, "
        "NO_BASELINE: %d, INVALID: %d"
        % (
            summary[C.VERDICT_REGRESSION], summary[C.VERDICT_NO_REGRESSION],
            summary[C.VERDICT_INCONCLUSIVE], summary[C.VERDICT_INCOMPARABLE],
            summary[C.VERDICT_NO_BASELINE], summary[C.VERDICT_INVALID],
        )
    )
    lines.append("")
    return "\n".join(lines)


def _fmt_ns(value: Any) -> str:
    if value is None:
        return "-"
    return "%.0f ns" % value


def _fmt_thr(value: Any) -> str:
    if value is None:
        return "-"
    return "%.1f u/s" % value


# ---------------------------------------------------------------------------
# drift check (H20.9 / H20.10)
# ---------------------------------------------------------------------------

def drift_check(root: Path, record: Dict[str, Any]) -> Dict[str, Any]:
    """Re-hash every identity input of a record against the current worktree.

    Invariants (manifest, suite, fixtures, workload, source-map file) must be
    byte-identical or the report is DRIFT.  Source-map entries are listed as
    informational items only: candidates are expected to be measured against
    changing sources; the source files themselves are not drift.
    """
    from tools.benchmarks import runner  # local import to avoid cycle at module load

    identity = record["identity"]
    if identity["case_manifest"]["sha256"] is None:
        raise C.ContractError(
            "record has no hashed identity (case never executed); drift check undefined"
        )
    ok, problems = runner.drift_recheck(root, identity)
    items = [
        {
            "kind": "drift",
            "path": p["path"],
            "state": p["state"],
            "detail": p["detail"],
        }
        for p in problems
    ]
    source_items: List[Dict[str, Any]] = []
    for measured in identity["measured_modules"]:
        rel = measured["source_relative_path"]
        try:
            info = runner.hash_relative(root, rel)
            state = "OK" if info["sha256"] == measured["sha256"] else "INFO_SOURCE_CHANGED"
            current = info["sha256"]
        except C.ContractError as exc:
            state = "INFO_SOURCE_MISSING"
            current = None
            rel = "%s (%s)" % (rel, exc)
        source_items.append(
            {
                "kind": "source",
                "logical_module": measured["logical_module"],
                "path": measured["source_relative_path"],
                "recorded_sha256": measured["sha256"],
                "current_sha256": current,
                "state": state,
            }
        )
    return {
        "schema": C.DRIFT_REPORT_SCHEMA,
        "contract_version": C.CONTRACT_VERSION,
        "run_id": record["run_id"],
        "case_id": record["case_id"],
        "generated_at_utc": _utc_now_iso(),
        "status": "NO_DRIFT" if ok else "DRIFT",
        "invariant_items": items,
        "source_items": source_items,
        "note": (
            "invariant inputs (case manifest, suite, fixtures, workload, "
            "source-map file) must be unchanged; measured source files are "
            "informational (H20.10: drift ends the attempt, result preserved)"
        ),
    }
