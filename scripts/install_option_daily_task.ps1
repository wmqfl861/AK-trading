param(
    [string]$TaskName = 'AK-trading Option Daily Update',
    [string]$Schedule = '18:30',
    [string]$TimeZone = 'China Standard Time',
    [int]$ShardCount = 6,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Python = (Get-Command python -ErrorAction Stop).Source
$Runner = Join-Path $Root 'scripts\run_option_daily_update.ps1'

foreach ($path in @($Python, $Runner)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required file does not exist: $path"
    }
}
try {
    [void][DateTime]::ParseExact($Schedule, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
} catch {
    throw "Schedule must use HH:mm, for example 18:30"
}
if ($TimeZone -ne 'China Standard Time') {
    throw "The configured schedule must remain in Asia/Shanghai (Windows: China Standard Time)"
}

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

$argLine = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Python "{1}" -ShardCount {2}' -f (
    $Runner, $Python, $ShardCount
)
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argLine -WorkingDirectory $Root

$definition = [ordered]@{
    task_name = $TaskName
    schedule = "$Schedule Asia/Shanghai (daily)"
    shard_count = $ShardCount
    runner = $Runner
    overlap = 'IgnoreNew'
    start_when_available = $true
    updates = 'active option daily bars, six futures exchanges, TqSdk free tier'
    action = if ($Apply) { 'register' } else { 'dry_run' }
}
Write-Output ('OPTION_DAILY_SCHEDULER ' + ($definition | ConvertTo-Json -Compress))

if (-not $Apply) {
    Write-Output 'OPTION_DAILY_SCHEDULER_DRY_RUN Task was not registered. Re-run with -Apply to register.'
    exit 0
}

Register-ScheduledTask -TaskName $TaskName `
    -Action $action `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $Schedule) `
    -Settings $settings -Principal $principal `
    -Description 'Update daily bars for all active option contracts (six futures exchanges) via TqSdk free tier.' -Force | Out-Null
Write-Output "OPTION_DAILY_SCHEDULER_REGISTERED $TaskName at $Schedule daily."
