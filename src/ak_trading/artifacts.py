"""Versioned, local artifacts for reproducible strategy runs.

The summaries intentionally describe only JSONL evidence already recorded by the
runner. They do not infer fills, trades, P&L, or performance metrics.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping
from uuid import uuid4


JOURNAL_SCHEMA_VERSION = 1
_KNOWN_EVENTS = frozenset(
    {"run_manifest", "run_started", "decision", "run_finished", "shutdown"}
)
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_SNAPSHOT_FIELDS = ("balance", "available", "margin", "position")
_SUMMARY_FIELDS = ("reason", "updates", "decisions", "target_changes", "final_position")


def new_run_id() -> str:
    """Create an opaque, non-sensitive identifier for one local run."""
    return uuid4().hex


def default_journal_path(
    directory: str | Path,
    *,
    prefix: str,
    symbol: str,
    run_id: str,
    now: datetime | None = None,
) -> Path:
    """Build a collision-resistant default journal path without creating it."""
    timestamp = _utc_timestamp(now)
    return Path(directory) / (
        f"{_filename_part(prefix)}_{_filename_part(symbol)}_"
        f"{timestamp}_{_filename_part(run_id)}.jsonl"
    )


def report_path_for(journal_path: str | Path) -> Path:
    """Place a report beside its journal while preserving an explicit journal path."""
    return Path(journal_path).with_suffix(".summary.json")


def require_distinct_artifact_paths(
    journal_path: str | Path, report_path: str | Path
) -> None:
    """Prevent a JSON report from atomically replacing its source journal."""
    if Path(journal_path).resolve() == Path(report_path).resolve():
        raise ValueError("journal and report paths must be different")


def build_run_manifest(
    *,
    run_id: str,
    mode: str,
    symbol: str,
    duration_seconds: int,
    fast_window: int,
    slow_window: int,
    max_abs_position: int,
    initial_balance: float,
    market_symbol: str | None = None,
    backtest_start: str | None = None,
    backtest_end: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the public, credential-free configuration record for one run."""
    if mode not in {"sim", "backtest"}:
        raise ValueError("mode must be 'sim' or 'backtest'")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty string")

    manifest: dict[str, Any] = {
        "event": "run_manifest",
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "run_id": run_id.strip(),
        "created_at": _utc_datetime(created_at).isoformat(),
        "mode": mode,
        "symbol": symbol.strip(),
        "strategy": {
            "name": "dual_ma",
            "fast_window": fast_window,
            "slow_window": slow_window,
        },
        "execution": {
            "duration_seconds": duration_seconds,
            "max_abs_position": max_abs_position,
            "allow_short": False,
            "initial_balance": initial_balance,
        },
    }
    if market_symbol:
        manifest["market_symbol"] = market_symbol.strip()
    if backtest_start is not None or backtest_end is not None:
        manifest["backtest"] = {"start": backtest_start, "end": backtest_end}
    return manifest


def summarize_journal(
    journal_path: str | Path, *, run_id: str | None = None
) -> dict[str, Any]:
    """Summarize one journal's recorded evidence without deriving trading results."""
    path = Path(journal_path)
    event_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    blocked_reason_counts: Counter[str] = Counter()
    manifests: list[Mapping[str, Any]] = []
    starts: list[Mapping[str, Any]] = []
    finishes: list[Mapping[str, Any]] = []
    shutdowns: list[Mapping[str, Any]] = []
    mode_values: set[str] = set()
    symbol_values: set[str] = set()
    malformed_lines = 0
    non_object_lines = 0
    blank_lines = 0
    unknown_events = 0
    foreign_records = 0
    selected_records = 0
    last_snapshot: dict[str, Any] | None = None

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                blank_lines += 1
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed_lines += 1
                continue
            if not isinstance(record, Mapping):
                non_object_lines += 1
                continue

            if run_id is not None and record.get("run_id") != run_id:
                foreign_records += 1
                continue

            event = record.get("event")
            if not isinstance(event, str) or event not in _KNOWN_EVENTS:
                unknown_events += 1
                continue

            selected_records += 1
            event_counts[event] += 1
            _collect_identity(record, mode_values, symbol_values)
            snapshot = _snapshot(record)
            if snapshot:
                last_snapshot = snapshot

            if event == "run_manifest":
                manifests.append(record)
            elif event == "run_started":
                starts.append(record)
            elif event == "run_finished":
                finishes.append(record)
            elif event == "shutdown":
                shutdowns.append(record)
            elif event == "decision":
                action = record.get("action")
                if isinstance(action, str):
                    action_counts[action] += 1
                blocked_reason = record.get("block_reason")
                if isinstance(blocked_reason, str):
                    blocked_reason_counts[blocked_reason] += 1

    manifest = manifests[0] if len(manifests) == 1 else None
    mode = _single_value(mode_values)
    symbol = _single_value(symbol_values)
    initial_snapshot = _snapshot(starts[0]) if len(starts) == 1 else None
    final_summary = _summary(finishes[-1]) if finishes else None
    shutdown = shutdowns[-1] if shutdowns else None
    shutdown_complete = (
        shutdown.get("flattened") is True and shutdown.get("task_finished") is True
        if shutdown is not None
        else None
    )
    run_complete = len(starts) == 1 and len(finishes) == 1
    lifecycle_complete = (
        len(manifests) == 1
        and run_complete
        and mode in {"sim", "backtest"}
        and symbol is not None
        and ((mode != "sim") or shutdown_complete is True)
    )
    reported_run_id = run_id
    if reported_run_id is None and manifest is not None:
        manifest_run_id = manifest.get("run_id")
        if isinstance(manifest_run_id, str) and manifest_run_id:
            reported_run_id = manifest_run_id

    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "journal": str(path),
        "run_id": reported_run_id,
        "manifest": dict(manifest) if manifest is not None else None,
        "identity": {"mode": mode, "symbol": symbol},
        "integrity": {
            "selected_records": selected_records,
            "foreign_records": foreign_records,
            "malformed_lines": malformed_lines,
            "non_object_lines": non_object_lines,
            "blank_lines": blank_lines,
            "unknown_events": unknown_events,
            "manifest_count": len(manifests),
            "run_started_count": len(starts),
            "run_finished_count": len(finishes),
            "shutdown_count": len(shutdowns),
        },
        "lifecycle": {
            "state": "complete" if lifecycle_complete else "incomplete",
            "run_complete": run_complete,
            "shutdown_complete": shutdown_complete,
        },
        "events": {
            "counts": dict(sorted(event_counts.items())),
            "decision_actions": dict(sorted(action_counts.items())),
            "blocked_reasons": dict(sorted(blocked_reason_counts.items())),
        },
        "account_snapshots": {
            "start": initial_snapshot,
            "last_recorded": last_snapshot,
        },
        "run_summary": final_summary,
    }


def write_journal_report(
    journal_path: str | Path,
    report_path: str | Path,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Write a complete JSON report atomically and return its contents."""
    require_distinct_artifact_paths(journal_path, report_path)
    summary = summarize_journal(journal_path, run_id=run_id)
    destination = Path(report_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return summary


def _utc_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_timestamp(value: datetime | None) -> str:
    return _utc_datetime(value).strftime("%Y%m%dT%H%M%S%fZ")


def _filename_part(value: str) -> str:
    cleaned = _FILENAME_UNSAFE.sub("_", str(value).strip()).strip("._")
    return cleaned or "run"


def _collect_identity(
    record: Mapping[str, Any], mode_values: set[str], symbol_values: set[str]
) -> None:
    mode = record.get("mode")
    symbol = record.get("symbol")
    if isinstance(mode, str) and mode:
        mode_values.add(mode)
    if isinstance(symbol, str) and symbol:
        symbol_values.add(symbol)


def _single_value(values: set[str]) -> str | None:
    return next(iter(values)) if len(values) == 1 else None


def _snapshot(record: Mapping[str, Any]) -> dict[str, Any] | None:
    snapshot = {field: record[field] for field in _SNAPSHOT_FIELDS if field in record}
    return snapshot or None


def _summary(record: Mapping[str, Any]) -> dict[str, Any]:
    return {field: record.get(field) for field in _SUMMARY_FIELDS}
