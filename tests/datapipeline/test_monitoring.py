"""DP06 monitoring tests (V11 observability vectors; work order test clause).

Covers the frozen alert vectors (fixtures/DP06/vectors_alerts.json), the
independent-heartbeat requirement (H06.3), cooldown/recovery/ACK semantics
(H06.4), per-period window thresholds and closure non-fabrication (H06.2),
the hash-verified frozen-contract loader, threshold validation, the
credential-shape gate (H06.8) and the atomic local store.

All inputs are SYNTHETIC fixtures; no network, no database, no task
registration.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ak_trading.datapipeline import monitoring as mon  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "DP06"
INPUTS = FIXTURES / "inputs"
AS_OF = mon.frozen_contracts_module().iso_instant_to_nano("2026-09-20T18:35:00+00:00")


def load_snapshot() -> dict:
    return mon.load_pipeline_facts(
        heartbeats_root=INPUTS / "heartbeats",
        events_root=INPUTS / "events",
        quality_root=INPUTS / "quality",
        tasks_file=INPUTS / "synthetic_tasks.json",
        calendar_file=INPUTS / "synthetic_calendar_answers.json",
        runs_file=INPUTS / "synthetic_run_registry.json",
    )


def apply_mods(snapshot: dict, mods: dict) -> dict:
    snap = copy.deepcopy(snapshot)
    for key, value in mods.items():
        if key == "set_heartbeat_at":
            for beat in snap["heartbeats"]:
                beat["heartbeat_at_nano"] = value
        elif key == "set_task_last_observed":
            for task_key, instant in value.items():
                targets = [
                    e for e in snap["status_events"]
                    if e.get("task_key") == task_key and "last_observed_nano" in e
                ]
                if not targets:
                    task_events = [e for e in snap["status_events"] if e.get("task_key") == task_key]
                    if task_events:
                        task_events[-1]["last_observed_nano"] = instant
                else:
                    for event in targets:
                        event["last_observed_nano"] = instant
        elif key == "remove_all_task_events":
            snap["status_events"] = [
                e for e in snap["status_events"] if str(e.get("kind")) != "TASK_EVENT"
            ]
        elif key == "replace_calendar":
            snap["calendar_answers"] = value
        else:
            raise AssertionError(f"unknown modification {key}")
    return snap


def raised_keys(result: mon.TickResult) -> set:
    return {
        record.alert_key
        for record in result.state.alerts.values()
        if record.status in ("OPEN", "ACKNOWLEDGED")
    }


# ---------------------------------------------------------------------------
# Frozen dependency loader
# ---------------------------------------------------------------------------


def test_frozen_contracts_load_with_pinned_hash():
    contracts = mon.frozen_contracts_module()
    assert contracts.CONTRACT_SCHEMA_VERSION == "datapipeline_contracts_v1"
    # Frozen enum value spaces the monitor relies on (same name = same meaning).
    assert {s.value for s in contracts.TaskState} >= set(mon.HARD_FAILURE_TASK_STATES)
    assert contracts.CheckExecution.NOT_RUN.value == "NOT_RUN"
    assert contracts.CoverageState.UNKNOWN.value == "UNKNOWN"


def test_frozen_loader_refuses_hash_drift(tmp_path, monkeypatch):
    frozen_root = mon.resolve_frozen_root()
    staged = tmp_path / "frozen"
    (staged / "src" / "ak_trading" / "datapipeline").mkdir(parents=True)
    staged.joinpath("dp01-freeze-manifest.json").write_text(
        (frozen_root / "dp01-freeze-manifest.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    contracts_path = frozen_root / "src" / "ak_trading" / "datapipeline" / "contracts.py"
    tampered = staged / "src" / "ak_trading" / "datapipeline" / "contracts.py"
    tampered.write_text(contracts_path.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    monkeypatch.setenv("AK_DP01_FROZEN_ROOT", str(staged))
    monkeypatch.setattr(mon, "_contracts_cache", {})
    sys.modules.pop("ak_trading.datapipeline.contracts", None)
    with pytest.raises(mon.MonitoringError) as excinfo:
        mon._load_contracts_from_freeze(staged)
    assert excinfo.value.code == "E_DP06_FROZEN_HASH_DRIFT"
    # The staged (real) frozen root still loads fine afterwards.
    monkeypatch.delenv("AK_DP01_FROZEN_ROOT", raising=False)
    assert mon.frozen_contracts_module().CONTRACT_SCHEMA_VERSION == "datapipeline_contracts_v1"


# ---------------------------------------------------------------------------
# Vector-driven alert evaluation (fixtures/DP06/vectors_alerts.json)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    json.loads((FIXTURES / "vectors_alerts.json").read_text(encoding="utf-8"))["scenarios"],
    ids=lambda scenario: scenario["id"],
)
def test_alert_vectors(scenario):
    snapshot = apply_mods(load_snapshot(), scenario["modifications"])
    state = mon.MonitorState(monitor_id="vector-monitor")
    result = mon.evaluate_tick(state, snapshot, int(scenario["now_nano"]))
    active = raised_keys(result)
    for key in scenario["expected_present"]:
        assert key in active, f"{scenario['id']}: expected alert {key} not active (have {sorted(active)})"
    for key in scenario["expected_absent"]:
        assert key not in active, f"{scenario['id']}: alert {key} must not fire (have {sorted(active)})"
    for task_key, health in scenario.get("expected_health", {}).items():
        assert result.health[task_key] == health, f"{scenario['id']}: health({task_key})"


# ---------------------------------------------------------------------------
# H06.3: the monitor detects a missing heartbeat with zero collector events
# ---------------------------------------------------------------------------


def test_missing_heartbeat_detected_without_any_collector_events():
    snapshot = apply_mods(
        load_snapshot(),
        {"remove_all_task_events": True, "set_heartbeat_at": AS_OF - 10 * 60 * 1_000_000_000},
    )
    result = mon.evaluate_tick(mon.MonitorState(monitor_id="m"), snapshot, AS_OF)
    active = raised_keys(result)
    assert "HEARTBEAT_EXPIRED|worker=w1" in active
    record = result.state.alerts["HEARTBEAT_EXPIRED|worker=w1"]
    assert record.cause  # explicit cause text present (H06.2)
    assert record.severity == "CRITICAL"


def test_unknown_never_renders_ok():
    # Silent collector: fresh heartbeat, zero task events -> no execution
    # evidence for t1/t2 (UNKNOWN, never OK); t3's manifest terminal state
    # PERMISSION_DENIED stays a known failure (FAILING), not UNKNOWN.
    snapshot = apply_mods(load_snapshot(), {"remove_all_task_events": True})
    result = mon.evaluate_tick(mon.MonitorState(monitor_id="m"), snapshot, AS_OF)
    assert "OK" not in set(result.health.values())
    t1 = "tqsdk-free-realtime-get-kline-serial|KQ.m@SHFE.cu|60"
    t2 = "tqsdk-free-realtime-get-kline-serial|KQ.m@SHFE.cu|86400"
    assert result.health[t1] == "UNKNOWN" and result.health[t2] == "UNKNOWN"


# ---------------------------------------------------------------------------
# H06.2: per-period window thresholds (60s vs 86400s) and closure behavior
# ---------------------------------------------------------------------------


def _daily_calendar_override(now: int) -> list:
    return [
        {
            "dataset": "futures_kline_r1",
            "source_symbol": "KQ.m@SHFE.cu",
            "duration_seconds": 86400,
            "status": "VERIFIED_SESSIONS",
            "window_start_nano": now - 3600 * 1_000_000_000,
            "window_end_nano": now + 3600 * 1_000_000_000,
            "sessions": [{"trading_day": "2026-09-20", "start_nano": now - 3600 * 10**9, "end_nano": now}],
            "evidence_sha256": "44" * 32,
            "evidence_version": "cal-daily-r1",
        }
    ]


def test_daily_window_loss_uses_daily_threshold_not_minute_threshold():
    source = load_snapshot()
    t2_key = "tqsdk-free-realtime-get-kline-serial|KQ.m@SHFE.cu|86400"
    hour = 3600 * 1_000_000_000

    # 2h lag: below the 36h daily threshold -> no alert (would fire on 60s).
    snap = apply_mods(
        source,
        {
            "replace_calendar": _daily_calendar_override(AS_OF),
            "set_task_last_observed": {t2_key: AS_OF - 2 * hour},
        },
    )
    result = mon.evaluate_tick(mon.MonitorState(monitor_id="m"), snap, AS_OF)
    assert f"WINDOW_LOST|task={t2_key}" not in raised_keys(result)

    # 40h lag: beyond the 36h daily threshold -> alert.
    snap = apply_mods(
        source,
        {
            "replace_calendar": _daily_calendar_override(AS_OF),
            "set_task_last_observed": {t2_key: AS_OF - 40 * hour},
        },
    )
    result = mon.evaluate_tick(mon.MonitorState(monitor_id="m"), snap, AS_OF)
    assert f"WINDOW_LOST|task={t2_key}" in raised_keys(result)


def test_evidenced_closure_never_fabricates_no_data_fault():
    saturday = mon.frozen_contracts_module().iso_instant_to_nano("2026-09-19T12:00:00+00:00")
    hour = 3600 * 1_000_000_000
    snap = apply_mods(
        load_snapshot(),
        {
            "set_heartbeat_at": saturday - 24 * hour,
            "set_task_last_observed": {
                "tqsdk-free-realtime-get-kline-serial|KQ.m@SHFE.cu|60": saturday - 48 * hour,
                "tqsdk-free-realtime-get-kline-serial|KQ.m@SHFE.cu|86400": saturday - 48 * hour,
            },
        },
    )
    result = mon.evaluate_tick(mon.MonitorState(monitor_id="m"), snap, saturday)
    active = raised_keys(result)
    # Closure evidenced -> no window-loss fault on either period...
    assert not any(key.startswith("WINDOW_LOST|") for key in active)
    # ...while process liveness stays honest (heartbeat expired is real).
    assert "HEARTBEAT_EXPIRED|worker=w1" in active


# ---------------------------------------------------------------------------
# H06.4: cooldown / renotify / recovery / reopen / ACK
# ---------------------------------------------------------------------------


def test_cooldown_suppression_and_renotification():
    snapshot = load_snapshot()
    minute = 60 * 1_000_000_000
    state = mon.MonitorState(monitor_id="m")

    first = mon.evaluate_tick(state, snapshot, AS_OF)
    backlog = [e for e in first.events if e.alert_key.startswith("BACKLOG|")]
    assert backlog and backlog[0].transition == "RAISED" and backlog[0].notified

    second = mon.evaluate_tick(state, snapshot, AS_OF + 10 * minute)
    suppressed = [e for e in second.events if e.alert_key.startswith("BACKLOG|")]
    assert suppressed and suppressed[0].transition == "SUPPRESSED" and not suppressed[0].notified
    # Cooldown means NO renotification of already-notified alerts at +10min
    # (brand-new first-time RAISEs at the later tick stay legal).
    assert not [e for e in second.events if e.transition == "RENOTIFIED"]

    third = mon.evaluate_tick(state, snapshot, AS_OF + 37 * minute)
    renotified = [e for e in third.events if e.alert_key.startswith("BACKLOG|")]
    assert renotified and renotified[0].transition == "RENOTIFIED" and renotified[0].notified

    backlog_key = next(k for k in state.alerts if k.startswith("BACKLOG|"))
    record = state.alerts[backlog_key]
    assert record.occurrence_count == 3
    assert record.suppressed_count == 1


def test_recovery_appends_resolved_notification_and_reopen():
    snapshot = load_snapshot()
    state = mon.MonitorState(monitor_id="m")
    mon.evaluate_tick(state, snapshot, AS_OF)
    open_before = raised_keys(mon.TickResult(state=state, events=(), notifications=(), health={}, health_reasons={}, as_of_nano=AS_OF))
    assert open_before

    # Conditions cleared: fresh heartbeat only, no task events, no failures.
    # Calendar-evidence alerts (CONFLICT) are condition-based and persist
    # independently of collector activity; everything event-based resolves.
    cleaned = apply_mods(snapshot, {"remove_all_task_events": True})
    recovered = mon.evaluate_tick(state, cleaned, AS_OF + 60 * 1_000_000_000)
    resolved_events = [e for e in recovered.events if e.transition == "RESOLVED"]
    assert resolved_events
    assert all(e.notified for e in resolved_events)          # recovery notification appended
    still_open = raised_keys(recovered)
    assert still_open == {k for k in still_open if k.startswith("CALENDAR_CONFLICT|")}

    # Condition returns -> REOPENED with notification.
    again = mon.evaluate_tick(state, snapshot, AS_OF + 2 * 60 * 1_000_000_000)
    reopened = [e for e in again.events if e.transition == "REOPENED"]
    assert reopened and all(e.notified for e in reopened)


def test_ack_changes_presentation_only_not_quality():
    from ak_trading.datapipeline import reporting as rep

    snapshot = load_snapshot()
    state = mon.MonitorState(monitor_id="m")
    mon.evaluate_tick(state, snapshot, AS_OF)
    backlog_key = next(k for k in state.alerts if k.startswith("BACKLOG|"))

    facts_before = rep.build_report(
        monitor_id="m", as_of_nano=AS_OF, snapshot=snapshot, alert_state=state.to_dict(),
        health={}, health_reasons={}, evidence_hashes={},
    )
    event = mon.acknowledge_alert(state, backlog_key, AS_OF + 30 * 10**9, "ops reviewed")
    assert event.transition == "ACKNOWLEDGED"
    assert state.alerts[backlog_key].status == "ACKNOWLEDGED"

    facts_after = rep.build_report(
        monitor_id="m", as_of_nano=AS_OF, snapshot=snapshot, alert_state=state.to_dict(),
        health={}, health_reasons={}, evidence_hashes={},
    )
    # ACK does not fill gaps: identical coverage numbers, alert still active.
    assert facts_after["coverage"]["known_scope_aggregate"]["missing"] == \
        facts_before["coverage"]["known_scope_aggregate"]["missing"]
    assert facts_after["alert_summary"]["active"] == facts_before["alert_summary"]["active"]
    assert facts_after["alerts"][backlog_key]["status"] == "ACKNOWLEDGED"


# ---------------------------------------------------------------------------
# Threshold validation + credential gate (H06.2 / H06.8)
# ---------------------------------------------------------------------------


def test_thresholds_validation_rejects_unknown_and_invalid():
    with pytest.raises(mon.MonitoringError) as exc:
        mon.thresholds_from_mapping({"not_a_threshold": 1})
    assert exc.value.code == "E_DP06_THRESHOLD_UNKNOWN_KEY"
    with pytest.raises(mon.MonitoringError) as exc:
        mon.thresholds_from_mapping({"window_loss_after_nano": [[300, 10]]})
    assert exc.value.code == "E_DP06_THRESHOLD_BAD_PERIOD"
    with pytest.raises(mon.MonitoringError) as exc:
        mon.thresholds_from_mapping({"heartbeat_timeout_nano": 0})
    assert exc.value.code == "E_DP06_THRESHOLD_NON_POSITIVE"
    ok = mon.thresholds_from_mapping({"heartbeat_timeout_nano": 300 * 10**9})
    assert ok.heartbeat_timeout_nano == 300 * 10**9


def test_credential_shape_blocks_and_never_echoes_secret():
    with pytest.raises(mon.MonitoringError) as exc:
        mon.validate_free_text("password=hunter2-secret", "alerts[0].cause")
    assert "hunter2-secret" not in str(exc.value)
    assert exc.value.code == "E_DP06_CREDENTIAL_SHAPE_BLOCKED"
    # A credential-shaped identity can never enter an alert cause: the
    # heartbeat worker_id flows into the cause text and must be refused.
    state = mon.MonitorState(monitor_id="m")
    poisoned = {
        "heartbeats": [
            {"worker_id": "w1?password=leak", "heartbeat_at_nano": 0, "run_id": "r", "heartbeat_source": "hb"}
        ]
    }
    with pytest.raises(mon.MonitoringError) as excinfo:
        mon.evaluate_tick(state, poisoned, 10**18)
    assert excinfo.value.code == "E_DP06_CREDENTIAL_SHAPE_BLOCKED"


# ---------------------------------------------------------------------------
# Local store: atomic publish, revisions, disk failure (V11 output-failure)
# ---------------------------------------------------------------------------


def test_store_roundtrip_and_revision_never_overwrites(tmp_path):
    snapshot = load_snapshot()
    store = mon.LocalMonitorStore(tmp_path, "m1")
    state = mon.MonitorState(monitor_id="m1")
    first = mon.evaluate_tick(state, snapshot, AS_OF)
    store.publish_tick(first)
    r1_path = tmp_path / "alerts" / "m1" / "state-r000001.json"
    r1_bytes = r1_path.read_bytes()

    reloaded = store.read_state()
    assert reloaded.tick_count == 1
    assert set(reloaded.alerts) == set(first.state.alerts)

    second = mon.evaluate_tick(reloaded, snapshot, AS_OF + 60 * 10**9)
    store.publish_tick(second)
    assert (tmp_path / "alerts" / "m1" / "state-r000002.json").exists()
    assert r1_path.read_bytes() == r1_bytes          # original record untouched (H06.5)
    index = json.loads((tmp_path / "publish-index" / "m1.json").read_text(encoding="utf-8"))
    assert index["revision"] == 2 and index["latest_state_path"].endswith("state-r000002.json")


def test_store_reports_disk_output_failure_without_partial_overwrite(tmp_path):
    # A healthy store publishes fine; its records stay intact when a second
    # monitor's target path is blocked by a file where its directory belongs.
    healthy = mon.LocalMonitorStore(tmp_path, "m-ok")
    state = mon.MonitorState(monitor_id="m-ok")
    first = mon.evaluate_tick(state, load_snapshot(), AS_OF)
    healthy.publish_tick(first)
    r1_bytes = (tmp_path / "alerts" / "m-ok" / "state-r000001.json").read_bytes()

    blocker_dir = tmp_path / "alerts" / "m3"
    blocker_dir.parent.mkdir(parents=True, exist_ok=True)
    blocker_dir.write_text("a file, not a directory", encoding="utf-8")
    blocked = mon.LocalMonitorStore(tmp_path, "m3")
    second_state = mon.MonitorState(monitor_id="m3")
    second = mon.evaluate_tick(second_state, load_snapshot(), AS_OF)
    with pytest.raises(mon.MonitoringError) as excinfo:
        blocked.publish_tick(second)
    assert excinfo.value.code == "E_DP06_DISK_OUTPUT_FAILED"
    assert (tmp_path / "alerts" / "m-ok" / "state-r000001.json").read_bytes() == r1_bytes


def test_store_neutralizes_unsafe_monitor_ids(tmp_path):
    store = mon.LocalMonitorStore(tmp_path, "../escape")
    assert ".." not in store.alerts_dir.name
    assert store.alerts_dir.parent == tmp_path / "alerts"
    assert store.index_path.parent == tmp_path / "publish-index"
    with pytest.raises(mon.MonitoringError):
        mon.LocalMonitorStore(tmp_path, ".hidden")


# ---------------------------------------------------------------------------
# Import hygiene (04-validation: no import-time side effects)
# ---------------------------------------------------------------------------


def test_import_purity_no_io_side_effects(tmp_path):
    import subprocess

    code = (
        "import sys, os;"
        "import ak_trading.datapipeline.monitoring as m;"
        "import ak_trading.datapipeline.reporting as r;"
        "import ak_trading.datapipeline.dashboard as d;"
        "sys.stdout.write(m.MONITOR_SCHEMA_VERSION)"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("AK_")}
    env["PYTHONPATH"] = str(SRC_ROOT)
    proc = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "dp06-monitoring/1"
    assert list(tmp_path.iterdir()) == []      # nothing written at import time
