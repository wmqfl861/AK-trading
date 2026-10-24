#!/usr/bin/env python
"""DP06 report CLI: build and publish the recomputable monitoring report.

Offline by construction: reads only the explicitly injected input roots
(heartbeat files, status-event spool, quality reports, calendar answers,
task manifest, run facts), evaluates one independent monitor tick, and
publishes JSON facts + Markdown summary + offline HTML into the injected
output root.  No network, no database, no .env, no task registration.

Exit codes (frozen DP01 CLIExitCode semantics applied to the report run):
    0  SUCCESS                 report published; no active WARNING/CRITICAL
                               alerts; every scope COMPLETED and overall
                               coverage COMPLETE
    1  CONTRACT_VIOLATION      bad inputs (missing roots, malformed facts,
                               credential-shaped text, output escape)
    2  RUN_FAILED              local output publication failed
                               (DISK_OUTPUT_FAILED)
    3  COMPLETED_WITH_KNOWN_GAPS  report published but active alerts or
                               non-complete coverage remain (never masked
                               with 0)

Determinism: with an explicit --as-of the produced report is byte-stable
across runs on identical inputs (V11).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

_REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from ak_trading.datapipeline.dashboard import render_html  # noqa: E402
from ak_trading.datapipeline.monitoring import (  # noqa: E402
    MonitoringError,
    default_thresholds,
    evaluate_tick,
    frozen_contracts_module,
    load_pipeline_facts,
    thresholds_from_mapping,
    LocalMonitorStore,
)
from ak_trading.datapipeline.reporting import (  # noqa: E402
    EVIDENCE_HASH_FIELDS,
    build_report,
    publish_report,
    render_markdown,
)


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="report_datapipeline.py",
        description="DP06 local monitoring report (offline, dry, no system changes)",
    )
    parser.add_argument("--output-root", required=True, help="R root for reports/, alerts/, publish-index/")
    parser.add_argument("--monitor-id", default="dp06-monitor-local")
    parser.add_argument("--heartbeats-root", default=None)
    parser.add_argument("--events-root", default=None)
    parser.add_argument("--quality-root", default=None)
    parser.add_argument("--tasks-file", default=None)
    parser.add_argument("--calendar-file", default=None)
    parser.add_argument("--runs-file", default=None)
    parser.add_argument("--hashes-file", default=None, help="JSON mapping of evidence-chain hashes")
    parser.add_argument("--thresholds-file", default=None, help="JSON monitoring thresholds (explicit override)")
    parser.add_argument("--as-of", default=None, help="ISO-8601 instant with offset; omit for wall clock")
    parser.add_argument("--data-origin", default="SYNTHETIC", choices=["SYNTHETIC", "SCENARIO", "REAL_SOURCE"])
    return parser.parse_args(argv)


def _load_json_file(path: str, field: str) -> Any:
    target = Path(path)
    if not target.is_file():
        raise MonitoringError("E_DP06_INPUT_ROOT_MISSING", f"{field} file missing: {target}")
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MonitoringError("E_DP06_INPUT_UNREADABLE", f"{field} unreadable ({type(exc).__name__})") from None


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    contracts = frozen_contracts_module()
    try:
        thresholds = default_thresholds()
        if args.thresholds_file:
            thresholds = thresholds_from_mapping(_load_json_file(args.thresholds_file, "--thresholds-file"))
        if args.as_of:
            as_of_nano = contracts.iso_instant_to_nano(args.as_of)
            wall_clock_used = False
        else:
            as_of_nano = int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)
            wall_clock_used = True

        hashes: Any = {}
        if args.hashes_file:
            hashes = _load_json_file(args.hashes_file, "--hashes-file")
        unknown_hash_keys = sorted(set(hashes) - set(EVIDENCE_HASH_FIELDS))
        if unknown_hash_keys:
            raise MonitoringError(
                "E_DP06_INPUT_UNKNOWN_HASH_KEY",
                f"hashes file carries unknown keys {unknown_hash_keys}",
            )

        snapshot = load_pipeline_facts(
            heartbeats_root=Path(args.heartbeats_root) if args.heartbeats_root else None,
            events_root=Path(args.events_root) if args.events_root else None,
            quality_root=Path(args.quality_root) if args.quality_root else None,
            tasks_file=Path(args.tasks_file) if args.tasks_file else None,
            calendar_file=Path(args.calendar_file) if args.calendar_file else None,
            runs_file=Path(args.runs_file) if args.runs_file else None,
        )

        output_root = Path(args.output_root)
        store = LocalMonitorStore(output_root, args.monitor_id)
        state = store.read_state()
        result = evaluate_tick(state, snapshot, as_of_nano, thresholds)
        try:
            store.publish_tick(result)
        except MonitoringError as exc:
            if exc.code == "E_DP06_DISK_OUTPUT_FAILED":
                _best_effort_failure_marker(output_root, exc.code)
                print(f"report run failed: {exc.code}", file=sys.stderr)
                return 2
            raise

        facts = build_report(
            monitor_id=args.monitor_id,
            as_of_nano=as_of_nano,
            snapshot=snapshot,
            alert_state=result.state.to_dict(),
            health=result.health,
            health_reasons=result.health_reasons,
            evidence_hashes=hashes,
            data_origin=args.data_origin,
            wall_clock_used=wall_clock_used,
        )
        markdown = render_markdown(facts)
        html = render_html(facts)
        try:
            published = publish_report(
                facts=facts,
                markdown=markdown,
                html=html,
                output_root=output_root,
                monitor_id=args.monitor_id,
            )
        except MonitoringError as exc:
            if exc.code == "E_DP06_DISK_OUTPUT_FAILED":
                _best_effort_failure_marker(output_root, exc.code)
                print(f"report run failed: {exc.code}", file=sys.stderr)
                return 2
            raise

        print(
            json.dumps(
                {
                    "as_of_nano": facts["as_of_nano"],
                    "path": published["path"],
                    "report_id": published["report_id"],
                    "revision": published["revision"],
                },
                sort_keys=True,
            )
        )

        active_blocking = any(
            count > 0
            for severity, count in facts["alert_summary"]["by_severity"].items()
            if severity in ("WARNING", "CRITICAL")
        )
        coverage = facts["coverage"]
        if (
            active_blocking
            or coverage["overall_check_execution"] != "COMPLETED"
            or coverage["overall_coverage_state"] != "COMPLETE"
        ):
            return 3
        return 0
    except MonitoringError as exc:
        print(f"contract violation: {exc.code}: {exc.detail}", file=sys.stderr)
        return 1


def _best_effort_failure_marker(output_root: Path, code: str) -> None:
    """Best-effort local marker when output publication fails; itself
    allowed to fail silently (the non-zero exit is the durable signal)."""
    try:
        marker = output_root / "alerts" / "output-failure.marker"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {"code": code, "schema_version": "dp06-report/1"},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
