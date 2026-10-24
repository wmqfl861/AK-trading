"""DP06 reporting tests: determinism, binding, Q07 number discipline,
cross-format consistency (JSON/Markdown/HTML), sanitization gate, atomic
revisioned publication, and the offline CLI exit codes.

All inputs are SYNTHETIC fixtures; the CLI runs offline against temporary
output roots.  No network, no database, no task registration.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ak_trading.datapipeline import monitoring as mon  # noqa: E402
from ak_trading.datapipeline import reporting as rep  # noqa: E402
from ak_trading.datapipeline.dashboard import render_html  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "DP06"
INPUTS = FIXTURES / "inputs"
CLI = REPO_ROOT / "scripts" / "report_datapipeline.py"
AS_OF = mon.frozen_contracts_module().iso_instant_to_nano("2026-09-20T18:35:00+00:00")
AS_OF_ISO = "2026-09-20T18:35:00+00:00"
HASHES = json.loads((INPUTS / "synthetic_hashes.json").read_text(encoding="utf-8"))


def load_snapshot() -> dict:
    return mon.load_pipeline_facts(
        heartbeats_root=INPUTS / "heartbeats",
        events_root=INPUTS / "events",
        quality_root=INPUTS / "quality",
        tasks_file=INPUTS / "synthetic_tasks.json",
        calendar_file=INPUTS / "synthetic_calendar_answers.json",
        runs_file=INPUTS / "synthetic_run_registry.json",
    )


def build_fixture_facts(snapshot: dict = None, as_of: int = AS_OF) -> dict:
    snapshot = snapshot or load_snapshot()
    state = mon.MonitorState(monitor_id="rep-test")
    result = mon.evaluate_tick(state, snapshot, as_of)
    return rep.build_report(
        monitor_id="rep-test",
        as_of_nano=as_of,
        snapshot=snapshot,
        alert_state=result.state.to_dict(),
        health=result.health,
        health_reasons=result.health_reasons,
        evidence_hashes=HASHES,
    )


# ---------------------------------------------------------------------------
# H06.5: determinism, binding, source scope
# ---------------------------------------------------------------------------


def test_report_is_deterministic_same_inputs_same_id():
    first = build_fixture_facts()
    second = build_fixture_facts()
    assert first == second
    assert first["report_id"] == second["report_id"]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_report_id_changes_with_as_of_or_inputs():
    base = build_fixture_facts()
    later = build_fixture_facts(as_of=AS_OF + 60 * 10**9)
    assert later["report_id"] != base["report_id"]
    snapshot = copy.deepcopy(load_snapshot())
    snapshot["status_events"] = snapshot["status_events"][:-1]
    changed = build_fixture_facts(snapshot=snapshot)
    assert changed["report_id"] != base["report_id"]


def test_report_binds_as_of_hashes_and_source_scope():
    facts = build_fixture_facts()
    assert facts["as_of_nano"] == str(AS_OF)
    assert facts["evidence_hashes"]["universe_sha256"] == HASHES["universe_sha256"]
    assert facts["evidence_hashes"]["generation_evidence_sha256"] is None   # UNKNOWN form
    scope = facts["source_query_scope"]
    assert scope["status_events"] == 15 and scope["tasks"] == 3
    assert scope["sources_read"] and all(entry["sha256"] for entry in scope["sources_read"])


# ---------------------------------------------------------------------------
# Q07: NOT_RUN nulls, subset percentages, no fake 0/100
# ---------------------------------------------------------------------------


def test_not_run_scopes_carry_null_numbers_and_no_percentage():
    facts = build_fixture_facts()
    coverage = facts["coverage"]
    # one COMPLETED + one NOT_RUN -> overall is not COMPLETED, no overall pct
    assert coverage["overall_check_execution"] == "NOT_RUN"
    assert coverage["overall_coverage_state"] is None
    t2_scope = next(s for s in coverage["per_scope"] if s["evaluation_id"] == "q-20260920-t2")
    assert t2_scope["check_execution"] == "NOT_RUN"
    for name in ("valid_unique_closed", "verified_denominator", "missing", "target_total"):
        assert t2_scope[name] is None
    markdown = rep.render_markdown(facts)
    assert "overall percentage: **not rendered**" in markdown
    assert "known subset" in markdown
    known_pct = coverage["known_scope_aggregate"]["known_subset_valid_closed_pct"]
    assert known_pct is not None and known_pct != 100.0
    assert f"known_subset_pct={known_pct}%" in markdown
    html = render_html(facts)
    assert "not rendered" in html
    assert "KNOWN SUBSET" in html
    # The NOT_RUN task row shows null, never a green zero.
    t2_row = next(row for row in facts["tasks"] if row["duration_seconds"] == 86400)
    assert t2_row["row_closed"] is None and t2_row["gap_missing_in_window"] is None


def test_totals_conservation_json_groups_match_summary():
    facts = build_fixture_facts()
    metrics = facts["consistency_metrics"]
    per_scope_completed = [
        s for s in facts["coverage"]["per_scope"] if s["check_execution"] == "COMPLETED"
    ]
    assert metrics["valid_unique_closed_known"] == sum(s["valid_unique_closed"] for s in per_scope_completed)
    assert metrics["missing_known"] == sum(s["missing"] for s in per_scope_completed)
    assert metrics["verified_denominator_known"] == sum(s["verified_denominator"] for s in per_scope_completed)
    assert metrics["tasks_total"] == len(facts["tasks"])
    assert metrics["alerts_active"] == facts["alert_summary"]["active"]


def test_markdown_and_html_numbers_match_json():
    facts = build_fixture_facts()
    markdown = rep.render_markdown(facts)
    html = render_html(facts)
    for name, value in facts["consistency_metrics"].items():
        assert f"| {name} | {value} |" in markdown
        assert f"<dt>{name}</dt><dd>{value}</dd>" in html


# ---------------------------------------------------------------------------
# H06.8: escaping, offline, no trading surface, synthetic labeling
# ---------------------------------------------------------------------------


def test_html_escapes_hostile_symbols_and_errors():
    facts = build_fixture_facts()
    hostile = "<script>alert('XSS')</script>"
    facts["tasks"][0]["source_symbol"] = hostile
    facts["alerts"] = {
        "X|task=1": {
            "cause": "quote & <error> \"text\"",
            "event_count": 1, "first_raised_nano": "1", "last_raised_nano": "2",
            "occurrence_count": 1, "reason": "R", "severity": "WARNING",
            "status": "OPEN", "suppressed_count": 0,
        }
    }
    html = render_html(facts)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html


def test_html_offline_no_external_refs_and_no_trading_controls():
    html = render_html(build_fixture_facts())
    lowered = html.lower()
    assert "http://" not in lowered and "https://" not in lowered
    assert "<script" not in lowered
    for marker in ("insert_order", "cancel_order", "enable_trading", "permit_trading"):
        assert marker not in lowered


def test_synthetic_origin_is_labeled():
    facts = build_fixture_facts()
    markdown = rep.render_markdown(facts)
    assert facts["data_origin"] == "SYNTHETIC"
    assert "SYNTHETIC" in markdown
    assert "SYNTHETIC" in render_html(facts)


# ---------------------------------------------------------------------------
# Publication: sanitize gate, new revisions, atomic index (H06.5)
# ---------------------------------------------------------------------------


def test_credential_shape_blocks_publish_entirely(tmp_path):
    facts = build_fixture_facts()
    facts["alerts"]["LEAK|task=1"] = {
        "cause": "db link postgres://user:pass@host/db failed",
        "event_count": 1, "first_raised_nano": "1", "last_raised_nano": "2",
        "occurrence_count": 1, "reason": "R", "severity": "WARNING",
        "status": "OPEN", "suppressed_count": 0,
    }
    with pytest.raises(mon.MonitoringError) as excinfo:
        rep.publish_report(
            facts=facts, markdown=rep.render_markdown(facts), html=render_html(facts),
            output_root=tmp_path, monitor_id="m",
        )
    assert excinfo.value.code == "E_DP06_CREDENTIAL_SHAPE_BLOCKED"
    assert not (tmp_path / "reports").exists()      # blocked before any write


def test_publish_creates_new_revisions_and_keeps_originals(tmp_path):
    facts = build_fixture_facts()
    markdown = rep.render_markdown(facts)
    html = render_html(facts)
    first = rep.publish_report(facts=facts, markdown=markdown, html=html, output_root=tmp_path, monitor_id="m")
    r1 = Path(first["path"])
    r1_bytes = r1.read_bytes()

    second = rep.publish_report(facts=facts, markdown=markdown, html=html, output_root=tmp_path, monitor_id="m")
    assert second["report_id"] == first["report_id"]
    assert second["revision"] == 2
    r2 = Path(second["path"])
    assert r2.parent.name == "r2"
    assert r1.read_bytes() == r1_bytes                     # original untouched

    index = json.loads((tmp_path / "publish-index" / "m.json").read_text(encoding="utf-8"))
    assert index["latest_report"]["revision"] == 2
    assert index["latest_report"]["sha256"] == second["sha256"]
    # published JSON carries the revision it was published under
    published = json.loads(r1.read_text(encoding="utf-8"))
    assert published["revision"] == 1


# ---------------------------------------------------------------------------
# CLI: offline run, exit codes, determinism across runs
# ---------------------------------------------------------------------------


def _run_cli(tmp_path: Path, output: Path, *extra: str) -> subprocess.CompletedProcess:
    args = [
        sys.executable, "-B", str(CLI),
        "--output-root", str(output),
        "--heartbeats-root", str(INPUTS / "heartbeats"),
        "--events-root", str(INPUTS / "events"),
        "--quality-root", str(INPUTS / "quality"),
        "--tasks-file", str(INPUTS / "synthetic_tasks.json"),
        "--calendar-file", str(INPUTS / "synthetic_calendar_answers.json"),
        "--runs-file", str(INPUTS / "synthetic_run_registry.json"),
        "--hashes-file", str(INPUTS / "synthetic_hashes.json"),
        *extra,
    ]
    return subprocess.run(args, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300)


def test_cli_known_gaps_report_exit_3_and_artifacts(tmp_path):
    output = tmp_path / "out"
    proc = _run_cli(tmp_path, output, "--as-of", AS_OF_ISO)
    assert proc.returncode == 3, proc.stderr
    summary = json.loads(proc.stdout.strip().splitlines()[-1])
    report_path = Path(summary["path"])
    assert report_path.exists()
    revision_dir = report_path.parent
    assert (revision_dir / "summary.md").exists()
    assert (revision_dir / "dashboard.html").exists()
    published = json.loads(report_path.read_text(encoding="utf-8"))
    assert published["wall_clock_used"] is False
    assert published["data_origin"] == "SYNTHETIC"
    # alert + state spool + publish index all present under the output root
    assert (output / "publish-index" / "dp06-monitor-local.json").exists()
    assert list((output / "alerts" / "dp06-monitor-local").glob("events-r*.jsonl"))


def _write_clean_world(root: Path) -> dict:
    """A fully-clean SYNTHETIC world: ACCEPTED task, COMPLETE coverage, fresh
    heartbeat, VERIFIED calendar, no failure events -> exit 0 territory."""
    as_of = AS_OF
    hour = 3600 * 10**9
    (root / "heartbeats").mkdir(parents=True)
    (root / "events").mkdir(parents=True)
    (root / "quality").mkdir(parents=True)
    task_key = "tqsdk-free-realtime-get-kline-serial|KQ.m@SHFE.cu|60"
    (root / "heartbeats" / "w1.json").write_text(json.dumps({
        "worker_id": "w1", "run_id": "run-clean", "heartbeat_at_nano": as_of - 30 * 10**9,
        "heartbeat_source": "heartbeats/w1.json",
    }), encoding="utf-8")
    events = [
        {"event_id": "c1", "run_id": "run-clean", "task_key": "run:run-clean", "kind": "RUN_EVENT",
         "occurred_at_nano": as_of - hour, "reason_sanitized": "RUN_STARTED"},
        {"event_id": "c2", "run_id": "run-clean", "task_key": task_key, "kind": "TASK_EVENT",
         "occurred_at_nano": as_of - hour, "reason_sanitized": "TASK_QUEUED"},
        {"event_id": "c3", "run_id": "run-clean", "task_key": task_key, "kind": "TASK_EVENT",
         "occurred_at_nano": as_of - hour + 5 * 10**9, "reason_sanitized": "TASK_RUNNING",
         "last_observed_nano": as_of - 60 * 10**9, "last_committed_nano": as_of - 55 * 10**9},
        {"event_id": "c4", "run_id": "run-clean", "task_key": task_key, "kind": "TASK_EVENT",
         "occurred_at_nano": as_of - 10 * 10**9, "reason_sanitized": "TASK_ACCEPTED",
         "last_observed_nano": as_of - 30 * 10**9, "last_committed_nano": as_of - 25 * 10**9},
    ]
    (root / "events" / "w1.jsonl").write_text(
        "\n".join(json.dumps(e, sort_keys=True) for e in events) + "\n", encoding="utf-8")
    (root / "quality" / "q.json").write_text(json.dumps([
        {"evaluation_id": "q-clean", "scope": task_key, "as_of_nano": as_of,
         "check_execution": "COMPLETED", "coverage_state": "COMPLETE",
         "target_total": 5, "verified_denominator": 5, "valid_unique_closed": 5,
         "missing": 0, "conflicts": 0, "quarantined": 0, "provisional": 0,
         "calendar_unknown_ranges": 0, "source_unknown_ranges": 0,
         "data_origin": "SYNTHETIC"}
    ]), encoding="utf-8")
    (root / "tasks.json").write_text(json.dumps({"tasks": [
        {"task_key": task_key, "source_id": "tqsdk-free-realtime-get-kline-serial",
         "source_symbol": "KQ.m@SHFE.cu", "instrument_kind": "MAIN_CONTINUOUS",
         "duration_seconds": 60, "run_id": "run-clean", "acquisition_state": "ACCEPTED",
         "target_window": {"start_nano": as_of - hour, "end_nano": as_of}}
    ]}), encoding="utf-8")
    (root / "calendar.json").write_text(json.dumps({"calendar_answers": [
        {"dataset": "futures_kline_r1", "source_symbol": "KQ.m@SHFE.cu", "duration_seconds": 60,
         "status": "VERIFIED_SESSIONS", "window_start_nano": as_of - hour, "window_end_nano": as_of + hour,
         "sessions": [{"trading_day": "2026-09-20", "start_nano": as_of - hour, "end_nano": as_of}],
         "evidence_sha256": "44" * 32, "evidence_version": "cal-clean-r1"}
    ]}), encoding="utf-8")
    (root / "runs.json").write_text(json.dumps({"runs": [
        {"run_id": "run-clean", "state": "RUNNING", "started_at_nano": as_of - hour}
    ]}), encoding="utf-8")
    return {"root": root}


def test_cli_clean_world_exit_0(tmp_path):
    world = _write_clean_world(tmp_path / "clean")
    output = tmp_path / "out"
    args = [
        sys.executable, "-B", str(CLI),
        "--output-root", str(output),
        "--heartbeats-root", str(world["root"] / "heartbeats"),
        "--events-root", str(world["root"] / "events"),
        "--quality-root", str(world["root"] / "quality"),
        "--tasks-file", str(world["root"] / "tasks.json"),
        "--calendar-file", str(world["root"] / "calendar.json"),
        "--runs-file", str(world["root"] / "runs.json"),
        "--as-of", AS_OF_ISO,
    ]
    proc = subprocess.run(args, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_cli_determinism_across_runs_new_revision_each_time(tmp_path):
    world = _write_clean_world(tmp_path / "clean")
    output = tmp_path / "out"
    base = [
        "--heartbeats-root", str(world["root"] / "heartbeats"),
        "--events-root", str(world["root"] / "events"),
        "--quality-root", str(world["root"] / "quality"),
        "--tasks-file", str(world["root"] / "tasks.json"),
        "--calendar-file", str(world["root"] / "calendar.json"),
        "--runs-file", str(world["root"] / "runs.json"),
        "--as-of", AS_OF_ISO,
    ]
    first = subprocess.run(
        [sys.executable, "-B", str(CLI), "--output-root", str(output), *base],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300)
    assert first.returncode == 0, first.stderr
    summary1 = json.loads(first.stdout.strip().splitlines()[-1])

    second = subprocess.run(
        [sys.executable, "-B", str(CLI), "--output-root", str(output), *base],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300)
    summary2 = json.loads(second.stdout.strip().splitlines()[-1])

    assert summary1["report_id"] == summary2["report_id"]          # recomputable identity
    assert summary2["revision"] == 2                               # new revision, no overwrite
    r1 = Path(summary1["path"]).read_bytes()
    r2 = Path(summary2["path"]).read_bytes()
    body1 = json.loads(r1); body2 = json.loads(r2)
    body1.pop("revision"); body2.pop("revision")
    assert body1 == body2                                          # byte-stable facts


def test_cli_disk_output_failure_exit_2_and_marker(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")     # output root occupied by a file
    proc = _run_cli(tmp_path, blocker, "--as-of", AS_OF_ISO)
    assert proc.returncode == 2, proc.stdout
    assert "E_DP06_DISK_OUTPUT_FAILED" in proc.stderr or "report run failed" in proc.stderr


def test_cli_monitor_id_traversal_neutralized(tmp_path):
    output = tmp_path / "out"
    args = [
        sys.executable, "-B", str(CLI),
        "--output-root", str(output),
        "--events-root", str(INPUTS / "events"),
        "--tasks-file", str(INPUTS / "synthetic_tasks.json"),
        "--monitor-id", "../../evil",
        "--as-of", AS_OF_ISO,
    ]
    proc = subprocess.run(args, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300)
    assert proc.returncode in (0, 3)
    assert not (tmp_path / "evil").exists()
    assert not (tmp_path.parent / "evil").exists()
    alert_dirs = list((output / "alerts").iterdir())
    assert len(alert_dirs) == 1                     # exactly one sanitized monitor dir
    assert ".." not in alert_dirs[0].name           # traversal segments neutralized
