param(
    [string]$TaskName = 'AK-trading EDB Contract Bar Update',
    [string]$Schedule = '18:00',
    [string]$TimeZone = 'China Standard Time',
    [int]$LookbackDays = 3,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Python = (Get-Command python -ErrorAction Stop).Source
$Runner = Join-Path $Root 'scripts\run_edb_update_daily.ps1'

foreach ($path in @($Python, $Runner)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required file does not exist: $path"
    }
}
try {
    [void][DateTime]::ParseExact($Schedule, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
} catch {
    throw "Schedule must use HH:mm, for example 18:00"
}
if ($TimeZone -ne 'China Standard Time') {
    throw "The configured schedule must remain in Asia/Shanghai (Windows: China Standard Time)"
}

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

$argLine = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Python "{1}" -LookbackDays {2}' -f (
    $Runner, $Python, $LookbackDays
)
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argLine -WorkingDirectory $Root

$definition = [ordered]@{
    task_name = $TaskName
    schedule = "$Schedule Asia/Shanghai (daily)"
    lookback_days = $LookbackDays
    runner = $Runner
    overlap = 'IgnoreNew'
    start_when_available = $true
    updates = 'KQ.i index daily + contract daily + contract minute (EDB free tier)'
    action = if ($Apply) { 'register' } else { 'dry_run' }
}
Write-Output ('EDB_SCHEDULER ' + ($definition | ConvertTo-Json -Compress))

if (-not $Apply) {
    Write-Output 'EDB_SCHEDULER_DRY_RUN Task was not registered. Re-run with -Apply to register.'
    exit 0
}

Register-ScheduledTask -TaskName $TaskName `
    -Action $action `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $Schedule) `
    -Settings $settings -Principal $principal `
    -Description 'Update contract-level daily/minute bars and index daily via EDB free tier.' -Force | Out-Null
Write-Output "EDB_SCHEDULER_REGISTERED $TaskName at $Schedule daily."
