from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from ak_trading.artifacts import (
    build_run_manifest,
    default_journal_path,
    report_path_for,
    require_distinct_artifact_paths,
    summarize_journal,
    write_journal_report,
)
from ak_trading.journal import JsonlJournal


def test_default_path_is_safe_and_report_is_adjacent(tmp_path: Path):
    path = default_journal_path(
        tmp_path,
        prefix="dual ma",
        symbol="KQ.m@SHFE.rb",
        run_id="run:01",
        now=datetime(2026, 7, 23, 12, 30, tzinfo=timezone.utc),
    )

    assert path.parent == tmp_path
    assert path.name == "dual_ma_KQ.m_SHFE.rb_20260723T123000000000Z_run_01.jsonl"
    assert report_path_for(path).name.endswith(".summary.json")


def test_summary_uses_recorded_evidence_without_pnl(tmp_path: Path):
    journal_path = tmp_path / "run.jsonl"
    journal = JsonlJournal(journal_path)
    journal(
        build_run_manifest(
            run_id="run-a",
            mode="sim",
            symbol="SHFE.rb2610",
            market_symbol="KQ.m@SHFE.rb",
            duration_seconds=300,
            fast_window=5,
            slow_window=20,
            max_abs_position=1,
            initial_balance=1_000_000.0,
            created_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
        )
    )
    journal(
        {
            "event": "run_started",
            "run_id": "run-a",
            "mode": "sim",
            "symbol": "SHFE.rb2610",
            "balance": 1_000_000.0,
            "available": 900_000.0,
            "margin": 100_000.0,
            "position": 0,
        }
    )
    journal(
        {
            "event": "decision",
            "run_id": "run-a",
            "mode": "sim",
            "symbol": "SHFE.rb2610",
            "action": "blocked",
            "block_reason": "quote_not_ready",
            "position": 0,
        }
    )
    journal(
        {
            "event": "run_finished",
            "run_id": "run-a",
            "mode": "sim",
            "symbol": "SHFE.rb2610",
            "reason": "max_updates",
            "updates": 4,
            "decisions": 1,
            "target_changes": 0,
            "final_position": 0,
            "balance": 1_000_100.0,
            "available": 900_100.0,
            "margin": 100_000.0,
            "position": 0,
        }
    )
    journal(
        {
            "event": "shutdown",
            "run_id": "run-a",
            "mode": "sim",
            "symbol": "SHFE.rb2610",
            "flattened": True,
            "task_finished": True,
            "position": 0,
        }
    )

    summary = summarize_journal(journal_path, run_id="run-a")

    assert summary["lifecycle"] == {
        "state": "complete",
        "run_complete": True,
        "shutdown_complete": True,
    }
    assert summary["events"]["decision_actions"] == {"blocked": 1}
    assert summary["events"]["blocked_reasons"] == {"quote_not_ready": 1}
    assert summary["account_snapshots"]["start"]["balance"] == 1_000_000.0
    assert summary["run_summary"] == {
        "reason": "max_updates",
        "updates": 4,
        "decisions": 1,
        "target_changes": 0,
        "final_position": 0,
    }
    assert "pnl" not in json.dumps(summary).lower()


def test_summary_counts_bad_unknown_and_foreign_lines(tmp_path: Path):
    journal_path = tmp_path / "mixed.jsonl"
    journal_path.write_text(
        "{not json}\n"
        "[\"not an event object\"]\n"
        '{"event":"unknown","run_id":"run-a"}\n'
        '{"event":"run_started","run_id":"other","mode":"sim"}\n'
        '\n',
        encoding="utf-8",
    )

    summary = summarize_journal(journal_path, run_id="run-a")

    assert summary["integrity"]["malformed_lines"] == 1
    assert summary["integrity"]["non_object_lines"] == 1
    assert summary["integrity"]["unknown_events"] == 1
    assert summary["integrity"]["foreign_records"] == 1
    assert summary["integrity"]["blank_lines"] == 1
    assert summary["lifecycle"]["state"] == "incomplete"


def test_write_report_is_parseable(tmp_path: Path):
    journal_path = tmp_path / "run.jsonl"
    JsonlJournal(journal_path)(
        {
            "event": "run_manifest",
            "run_id": "run-a",
            "mode": "backtest",
            "symbol": "DCE.m2609",
        }
    )
    report_path = tmp_path / "result.json"

    summary = write_journal_report(journal_path, report_path, run_id="run-a")

    assert json.loads(report_path.read_text(encoding="utf-8")) == summary


def test_unfiltered_single_manifest_reports_its_run_id(tmp_path: Path):
    journal_path = tmp_path / "run.jsonl"
    JsonlJournal(journal_path)(
        {
            "event": "run_manifest",
            "run_id": "run-a",
            "mode": "backtest",
            "symbol": "DCE.m2609",
        }
    )

    assert summarize_journal(journal_path)["run_id"] == "run-a"


def test_report_cannot_replace_its_source_journal(tmp_path: Path):
    journal_path = tmp_path / "run.jsonl"
    original = '{"event":"run_manifest","run_id":"run-a"}\n'
    journal_path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="must be different"):
        require_distinct_artifact_paths(journal_path, journal_path)
    with pytest.raises(ValueError, match="must be different"):
        write_journal_report(journal_path, journal_path, run_id="run-a")

    assert journal_path.read_text(encoding="utf-8") == original


def test_string_shutdown_flags_do_not_claim_completion(tmp_path: Path):
    journal_path = tmp_path / "invalid-flags.jsonl"
    journal = JsonlJournal(journal_path)
    common = {"run_id": "run-a", "mode": "sim", "symbol": "SHFE.rb2610"}
    journal({"event": "run_manifest", "schema_version": 1, **common})
    journal({"event": "run_started", **common})
    journal({"event": "run_finished", **common})
    journal(
        {
            "event": "shutdown",
            "flattened": "false",
            "task_finished": "true",
            **common,
        }
    )

    assert summarize_journal(journal_path, run_id="run-a")["lifecycle"] == {
        "state": "incomplete",
        "run_complete": True,
        "shutdown_complete": False,
    }
