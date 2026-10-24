param(
    [string]$LogDir = '',
    [string]$Python = '',
    [int]$LookbackDays = 3
)

# Daily runner for EDB contract-level bar updates (daily + minute) and
# KQ.i index daily bars. Appends JSON-lines output to a dated log under
# data\downloads\edb_task_logs\.

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable does not exist: $Python"
}
if ([string]::IsNullOrWhiteSpace($LogDir)) {
    $LogDir = Join-Path $Root 'data\downloads\edb_task_logs'
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$log = Join-Path $LogDir ("edb_update_{0}.log" -f $stamp)

$endDate = Get-Date -Format 'yyyy-MM-dd'
$startDate = (Get-Date).AddDays(-$LookbackDays).ToString('yyyy-MM-dd')
$env:AK_DATABASE_STATEMENT_TIMEOUT_MS = '120000'
$env:PYTHONDONTWRITEBYTECODE = '1'

# Freshness baseline: the last fully completed session. On weekends/holidays
# the calendar date itself marks every symbol stale, so compare against the
# last session EDB can have data for. Falls back to $endDate on any failure.
$LASTEXITCODE = 1
$expectedThrough = & $Python -X utf8 (Join-Path $Root 'scripts\compute_last_session.py') 2>$null
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($expectedThrough)) {
    $expectedThrough = $endDate
}
# update_recent_via_edb.py requires start <= expected-through <= end; during a
# long holiday the lookback start can fall after the last session, so pull it back.
try {
    $expDate = [datetime]::ParseExact($expectedThrough, 'yyyy-MM-dd', $null)
    $startDateDate = [datetime]::ParseExact($startDate, 'yyyy-MM-dd', $null)
    if ($expDate -lt $startDateDate) {
        $startDate = $expectedThrough
    }
} catch {
    # Malformed dates should never happen; keep the original range if so.
}
"expected-through: $expectedThrough (range $startDate..$endDate)" | Add-Content -Path $log -Encoding UTF8

function Write-StageResult {
    param([string]$Stage, [int]$ExitCode)
    $status = if ($ExitCode -eq 0) { 'complete' } else { 'failed' }
    [ordered]@{ stage = $Stage; status = $status; exit_code = $ExitCode } |
        ConvertTo-Json -Compress | Add-Content -Path $log -Encoding UTF8
}

"=== EDB daily update $stamp (range $startDate..$endDate) ===" | Add-Content -Path $log -Encoding UTF8

# 1) KQ.i index daily (uses get_kline_serial, fast)
"--- KQ.i index daily ---" | Add-Content -Path $log -Encoding UTF8
$LASTEXITCODE = 1  # A failed process launch must not reuse a previous success.
& $Python -X utf8 (Join-Path $Root 'scripts\backfill_kqi_index.py') 2>&1 | Add-Content -Path $log -Encoding UTF8
$kqiExitCode = $LASTEXITCODE
Write-StageResult -Stage 'kqi_daily' -ExitCode $kqiExitCode

# 2) Contract daily via EDB
"--- Contract daily via EDB ---" | Add-Content -Path $log -Encoding UTF8
$LASTEXITCODE = 1
& $Python -X utf8 (Join-Path $Root 'scripts\update_recent_via_edb.py') `
    --duration 86400 --start $startDate --end $endDate --expected-through $expectedThrough 2>&1 | Add-Content -Path $log -Encoding UTF8
$dailyExitCode = $LASTEXITCODE
Write-StageResult -Stage 'contract_daily' -ExitCode $dailyExitCode

# 3) Contract minute via EDB
"--- Contract minute via EDB ---" | Add-Content -Path $log -Encoding UTF8
$LASTEXITCODE = 1
& $Python -X utf8 (Join-Path $Root 'scripts\update_recent_via_edb.py') `
    --duration 60 --start $startDate --end $endDate --expected-through $expectedThrough 2>&1 | Add-Content -Path $log -Encoding UTF8
$minuteExitCode = $LASTEXITCODE
Write-StageResult -Stage 'contract_minute' -ExitCode $minuteExitCode

# Finish every useful stage, then preserve the first nonzero child status.
$exitCode = 0
foreach ($stageCode in @($kqiExitCode, $dailyExitCode, $minuteExitCode)) {
    if ($stageCode -ne 0) {
        $exitCode = $stageCode
        break
    }
}
$status = if ($exitCode -eq 0) { 'complete' } else { 'failed' }
[ordered]@{ status = $status; exit_code = $exitCode } |
    ConvertTo-Json -Compress | Add-Content -Path $log -Encoding UTF8
"=== exit ===" | Add-Content -Path $log -Encoding UTF8

# Keep the newest 90 task logs.
Get-ChildItem -Path $LogDir -Filter 'edb_update_*.log' |
    Sort-Object Name -Descending |
    Select-Object -Skip 90 |
    Remove-Item -ErrorAction SilentlyContinue

exit $exitCode
