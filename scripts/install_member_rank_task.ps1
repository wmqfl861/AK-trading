param(
    [string]$TaskName = 'AK-trading Member Rank Collection',
    [string]$DceTaskName = 'AK-trading Member Rank DCE',
    [string]$Schedule = '18:15',
    [string]$DceSchedule = '21:30',
    [int]$Lookback = 5,
    [string]$TimeZone = 'China Standard Time',
    [switch]$Apply,
    [switch]$SkipDce
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Python = (Get-Command python -ErrorAction Stop).Source
$Runner = Join-Path $Root 'scripts\run_member_rank_daily.ps1'

foreach ($path in @($Python, $Runner)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required file does not exist: $path"
    }
}
foreach ($time in @($Schedule, $DceSchedule)) {
    try {
        [void][DateTime]::ParseExact($time, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
    } catch {
        throw "Schedule must use HH:mm, for example 18:15"
    }
}
if ($TimeZone -ne 'China Standard Time') {
    throw "The configured schedule must remain in Asia/Shanghai (Windows: China Standard Time)"
}

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable
# S4U lets the task start headless without stored credentials, whether or not
# the user is logged on interactively (refused with the default principal).
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

function New-RunnerAction([string[]]$ExchangeList) {
    # Single quoted space-separated token: repeated/array arguments bind
    # positionally (and mis-bind to other parameters) under powershell -File.
    $argLine = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Lookback {1} -Python "{2}" -Exchanges "{3}"' -f (
        $Runner, $Lookback, $Python, ($ExchangeList -join ' ')
    )
    New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argLine -WorkingDirectory $Root
}

$mainExchanges = @('CFFEX', 'CZCE', 'GFEX', 'INE', 'SHFE')
$definition = [ordered]@{
    task_name = $TaskName
    schedule = "$Schedule Asia/Shanghai (daily)"
    lookback_days = $Lookback
    exchanges = $mainExchanges
    runner = $Runner
    overlap = 'IgnoreNew'
    start_when_available = $true
    dce_task = if ($SkipDce) { 'skipped' } else { "$DceSchedule Asia/Shanghai (daily), best effort" }
    action = if ($Apply) { 'register' } else { 'dry_run' }
}
Write-Output ('MEMBER_RANK_SCHEDULER ' + ($definition | ConvertTo-Json -Compress))

if (-not $Apply) {
    Write-Output 'MEMBER_RANK_SCHEDULER_DRY_RUN Task was not registered. Re-run with -Apply to register.'
    exit 0
}

Register-ScheduledTask -TaskName $TaskName `
    -Action (New-RunnerAction $mainExchanges) `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $Schedule) `
    -Settings $settings -Principal $principal `
    -Description 'Collect official member position rank disclosures after settlement.' -Force | Out-Null
Write-Output "MEMBER_RANK_SCHEDULER_REGISTERED $TaskName at $Schedule daily."

if (-not $SkipDce) {
    Register-ScheduledTask -TaskName $DceTaskName `
        -Action (New-RunnerAction @('DCE')) `
        -Trigger (New-ScheduledTaskTrigger -Daily -At $DceSchedule) `
        -Settings $settings -Principal $principal `
        -Description 'Best-effort DCE member rank collection (Ruishu WAF sensitive).' -Force | Out-Null
    Write-Output "MEMBER_RANK_SCHEDULER_REGISTERED $DceTaskName at $DceSchedule daily."
}
