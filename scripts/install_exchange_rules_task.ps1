param(
    [string]$TaskName = 'AK-trading Exchange Rules Collection',
    [string]$Python = '',
    [string]$WorkingDirectory = '',
    [string]$Collector = '',
    [string]$Schedule = '20:30',
    [string]$TimeZone = 'China Standard Time',
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) {
    $WorkingDirectory = $Root
}
if ([string]::IsNullOrWhiteSpace($Collector)) {
    $Collector = Join-Path $Root 'scripts\collect_exchange_rules.py'
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable does not exist: $Python"
}
if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
    throw "Working directory does not exist: $WorkingDirectory"
}
if (-not (Test-Path -LiteralPath $Collector -PathType Leaf)) {
    throw "Collector script does not exist: $Collector"
}
try {
    $scheduleTime = [DateTime]::ParseExact($Schedule, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
} catch {
    throw "Schedule must use HH:mm, for example 20:30"
}
if ($TimeZone -ne 'China Standard Time') {
    throw "The configured schedule must remain in Asia/Shanghai (Windows: China Standard Time)"
}

$actionArgs = "`"$Collector`" --artifact-root `"$(Join-Path $WorkingDirectory 'data\exchange_rules')`""
$taskAction = New-ScheduledTaskAction -Execute $Python -Argument $actionArgs -WorkingDirectory $WorkingDirectory
$taskTrigger = New-ScheduledTaskTrigger -Daily -At $scheduleTime
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable

$definition = [ordered]@{
    task_name = $TaskName
    schedule = "$Schedule Asia/Shanghai"
    windows_timezone = $TimeZone
    python = $Python
    working_directory = $WorkingDirectory
    collector = $Collector
    arguments = $actionArgs
    overlap = 'IgnoreNew'
    action = if ($Apply) { 'register' } else { 'dry_run' }
}
Write-Output ('EXCHANGE_RULES_SCHEDULER ' + ($definition | ConvertTo-Json -Compress))

if (-not $Apply) {
    Write-Output 'EXCHANGE_RULES_SCHEDULER_DRY_RUN Task was not registered. Re-run with -Apply only after explicit authorization.'
    exit 0
}

Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $taskTrigger -Settings $settings -Description 'Collect official exchange rules before the next trading session.' -Force | Out-Null
Write-Output 'EXCHANGE_RULES_SCHEDULER_REGISTERED Task registered.'
