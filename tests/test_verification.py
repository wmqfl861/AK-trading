from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from ak_trading.verification import (
    ExpectedParams,
    parse_backtest_stat,
    parse_run_summary,
    verify_backtest_artifacts,
    VerificationError,
    main as verifier_main,
)


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SYMBOL = "SHFE.rb2610"


def _write(path: Path, data: str | bytes, *, encoding: str | None = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding=encoding or "utf-8", newline="\n")
    return path


def _manifest(**overrides):
    payload = {
        "event": "run_manifest",
        "schema_version": 1,
        "run_id": RUN_ID,
        "created_at": "2026-07-23T13:48:00.775323+00:00",
        "mode": "backtest",
        "symbol": SYMBOL,
        "strategy": {"name": "dual_ma", "fast_window": 5, "slow_window": 20},
        "execution": {
            "duration_seconds": 300,
            "max_abs_position": 1,
            "allow_short": False,
            "initial_balance": 1_000_000.0,
        },
        "backtest": {"start": "2026-07-01", "end": "2026-07-03"},
    }
    payload.update(overrides)
    return payload


def _started(**overrides):
    payload = {
        "event": "run_started",
        "mode": "backtest",
        "symbol": SYMBOL,
        "duration_seconds": 300,
        "balance": 1_000_000.0,
        "available": 1_000_000.0,
        "margin": 0.0,
        "position": 0,
        "run_id": RUN_ID,
    }
    payload.update(overrides)
    return payload


def _decision(bar: str, action: str = "target_already_requested", **overrides):
    payload = {
        "event": "decision",
        "mode": "backtest",
        "symbol": SYMBOL,
        "duration_seconds": 300,
        "balance": 999_900.0,
        "available": 999_900.0,
        "margin": 0.0,
        "position": 0,
        "run_id": RUN_ID,
        "bar_datetime": bar,
        "close": 3000.0,
        "fast_ma": 3001.0,
        "slow_ma": 2999.0,
        "signal_target": 0,
        "signal_reason": "fast_not_above_slow",
        "risk_target": 0,
        "risk_reason": "allowed",
        "current_position": 0,
        "requested_target": 0,
        "last_price": 3000.0,
        "action": action,
        "block_reason": None,
    }
    payload.update(overrides)
    return payload


def _finished(**overrides):
    payload = {
        "event": "run_finished",
        "mode": "backtest",
        "symbol": SYMBOL,
        "duration_seconds": 300,
        "balance": 999_486.74,
        "available": 999_486.74,
        "margin": 0.0,
        "position": 0,
        "run_id": RUN_ID,
        "reason": "backtest_finished",
        "updates": 10,
        "decisions": 2,
        "target_changes": 1,
        "final_position": 0,
    }
    payload.update(overrides)
    return payload


def _jsonl(records) -> str:
    return "".join(
        json.dumps(item, ensure_ascii=True, separators=(",", ":")) + "\n" for item in records
    )


def _report_from_records(journal_path: Path, records, *, foreign: int = 0) -> dict:
    selected = [item for item in records if item.get("run_id") == RUN_ID]
    manifests = [item for item in selected if item["event"] == "run_manifest"]
    starts = [item for item in selected if item["event"] == "run_started"]
    finishes = [item for item in selected if item["event"] == "run_finished"]
    decisions = [item for item in selected if item["event"] == "decision"]
    shutdowns = [item for item in selected if item["event"] == "shutdown"]
    event_counts = {}
    action_counts = {}
    blocked = {}
    for item in selected:
        event_counts[item["event"]] = event_counts.get(item["event"], 0) + 1
        if item["event"] == "decision":
            action = item.get("action")
            if isinstance(action, str):
                action_counts[action] = action_counts.get(action, 0) + 1
            reason = item.get("block_reason")
            if isinstance(reason, str):
                blocked[reason] = blocked.get(reason, 0) + 1
    last_snapshot = None
    for item in selected:
        if all(key in item for key in ("balance", "available", "margin", "position")):
            last_snapshot = {
                key: item[key] for key in ("balance", "available", "margin", "position")
            }
    start_snapshot = None
    if starts and all(
        key in starts[0] for key in ("balance", "available", "margin", "position")
    ):
        start_snapshot = {
            key: starts[0][key]
            for key in ("balance", "available", "margin", "position")
        }
    finish = finishes[-1] if finishes else {}
    return {
        "schema_version": 1,
        "journal": str(journal_path),
        "run_id": RUN_ID,
        "manifest": manifests[0] if manifests else None,
        "identity": {"mode": "backtest", "symbol": SYMBOL},
        "integrity": {
            "selected_records": len(selected),
            "foreign_records": foreign,
            "malformed_lines": 0,
            "non_object_lines": 0,
            "blank_lines": 0,
            "unknown_events": 0,
            "manifest_count": len(manifests),
            "run_started_count": len(starts),
            "run_finished_count": len(finishes),
            "shutdown_count": len(shutdowns),
        },
        "lifecycle": {
            "state": "complete",
            "run_complete": True,
            "shutdown_complete": None,
        },
        "events": {
            "counts": dict(sorted(event_counts.items())),
            "decision_actions": dict(sorted(action_counts.items())),
            "blocked_reasons": dict(sorted(blocked.items())),
        },
        "account_snapshots": {
            "start": start_snapshot,
            "last_recorded": last_snapshot,
        },
        "run_summary": {
            "reason": finish.get("reason"),
            "updates": finish.get("updates"),
            "decisions": finish.get("decisions"),
            "target_changes": finish.get("target_changes"),
            "final_position": finish.get("final_position"),
        },
    }


def _default_records(*, final_position: int = 0, set_target_to_zero: bool = True):
    d1 = _decision(
        "bar-1",
        action="set_target",
        signal_target=1,
        signal_reason="fast_above_slow",
        risk_target=1,
        requested_target=1,
        current_position=0,
        position=0,
    )
    if set_target_to_zero:
        d2 = _decision(
            "bar-2",
            action="set_target",
            signal_target=0,
            risk_target=0,
            requested_target=0,
            current_position=1,
            position=1,
            balance=999_900.0,
        )
        target_changes = 2
        decisions = 2
    else:
        d2 = _decision("bar-2", action="target_already_requested", position=final_position)
        target_changes = 1
        decisions = 2
    finish = _finished(
        decisions=decisions,
        target_changes=target_changes,
        final_position=final_position,
        position=final_position,
        balance=999_486.74,
        available=999_486.74,
    )
    return [_manifest(), _started(), d1, d2, finish]


def _stat_text(
    balance: float = 999_486.74,
    *,
    start_date: str = "2026-07-01",
    end_date: str = "2026-07-03",
) -> str:
    return (
        "{"
        f"'start_date': '{start_date}', 'end_date': '{end_date}', "
        f"'init_balance': np.float64(1000000.0), 'balance': np.float64({balance}), "
        f"'start_balance': np.float64(1000000.0), 'end_balance': np.float64({balance}), "
        f"'ror': np.float64(-0.0005), 'annual_yield': np.float64(-0.04), "
        f"'trading_days': 3, 'cum_profit_days': np.int64(1), 'cum_loss_days': np.int64(2), "
        f"'max_drawdown': np.float64(0.0001), 'commission': np.float64(43.26), "
        f"'open_times': 1, 'close_times': 1, 'daily_risk_ratio': np.float64(0.0), "
        f"'max_cont_profit_days': np.int64(1), 'max_cont_loss_days': np.int64(2), "
        f"'sharpe_ratio': np.float64(-1.0), 'calmar_ratio': np.float64(-1.0), "
        f"'sortino_ratio': np.float64(-1.0), 'tqsdk_punchline': 'ok', "
        f"'profit_volumes': 0, 'loss_volumes': 1, 'profit_value': np.float64(0.0), "
        f"'loss_value': np.float64(-10.0), 'winning_rate': 0.0, "
        f"'profit_loss_ratio': np.float64(0.0)"
        "}"
    )


def _done_text(
    *,
    updates: int = 10,
    decisions: int = 2,
    target_changes: int = 2,
    final_position: int = 0,
) -> str:
    return (
        "RunSummary("
        f"reason='backtest_finished', updates={updates}, decisions={decisions}, "
        f"target_changes={target_changes}, final_position={final_position})"
    )


def _build_bundle(
    tmp_path: Path,
    *,
    records=None,
    stdout_extra_prefix: bytes | None = None,
    stdout_encoding: str = "utf-8",
    final_position: int = 0,
    set_target_to_zero: bool = True,
    exit_code: int = 0,
    mutate_report=None,
    foreign_records=None,
    include_tokens: bool = True,
    token_order=None,
    report_status: str = "complete",
    journal_token_path: str | None = None,
    report_token_path: str | None = None,
    journal_name: str = "run.jsonl",
    report_name: str = "run.summary.json",
    stat_text: str | None = None,
    done_text: str | None = None,
):
    records = list(
        records
        if records is not None
        else _default_records(
            final_position=final_position,
            set_target_to_zero=set_target_to_zero,
        )
    )
    if foreign_records:
        records = list(foreign_records) + records
    journal_path = tmp_path / journal_name
    report_path = tmp_path / report_name
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"

    _write(journal_path, _jsonl(records))
    report = _report_from_records(
        journal_path,
        records,
        foreign=len(foreign_records or []),
    )
    if mutate_report is not None:
        mutate_report(report)
    _write(
        report_path,
        json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
    )

    finish = next(
        item
        for item in records
        if item.get("event") == "run_finished" and item.get("run_id") == RUN_ID
    )
    done = done_text or _done_text(
        updates=finish["updates"],
        decisions=finish["decisions"],
        target_changes=finish["target_changes"],
        final_position=finish["final_position"],
    )
    balance = float(finish["balance"])
    stat = stat_text if stat_text is not None else _stat_text(balance)

    j_token = journal_token_path if journal_token_path is not None else str(journal_path)
    r_token = report_token_path if report_token_path is not None else str(report_path)
    token_lines = {
        "BACKTEST_REPORT": f"BACKTEST_REPORT {r_token} {report_status}",
        "BACKTEST_DONE": f"BACKTEST_DONE {done}",
        "BACKTEST_STAT": f"BACKTEST_STAT {stat}",
        "JOURNAL": f"JOURNAL {j_token}",
    }
    order = token_order or ["BACKTEST_REPORT", "BACKTEST_DONE", "BACKTEST_STAT", "JOURNAL"]
    chinese = "模拟交易下单 TQSIM 回测结束"
    body_lines = [chinese]
    if include_tokens:
        body_lines.extend(token_lines[name] for name in order)
    body_text = "\n".join(body_lines) + "\n"
    if stdout_extra_prefix is not None:
        _write(stdout_path, stdout_extra_prefix + body_text.encode(stdout_encoding))
    else:
        _write(stdout_path, body_text.encode(stdout_encoding))
    _write(stderr_path, "免责声明 disclaimer\n".encode("gbk"))

    return {
        "stdout": stdout_path,
        "stderr": stderr_path,
        "journal": journal_path,
        "report": report_path,
        "exit_code": exit_code,
        "records": records,
    }


def _verify(bundle, *, min_decisions: int = 1, **expected_kwargs):
    expected = ExpectedParams(**expected_kwargs) if expected_kwargs else None
    return verify_backtest_artifacts(
        stdout_path=bundle["stdout"],
        stderr_path=bundle["stderr"],
        journal_path=bundle["journal"],
        report_path=bundle["report"],
        exit_code=bundle["exit_code"],
        expected=expected,
        min_decisions=min_decisions,
    )


def _bundle_with_extra_decision(tmp_path: Path, action: str, **overrides):
    records = _default_records()
    records.insert(3, _decision(f"bar-{tmp_path.name}", action=action, **overrides))
    records[-1]["decisions"] = 3
    records[-1]["updates"] = 11
    if action == "set_target":
        records[-1]["target_changes"] = 3
    return _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")


def _bundle_after_observed_target_task_finished(
    tmp_path: Path, followup: dict
):
    records = _default_records()
    finish = records.pop()
    records.extend(
        [
            _decision(
                "bar-task-finished",
                action="blocked",
                block_reason="target_task_finished",
                risk_target=0,
                requested_target=0,
            ),
            followup,
        ]
    )
    finish.update(
        decisions=4,
        updates=12,
        target_changes=2 + int(followup["action"] == "set_target"),
    )
    records.append(finish)
    return _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")


def _failed_codes(result) -> set[str]:
    return {item["code"] for item in result["checks"] if not item["ok"]}


def _assert_no_canary(text: str, *canaries: str) -> None:
    for canary in canaries:
        assert canary not in text


def test_valid_bundle_with_gbk_stdout_passes(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="gbk")
    result = _verify(
        bundle,
        symbol=SYMBOL,
        start_date="2026-07-01",
        end_date="2026-07-03",
        duration=300,
        fast_window=5,
        slow_window=20,
        max_position=1,
        run_id=RUN_ID,
    )
    assert result["verdict"] == "pass"
    assert result["valid"] is True
    assert result["exit_code"] == 0
    assert result["run_id"] == RUN_ID
    assert result["schema_version"] == 1
    assert "checks" in result


def test_nonzero_final_position_and_set_target_zero_pass(tmp_path: Path):
    records = _default_records(final_position=1, set_target_to_zero=True)
    records[-1]["final_position"] = 1
    records[-1]["position"] = 1
    assert any(
        item.get("action") == "set_target" and item.get("risk_target") == 0
        for item in records
    )
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="gbk")
    result = _verify(bundle)
    assert result["verdict"] == "pass"


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("missing", "token_missing"),
        ("duplicate", "token_duplicate"),
        ("reorder", "token_order"),
        ("embedded", "token_embedded"),
        ("leading_ws", "token_leading_whitespace"),
    ],
)
def test_token_failures(tmp_path: Path, mutation: str, code: str):
    if mutation == "missing":
        bundle = _build_bundle(tmp_path, include_tokens=False, stdout_encoding="utf-8")
        body = (
            f"BACKTEST_REPORT {bundle['report']} complete\n"
            f"BACKTEST_DONE {_done_text()}\n"
            f"JOURNAL {bundle['journal']}\n"
        )
        bundle["stdout"].write_text(body, encoding="utf-8")
    elif mutation == "duplicate":
        bundle = _build_bundle(tmp_path)
        text = bundle["stdout"].read_bytes()
        bundle["stdout"].write_bytes(text + b"JOURNAL extra.jsonl\n")
    elif mutation == "reorder":
        bundle = _build_bundle(
            tmp_path,
            token_order=["BACKTEST_DONE", "BACKTEST_REPORT", "BACKTEST_STAT", "JOURNAL"],
            stdout_encoding="utf-8",
        )
    elif mutation == "leading_ws":
        bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
        text = bundle["stdout"].read_text(encoding="utf-8")
        bundle["stdout"].write_text("   JOURNAL fake.jsonl\n" + text, encoding="utf-8")
    else:
        bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
        text = bundle["stdout"].read_text(encoding="utf-8")
        bundle["stdout"].write_text(
            "    INFO - noise BACKTEST_DONE embedded payload\n" + text,
            encoding="utf-8",
        )
    result = _verify(bundle)
    assert result["verdict"] == "invalid"
    assert result["exit_code"] == 1
    assert any(item["code"] == code and not item["ok"] for item in result["checks"])


def test_lifecycle_and_integrity_failures(tmp_path: Path):
    good = _default_records()
    foreign = {
        "event": "run_started",
        "run_id": "foreignrunid0000000000000000000",
        "mode": "backtest",
        "symbol": SYMBOL,
    }
    journal = tmp_path / "run.jsonl"
    lines = [
        json.dumps(foreign, separators=(",", ":")),
        json.dumps(good[0], separators=(",", ":")),
        json.dumps(good[1], separators=(",", ":")),
        "",
        "{not-json",
        "[1,2,3]",
        '{"event":"weird","run_id":"%s"}' % RUN_ID,
        '{"event":"decision","run_id":"%s","a":1,"a":2}' % RUN_ID,
        json.dumps(good[2], separators=(",", ":")),
        json.dumps(good[3], separators=(",", ":")),
        json.dumps(good[4], separators=(",", ":")),
    ]
    journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report_path = tmp_path / "run.summary.json"
    report = _report_from_records(journal, good, foreign=1)
    report_path.write_text(
        json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    stdout.write_text(
        "\n".join(
            [
                f"BACKTEST_REPORT {report_path} complete",
                f"BACKTEST_DONE {_done_text()}",
                f"BACKTEST_STAT {_stat_text()}",
                f"JOURNAL {journal}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    stderr.write_text("ok\n", encoding="utf-8")
    result = verify_backtest_artifacts(
        stdout_path=stdout,
        stderr_path=stderr,
        journal_path=journal,
        report_path=report_path,
        exit_code=0,
    )
    assert result["exit_code"] == 1
    codes = {item["code"] for item in result["checks"] if not item["ok"]}
    assert "journal_blank_line" in codes
    assert "journal_malformed" in codes
    assert "journal_non_object" in codes
    assert "journal_unknown_event" in codes


def test_duplicate_manifest_lifecycle(tmp_path: Path):
    records = _default_records()
    records.insert(1, _manifest())
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 1
    assert any(
        item["code"] in {"manifest_count", "lifecycle_order"} and not item["ok"]
        for item in result["checks"]
    )


def test_schema_run_id_and_param_mismatch(tmp_path: Path):
    records = _default_records()
    records[0]["symbol"] = "DCE.m2609"
    for item in records:
        item["symbol"] = "DCE.m2609"
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(
        bundle,
        symbol=SYMBOL,
        run_id=RUN_ID,
        duration=60,
        fast_window=3,
        slow_window=10,
        max_position=2,
        start_date="2020-01-01",
        end_date="2020-01-02",
    )
    assert result["exit_code"] == 1
    failed = _failed_codes(result)
    assert "symbol_expected" in failed
    assert "duration_expected" in failed


def test_unsupported_manifest_schema_exit2(tmp_path: Path):
    records = _default_records()
    records[0]["schema_version"] = 99
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 2
    assert result["verdict"] == "error"
    assert "manifest_schema_unsupported" in _failed_codes(result)


def test_unsupported_report_schema_and_extra_fields_exit2(tmp_path: Path):
    def mutate(report):
        report["schema_version"] = 99

    bundle = _build_bundle(tmp_path, mutate_report=mutate, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 2
    assert "report_schema_unsupported" in _failed_codes(result)

    def mutate_extra(report):
        report["extra_top"] = True

    bundle2 = _build_bundle(tmp_path / "extra", mutate_report=mutate_extra, stdout_encoding="utf-8")
    result2 = _verify(bundle2)
    assert result2["exit_code"] == 2
    assert "report_schema_unsupported" in _failed_codes(result2)


def test_summary_count_account_native_stat_mismatch(tmp_path: Path):
    def mutate(report):
        report["run_summary"]["decisions"] = 999
        report["account_snapshots"]["last_recorded"]["balance"] = 1.0

    bundle = _build_bundle(tmp_path, mutate_report=mutate, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 1
    failed = _failed_codes(result)
    assert "report_mismatch" in failed or "account_snapshot_mismatch" in failed


def test_account_tolerance_within_1e9_passes(tmp_path: Path):
    def mutate(report):
        base = report["account_snapshots"]["last_recorded"]["balance"]
        report["account_snapshots"]["last_recorded"]["balance"] = base + 1e-12
        report["account_snapshots"]["last_recorded"]["available"] = base + 1e-12

    bundle = _build_bundle(tmp_path, mutate_report=mutate, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 0
    assert result["verdict"] == "pass"


def test_ast_malicious_and_nonfinite_rejected():
    with pytest.raises(VerificationError):
        parse_run_summary(
            "RunSummary(reason=__import__('os').system('id'), updates=1, decisions=1, target_changes=0, final_position=0)"
        )
    with pytest.raises(VerificationError):
        parse_run_summary(
            "RunSummary(reason='x', updates=float('nan'), decisions=1, target_changes=0, final_position=0)"
        )
    with pytest.raises(VerificationError):
        parse_backtest_stat("{'balance': float('inf')}")
    with pytest.raises(VerificationError):
        parse_backtest_stat("__import__('os').system('id')")
    with pytest.raises(VerificationError):
        parse_run_summary(
            "RunSummary(reason='x', updates=1, decisions=1, target_changes=0, final_position=0)\nprint(1)"
        )
    with pytest.raises(VerificationError):
        parse_backtest_stat("{}")
    with pytest.raises(VerificationError):
        parse_backtest_stat("{'x': np.int64(1.9)}")
    with pytest.raises(VerificationError):
        parse_backtest_stat("{'x': " + ("9" * 400) + "}")


def test_path_collision_and_alias(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    result = verify_backtest_artifacts(
        stdout_path=bundle["stdout"],
        stderr_path=bundle["stderr"],
        journal_path=bundle["journal"],
        report_path=bundle["journal"],
        exit_code=0,
    )
    assert result["exit_code"] == 2
    assert result["verdict"] == "error"
    assert any(item["code"] == "input_path_collision" for item in result["checks"])


def test_hardlink_collision_rejected(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    alias = tmp_path / "report_alias.json"
    try:
        os.link(bundle["report"], alias)
    except (OSError, NotImplementedError):
        pytest.skip("hardlink unsupported")
    result = verify_backtest_artifacts(
        stdout_path=bundle["stdout"],
        stderr_path=bundle["stderr"],
        journal_path=bundle["journal"],
        report_path=alias,
        exit_code=0,
    )
    # report path alias hardlinked to report is fine for report itself; collide journal+report
    alias_j = tmp_path / "journal_alias.jsonl"
    try:
        os.link(bundle["journal"], alias_j)
    except (OSError, NotImplementedError):
        pytest.skip("hardlink unsupported")
    result = verify_backtest_artifacts(
        stdout_path=bundle["stdout"],
        stderr_path=bundle["stderr"],
        journal_path=bundle["journal"],
        report_path=alias_j,
        exit_code=0,
    )
    assert result["exit_code"] == 2
    assert "input_path_collision" in _failed_codes(result)


def test_secret_canary_not_echoed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    secret = "SuperSecretCanary-42"
    monkeypatch.setenv("TQ_PASS", secret)
    monkeypatch.setenv("TQ_USER", "canary-user")
    records = _default_records()
    records[2]["signal_reason"] = secret
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    text = json.dumps(result, ensure_ascii=True)
    _assert_no_canary(text, secret, "canary-user")
    assert result["exit_code"] == 1
    assert result["secret_scan"] == "failed"
    assert any(item["code"] == "secret_leak" and not item["ok"] for item in result["checks"])
    for item in result["checks"]:
        if item["code"] == "secret_leak":
            assert secret not in item.get("location", "")
            assert secret not in item.get("detail", "")


def test_secret_json_escaped_and_value_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    secret = "Canary/Value\"Quote"
    monkeypatch.setenv("TQ_PASS", secret)
    monkeypatch.delenv("TQ_USER", raising=False)
    records = _default_records()
    records[2]["signal_reason"] = secret
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    text = json.dumps(result, ensure_ascii=True)
    _assert_no_canary(text, secret, "Canary/Value")
    assert result["secret_scan"] == "failed"
    assert result["exit_code"] == 1


def test_secret_in_path_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    secret = "PathCanarySecret99"
    monkeypatch.setenv("TQ_PASS", secret)
    monkeypatch.delenv("TQ_USER", raising=False)
    nested = tmp_path / f"dir-{secret}"
    nested.mkdir()
    bundle = _build_bundle(nested, stdout_encoding="utf-8")
    result = _verify(bundle)
    text = json.dumps(result, ensure_ascii=True)
    _assert_no_canary(text, secret)
    assert result["secret_scan"] == "failed"


def test_strict_decode_failure(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    bundle["stdout"].write_bytes(b"\xff\xfe\x00\xd8")
    result = _verify(bundle)
    assert result["exit_code"] == 2
    assert any(item["code"] == "decode_error" for item in result["checks"])


def test_resource_boundary_line_limit(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    huge = "x\n" * 20_001
    bundle["stdout"].write_text(huge, encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 2
    assert any(item["code"] == "stream_too_many_lines" for item in result["checks"])


def test_oversized_file_exit2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import ak_trading.verification as verification

    monkeypatch.setattr(verification, "MAX_FILE_BYTES", 64)
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    bundle["journal"].write_bytes(b"x" * 128)
    result = _verify(bundle)
    assert result["exit_code"] == 2
    assert "input_too_large" in _failed_codes(result)


def test_exit_code_nonzero_invalidates(tmp_path: Path):
    bundle = _build_bundle(tmp_path, exit_code=2, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 1
    assert any(item["code"] == "exit_code" and not item["ok"] for item in result["checks"])


def test_cli_valid_bundle(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="gbk")
    env = os.environ.copy()
    env.pop("TQ_USER", None)
    env.pop("TQ_PASS", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "verify_backtest_artifacts.py"),
            "--stdout",
            str(bundle["stdout"]),
            "--stderr",
            str(bundle["stderr"]),
            "--journal",
            str(bundle["journal"]),
            "--report",
            str(bundle["report"]),
            "--exit-code",
            "0",
            "--expected-symbol",
            SYMBOL,
            "--expected-run-id",
            RUN_ID,
            "--min-decisions",
            "1",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["verdict"] == "pass"
    assert payload["valid"] is True
    assert secret_free(completed.stdout, completed.stderr)


def secret_free(*parts: str) -> bool:
    joined = "".join(parts)
    return all(ord(ch) < 128 for ch in joined)


def test_cli_missing_args_exits_2_json():
    env = os.environ.copy()
    env.pop("TQ_USER", None)
    env.pop("TQ_PASS", None)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_backtest_artifacts.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["verdict"] == "error"
    assert payload["exit_code"] == 2


def test_cli_double_run_byte_identical(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    env = os.environ.copy()
    env.pop("TQ_USER", None)
    env.pop("TQ_PASS", None)
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "verify_backtest_artifacts.py"),
        "--stdout",
        str(bundle["stdout"]),
        "--stderr",
        str(bundle["stderr"]),
        "--journal",
        str(bundle["journal"]),
        "--report",
        str(bundle["report"]),
        "--exit-code",
        "0",
    ]
    first = subprocess.run(cmd, cwd=ROOT, capture_output=True, check=False, env=env)
    second = subprocess.run(cmd, cwd=ROOT, capture_output=True, check=False, env=env)
    assert first.returncode == 0
    assert first.stdout == second.stdout
    assert first.stdout.endswith(b"\n")


def test_report_status_not_complete(tmp_path: Path):
    bundle = _build_bundle(tmp_path, report_status="incomplete", stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 1
    assert any(item["code"] == "report_status" and not item["ok"] for item in result["checks"])


def test_secret_scan_skipped_without_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TQ_USER", raising=False)
    monkeypatch.delenv("TQ_PASS", raising=False)
    monkeypatch.delenv("AK_DATABASE_URL", raising=False)
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["secret_scan"] == "skipped"
    assert result["verdict"] == "pass"


def test_database_secret_alone_enables_scan_without_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database_password = "DatabaseCanarySecret42"
    monkeypatch.delenv("TQ_USER", raising=False)
    monkeypatch.delenv("TQ_PASS", raising=False)
    monkeypatch.setenv(
        "AK_DATABASE_URL",
        f"postgresql://ak_trading:{database_password}@127.0.0.1:5432/ak_trading",
    )
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")

    result = _verify(bundle)

    assert result["secret_scan"] == "performed"
    assert result["verdict"] == "pass"
    assert database_password not in json.dumps(result, ensure_ascii=True)


def test_parse_real_shape_np_values():
    done = parse_run_summary(
        "RunSummary(reason='backtest_finished', updates=2085, decisions=207, target_changes=14, final_position=0)"
    )
    assert done["updates"] == 2085
    stat = parse_backtest_stat(
        "{'start_date': '2026-07-01', 'balance': np.float64(999486.7400000005), "
        "'cum_loss_days': np.int64(2), 'winning_rate': 0.14285714285714285}"
    )
    assert stat["balance"] == pytest.approx(999486.7400000005)
    assert stat["cum_loss_days"] == 2


def test_path_with_spaces_supported(tmp_path: Path):
    spaced = tmp_path / "path with spaces"
    spaced.mkdir()
    bundle = _build_bundle(spaced, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 0


def test_wrong_same_basename_path_rejected(tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "run.jsonl").write_text("{}\n", encoding="utf-8")
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    # token points to different file with same basename
    text = bundle["stdout"].read_text(encoding="utf-8")
    text = text.replace(str(bundle["journal"]), str(other / "run.jsonl"))
    bundle["stdout"].write_text(text, encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 1
    assert "journal_path_match" in _failed_codes(result)


def test_wrong_basename_nonexisting_rejected(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    text = bundle["stdout"].read_text(encoding="utf-8")
    fake = tmp_path / "missing" / "run.jsonl"
    text = text.replace(str(bundle["journal"]), str(fake))
    bundle["stdout"].write_text(text, encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 1
    assert "journal_path_match" in _failed_codes(result)


def test_zero_decisions_fail(tmp_path: Path):
    records = [_manifest(), _started(), _finished(decisions=0, target_changes=0)]
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle, min_decisions=1)
    assert result["exit_code"] == 1
    failed = _failed_codes(result)
    assert "min_decisions" in failed or "set_target_required" in failed


def test_no_set_target_to_zero_fail(tmp_path: Path):
    records = _default_records(set_target_to_zero=False)
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 1
    assert "set_target_to_zero" in _failed_codes(result)


def test_shutdown_event_fails(tmp_path: Path):
    records = _default_records()
    records.append(
        {
            "event": "shutdown",
            "mode": "backtest",
            "symbol": SYMBOL,
            "duration_seconds": 300,
            "run_id": RUN_ID,
            "balance": 1.0,
            "available": 1.0,
            "margin": 0.0,
            "position": 0,
        }
    )
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 1
    failed = _failed_codes(result)
    assert "shutdown_count" in failed or "lifecycle_order" in failed


def test_missing_event_identity_fails(tmp_path: Path):
    records = _default_records()
    del records[2]["duration_seconds"]
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 2
    assert "decision_schema" in _failed_codes(result)


def test_negative_final_position_fails(tmp_path: Path):
    records = _default_records(final_position=-1, set_target_to_zero=True)
    records[-1]["final_position"] = -1
    records[-1]["position"] = -1
    # also put negative on a non-set_target decision
    records[3]["position"] = -1
    records[3]["current_position"] = -1
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 1
    failed = _failed_codes(result)
    assert "long_only" in failed or "final_position_long_only" in failed


def test_non_set_target_negative_target_fails(tmp_path: Path):
    records = _default_records()
    records[3]["risk_target"] = -1
    records[3]["requested_target"] = -1
    records[3]["signal_target"] = -1
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 1
    assert "long_only" in _failed_codes(result)


def test_stat_wrong_dates_fail(tmp_path: Path):
    bundle = _build_bundle(
        tmp_path,
        stdout_encoding="utf-8",
        stat_text=_stat_text(start_date="2020-01-01", end_date="2020-01-02"),
    )
    result = _verify(bundle)
    assert result["exit_code"] == 1
    failed = _failed_codes(result)
    assert "stat_start_date" in failed or "stat_end_date" in failed


def test_manifest_initial_balance_mismatch_start(tmp_path: Path):
    records = _default_records()
    records[0]["execution"] = dict(records[0]["execution"])
    records[0]["execution"]["initial_balance"] = 500_000.0
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 1
    assert "start_balance_vs_manifest" in _failed_codes(result)


def test_bool_not_int_rejected(tmp_path: Path):
    records = _default_records()
    records[0]["execution"] = dict(records[0]["execution"])
    records[0]["execution"]["duration_seconds"] = True
    for item in records[1:]:
        item["duration_seconds"] = True
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 2
    failed = _failed_codes(result)
    assert "manifest_schema" in failed or "started_schema" in failed


def test_illegal_expected_params_exit2(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    result = verify_backtest_artifacts(
        stdout_path=bundle["stdout"],
        stderr_path=bundle["stderr"],
        journal_path=bundle["journal"],
        report_path=bundle["report"],
        exit_code=0,
        expected=ExpectedParams(symbol="KQ.m@SHFE.rb"),
    )
    assert result["exit_code"] == 2
    assert "usage_error" in _failed_codes(result)


def test_illegal_min_decisions_exit2(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    result = verify_backtest_artifacts(
        stdout_path=bundle["stdout"],
        stderr_path=bundle["stderr"],
        journal_path=bundle["journal"],
        report_path=bundle["report"],
        exit_code=0,
        min_decisions=-1,
    )
    assert result["exit_code"] == 2


def test_continuous_symbol_rejected(tmp_path: Path):
    records = _default_records()
    for item in records:
        item["symbol"] = "KQ.m@SHFE.rb"
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 1
    assert "symbol" in _failed_codes(result)


def test_empty_stat_fails(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8", stat_text="{}")
    result = _verify(bundle)
    assert result["exit_code"] == 2
    assert any(
        item["code"] in {"stat_ast_error", "stat_schema"} and not item["ok"]
        for item in result["checks"]
    )


def test_missing_input_exit2(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    missing = tmp_path / "nope.log"
    result = verify_backtest_artifacts(
        stdout_path=missing,
        stderr_path=bundle["stderr"],
        journal_path=bundle["journal"],
        report_path=bundle["report"],
        exit_code=0,
    )
    assert result["exit_code"] == 2
    assert "input_missing" in _failed_codes(result)


def test_malformed_report_exit2(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    bundle["report"].write_text("{not-json", encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 2
    assert "json_parse_error" in _failed_codes(result)


def test_api_double_call_deterministic(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    first = _verify(bundle)
    second = _verify(bundle)
    assert json.dumps(first, ensure_ascii=True, sort_keys=True, separators=(",", ":")) == json.dumps(
        second, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    # input hash/mtime unchanged
    before = bundle["journal"].stat()
    _verify(bundle)
    after = bundle["journal"].stat()
    assert before.st_mtime_ns == after.st_mtime_ns
    assert before.st_size == after.st_size


def test_main_usage_returns_json(tmp_path: Path, capsys):
    code = verifier_main([])
    captured = capsys.readouterr()
    assert code == 2
    payload = json.loads(captured.out)
    assert payload["exit_code"] == 2
    assert payload["verdict"] == "error"


# --- R13-R19 focused regressions ---


@pytest.mark.parametrize(
    "mutate,code_prefix",
    [
        (lambda r: r.__setitem__("schema_version", True), "report_schema"),
        (lambda r: r.__setitem__("schema_version", 1.0), "report_schema"),
        (lambda r: r["manifest"].__setitem__("schema_version", True), "manifest_schema"),
        (lambda r: r["run_summary"].__setitem__("final_position", False), "report_schema"),
        (lambda r: r["integrity"].__setitem__("manifest_count", True), "report_schema"),
        (
            lambda r: r["account_snapshots"]["start"].__setitem__("position", False),
            "report_schema",
        ),
    ],
)
def test_r13_bool_float_type_bypasses_exit2(tmp_path: Path, mutate, code_prefix: str):
    bundle = _build_bundle(tmp_path, mutate_report=mutate, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 2
    assert result["verdict"] == "error"
    failed = _failed_codes(result)
    assert any(code_prefix in code or "schema" in code for code in failed)


@pytest.mark.parametrize(
    "family,mutator",
    [
        ("manifest", lambda r: r["manifest"].__setitem__("mode", "sim")),
        ("identity", lambda r: r["identity"].__setitem__("symbol", "DCE.m2609")),
        ("integrity", lambda r: r["integrity"].__setitem__("selected_records", 0)),
        ("lifecycle", lambda r: r["lifecycle"].__setitem__("state", "incomplete")),
        ("events", lambda r: r["events"]["counts"].__setitem__("decision", 0)),
        ("run_summary", lambda r: r["run_summary"].__setitem__("decisions", 0)),
        (
            "start",
            lambda r: r["account_snapshots"]["start"].__setitem__("balance", 1.0),
        ),
        (
            "last",
            lambda r: r["account_snapshots"]["last_recorded"].__setitem__("balance", 1.0),
        ),
    ],
)
def test_r19_report_family_wrong_value_invalid1(tmp_path: Path, family: str, mutator):
    bundle = _build_bundle(tmp_path, mutate_report=mutator, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 1, family
    assert result["verdict"] == "invalid"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda r: r["integrity"].__setitem__("manifest_count", 1.0),
        lambda r: r["run_summary"].__setitem__("updates", True),
        lambda r: r["lifecycle"].__setitem__("run_complete", 1),
        lambda r: r["events"]["counts"].__setitem__("decision", True),
        lambda r: r["account_snapshots"]["last_recorded"].__setitem__("position", 0.0),
    ],
)
def test_r19_report_family_wrong_type_schema2(tmp_path: Path, mutator):
    bundle = _build_bundle(tmp_path, mutate_report=mutator, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 2
    assert result["verdict"] == "error"


def test_r14_no_signal_none_targets_pass(tmp_path: Path):
    records = _default_records()
    records.insert(
        3,
        _decision(
            "bar-ns",
            action="no_signal",
            signal_target=None,
            risk_target=None,
            requested_target=1,
            current_position=0,
            position=0,
            signal_reason="no_signal",
            risk_reason="no_signal",
        ),
    )
    records[-1]["decisions"] = 3
    records[-1]["updates"] = 11
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 0


def test_r14_missing_decision_field_schema2(tmp_path: Path):
    records = _default_records()
    del records[2]["risk_target"]
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 2
    assert "decision_schema" in _failed_codes(result)


def test_r14_target_already_requested_mismatch_invalid1(tmp_path: Path):
    records = _default_records()
    records.append(
        _decision(
            "bar-tar",
            action="target_already_requested",
            risk_target=1,
            requested_target=0,
            current_position=1,
            position=1,
            signal_target=1,
        )
    )
    records[-2]["decisions"] = 3
    records[-2]["updates"] = 11
    # move finish after extra decision
    finish = records.pop(-2)
    finish["decisions"] = 3
    finish["updates"] = 11
    records.append(finish)
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    assert result["exit_code"] == 1
    assert "target_already_requested" in _failed_codes(result)


def test_r14_already_at_target_shapes(tmp_path: Path):
    good = _default_records()
    good.insert(
        2,
        _decision(
            "bar-aat",
            action="already_at_target",
            risk_target=0,
            requested_target=0,
            current_position=0,
            position=0,
            signal_target=0,
        ),
    )
    good[-1]["decisions"] = 3
    good[-1]["updates"] = 11
    bundle = _build_bundle(tmp_path, records=good, stdout_encoding="utf-8")
    assert _verify(bundle)["exit_code"] == 0

    bad = _default_records()
    bad.insert(
        3,
        _decision(
            "bar-aat-bad",
            action="already_at_target",
            risk_target=1,
            requested_target=1,
            current_position=1,
            position=1,
            signal_target=1,
        ),
    )
    bad[-1]["decisions"] = 3
    bad[-1]["updates"] = 11
    bundle_bad = _build_bundle(tmp_path / "bad", records=bad, stdout_encoding="utf-8")
    result = _verify(bundle_bad)
    assert result["exit_code"] == 1
    assert "decision_action_transition" in _failed_codes(result)


@pytest.mark.parametrize(
    "action_kwargs,ok",
    [
        (
            {
                "action": "blocked",
                "block_reason": "position_not_ready",
                "current_position": None,
                "position": None,
                "risk_target": 1,
                "requested_target": 1,
                "signal_target": 1,
                "last_price": 3000.0,
            },
            True,
        ),
        (
            {
                "action": "blocked",
                "block_reason": "position_not_ready",
                "current_position": 0,
                "position": 0,
                "risk_target": 1,
                "requested_target": 1,
                "signal_target": 1,
            },
            False,
        ),
        (
            {
                "action": "blocked",
                "block_reason": "quote_not_ready",
                "current_position": 0,
                "position": 0,
                "last_price": None,
                "risk_target": 1,
                "requested_target": 1,
                "signal_target": 1,
            },
            True,
        ),
        (
            {
                "action": "blocked",
                "block_reason": "quote_not_ready",
                "current_position": 0,
                "position": 0,
                "last_price": 3000.0,
                "risk_target": 1,
                "requested_target": 1,
                "signal_target": 1,
            },
            False,
        ),
        (
            {
                "action": "blocked",
                "block_reason": "target_task_finished",
                "current_position": 0,
                "position": 0,
                "risk_target": 1,
                "requested_target": 1,
                "signal_target": 1,
            },
            False,
        ),
        (
            {
                "action": "blocked",
                "block_reason": "target_task_finished",
                "current_position": 0,
                "position": 0,
                "risk_target": None,
                "requested_target": 1,
                "signal_target": 1,
            },
            False,
        ),
    ],
)
def test_r14_blocked_reason_shapes(tmp_path: Path, action_kwargs, ok: bool):
    records = _default_records()
    records.insert(3, _decision("bar-blk", **action_kwargs))
    records[-1]["decisions"] = 3
    records[-1]["updates"] = 11
    bundle = _build_bundle(tmp_path, records=records, stdout_encoding="utf-8")
    result = _verify(bundle)
    if ok:
        assert result["exit_code"] == 0
    else:
        assert result["exit_code"] in {1, 2}


def test_r15_created_at_missing_invalid_naive(tmp_path: Path):
    records = _default_records()
    del records[0]["created_at"]
    bundle = _build_bundle(tmp_path / "miss", records=records, stdout_encoding="utf-8")
    assert _verify(bundle)["exit_code"] == 2

    records = _default_records()
    records[0]["created_at"] = "not-a-date"
    bundle = _build_bundle(tmp_path / "bad", records=records, stdout_encoding="utf-8")
    assert _verify(bundle)["exit_code"] == 2

    records = _default_records()
    records[0]["created_at"] = "2026-07-23T13:48:00.775323"
    bundle = _build_bundle(tmp_path / "naive", records=records, stdout_encoding="utf-8")
    assert _verify(bundle)["exit_code"] == 2


def test_r15_manifest_nested_missing_extra_bool(tmp_path: Path):
    records = _default_records()
    records[0]["strategy"] = dict(records[0]["strategy"])
    del records[0]["strategy"]["fast_window"]
    bundle = _build_bundle(tmp_path / "miss", records=records, stdout_encoding="utf-8")
    assert _verify(bundle)["exit_code"] == 2

    records = _default_records()
    records[0]["execution"] = dict(records[0]["execution"])
    records[0]["execution"]["extra"] = 1
    bundle = _build_bundle(tmp_path / "extra", records=records, stdout_encoding="utf-8")
    assert _verify(bundle)["exit_code"] == 2

    records = _default_records()
    records[0]["schema_version"] = True
    bundle = _build_bundle(tmp_path / "bool", records=records, stdout_encoding="utf-8")
    assert _verify(bundle)["exit_code"] == 2


def test_r17_usage_canary_not_leaked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    secret = "UsageCanarySecret-99"
    monkeypatch.setenv("TQ_PASS", secret)
    monkeypatch.delenv("TQ_USER", raising=False)
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    env = os.environ.copy()
    env["TQ_PASS"] = secret
    env.pop("TQ_USER", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "verify_backtest_artifacts.py"),
            "--stdout",
            str(bundle["stdout"]),
            "--stderr",
            str(bundle["stderr"]),
            "--journal",
            str(bundle["journal"]),
            "--report",
            str(bundle["report"]),
            "--exit-code",
            secret,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert completed.returncode == 2
    assert secret not in completed.stdout
    assert secret not in completed.stderr
    assert secret_free(completed.stdout, completed.stderr)
    payload = json.loads(completed.stdout)
    assert payload["exit_code"] == 2
    assert payload["verdict"] == "error"
    assert payload["checks"][0]["detail"] == "invalid or missing arguments"


def test_r18_expected_date_order_and_whitespace(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    result = verify_backtest_artifacts(
        stdout_path=bundle["stdout"],
        stderr_path=bundle["stderr"],
        journal_path=bundle["journal"],
        report_path=bundle["report"],
        exit_code=0,
        expected=ExpectedParams(start_date="2026-07-03", end_date="2026-07-01"),
    )
    assert result["exit_code"] == 2
    assert "usage_error" in _failed_codes(result)

    result = verify_backtest_artifacts(
        stdout_path=bundle["stdout"],
        stderr_path=bundle["stderr"],
        journal_path=bundle["journal"],
        report_path=bundle["report"],
        exit_code=0,
        expected=ExpectedParams(symbol=" SHFE.rb2610"),
    )
    assert result["exit_code"] == 2

    result = verify_backtest_artifacts(
        stdout_path=bundle["stdout"],
        stderr_path=bundle["stderr"],
        journal_path=bundle["journal"],
        report_path=bundle["report"],
        exit_code=0,
        expected=ExpectedParams(run_id=" " + RUN_ID),
    )
    assert result["exit_code"] == 2


def test_r18_min_decisions_positive_only(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    for bad in (0, -1, True, 10**9):
        result = verify_backtest_artifacts(
            stdout_path=bundle["stdout"],
            stderr_path=bundle["stderr"],
            journal_path=bundle["journal"],
            report_path=bundle["report"],
            exit_code=0,
            min_decisions=bad,  # type: ignore[arg-type]
        )
        assert result["exit_code"] == 2, bad


def test_r19_tolerance_boundaries(tmp_path: Path):
    def near_zero_pass(report):
        report["account_snapshots"]["last_recorded"]["margin"] = 5e-10

    def near_zero_fail(report):
        report["account_snapshots"]["last_recorded"]["margin"] = 2e-9

    def large_pass(report):
        base = report["account_snapshots"]["last_recorded"]["balance"]
        report["account_snapshots"]["last_recorded"]["balance"] = base + 5e-4
        report["account_snapshots"]["last_recorded"]["available"] = (
            report["account_snapshots"]["last_recorded"]["available"] + 5e-4
        )

    def large_fail(report):
        base = report["account_snapshots"]["last_recorded"]["balance"]
        report["account_snapshots"]["last_recorded"]["balance"] = base + 2e-3
        report["account_snapshots"]["last_recorded"]["available"] = (
            report["account_snapshots"]["last_recorded"]["available"] + 2e-3
        )

    assert (
        _verify(
            _build_bundle(tmp_path / "z1", mutate_report=near_zero_pass, stdout_encoding="utf-8")
        )["exit_code"]
        == 0
    )
    assert (
        _verify(
            _build_bundle(tmp_path / "z2", mutate_report=near_zero_fail, stdout_encoding="utf-8")
        )["exit_code"]
        == 1
    )
    assert (
        _verify(
            _build_bundle(tmp_path / "l1", mutate_report=large_pass, stdout_encoding="utf-8")
        )["exit_code"]
        == 0
    )
    assert (
        _verify(
            _build_bundle(tmp_path / "l2", mutate_report=large_fail, stdout_encoding="utf-8")
        )["exit_code"]
        == 1
    )


def test_r19_stat_schema_and_value_classes(tmp_path: Path):
    # missing required -> exit2
    missing = (
        "{'start_date': '2026-07-01', 'end_date': '2026-07-03', "
        "'init_balance': 1000000.0, 'balance': 999486.74, 'start_balance': 1000000.0}"
    )
    result = _verify(
        _build_bundle(tmp_path / "m", stdout_encoding="utf-8", stat_text=missing)
    )
    assert result["exit_code"] == 2

    # unknown key -> exit2
    unknown = _stat_text()[:-1] + ", 'unknown_key': 1}"
    result = _verify(
        _build_bundle(tmp_path / "u", stdout_encoding="utf-8", stat_text=unknown)
    )
    assert result["exit_code"] == 2

    # wrong type bool on int field -> exit2
    wrong = _stat_text().replace("'trading_days': 3", "'trading_days': True")
    result = _verify(
        _build_bundle(tmp_path / "t", stdout_encoding="utf-8", stat_text=wrong)
    )
    assert result["exit_code"] == 2

    # date mismatch -> exit1
    result = _verify(
        _build_bundle(
            tmp_path / "d",
            stdout_encoding="utf-8",
            stat_text=_stat_text(start_date="2020-01-01", end_date="2020-01-02"),
        )
    )
    assert result["exit_code"] == 1

    # empty -> exit2
    result = _verify(
        _build_bundle(tmp_path / "e", stdout_encoding="utf-8", stat_text="{}")
    )
    assert result["exit_code"] == 2


def test_r19_cli_raw_ascii_and_canary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    secret = "RawCliCanary-77"
    monkeypatch.setenv("TQ_PASS", secret)
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    env = os.environ.copy()
    env["TQ_PASS"] = secret
    env.pop("TQ_USER", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "verify_backtest_artifacts.py"),
            "--stdout",
            str(bundle["stdout"]),
            "--stderr",
            str(bundle["stderr"]),
            "--journal",
            str(bundle["journal"]),
            "--report",
            str(bundle["report"]),
            "--exit-code",
            "0",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert completed.returncode == 0
    assert secret not in completed.stdout
    assert secret not in completed.stderr
    assert all(ord(ch) < 128 for ch in completed.stdout)
    assert all(ord(ch) < 128 for ch in completed.stderr)


@pytest.mark.parametrize(
    "name,action,overrides,expected_exit",
    [
        ("unchanged", "unchanged", {}, 2),
        (
            "no-signal-without-position",
            "no_signal",
            {
                "signal_target": None,
                "risk_target": None,
                "requested_target": None,
                "current_position": None,
                "position": None,
                "last_price": None,
            },
            1,
        ),
        (
            "quote-block-without-position",
            "blocked",
            {
                "block_reason": "quote_not_ready",
                "current_position": None,
                "position": None,
                "last_price": None,
            },
            1,
        ),
        (
            "task-block-without-price",
            "blocked",
            {"block_reason": "target_task_finished", "last_price": None},
            1,
        ),
        (
            "already-at-target-without-price",
            "already_at_target",
            {"last_price": None},
            1,
        ),
        (
            "target-already-requested-without-price",
            "target_already_requested",
            {"last_price": None},
            1,
        ),
        (
            "set-target-without-price",
            "set_target",
            {
                "signal_target": 1,
                "risk_target": 1,
                "requested_target": 1,
                "last_price": None,
            },
            1,
        ),
    ],
)
def test_runner_impossible_decisions_are_rejected(
    tmp_path: Path,
    name: str,
    action: str,
    overrides: dict,
    expected_exit: int,
):
    result = _verify(
        _bundle_with_extra_decision(tmp_path / name, action, **overrides)
    )
    assert result["exit_code"] == expected_exit
    assert result["verdict"] == ("error" if expected_exit == 2 else "invalid")


@pytest.mark.parametrize("last_price", [0.0, -1.5])
def test_finite_zero_and_negative_prices_follow_runner_semantics(
    tmp_path: Path, last_price: float
):
    records = _default_records()
    records[2]["last_price"] = last_price
    valid = _build_bundle(
        tmp_path / "valid" / str(last_price),
        records=records,
        stdout_encoding="utf-8",
    )
    assert _verify(valid)["exit_code"] == 0

    invalid = _bundle_with_extra_decision(
        tmp_path / "quote-block" / str(last_price),
        "blocked",
        block_reason="quote_not_ready",
        current_position=0,
        position=0,
        last_price=last_price,
    )
    result = _verify(invalid)
    assert result["exit_code"] == 1
    assert "blocked_quote_not_ready" in _failed_codes(result)


def _drop_stdout_token(bundle, token: str) -> None:
    lines = bundle["stdout"].read_text(encoding="utf-8").splitlines()
    bundle["stdout"].write_text(
        "\n".join(line for line in lines if not line.startswith(token + " ")) + "\n",
        encoding="utf-8",
    )


def _drop_run_finished(bundle) -> None:
    bundle["journal"].write_text(
        _jsonl(_default_records()[:-1]),
        encoding="utf-8",
    )


def test_incomplete_lifecycle_with_valid_intrinsic_schemas_is_invalid1(tmp_path: Path):
    bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")
    _drop_run_finished(bundle)
    result = _verify(bundle)
    assert result["exit_code"] == 1
    assert result["verdict"] == "invalid"
    assert "run_finished_count" in _failed_codes(result)


@pytest.mark.parametrize("kind", ["report", "manifest", "done", "stat"])
def test_incomplete_lifecycle_does_not_mask_schema_errors(tmp_path: Path, kind: str):
    if kind == "report":
        bundle = _build_bundle(
            tmp_path,
            stdout_encoding="utf-8",
            mutate_report=lambda report: report["run_summary"].__setitem__(
                "updates", True
            ),
        )
    elif kind == "done":
        bundle = _build_bundle(
            tmp_path,
            stdout_encoding="utf-8",
            done_text=(
                "RunSummary(reason='backtest_finished', updates=True, decisions=2, "
                "target_changes=2, final_position=0)"
            ),
        )
    elif kind == "stat":
        bundle = _build_bundle(tmp_path, stdout_encoding="utf-8", stat_text="{}")
    else:
        bundle = _build_bundle(tmp_path, stdout_encoding="utf-8")

    _drop_run_finished(bundle)
    if kind == "manifest":
        records = _default_records()[:-1]
        records[0] = dict(records[0])
        records[0]["schema_version"] = True
        bundle["journal"].write_text(_jsonl(records), encoding="utf-8")

    result = _verify(bundle)
    assert result["exit_code"] == 2
    assert result["verdict"] == "error"


def test_available_console_schema_is_checked_when_other_token_is_missing(tmp_path: Path):
    bad_stat = _build_bundle(
        tmp_path / "bad-stat", stdout_encoding="utf-8", stat_text="{}"
    )
    _drop_stdout_token(bad_stat, "BACKTEST_DONE")
    assert _verify(bad_stat)["exit_code"] == 2

    bad_done = _build_bundle(
        tmp_path / "bad-done",
        stdout_encoding="utf-8",
        done_text=(
            "RunSummary(reason='backtest_finished', updates=True, decisions=2, "
            "target_changes=2, final_position=0)"
        ),
    )
    _drop_stdout_token(bad_done, "BACKTEST_STAT")
    assert _verify(bad_done)["exit_code"] == 2

    valid_stat = _build_bundle(tmp_path / "valid-stat", stdout_encoding="utf-8")
    _drop_stdout_token(valid_stat, "BACKTEST_DONE")
    result = _verify(valid_stat)
    assert result["exit_code"] == 1
    assert "token_missing" in _failed_codes(result)


def test_duplicate_events_validate_each_schema_before_count_relationship(tmp_path: Path):
    valid_records = _default_records()
    valid_records.insert(2, dict(valid_records[1]))
    valid = _build_bundle(
        tmp_path / "valid-duplicate",
        records=valid_records,
        stdout_encoding="utf-8",
    )
    assert _verify(valid)["exit_code"] == 1

    invalid_records = _default_records()
    invalid_start = dict(invalid_records[1])
    del invalid_start["balance"]
    invalid_records.insert(2, invalid_start)
    invalid = _build_bundle(
        tmp_path / "invalid-duplicate",
        records=invalid_records,
        stdout_encoding="utf-8",
    )
    assert _verify(invalid)["exit_code"] == 2


def test_unknown_action_is_schema2_but_shutdown_remains_invalid1(tmp_path: Path):
    unknown_action = _bundle_with_extra_decision(
        tmp_path / "unknown-action", "fly"
    )
    assert _verify(unknown_action)["exit_code"] == 2

    records = _default_records()
    records.insert(
        -1,
        {
            "event": "shutdown",
            "mode": "backtest",
            "symbol": SYMBOL,
            "duration_seconds": 300,
            "run_id": RUN_ID,
        },
    )
    shutdown = _build_bundle(
        tmp_path / "shutdown", records=records, stdout_encoding="utf-8"
    )
    assert _verify(shutdown)["exit_code"] == 1


@pytest.mark.parametrize(
    "name,action,overrides",
    [
        (
            "position-not-ready",
            "blocked",
            {
                "block_reason": "position_not_ready",
                "position": None,
                "current_position": None,
                "requested_target": 0,
            },
        ),
        (
            "quote-not-ready",
            "blocked",
            {
                "block_reason": "quote_not_ready",
                "last_price": None,
                "requested_target": 0,
            },
        ),
        (
            "no-signal",
            "no_signal",
            {
                "signal_target": None,
                "risk_target": None,
                "risk_reason": "no_signal",
                "requested_target": 0,
            },
        ),
        (
            "target-task-finished",
            "blocked",
            {
                "block_reason": "target_task_finished",
                "risk_target": 0,
                "requested_target": 0,
            },
        ),
        (
            "target-already-requested",
            "target_already_requested",
            {"risk_target": 0, "requested_target": 0},
        ),
        (
            "already-at-target",
            "already_at_target",
            {
                "signal_target": 1,
                "risk_target": 1,
                "requested_target": 1,
                "position": 1,
                "current_position": 1,
            },
        ),
        (
            "set-target",
            "set_target",
            {
                "signal_target": 1,
                "risk_target": 1,
                "requested_target": 1,
            },
        ),
    ],
)
def test_requested_target_state_is_replayed_across_decisions(
    tmp_path: Path, name: str, action: str, overrides: dict
):
    result = _verify(
        _bundle_with_extra_decision(tmp_path / name, action, **overrides)
    )
    assert result["exit_code"] == 1
    assert {
        "decision_action_transition",
        "decision_requested_state",
    } & _failed_codes(result)


@pytest.mark.parametrize(
    "name,followup,expected_codes",
    [
        (
            "set-target",
            _decision(
                "bar-after-finished-set",
                action="set_target",
                signal_target=1,
                signal_reason="fast_above_slow",
                risk_target=1,
                requested_target=1,
            ),
            {"decision_action_transition", "decision_requested_state"},
        ),
        (
            "target-already-requested",
            _decision(
                "bar-after-finished-same",
                action="target_already_requested",
                risk_target=0,
                requested_target=0,
            ),
            {"decision_action_transition"},
        ),
    ],
)
def test_observed_target_task_finished_rejects_later_ready_actions(
    tmp_path: Path, name: str, followup: dict, expected_codes: set[str]
):
    result = _verify(
        _bundle_after_observed_target_task_finished(tmp_path / name, followup)
    )
    assert result["exit_code"] == 1
    assert expected_codes <= _failed_codes(result)


def test_observed_target_task_finished_remains_terminal(tmp_path: Path):
    result = _verify(
        _bundle_after_observed_target_task_finished(
            tmp_path,
            _decision(
                "bar-after-finished",
                action="blocked",
                block_reason="target_task_finished",
                signal_target=1,
                signal_reason="fast_above_slow",
                risk_target=1,
                requested_target=0,
            ),
        )
    )
    assert result["exit_code"] == 0


@pytest.mark.parametrize(
    "name,followup",
    [
        (
            "position-not-ready",
            _decision(
                "bar-after-finished-position",
                action="blocked",
                block_reason="position_not_ready",
                signal_target=1,
                signal_reason="fast_above_slow",
                risk_target=1,
                requested_target=0,
                position=None,
                current_position=None,
            ),
        ),
        (
            "quote-not-ready",
            _decision(
                "bar-after-finished-quote",
                action="blocked",
                block_reason="quote_not_ready",
                signal_target=1,
                signal_reason="fast_above_slow",
                risk_target=1,
                requested_target=0,
                last_price=None,
            ),
        ),
        (
            "no-signal",
            _decision(
                "bar-after-finished-no-signal",
                action="no_signal",
                signal_target=None,
                signal_reason="no_signal",
                risk_target=None,
                risk_reason="no_signal",
                requested_target=0,
            ),
        ),
    ],
)
def test_observed_target_task_finished_preserves_runner_precedence(
    tmp_path: Path, name: str, followup: dict
):
    result = _verify(
        _bundle_after_observed_target_task_finished(tmp_path / name, followup)
    )
    assert result["exit_code"] == 0


def test_recorded_risk_target_and_reason_are_recomputed(tmp_path: Path):
    wrong_target = _default_records()
    wrong_target[2].update(
        fast_ma=2998.0,
        slow_ma=2999.0,
        signal_target=0,
        signal_reason="fast_not_above_slow",
        risk_target=1,
        risk_reason="allowed",
        requested_target=1,
        action="set_target",
    )
    result = _verify(
        _build_bundle(
            tmp_path / "target", records=wrong_target, stdout_encoding="utf-8"
        )
    )
    assert result["exit_code"] == 1
    assert "decision_risk_target" in _failed_codes(result)

    wrong_reason = _default_records()
    wrong_reason[2]["risk_reason"] = "clamped_max"
    result = _verify(
        _build_bundle(
            tmp_path / "reason", records=wrong_reason, stdout_encoding="utf-8"
        )
    )
    assert result["exit_code"] == 1
    assert "decision_risk_reason" in _failed_codes(result)


def test_intrinsic_schema_checks_are_recorded_once(tmp_path: Path):
    result = _verify(_build_bundle(tmp_path, stdout_encoding="utf-8"))
    for code in (
        "report_family_schema",
        "selected_event_schema",
        "done_parse",
        "stat_parse",
        "stat_schema",
    ):
        assert sum(check["code"] == code for check in result["checks"]) == 1
    signatures = [
        (check["code"], check["location"], check["ok"], check["detail"])
        for check in result["checks"]
    ]
    assert len(signatures) == len(set(signatures))


def test_done_and_stat_mismatches_are_not_duplicated(tmp_path: Path):
    wrong_done = _build_bundle(
        tmp_path / "done",
        stdout_encoding="utf-8",
        done_text=(
            "RunSummary(reason='wrong', updates=10, decisions=2, "
            "target_changes=2, final_position=0)"
        ),
    )
    done_result = _verify(wrong_done)
    assert (
        sum(
            check["code"] == "done_summary_mismatch"
            and check["location"] == "done.reason"
            for check in done_result["checks"]
        )
        == 1
    )

    wrong_stat = _build_bundle(
        tmp_path / "stat",
        stdout_encoding="utf-8",
        stat_text=_stat_text(balance=999_000.0),
    )
    stat_result = _verify(wrong_stat)
    for location in ("stat.balance", "stat.end_balance"):
        assert (
            sum(
                check["code"] == "native_stat_mismatch"
                and check["location"] == location
                for check in stat_result["checks"]
            )
            == 1
        )
