<#
.SYNOPSIS
    DP06 daily runner: one report run per invocation, single instance, explicit roots.

.DESCRIPTION
    Wraps scripts/report_datapipeline.py with an independent log root and a
    PID lock file so at most one daily report run executes at a time
    (H06.6: the future scheduled task runs this script; the task itself is
    NOT registered here -- installation lives in install_datapipeline_task.ps1
    and defaults to dry-run).

    No implicit CWD defaults: OutputRoot is mandatory and every input root is
    passed through verbatim.  The python exit code is passed through
    (0 clean / 1 contract violation / 2 output failure / 3 known gaps).

    Lock: <LogRoot>\run.lock holds the owning PID.  A live owner makes this
    invocation exit 16 without touching any output; a stale lock is removed.

.NOTES
    Offline: no network, no database, no .env, no system task changes.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputRoot,
    [string]$HeartbeatsRoot = "",
    [string]$EventsRoot = "",
    [string]$QualityRoot = "",
    [string]$TasksFile = "",
    [string]$CalendarFile = "",
    [string]$RunsFile = "",
    [string]$HashesFile = "",
    [string]$ThresholdsFile = "",
    [string]$AsOf = "",
    [string]$DataOrigin = "SYNTHETIC",
    [string]$PythonExe = "python",
    [string]$MonitorId = "dp06-monitor-local",
    [string]$ReportScript = ""
)

$ErrorActionPreference = 'Stop'

if (-not $ReportScript) {
    $ReportScript = Join-Path $PSScriptRoot 'report_datapipeline.py'
}
if (-not (Test-Path -LiteralPath $ReportScript -PathType Leaf)) {
    Write-Error "report script not found: $ReportScript"
    exit 2
}

$logRoot = Join-Path $OutputRoot 'logs'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$logFile = Join-Path $logRoot ("daily-$stamp.log")
$lockFile = Join-Path $logRoot 'run.lock'

function Test-ProcessAlive([int]$ProcessId_) {
    if ($ProcessId_ -le 0) { return $false }
    try { return ($null -ne (Get-Process -Id $ProcessId_ -ErrorAction Stop)) } catch { return $false }
}

# --- single-instance lock ---------------------------------------------------
$lockHeld = $false
if (Test-Path -LiteralPath $lockFile -PathType Leaf) {
    $existing = Get-Content -LiteralPath $lockFile -Raw
    $existingPid = 0
    if ([int]::TryParse("$existing".Trim(), [ref]$existingPid)) {
        if (Test-ProcessAlive $existingPid) {
            Add-Content -LiteralPath $logFile -Value "SKIP: another daily run (pid $existingPid) holds the lock"
            exit 16
        }
    }
    Remove-Item -LiteralPath $lockFile -Force
}
"$PID" | Set-Content -LiteralPath $lockFile
$lockHeld = $true

try {
    Add-Content -LiteralPath $logFile -Value "START utc=$stamp monitor=$MonitorId output=$OutputRoot"
    $pyArgs = @('-B', $ReportScript, '--output-root', $OutputRoot, '--monitor-id', $MonitorId, '--data-origin', $DataOrigin)
    foreach ($pair in @(
        @('--heartbeats-root', $HeartbeatsRoot), @('--events-root', $EventsRoot),
        @('--quality-root', $QualityRoot), @('--tasks-file', $TasksFile),
        @('--calendar-file', $CalendarFile), @('--runs-file', $RunsFile),
        @('--hashes-file', $HashesFile), @('--thresholds-file', $ThresholdsFile),
        @('--as-of', $AsOf)
    )) {
        if ($pair[1]) { $pyArgs += @($pair[0], $pair[1]) }
    }

    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $PythonExe @pyArgs 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    Add-Content -LiteralPath $logFile -Value "PYTHON exit=$code"
    if ($output) { Add-Content -LiteralPath $logFile -Value $output }
    Add-Content -LiteralPath $logFile -Value "END utc=$((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))"
    exit $code
}
finally {
    if ($lockHeld -and (Test-Path -LiteralPath $lockFile -PathType Leaf)) {
        Remove-Item -LiteralPath $lockFile -Force
    }
}
