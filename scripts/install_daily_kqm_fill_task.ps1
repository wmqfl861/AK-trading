param(
    [string]$TaskName = 'AK-trading Main Minute Fill',
    [string]$Schedule = '17:30',
    [string]$TimeZone = 'China Standard Time',
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Python = (Get-Command python -ErrorAction Stop).Source
$Runner = Join-Path $Root 'scripts\run_kqm_fill_daily.ps1'

foreach ($path in @($Python, $Runner,
                    (Join-Path $Root 'scripts\fill_recent_main_minute.py'))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required file does not exist: $path"
    }
}
try {
    [void][DateTime]::ParseExact($Schedule, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
} catch {
    throw "Schedule must use HH:mm, for example 17:30"
}
if ($TimeZone -ne 'China Standard Time') {
    throw "The configured schedule must remain in Asia/Shanghai (Windows: China Standard Time)"
}

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}" -Python "{1}"' -f $Runner, $Python) `
    -WorkingDirectory $Root

$definition = [ordered]@{
    task_name = $TaskName
    schedule = "$Schedule Asia/Shanghai (daily)"
    runner = $Runner
    python = $Python
    overlap = 'IgnoreNew'
    start_when_available = $true
    action = if ($Apply) { 'register' } else { 'dry_run' }
}
Write-Output ('KQM_FILL_SCHEDULER ' + ($definition | ConvertTo-Json -Compress))

if (-not $Apply) {
    Write-Output 'KQM_FILL_SCHEDULER_DRY_RUN Task was not registered. Re-run with -Apply to register.'
    exit 0
}

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger (New-ScheduledTaskTrigger -Daily -At $Schedule) `
    -Settings $settings -Principal $principal `
    -Description 'Daily main-continuous minute-bar increment via free TqSdk window.' -Force | Out-Null
Write-Output "KQM_FILL_SCHEDULER_REGISTERED $TaskName at $Schedule daily."
