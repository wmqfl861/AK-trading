"""Offline regression coverage for EDB updater and scheduled-runner reporting."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = ("CFFEX.IF2609", "CFFEX.IF2612", "SHFE.cu2609")
FIELDS = ("open", "high", "low", "close", "volume", "open_oi", "close_oi")
CSV_RESPONSE = "datetime,open,high,low,close,volume,open_oi,close_oi\n1788739200000000000,10,12,9,11,5,7,8\n"


class ReadTimeout(TimeoutError):
    """Explicit simulated provider failure, never a real HTTP request."""


class ImportRejected(RuntimeError):
    """Explicit simulated persistence failure, never a real database call."""


@pytest.fixture
def run_updater(tmp_path, monkeypatch, capsys):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "update_recent_via_edb.py"
    script.write_bytes((ROOT / "scripts" / script.name).read_bytes())
    (tmp_path / "data" / "downloads").mkdir(parents=True)
    monkeypatch.setattr(sys, "path", sys.path[:])
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    def module(name, **attributes):
        fake = ModuleType(name)
        fake.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, fake)
        return fake

    def run(outcomes, *, duration="60", error_detail=None):
        assert len(outcomes) <= len(SYMBOLS)
        symbols = SYMBOLS[:len(outcomes)]
        by_symbol = dict(zip(symbols, outcomes))
        symbols_file = tmp_path / "symbols.txt"
        symbols_file.write_text("\n".join(symbols), encoding="utf-8")
        imported = []
        disposed = []
        engine = SimpleNamespace(dispose=lambda: disposed.append(True))

        def get(url, *, params, headers, timeout):
            outcome = by_symbol[params["symbol"]]
            if outcome == "timeout":
                raise ReadTimeout(error_detail or "offline provider timeout")
            text = CSV_RESPONSE.splitlines()[0] if outcome == "empty" else CSV_RESPONSE
            status_code = outcome if isinstance(outcome, int) else 200
            return SimpleNamespace(status_code=status_code, text=text)

        def import_bars(engine, name, *, symbol, **kwargs):
            if by_symbol[symbol] == "import_error":
                raise ImportRejected(error_detail or "offline import rejected")
            with open(name, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            imported.append((symbol, rows))
            return SimpleNamespace(rows_written=0 if by_symbol[symbol] == "zero_rows" else len(rows))

        def no_database_connection(*args, **kwargs):
            pytest.fail("the offline updater must not connect to a database")

        module("dotenv", dotenv_values=lambda path: {"TQ_USER": "offline-user", "TQ_PASS": "offline-password"})
        module("httpx", get=get, post=lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"token": "offline-token"}))
        module("psycopg", connect=no_database_connection)
        module("ak_trading", __path__=[])
        module("ak_trading.persistence", __path__=[])
        module("ak_trading.market_reference", load_reference_book=lambda path: SimpleNamespace(calendar=object()))
        module("ak_trading.persistence.config", load_database_settings=lambda **kwargs: object())
        module("ak_trading.persistence.database", create_database_engine=lambda settings: engine)
        module("ak_trading.persistence.storage", store_reference_snapshot=lambda *args, **kwargs: SimpleNamespace(content_sha256="offline"))
        module("ak_trading.persistence.market_data", import_market_bars=import_bars,
               expected_kline_columns=lambda symbol: ("datetime", "datetime_nano", *FIELDS))
        monkeypatch.setattr(sys, "argv", [str(script), "--duration", duration,
                                        "--start", "2026-09-04", "--end", "2026-09-07",
                                        "--symbols-file", str(symbols_file)])
        with pytest.raises(SystemExit) as exit_info:
            runpy.run_path(str(script), run_name="__main__")
        output = capsys.readouterr()
        events = [json.loads(line) for line in output.out.splitlines()]
        # OS locks intentionally retain their inode; temporary import CSVs must be removed.
        leftovers = sorted(p.name for p in (tmp_path / "data" / "downloads").iterdir())
        expected_lock = "edb_daily_update.lock" if duration == "86400" else "edb_minute_update.lock"
        assert leftovers == ([] if duration == "invalid" else [expected_lock])
        return SimpleNamespace(code=exit_info.value.code, events=events,
                               imported=imported, disposed=disposed, stderr=output.err)

    return run


@pytest.mark.parametrize("failure,error_type", [("timeout", "ReadTimeout"), ("import_error", "ImportRejected")])
def test_partial_failure_is_nonzero_and_later_symbols_are_imported(run_updater, failure, error_type):
    result = run_updater(["ok", failure, "ok"])

    assert result.events[-1]["SUMMARY"] == {
        "symbols": 3, "done": 2, "failed": 1, "rows": 2,
        "duration": 60, "range": "2026-09-04..2026-09-07",
    }
    assert [event["error"] for event in result.events if "error" in event] == [error_type]
    assert [symbol for symbol, rows in result.imported] == [SYMBOLS[0], SYMBOLS[2]]
    assert result.disposed == [True]
    assert result.code == 1


@pytest.mark.parametrize("outcomes,code,done,failed,rows", [
    (["ok", "ok"], 0, 2, 0, 2),
    (["timeout", "import_error"], 1, 0, 2, 0),
    # Empty provider data is incomplete; an acknowledged idempotent import stays done.
    (["empty", "zero_rows"], 1, 1, 0, 0),
])
def test_existing_success_and_total_failure_outcomes_are_preserved(run_updater, outcomes, code, done, failed, rows):
    result = run_updater(outcomes, duration="86400")

    assert result.events[-1]["SUMMARY"] == {
        "symbols": 2, "done": done, "failed": failed, "rows": rows,
        "duration": 86400, "range": "2026-09-04..2026-09-07",
    }
    assert [event["error"] for event in result.events if "error" in event] == (
        ["ReadTimeout", "ImportRejected"] if failed else []
    )
    assert result.code == code


@pytest.mark.parametrize("failure,error_type", [("timeout", "ReadTimeout"), ("import_error", "ImportRejected")])
def test_exception_messages_cannot_disclose_credentials(run_updater, failure, error_type):
    result = run_updater(["ok", failure, "ok"], error_detail=(
        "postgresql://offline-user:offline-password@db.invalid/market "
        "Authorization: Bearer offline-token https://provider.invalid/?token=offline-token"
    ))

    errors = [event for event in result.events if "error" in event]
    assert len(errors) == 1
    assert errors[0]["error"] == error_type
    assert errors[0]["detail"] == error_type
    serialized = json.dumps(result.events) + result.stderr
    for value in ("offline-user", "offline-password", "offline-token", "postgresql://", "https://", "Authorization"):
        assert value not in serialized
    assert result.events[-1]["SUMMARY"]["failed"] == 1
    assert result.code == 1


@pytest.mark.parametrize("http_status", [401, 429, 500])
def test_http_error_is_failed_instead_of_a_successful_empty_response(run_updater, http_status):
    result = run_updater(["ok", http_status, "ok"])

    assert result.events[-1]["SUMMARY"] == {
        "symbols": 3, "done": 2, "failed": 1, "rows": 2,
        "duration": 60, "range": "2026-09-04..2026-09-07",
    }
    assert [event["error"] for event in result.events if "error" in event] == ["RuntimeError"]
    assert [symbol for symbol, rows in result.imported] == [SYMBOLS[0], SYMBOLS[2]]
    assert result.code == 1


def test_http_200_header_only_is_an_explicit_incomplete_response(run_updater):
    result = run_updater(["empty"])

    assert result.events[-1]["SUMMARY"] == {
        "symbols": 1, "done": 0, "failed": 0, "rows": 0,
        "duration": 60, "range": "2026-09-04..2026-09-07",
    }
    assert all("error" not in event for event in result.events)
    assert any(event.get("status") == "empty" for event in result.events)
    assert result.imported == []
    assert result.code == 1


def test_invalid_duration_keeps_argument_error_exit_code(run_updater):
    result = run_updater(["ok"], duration="invalid")

    assert result.code == 2
    assert result.events == []
    assert "invalid int value" in result.stderr
    assert result.imported == []


@pytest.fixture
def run_daily_runner(tmp_path):
    from ak_trading.testing.offline_guard import allow_powershell_fixture

    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is required for the offline runner regression")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    runner = scripts / "run_edb_update_daily.ps1"
    runner.write_bytes((ROOT / "scripts" / runner.name).read_bytes())
    calls = tmp_path / "calls.jsonl"
    logs = tmp_path / "logs"

    def run(codes):
        fake_child = (
            "import json, pathlib, sys\n"
            f"codes = {codes!r}\n"
            "stage = 'kqi_daily' if pathlib.Path(__file__).name == 'backfill_kqi_index.py' "
            "else ('contract_daily' if sys.argv[sys.argv.index('--duration') + 1] == '86400' else 'contract_minute')\n"
            "with (pathlib.Path(__file__).resolve().parents[1] / 'calls.jsonl').open('a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps({'stage': stage, 'exit_code': codes[stage]}) + '\\n')\n"
            "print('offline child ' + stage, flush=True)\n"
            "if codes[stage]:\n"
            "    print('offline stage failure', file=sys.stderr, flush=True)\n"
            "sys.exit(codes[stage])\n"
        )
        for name in ("backfill_kqi_index.py", "update_recent_via_edb.py"):
            (scripts / name).write_text(fake_child, encoding="utf-8")
        with allow_powershell_fixture(runner, powershell):
            result = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(runner), "-Python", sys.executable, "-LogDir", str(logs)],
                cwd=tmp_path, capture_output=True, text=True, timeout=30,
            )
        called = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
        log_files = list(logs.glob("edb_update_*.log"))
        assert len(log_files) == 1
        log = log_files[0].read_text(encoding="utf-8-sig")
        records = [json.loads(line) for line in log.splitlines() if line.startswith("{")]
        return SimpleNamespace(code=result.returncode, called=called, log=log, records=records,
                               stdout=result.stdout, stderr=result.stderr)

    return run


@pytest.mark.parametrize("codes,expected_code", [
    ({"kqi_daily": 0, "contract_daily": 0, "contract_minute": 0}, 0),
    ({"kqi_daily": 1, "contract_daily": 0, "contract_minute": 0}, 1),
    ({"kqi_daily": 0, "contract_daily": 1, "contract_minute": 0}, 1),
    ({"kqi_daily": 0, "contract_daily": 0, "contract_minute": 1}, 1),
    ({"kqi_daily": 0, "contract_daily": 2, "contract_minute": 0}, 2),
    ({"kqi_daily": 0, "contract_daily": 2, "contract_minute": 1}, 2),
])
def test_runner_executes_every_stage_and_preserves_first_failure(run_daily_runner, codes, expected_code):
    result = run_daily_runner(codes)

    assert result.called == [
        {"stage": "kqi_daily", "exit_code": codes["kqi_daily"]},
        {"stage": "contract_daily", "exit_code": codes["contract_daily"]},
        {"stage": "contract_minute", "exit_code": codes["contract_minute"]},
    ]
    assert result.code == expected_code
    assert result.records == [
        {"stage": "kqi_daily", "status": "failed" if codes["kqi_daily"] else "complete", "exit_code": codes["kqi_daily"]},
        {"stage": "contract_daily", "status": "failed" if codes["contract_daily"] else "complete", "exit_code": codes["contract_daily"]},
        {"stage": "contract_minute", "status": "failed" if codes["contract_minute"] else "complete", "exit_code": codes["contract_minute"]},
        {"status": "failed" if expected_code else "complete", "exit_code": expected_code},
    ]
