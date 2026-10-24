"""DP06 task installation tests (H06.6; work order test clause).

The Windows task API is MOCKED: the harness shadows Register-ScheduledTask,
Unregister-ScheduledTask, Start-ScheduledTask, Stop-ScheduledTask,
Set-ScheduledTask and the schtasks executable with logging fakes, then
invokes the real installer script in-process.  Assertions:

- default invocation (no -Execute) registers/deletes/replaces/starts/
  stops NOTHING (call count 0) and only prints the would-be definition;
- -Execute without -AuthorizationReference refuses before any registration;
- -Execute -AuthorizationReference calls register exactly once and never
  unregisters/stops/replaces anything;
- the daily runner smoke-runs offline and honours its single-instance lock.

No real scheduled task is ever created in these tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "DP06"
INPUTS = FIXTURES / "inputs"
INSTALLER = REPO_ROOT / "scripts" / "install_datapipeline_task.ps1"
DAILY = REPO_ROOT / "scripts" / "run_datapipeline_daily.ps1"
AS_OF_ISO = "2026-09-20T18:35:00+00:00"


def _ps_exe() -> str:
    pwsh = shutil.which("pwsh")
    return pwsh or shutil.which("powershell") or "powershell.exe"


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def run_installer(tmp_path: Path, *extra_args: str) -> subprocess.CompletedProcess:
    """Run the installer with the task API shadowed by logging fakes.

    The ScheduledTasks module is imported BEFORE the shadow functions are
    defined: a module auto-loaded AFTER a function definition would take
    name-precedence over it (verified on this host), silently re-enabling
    the real cmdlet.  The shadows are GLOBAL functions so they take
    precedence over the already-loaded module commands inside the
    installer's own functions; the installer is invoked with ``&`` and the
    harness re-exits with the installer's ``$LASTEXITCODE`` so refusal and
    success codes propagate exactly.  A pre-flight guard aborts before
    invoking the installer if the shadow did not install -- a real
    registration can never happen from this harness.  The fakes append one
    line per call to ``task-api-calls.log`` (absent/empty = zero system
    contact).
    """
    harness = tmp_path / "harness.ps1"
    call_log = tmp_path / "task-api-calls.log"
    result_json = tmp_path / "result.json"
    shadow = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            "Import-Module ScheduledTasks",
            f"$log = {_ps_quote(str(call_log))}",
            "function Global:Register-ScheduledTask { param($TaskName,$Action,$Trigger,$Settings,$Confirm) Add-Content -Path $log -Value 'register' ; return $true }",
            "function Global:Unregister-ScheduledTask { param($TaskName,$Confirm) Add-Content -Path $log -Value 'unregister' }",
            "function Global:Start-ScheduledTask { param($TaskName) Add-Content -Path $log -Value 'start' }",
            "function Global:Stop-ScheduledTask { param($TaskName) Add-Content -Path $log -Value 'stop' }",
            "function Global:Set-ScheduledTask { param($TaskName,$Settings) Add-Content -Path $log -Value 'set' }",
            "function Global:schtasks { param($x) Add-Content -Path $log -Value 'schtasks' }",
            "$shadowCmd = Get-Command Register-ScheduledTask",
            "if ($shadowCmd.ModuleName -or $shadowCmd.CommandType -ne 'Function') { throw 'task API shadow missing; refusing to invoke installer' }",
            f"& {_ps_quote(str(INSTALLER))} -ResultJsonFile {_ps_quote(str(result_json))} " + " ".join(extra_args),
            "exit $LASTEXITCODE",
        ]
    )
    harness.write_text(shadow, encoding="utf-8-sig")
    proc = subprocess.run(
        [_ps_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    proc._call_log = call_log      # type: ignore[attr-defined]
    proc._result_json = result_json  # type: ignore[attr-defined]
    return proc


def _calls(proc: subprocess.CompletedProcess) -> list:
    log = proc._call_log  # type: ignore[attr-defined]
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _result(proc: subprocess.CompletedProcess) -> dict:
    return json.loads(proc._result_json.read_text(encoding="utf-8-sig"))  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# H06.6: default is dry-run; register/delete/replace/start/stop count == 0
# ---------------------------------------------------------------------------


def test_default_dry_run_calls_no_task_api(tmp_path):
    proc = run_installer(
        tmp_path,
        "-TaskName", "AK-Datapipeline-DailyReport",
        "-DailyTime", "03:30",
        "-WorkingDirectory", str(tmp_path / "wd"),
        "-LogRoot", str(tmp_path / "logs"),
    )
    assert proc.returncode == 0, proc.stderr
    assert _calls(proc) == []                      # zero registration/deletion/replacement calls
    result = _result(proc)
    assert result["dry_run"] is True and result["registered"] is False
    definition = result["definition"]
    assert definition["task_name"] == "AK-Datapipeline-DailyReport"
    assert definition["trigger"]["type"] == "daily" and definition["trigger"]["at"] == "03:30"
    assert definition["action"]["program"] == "powershell.exe"
    assert definition["settings"]["multiple_instances"] == "IgnoreNew"
    assert definition["replaces_existing_task"] is False
    assert "DRY-RUN" in proc.stdout


def test_dry_run_definition_is_independent_of_existing_tasks(tmp_path):
    proc = run_installer(tmp_path)
    definition = _result(proc)["definition"]
    # Distinct name prefix; never the 17:30 ranking / observer tasks.
    assert definition["task_name"].startswith("AK-Datapipeline-")
    notes = " ".join(definition["notes"])
    assert "17:30" in notes
    assert definition["log_root"]
    assert definition["action"]["working_directory"]


def test_execute_without_authorization_refused_before_registration(tmp_path):
    proc = run_installer(tmp_path, "-Execute")
    assert proc.returncode == 10
    assert _calls(proc) == []
    result = _result(proc)
    assert result["refused"] == "authorization_missing" and result["registered"] is False


def test_execute_with_authorization_registers_once_and_replaces_nothing(tmp_path):
    proc = run_installer(
        tmp_path,
        "-Execute",
        "-AuthorizationReference", "OPS-AUTH-20260923-01",
        "-TaskName", "AK-Datapipeline-DailyReport-TEST",
    )
    assert proc.returncode == 0, proc.stderr
    calls = _calls(proc)
    assert calls == ["register"]                   # exactly one registration
    result = _result(proc)
    assert result["registered"] is True
    assert result["authorization"] == "OPS-AUTH-20260923-01"
    assert result["task_name"] == "AK-Datapipeline-DailyReport-TEST"


# ---------------------------------------------------------------------------
# Daily runner: offline smoke + single-instance lock
# ---------------------------------------------------------------------------


def _run_daily(tmp_path: Path, output: Path) -> subprocess.CompletedProcess:
    args = [
        _ps_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(DAILY),
        "-OutputRoot", str(output),
        "-EventsRoot", str(INPUTS / "events"),
        "-QualityRoot", str(INPUTS / "quality"),
        "-TasksFile", str(INPUTS / "synthetic_tasks.json"),
        "-CalendarFile", str(INPUTS / "synthetic_calendar_answers.json"),
        "-RunsFile", str(INPUTS / "synthetic_run_registry.json"),
        "-HeartbeatsRoot", str(INPUTS / "heartbeats"),
        "-AsOf", AS_OF_ISO,
    ]
    return subprocess.run(args, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300)


def test_daily_runner_smoke_reports_and_exits_with_gaps(tmp_path):
    output = tmp_path / "daily-out"
    proc = _run_daily(tmp_path, output)
    assert proc.returncode == 3, proc.stderr + proc.stdout          # known gaps, report published
    reports = list((output / "reports").glob("*/*/report.json"))
    assert len(reports) == 1
    assert list((output / "logs").glob("daily-*.log"))
    assert not (output / "logs" / "run.lock").exists()              # lock released


def test_daily_runner_single_instance_lock(tmp_path):
    output = tmp_path / "daily-out"
    output.mkdir(parents=True)
    (output / "logs").mkdir(parents=True)
    # A live owner (this very pytest process) holds the lock -> skip exit 16.
    (output / "logs" / "run.lock").write_text(str(os.getpid()), encoding="utf-8")
    proc = _run_daily(tmp_path, output)
    assert proc.returncode == 16
    assert not list((output / "reports").glob("*/*/report.json"))   # nothing produced
