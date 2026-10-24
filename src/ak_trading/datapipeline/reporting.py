"""DP06 recomputable summary reports (H06.1, H06.5; Q07 reporting contract).

Every report is a JSON FACTS document first: the same input snapshot, the
same alert state and the same ``as_of`` produce the byte-identical report
(``report_id`` is the canonical content hash).  Markdown summaries and the
offline HTML dashboard render FROM the same facts, so JSON / Markdown /
HTML numbers are consistent by construction and checked by tests.

Binding (H06.5): each report carries ``as_of``, the run/commit/calendar/
universe/policy hashes and the exact source query scope (input files with
content hashes).  Known-subset percentages are labelled as subsets; overall
unknown and NOT_RUN are surfaced distinctly and NEVER rendered as 0%/100%
(Q07).  Publication creates a NEW revision directory and atomically swaps
the publish index; original records are never overwritten (H06.5).

Import hygiene: importing this module performs no I/O.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .monitoring import (
    MONITOR_SCHEMA_VERSION,
    MonitoringError,
    assert_within_root,
    frozen_contracts_module,
    validate_free_text,
)

__all__ = [
    "REPORT_SCHEMA_VERSION",
    "EVIDENCE_HASH_FIELDS",
    "build_report",
    "report_id_of",
    "render_markdown",
    "publish_report",
    "sanitize_report_facts",
    "aggregate_quality_scopes",
]

REPORT_SCHEMA_VERSION = "dp06-report/1"

#: H06.5: every report binds these evidence-chain hashes (mirrors the DP01
#: frozen ``EVIDENCE_CHAIN_HASHES`` subset that a report can carry).
EVIDENCE_HASH_FIELDS: Tuple[str, ...] = (
    "run_registry_sha256",
    "commit_view_sha256",
    "calendar_evidence_sha256",
    "universe_sha256",
    "policy_sha256",
    "generation_evidence_sha256",
    "source_bundle_sha256",
)


# ---------------------------------------------------------------------------
# Coverage aggregation (frozen Q05 semantics, reused from DP01)
# ---------------------------------------------------------------------------


def aggregate_quality_scopes(quality_reports: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Aggregate DP05 QualityReport facts into a Q05-conformant summary.

    - per-scope rows keep their own check_execution/coverage_state;
    - overall check_execution is COMPLETED only when EVERY scope COMPLETED
      (else the highest-priority non-COMPLETED state wins:
      FAILED > DEFERRED_BUDGET > RUNNING > NOT_RUN);
    - coverage_state is attached only when the overall check COMPLETED;
      otherwise it is null and no overall percentage is rendered (Q07);
    - ``known_scope_aggregate`` always aggregates the COMPLETED scopes and
      is explicitly labelled a known subset (never the whole scope).  A
      verified_denominator missing from any completed scope propagates as
      None (unknown), never as zero.
    """
    contracts = frozen_contracts_module()
    check_priority = ("FAILED", "DEFERRED_BUDGET", "RUNNING", "NOT_RUN")
    if not quality_reports:
        overall_check = "NOT_RUN"
    elif all(str(r.get("check_execution", "NOT_RUN")) == "COMPLETED" for r in quality_reports):
        overall_check = "COMPLETED"
    else:
        overall_check = next(
            candidate for candidate in check_priority
            if any(str(r.get("check_execution", "NOT_RUN")) == candidate for r in quality_reports)
        )

    completed = [r for r in quality_reports if str(r.get("check_execution", "")) == "COMPLETED"]
    overall_state: Optional[str] = None
    if overall_check == "COMPLETED":
        totals = _sum_known(quality_reports)
        _, state = contracts.coverage_state_from_counts(
            check_execution="COMPLETED",
            target_total=totals["target_total"],
            verified_denominator=totals["verified_denominator"],
            valid_unique_closed=totals["valid_unique_closed"],
            missing=totals["missing"],
            conflicts=totals["conflicts"],
            quarantined=totals["quarantined"],
            provisional=totals["provisional"],
            calendar_unknown_ranges=totals["calendar_unknown_ranges"],
            source_unknown_ranges=totals["source_unknown_ranges"],
            blocked_by_conflict=(totals["conflicts"] or 0) > 0,
            reason_subset_complete=False,
        )
        overall_state = state.value if state is not None else None

    known_totals = _sum_known(completed)
    known_state: Optional[str] = None
    if completed:
        _, subset_state = contracts.coverage_state_from_counts(
            check_execution="COMPLETED",
            target_total=known_totals["target_total"],
            verified_denominator=known_totals["verified_denominator"],
            valid_unique_closed=known_totals["valid_unique_closed"],
            missing=known_totals["missing"],
            conflicts=known_totals["conflicts"],
            quarantined=known_totals["quarantined"],
            provisional=known_totals["provisional"],
            calendar_unknown_ranges=known_totals["calendar_unknown_ranges"],
            source_unknown_ranges=known_totals["source_unknown_ranges"],
            blocked_by_conflict=(known_totals["conflicts"] or 0) > 0,
            reason_subset_complete=False,
        )
        known_state = subset_state.value if subset_state is not None else None

    known_pct = None
    if known_totals["verified_denominator"] and known_totals["valid_unique_closed"] is not None:
        known_pct = round(
            100.0 * known_totals["valid_unique_closed"] / known_totals["verified_denominator"], 4
        )

    return {
        "scope_count": len(quality_reports),
        "completed_scope_count": len(completed),
        "not_run_scope_count": sum(1 for r in quality_reports if str(r.get("check_execution", "")) == "NOT_RUN"),
        "overall_check_execution": overall_check,
        "overall_coverage_state": overall_state,
        "overall_target_total": (known_totals["target_total"] if overall_check == "COMPLETED" else None),
        "known_scope_aggregate": {
            "label": "known subset (COMPLETED scopes only; never the whole scope)",
            "coverage_state": known_state,
            **{name: known_totals[name] for name in _KNOWN_KEYS},
            "known_subset_valid_closed_pct": known_pct,
            "scope_count": len(completed),
        },
        "per_scope": [
            {
                "check_execution": str(r.get("check_execution", "NOT_RUN")),
                "coverage_state": r.get("coverage_state"),
                "evaluation_id": str(r.get("evaluation_id", "")),
                "missing": r.get("missing"),
                "scope": str(r.get("scope", "")),
                "target_total": r.get("target_total"),
                "valid_unique_closed": r.get("valid_unique_closed"),
                "verified_denominator": r.get("verified_denominator"),
            }
            for r in sorted(quality_reports, key=lambda r: str(r.get("evaluation_id", "")))
        ],
    }


_KNOWN_KEYS = (
    "target_total",
    "verified_denominator",
    "valid_unique_closed",
    "missing",
    "conflicts",
    "quarantined",
    "provisional",
    "calendar_unknown_ranges",
    "source_unknown_ranges",
)


def _sum_known(reports: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Sum Q05 numbers over the given scopes.

    ``verified_denominator`` propagates as None (unknown) when ANY scope
    lacks it -- an unjudgeable denominator may never become zero.  With no
    scopes at all every number is None (NOT_RUN carries null numbers).
    Counts sum their non-None values.
    """
    if not reports:
        return {name: None for name in _KNOWN_KEYS}
    totals: Dict[str, Any] = {name: 0 for name in _KNOWN_KEYS}
    denominator_unknown = False
    for report in reports:
        for name in _KNOWN_KEYS:
            value = report.get(name)
            if value is None:
                if name == "verified_denominator":
                    denominator_unknown = True
                continue
            totals[name] += int(value)
    if denominator_unknown:
        totals["verified_denominator"] = None
    return totals


# ---------------------------------------------------------------------------
# Report facts (H06.5)
# ---------------------------------------------------------------------------


def _sha256_of_mapping(payload: Mapping[str, Any]) -> str:
    contracts = frozen_contracts_module()
    return contracts.canonical_sha256(dict(payload))


def report_id_of(facts: Mapping[str, Any]) -> str:
    """Deterministic report identity: canonical hash of the facts without
    the identity/revision fields themselves."""
    payload = {k: v for k, v in facts.items() if k not in ("report_id", "revision")}
    return _sha256_of_mapping(payload)


def build_report(
    *,
    monitor_id: str,
    as_of_nano: int,
    snapshot: Mapping[str, Any],
    alert_state: Mapping[str, Any],
    health: Mapping[str, str],
    health_reasons: Mapping[str, Sequence[str]],
    evidence_hashes: Mapping[str, Any],
    data_origin: str = "SYNTHETIC",
    wall_clock_used: bool = False,
) -> Dict[str, Any]:
    """Build the deterministic report facts document.

    ``snapshot`` is the pipeline-facts document (monitoring.load_pipeline_facts);
    ``alert_state`` is MonitorState.to_dict(); ``health``/``health_reasons``
    come from the latest tick.  The result is JSON-serializable and its
    canonical hash is the report identity (V11 determinism).
    """
    contracts = frozen_contracts_module()
    status_events = list(snapshot.get("status_events", []))
    tasks = list(snapshot.get("tasks", []))

    def events_for(task_key: str) -> List[Mapping[str, Any]]:
        return [e for e in status_events if str(e.get("task_key", "")) == task_key]

    task_rows: List[Dict[str, Any]] = []
    for task in sorted(tasks, key=lambda t: (str(t.get("source_symbol", "")), int(t.get("duration_seconds", 0)))):
        symbol = str(task.get("source_symbol", ""))
        duration = int(task.get("duration_seconds", 0))
        task_key = str(
            task.get("task_key", f"{task.get('source_id', '')}|{symbol}|{duration}")
        )
        task_events = events_for(task_key)
        last_observed = max([int(e.get("last_observed_nano", 0) or 0) for e in task_events] + [0]) or None
        last_commit = max([int(e.get("last_committed_nano", 0) or 0) for e in task_events] + [0]) or None
        target_window = task.get("target_window") or {}
        quality_row = next(
            (
                q for q in snapshot.get("quality_reports", [])
                if str(q.get("scope", "")) == task_key and str(q.get("check_execution", "")) == "COMPLETED"
            ),
            None,
        )
        task_rows.append(
            {
                "acquisition_state": str(task.get("acquisition_state", "PLANNED")),
                "conflicts": (quality_row or {}).get("conflicts"),
                "duration_seconds": duration,
                "gap_missing_in_window": (quality_row or {}).get("missing"),
                "gap_outside_window_events": sum(
                    1 for e in task_events if str(e.get("reason_sanitized", "")).startswith("OUTSIDE_FREE_WINDOW")
                ),
                "health": health.get(task_key, "UNKNOWN"),
                "health_reasons": list(health_reasons.get(task_key, ("no evaluation evidence",))),
                "instrument_kind": str(task.get("instrument_kind", "")),
                "last_commit_nano": (str(last_commit) if last_commit is not None else None),
                "last_observed_nano": (str(last_observed) if last_observed is not None else None),
                "latency_seconds": (
                    (as_of_nano - last_observed) // 1_000_000_000 if last_observed is not None else None
                ),
                "queued_events": sum(1 for e in task_events if str(e.get("reason_sanitized", "")).startswith("TASK_QUEUED")),
                "retry_count_max": max([int(e.get("retry_count", 0) or 0) for e in task_events] + [0]),
                "row_closed": (quality_row or {}).get("valid_unique_closed"),
                "calendar_unknown_ranges": (quality_row or {}).get("calendar_unknown_ranges"),
                "run_id": str(task.get("run_id", "")),
                "source_id": str(task.get("source_id", "")),
                "source_symbol": symbol,
                "target_window": {
                    "end_nano": (str(target_window.get("end_nano")) if target_window.get("end_nano") is not None else None),
                    "start_nano": (str(target_window.get("start_nano")) if target_window.get("start_nano") is not None else None),
                },
                "task_key": task_key,
            }
        )

    coverage = aggregate_quality_scopes(snapshot.get("quality_reports", []))

    alerts_summary = {
        "active": 0,
        "by_severity": {"CRITICAL": 0, "INFO": 0, "WARNING": 0},
        "by_state": {"ACKNOWLEDGED": 0, "OPEN": 0, "RESOLVED": 0},
        "open_alert_keys": [],
    }
    for key, record in sorted(alert_state.get("alerts", {}).items()):
        status = str(record.get("status", "OPEN"))
        severity = str(record.get("severity", "WARNING"))
        alerts_summary["by_state"][status] = alerts_summary["by_state"].get(status, 0) + 1
        if status in ("OPEN", "ACKNOWLEDGED"):
            alerts_summary["active"] += 1
            alerts_summary["by_severity"][severity] = alerts_summary["by_severity"].get(severity, 0) + 1
            alerts_summary["open_alert_keys"].append(key)

    facts: Dict[str, Any] = {
        "alert_summary": alerts_summary,
        "alerts": {
            key: {
                "cause": str(record.get("cause", "")),
                "event_count": len(record.get("events", [])),
                "first_raised_nano": record.get("first_raised_nano"),
                "last_raised_nano": record.get("last_raised_nano"),
                "occurrence_count": record.get("occurrence_count"),
                "reason": str(record.get("reason", "")),
                "severity": str(record.get("severity", "")),
                "status": str(record.get("status", "")),
                "suppressed_count": record.get("suppressed_count", 0),
            }
            for key, record in sorted(alert_state.get("alerts", {}).items())
        },
        "as_of_iso": contracts.nano_to_iso(as_of_nano),
        "as_of_nano": str(as_of_nano),
        "data_origin": data_origin,
        "evidence_hashes": {name: evidence_hashes.get(name) for name in EVIDENCE_HASH_FIELDS},
        "monitor_id": monitor_id,
        "schema_version": REPORT_SCHEMA_VERSION,
        "source_query_scope": {
            "calendar_answers": len(snapshot.get("calendar_answers", [])),
            "heartbeats": len(snapshot.get("heartbeats", [])),
            "quality_reports": len(snapshot.get("quality_reports", [])),
            "runs": len(snapshot.get("runs", [])),
            "sources_read": [
                {
                    "path": str(p),
                    "sha256": hashlib.sha256(Path(p).read_bytes()).hexdigest() if Path(p).is_file() else None,
                }
                for p in sorted(snapshot.get("sources_read", []))
            ],
            "status_events": len(snapshot.get("status_events", [])),
            "tasks": len(snapshot.get("tasks", [])),
        },
        "tasks": task_rows,
        "wall_clock_used": wall_clock_used,
        "coverage": coverage,
    }

    facts["consistency_metrics"] = _consistency_metrics(facts)
    facts["report_id"] = report_id_of(facts)
    return facts


def _consistency_metrics(facts: Mapping[str, Any]) -> Dict[str, int]:
    """The single number source shared by JSON/Markdown/HTML (V11)."""
    coverage = facts["coverage"]
    known = coverage["known_scope_aggregate"]
    return {
        "alerts_active": int(facts["alert_summary"]["active"]),
        "scope_count": int(coverage["scope_count"]),
        "tasks_total": len(facts["tasks"]),
        "valid_unique_closed_known": int(known["valid_unique_closed"] or 0),
        "verified_denominator_known": int(known["verified_denominator"] or 0),
        "missing_known": int(known["missing"] or 0),
    }


# ---------------------------------------------------------------------------
# Sanitization gate (H06.8)
# ---------------------------------------------------------------------------


def sanitize_report_facts(facts: Mapping[str, Any]) -> None:
    """Walk every string in the facts and block publication when any field
    is credential-shaped.  The offending VALUE never enters the error text
    (only its JSON path).  Recurses mappings/lists; bounds depth."""
    def walk(node: Any, path: str, depth: int) -> None:
        if depth > 12:
            raise MonitoringError("E_DP06_SANITIZE_DEPTH", f"report nesting too deep at {path}")
        if isinstance(node, str):
            validate_free_text(node, path)
        elif isinstance(node, Mapping):
            for key, value in node.items():
                walk(value, f"{path}.{key}", depth + 1)
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]", depth + 1)

    walk(facts, "report", 0)


# ---------------------------------------------------------------------------
# Markdown rendering (deterministic; numbers straight from the facts)
# ---------------------------------------------------------------------------


def render_markdown(facts: Mapping[str, Any]) -> str:
    coverage = facts["coverage"]
    known = coverage["known_scope_aggregate"]
    overall_state = coverage["overall_coverage_state"]
    overall_check = coverage["overall_check_execution"]

    def pct_or_none(value: Any) -> str:
        if value is None:
            return "n/a (unknown denominator; NOT_RUN scopes carry no numbers)"
        return f"{value}%"

    lines: List[str] = []
    origin = str(facts.get("data_origin", "SYNTHETIC"))
    lines.append(f"# Datapipeline monitoring report")
    lines.append("")
    lines.append(f"- data_origin: **{origin}**" + (" (SYNTHETIC fixtures; not historical fact)" if origin in ("SYNTHETIC", "SCENARIO") else ""))
    lines.append(f"- report_id: `{facts['report_id']}`")
    lines.append(f"- monitor: `{facts['monitor_id']}`")
    lines.append(f"- as_of: {facts['as_of_iso']} ({facts['as_of_nano']} ns)")
    lines.append(f"- wall_clock_used: {facts['wall_clock_used']}")
    lines.append("- evidence_hashes: " + ", ".join(
        f"{name}={value}" if value is not None else f"{name}=UNKNOWN"
        for name, value in facts["evidence_hashes"].items()
    ))
    lines.append("")
    lines.append("## Coverage (Q05 orthogonal states)")
    lines.append("")
    lines.append(f"- overall check_execution: **{overall_check}**; overall coverage_state: **{overall_state if overall_state is not None else 'null'}**")
    lines.append(
        f"- scopes: {coverage['scope_count']} total, {coverage['completed_scope_count']} COMPLETED, "
        f"{coverage['not_run_scope_count']} NOT_RUN"
    )
    if overall_check != "COMPLETED":
        lines.append("- overall percentage: **not rendered** (overall check did not COMPLETED; partial checks never display 0%/100%)")
    lines.append(
        f"- {known['label']}: state={known['coverage_state']}, "
        f"valid_closed={known['valid_unique_closed']}, denominator={known['verified_denominator']}, "
        f"known_subset_pct={pct_or_none(known['known_subset_valid_closed_pct'])}"
    )
    lines.append("")
    lines.append("## Alerts (local only; no outbound delivery)")
    lines.append("")
    summary = facts["alert_summary"]
    lines.append(
        f"- active (OPEN+ACKNOWLEDGED): {summary['active']} "
        f"(CRITICAL={summary['by_severity']['CRITICAL']}, WARNING={summary['by_severity']['WARNING']}, INFO={summary['by_severity']['INFO']}); "
        f"resolved: {summary['by_state']['RESOLVED']}"
    )
    if summary["open_alert_keys"]:
        lines.append("- open keys: " + ", ".join(f"`{key}`" for key in summary["open_alert_keys"]))
    lines.append("")
    lines.append("## Tasks (symbol x period)")
    lines.append("")
    lines.append("| symbol | period | state | health | last_observed | last_commit | latency_s | closed | missing | outside-window | queued | retry_max |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in facts["tasks"]:
        lines.append(
            "| {symbol} | {period} | {state} | {health} | {obs} | {commit} | {latency} | {closed} | {missing} | {outside} | {queued} | {retry} |".format(
                symbol=row["source_symbol"],
                period=row["duration_seconds"],
                state=row["acquisition_state"],
                health=row["health"],
                obs=row["last_observed_nano"] or "-",
                commit=row["last_commit_nano"] or "-",
                latency=(row["latency_seconds"] if row["latency_seconds"] is not None else "-"),
                closed=(row["row_closed"] if row["row_closed"] is not None else "null"),
                missing=(row["gap_missing_in_window"] if row["gap_missing_in_window"] is not None else "null"),
                outside=row["gap_outside_window_events"],
                queued=row["queued_events"],
                retry=row["retry_count_max"],
            )
        )
    lines.append("")
    lines.append("## Consistency metrics (identical in JSON / Markdown / HTML)")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("| --- | --- |")
    for name, value in facts["consistency_metrics"].items():
        lines.append(f"| {name} | {value} |")
    lines.append("")
    lines.append("## Drill-down evidence (non-secret sources)")
    lines.append("")
    for source in facts["source_query_scope"]["sources_read"]:
        lines.append(f"- `{source['path']}` (sha256 {source['sha256'] or 'n/a'})")
    lines.append("")
    lines.append("_Report pages contain no trading controls; alerts are local events only (H06.7)._")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Publication: new revision + atomic index swap (H06.5)
# ---------------------------------------------------------------------------


def publish_report(
    *,
    facts: Mapping[str, Any],
    markdown: str,
    html: str,
    output_root: Path,
    monitor_id: str,
) -> Dict[str, Any]:
    """Sanitize, then publish report.json / summary.md / dashboard.html into
    ``reports/<report_id>/rN/`` (new revision via exclusive mkdir; previous
    revisions untouched) and atomically swap the publish index pointer."""
    sanitize_report_facts(facts)

    contracts = frozen_contracts_module()
    report_id = str(facts["report_id"])
    root = Path(output_root)
    report_dir = root / "reports" / report_id
    assert_within_root(root, report_dir)
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MonitoringError("E_DP06_DISK_OUTPUT_FAILED", f"cannot create report dir ({type(exc).__name__})") from None

    revision = 1
    while True:
        revision_dir = report_dir / f"r{revision}"
        try:
            revision_dir.mkdir()
            break
        except FileExistsError:
            revision += 1
            if revision > 9999:
                raise MonitoringError("E_DP06_DISK_OUTPUT_FAILED", "revision counter exhausted") from None
        except OSError as exc:
            raise MonitoringError("E_DP06_DISK_OUTPUT_FAILED", f"cannot create revision dir ({type(exc).__name__})") from None

    payload_with_revision = dict(facts)
    payload_with_revision["revision"] = revision
    body = {
        "dashboard.html": html,
        "report.json": json.dumps(payload_with_revision, sort_keys=True, ensure_ascii=False, indent=1) + "\n",
        "summary.md": markdown,
    }
    try:
        for name, content in body.items():
            (revision_dir / name).write_text(content, encoding="utf-8")
    except OSError as exc:
        raise MonitoringError("E_DP06_DISK_OUTPUT_FAILED", f"cannot write report files ({type(exc).__name__})") from None

    report_path = revision_dir / "report.json"
    index_payload = {
        "latest_report": {
            "path": str(report_path.relative_to(root)).replace("\\", "/"),
            "report_id": report_id,
            "revision": revision,
            "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        },
        "monitor_id": monitor_id,
        "schema_version": REPORT_SCHEMA_VERSION,
    }
    index_path = root / "publish-index" / f"{_safe_name(monitor_id)}.json"
    _merge_publish_index(root, index_path, index_payload)
    _ = contracts
    return {
        "path": str(report_path),
        "report_id": report_id,
        "revision": revision,
        "sha256": index_payload["latest_report"]["sha256"],
    }


def _safe_name(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").replace("..", "_")


def _merge_publish_index(root: Path, index_path: Path, payload: Mapping[str, Any]) -> None:
    assert_within_root(root, index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    merged: Dict[str, Any] = {}
    if index_path.exists():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                merged = loaded
        except (OSError, ValueError):
            merged = {}
    merged.update(payload)
    data = json.dumps(merged, sort_keys=True, ensure_ascii=False, indent=1)
    fd, tmp_name = tempfile.mkstemp(dir=str(index_path.parent), prefix=".tmp-", suffix=".json")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, index_path)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise MonitoringError("E_DP06_DISK_OUTPUT_FAILED", "publish-index atomic swap failed") from None
