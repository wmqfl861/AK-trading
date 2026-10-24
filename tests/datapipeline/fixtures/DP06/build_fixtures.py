"""Build the DP06 SYNTHETIC fixtures (inputs + alert vectors).

Run:  python -B tests/datapipeline/fixtures/DP06/build_fixtures.py

All payloads are SYNTHETIC engineering fixtures (data_origin=SYNTHETIC);
nothing here is historical market fact.  The generator is deterministic:
same code -> byte-identical fixtures.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def ns(iso: str) -> int:
    dt = datetime.fromisoformat(iso)
    delta = dt.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


# --- world clock (UTC instants; +08:00 renderings in comments) -----------
FRI_NIGHT_END = ns("2026-09-18T18:30:00+00:00")          # Sat 02:30+08
WEEKEND_END = ns("2026-09-20T13:00:00+00:00")            # Mon 21:00+08
MON_NIGHT_START = WEEKEND_END
MON_NIGHT_END = ns("2026-09-20T18:30:00+00:00")          # Tue 02:30+08
MON_EVIDENCE_END = ns("2026-09-20T19:00:00+00:00")       # evidence window margin
AS_OF = ns("2026-09-20T18:35:00+00:00")                  # Tue 02:35+08
W1_HEARTBEAT = ns("2026-09-20T18:34:30+00:00")
T1_LAST_OBS = ns("2026-09-20T18:34:00+00:00")
T1_LAST_COMMIT = ns("2026-09-20T18:34:10+00:00")

SOURCE_ID = "tqsdk-free-realtime-get-kline-serial"
RUN_ID = "run-20260920-x"

T1_KEY = f"{SOURCE_ID}|KQ.m@SHFE.cu|60"
T2_KEY = f"{SOURCE_ID}|KQ.m@SHFE.cu|86400"
T3_KEY = f"{SOURCE_ID}|KQ.i@SHFE.cu|60"


def task_key(symbol: str, duration: int) -> str:
    return f"{SOURCE_ID}|{symbol}|{duration}"


def main() -> None:
    inputs = HERE / "inputs"
    (inputs / "heartbeats").mkdir(parents=True, exist_ok=True)
    (inputs / "events").mkdir(parents=True, exist_ok=True)
    (inputs / "quality").mkdir(parents=True, exist_ok=True)

    marker = {"data_origin": "SYNTHETIC", "schema": "dp06-fixtures/1"}

    # --- tasks manifest -----------------------------------------------------
    tasks = dict(
        marker,
        tasks=[
            {
                "task_key": T1_KEY,
                "source_id": SOURCE_ID,
                "source_symbol": "KQ.m@SHFE.cu",
                "instrument_kind": "MAIN_CONTINUOUS",
                "duration_seconds": 60,
                "run_id": RUN_ID,
                "acquisition_state": "RUNNING",
                "target_window": {"start_nano": MON_NIGHT_START, "end_nano": MON_NIGHT_END},
            },
            {
                "task_key": T2_KEY,
                "source_id": SOURCE_ID,
                "source_symbol": "KQ.m@SHFE.cu",
                "instrument_kind": "MAIN_CONTINUOUS",
                "duration_seconds": 86400,
                "run_id": RUN_ID,
                "acquisition_state": "RUNNING",
                "target_window": {"start_nano": MON_NIGHT_START, "end_nano": MON_NIGHT_END},
            },
            {
                "task_key": T3_KEY,
                "source_id": SOURCE_ID,
                "source_symbol": "KQ.i@SHFE.cu",
                "instrument_kind": "INDEX",
                "duration_seconds": 60,
                "run_id": RUN_ID,
                "acquisition_state": "PERMISSION_DENIED",
                "target_window": {"start_nano": MON_NIGHT_START, "end_nano": MON_NIGHT_END},
            },
        ],
    )
    (inputs / "synthetic_tasks.json").write_text(json.dumps(tasks, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    # --- heartbeat (fresh at AS_OF; stale in later scenarios) -------------
    heartbeat = dict(
        marker,
        worker_id="w1",
        run_id=RUN_ID,
        started_at_nano=ns("2026-09-20T12:50:00+00:00"),
        heartbeat_at_nano=W1_HEARTBEAT,
        heartbeat_interval_nano=30 * 1_000_000_000,
        heartbeat_source="heartbeats/w1.json",
    )
    (inputs / "heartbeats" / "w1.json").write_text(json.dumps(heartbeat, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    # --- status events (worker spool; ordered) ------------------------------
    events: list[dict] = [
        {
            "event_id": "evt-0001",
            "run_id": RUN_ID,
            "task_key": f"run:{RUN_ID}",
            "kind": "RUN_EVENT",
            "occurred_at_nano": ns("2026-09-20T12:50:00+00:00"),
            "reason_sanitized": "RUN_STARTED mode=REALTIME universe=<synthetic>",
        },
        {
            "event_id": "evt-0002",
            "run_id": RUN_ID,
            "task_key": T1_KEY,
            "kind": "TASK_EVENT",
            "occurred_at_nano": ns("2026-09-20T13:00:05+00:00"),
            "reason_sanitized": "TASK_QUEUED subscription admitted",
            "retry_count": 0,
        },
        {
            "event_id": "evt-0003",
            "run_id": RUN_ID,
            "task_key": T1_KEY,
            "kind": "TASK_EVENT",
            "occurred_at_nano": ns("2026-09-20T13:00:06+00:00"),
            "reason_sanitized": "TASK_RUNNING baseline recorded",
            "last_observed_nano": ns("2026-09-20T13:00:00+00:00"),
        },
        {
            "event_id": "evt-0004",
            "run_id": RUN_ID,
            "task_key": T1_KEY,
            "kind": "TASK_EVENT",
            "occurred_at_nano": ns("2026-09-20T17:40:00+00:00"),
            "reason_sanitized": "SESSION_DISCONNECTED wait_update deadline exceeded; reconnect epoch+1",
        },
        {
            "event_id": "evt-0005",
            "run_id": RUN_ID,
            "task_key": T1_KEY,
            "kind": "TASK_EVENT",
            "occurred_at_nano": ns("2026-09-20T18:05:00+00:00"),
            "reason_sanitized": "SESSION_DISCONNECTED socket reset; reconnect epoch+2",
        },
        {
            "event_id": "evt-0006",
            "run_id": RUN_ID,
            "task_key": T1_KEY,
            "kind": "TASK_EVENT",
            "occurred_at_nano": ns("2026-09-20T18:20:00+00:00"),
            "reason_sanitized": "OUTSIDE_FREE_WINDOW request beyond free realtime window",
        },
        {
            "event_id": "evt-0007",
            "run_id": RUN_ID,
            "task_key": T1_KEY,
            "kind": "TASK_EVENT",
            "occurred_at_nano": ns("2026-09-20T18:25:00+00:00"),
            "reason_sanitized": "TASK_RETRY_SCHEDULED backoff after transient",
            "retry_count": 30,
        },
        {
            "event_id": "evt-0008",
            "run_id": RUN_ID,
            "task_key": T1_KEY,
            "kind": "TASK_EVENT",
            "occurred_at_nano": ns("2026-09-20T18:30:00+00:00"),
            "reason_sanitized": "SESSION_DISCONNECTED night-close jitter; reconnect epoch+3",
        },
        {
            "event_id": "evt-0009",
            "run_id": RUN_ID,
            "task_key": T1_KEY,
            "kind": "TASK_EVENT",
            "occurred_at_nano": ns("2026-09-20T18:34:05+00:00"),
            "reason_sanitized": "TASK_RUNNING frames flowing",
            "last_observed_nano": T1_LAST_OBS,
            "last_committed_nano": T1_LAST_COMMIT,
        },
        {
            "event_id": "evt-0010",
            "run_id": RUN_ID,
            "task_key": T2_KEY,
            "kind": "TASK_EVENT",
            "occurred_at_nano": ns("2026-09-20T18:10:00+00:00"),
            "reason_sanitized": "COMMIT_FAILED database commit rejected after retries",
            "failure_class": "LOCAL_ERROR",
        },
        {
            "event_id": "evt-0011",
            "run_id": RUN_ID,
            "task_key": T2_KEY,
            "kind": "TASK_EVENT",
            "occurred_at_nano": ns("2026-09-20T18:31:00+00:00"),
            "reason_sanitized": "SOURCE_EMPTY unexplained empty return",
        },
        {
            "event_id": "evt-0012",
            "run_id": RUN_ID,
            "task_key": T2_KEY,
            "kind": "TASK_EVENT",
            "occurred_at_nano": ns("2026-09-20T18:32:00+00:00"),
            "reason_sanitized": "SOURCE_EMPTY unexplained empty return",
        },
        {
            "event_id": "evt-0013",
            "run_id": RUN_ID,
            "task_key": T2_KEY,
            "kind": "TASK_EVENT",
            "occurred_at_nano": ns("2026-09-20T18:33:00+00:00"),
            "reason_sanitized": "SOURCE_EMPTY unexplained empty return",
        },
        {
            "event_id": "evt-0014",
            "run_id": RUN_ID,
            "task_key": T3_KEY,
            "kind": "TASK_EVENT",
            "occurred_at_nano": ns("2026-09-20T18:20:00+00:00"),
            "reason_sanitized": "PERMISSION_DENIED quote permission refused for symbol",
            "failure_class": "ACCOUNT_DENIED",
            "evidence_ref": "collector-events/w1/evt-0014.json",
        },
        {
            "event_id": "evt-0015",
            "run_id": RUN_ID,
            "task_key": T3_KEY,
            "kind": "TASK_EVENT",
            "occurred_at_nano": ns("2026-09-20T18:21:00+00:00"),
            "reason_sanitized": "TASK_RUNNING degraded: permission denied upstream",
            "last_observed_nano": ns("2026-09-20T18:21:00+00:00"),
        },
    ]
    lines = [json.dumps(event, sort_keys=True, ensure_ascii=False) for event in events]
    (inputs / "events" / "worker-w1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # --- quality reports (DP05 facts; NOT_RUN carries null numbers) --------
    quality = [
        {
            "evaluation_id": "q-20260920-t1",
            "scope": T1_KEY,
            "as_of_nano": AS_OF,
            "check_execution": "COMPLETED",
            "coverage_state": "PARTIAL",
            "target_total": 8192,
            "verified_denominator": 8192,
            "valid_unique_closed": 8185,
            "missing": 5,
            "conflicts": 0,
            "quarantined": 2,
            "provisional": 0,
            "calendar_unknown_ranges": 0,
            "source_unknown_ranges": 0,
            "generation_evidence_sha256": None,
            "universe_sha256": "11" * 32,
            "commit_view_sha256": "22" * 32,
            "data_origin": "SYNTHETIC",
        },
        {
            "evaluation_id": "q-20260920-t2",
            "scope": T2_KEY,
            "as_of_nano": AS_OF,
            "check_execution": "NOT_RUN",
            "coverage_state": None,
            "target_total": None,
            "verified_denominator": None,
            "valid_unique_closed": None,
            "missing": None,
            "conflicts": None,
            "quarantined": None,
            "provisional": None,
            "calendar_unknown_ranges": None,
            "source_unknown_ranges": None,
            "generation_evidence_sha256": None,
            "universe_sha256": "11" * 32,
            "commit_view_sha256": None,
            "data_origin": "SYNTHETIC",
        },
    ]
    (inputs / "quality" / "q-20260920.json").write_text(json.dumps(quality, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    # --- calendar answers ----------------------------------------------------
    calendar = dict(
        marker,
        calendar_answers=[
            {
                "dataset": "futures_kline_r1",
                "source_symbol": "KQ.m@SHFE.cu",
                "duration_seconds": 60,
                "status": "NO_TRADING_EVIDENCED",
                "window_start_nano": FRI_NIGHT_END,
                "window_end_nano": WEEKEND_END,
                "sessions": [],
                "evidence_sha256": "33" * 32,
                "evidence_version": "cal-weekend-2026-09-r1",
            },
            {
                "dataset": "futures_kline_r1",
                "source_symbol": "KQ.m@SHFE.cu",
                "duration_seconds": 60,
                "status": "VERIFIED_SESSIONS",
                "window_start_nano": MON_NIGHT_START,
                "window_end_nano": MON_EVIDENCE_END,
                "sessions": [
                    {"trading_day": "2026-09-20", "start_nano": MON_NIGHT_START, "end_nano": MON_NIGHT_END}
                ],
                "evidence_sha256": "44" * 32,
                "evidence_version": "cal-2026-09-20-r1",
            },
            {
                "dataset": "futures_kline_r1",
                "source_symbol": "KQ.m@SHFE.cu",
                "duration_seconds": 86400,
                "status": "NO_TRADING_EVIDENCED",
                "window_start_nano": FRI_NIGHT_END,
                "window_end_nano": WEEKEND_END,
                "sessions": [],
                "evidence_sha256": "33" * 32,
                "evidence_version": "cal-weekend-2026-09-r1",
            },
            {
                "dataset": "futures_kline_r1",
                "source_symbol": "KQ.i@SHFE.cu",
                "duration_seconds": 60,
                "status": "CONFLICT",
                "window_start_nano": MON_NIGHT_START,
                "window_end_nano": MON_EVIDENCE_END,
                "sessions": [],
                "conflict_refs": ["cal-ev-a-2026-09-20", "cal-ev-b-2026-09-20"],
                "evidence_sha256": "55" * 32,
                "evidence_version": None,
            },
        ],
    )
    (inputs / "synthetic_calendar_answers.json").write_text(json.dumps(calendar, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    # --- run registry facts ---------------------------------------------------
    runs = dict(
        marker,
        runs=[
            {
                "run_id": RUN_ID,
                "state": "RUNNING",
                "mode": "REALTIME",
                "started_at_nano": ns("2026-09-20T12:50:00+00:00"),
                "run_source": "storage-events/dp02/runs/run-20260920-x.json",
            }
        ],
    )
    (inputs / "synthetic_run_registry.json").write_text(json.dumps(runs, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    # --- evidence-chain hashes fixture ---------------------------------------
    hashes = {
        "run_registry_sha256": "aa" * 32,
        "commit_view_sha256": "bb" * 32,
        "calendar_evidence_sha256": "cc" * 32,
        "universe_sha256": "4e7952223d9b28c9e16b2460f2d006f5db17687a715ca4cece1d45b569589874",
        "policy_sha256": "ee" * 32,
        "generation_evidence_sha256": None,
        "source_bundle_sha256": "ff" * 32,
    }
    (inputs / "synthetic_hashes.json").write_text(json.dumps(hashes, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    # --- alert vectors ---------------------------------------------------------
    vectors = {
        "schema": "dp06-alert-vectors/1",
        "data_origin": "SYNTHETIC",
        "now_asof": AS_OF,
        "cooldown_nano": 1_800_000_000_000,
        "scenarios": [
            {
                "id": "S1-baseline-async-of",
                "description": "fresh heartbeat, run healthy; commit failure + empty streak + permission + disconnects + backlog + calendar unknown/conFLICT",
                "now_nano": AS_OF,
                "modifications": {},
                "expected_present": [
                    f"DB_COMMIT_FAILED|task={T2_KEY}",
                    f"PERSISTENT_EMPTY_SOURCE|task={T2_KEY}",
                    f"PERMISSION_DENIED|task={T3_KEY}",
                    f"CALENDAR_CONFLICT|task={T3_KEY}",
                    f"SESSION_DISCONNECTED|task={T1_KEY}",
                    f"BACKLOG|task={T1_KEY}",
                    f"CALENDAR_UNKNOWN|task={T2_KEY}",
                ],
                "expected_absent": [
                    "HEARTBEAT_EXPIRED|worker=w1",
                    f"RUN_LOST|run={RUN_ID}",
                    f"WINDOW_LOST|task={T1_KEY}",
                    f"WINDOW_LOST|task={T2_KEY}",
                    f"WINDOW_LOST|task={T3_KEY}",
                    f"CALENDAR_UNKNOWN|task={T1_KEY}",
                ],
                "expected_health": {
                    T1_KEY: "DEGRADED",
                    T2_KEY: "FAILING",
                    T3_KEY: "FAILING",
                },
            },
            {
                "id": "S2-weekend-closure-no-fabricated-fault",
                "description": "Saturday noon; closure evidenced -> heartbeat may expire (process liveness) but NO window-loss fault fabricated",
                "now_nano": ns("2026-09-19T12:00:00+00:00"),
                "modifications": {"set_heartbeat_at": ns("2026-09-18T18:34:30+00:00")},
                "expected_present": ["HEARTBEAT_EXPIRED|worker=w1", f"CALENDAR_UNKNOWN|task={T3_KEY}"],
                "expected_absent": [
                    f"WINDOW_LOST|task={T1_KEY}",
                    f"WINDOW_LOST|task={T2_KEY}",
                    f"CALENDAR_UNKNOWN|task={T1_KEY}",
                    f"CALENDAR_UNKNOWN|task={T2_KEY}",
                ],
                "expected_health": {},
            },
            {
                "id": "S3-night-window-loss",
                "description": "inside VERIFIED session, 16 minutes without observation on the 60s stream",
                "now_nano": ns("2026-09-20T18:36:00+00:00"),
                "modifications": {"set_task_last_observed": {T1_KEY: ns("2026-09-20T18:20:00+00:00")}},
                "expected_present": [f"WINDOW_LOST|task={T1_KEY}"],
                "expected_absent": [f"WINDOW_LOST|task={T2_KEY}"],
                "expected_health": {T1_KEY: "FAILING"},
            },
            {
                "id": "S4-run-lost",
                "description": "run says RUNNING but no heartbeat/event for > run_lost threshold",
                "now_nano": ns("2026-09-20T18:45:00+00:00"),
                "modifications": {},
                "expected_present": [f"RUN_LOST|run={RUN_ID}", "HEARTBEAT_EXPIRED|worker=w1"],
                "expected_absent": [],
                "expected_health": {},
            },
            {
                "id": "S8-silent-collector-no-task-events",
                "description": "fresh heartbeat but zero task events -> t1/t2 UNKNOWN (never OK); t3 keeps its manifest terminal PERMISSION_DENIED (FAILING); no window fabrication",
                "now_nano": AS_OF,
                "modifications": {"remove_all_task_events": True},
                "expected_present": [],
                "expected_absent": [f"CALENDAR_UNKNOWN|task={T1_KEY}", f"WINDOW_LOST|task={T1_KEY}"],
                "expected_health": {T1_KEY: "UNKNOWN", T2_KEY: "UNKNOWN", T3_KEY: "FAILING"},
            },
        ],
    }
    (HERE / "vectors_alerts.json").write_text(json.dumps(vectors, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print("fixtures written to", inputs)


if __name__ == "__main__":
    main()
