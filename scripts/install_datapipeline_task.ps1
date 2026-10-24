<#
.SYNOPSIS
    DP06 scheduled-task installer.  DEFAULT IS DRY-RUN: it only prints the
    task definition it WOULD create and never touches the Windows task
    registry (H06.6).

.DESCRIPTION
    Registration requires BOTH:
      -Execute                        (explicit opt-in switch), and
      -AuthorizationReference <id>    (the data-operations authorization the
                                      registrant records into the result).
    Without them the script only emits the definition.  The created task
    name, working directory, program path and log root are independent of
    every existing task (distinct "AK-Datapipeline-" prefix; it never
    replaces the 17:30 ranking/observer tasks), and the runtime enforces a
    single instance via the daily runner's PID lock.

    The script never unregisters, stops, starts or updates an existing
    task, and never calls schtasks directly.  All state-changing operations
    route through the Register-DpTask seam below (shadowable for tests).

    Exit codes: 0 dry-run emitted / registered; 10 -Execute without
    -AuthorizationReference; 11 registration error.

.NOTES
    Offline and side-effect free in dry-run (the default).  NOT_RUN: no
    real registration is performed anywhere in DP06 r1 (see the node
    NOT_RUN register).
#>
[CmdletBinding()]
param(
    [string]$TaskName = "AK-Datapipeline-DailyReport",
    [string]$DailyTime = "03:30",
    [string]$WorkingDirectory = "",
    [string]$DailyRunnerPath = "",
    [string]$RunnerParameters = "",
    [string]$LogRoot = "",
    [string]$ResultJsonFile = "",
    [switch]$Execute,
    [string]$AuthorizationReference = ""
)

$ErrorActionPreference = 'Stop'

if (-not $WorkingDirectory) { $WorkingDirectory = Split-Path -Parent $PSScriptRoot }
if (-not $DailyRunnerPath) { $DailyRunnerPath = Join-Path $PSScriptRoot 'run_datapipeline_daily.ps1' }
if (-not $LogRoot) { $LogRoot = Join-Path $WorkingDirectory 'data\datapipeline\task-logs' }

# --- task API seam: the ONLY functions that touch the real task registry --
function Register-DpTask {
    param([string]$Name, [object]$Action, [object]$Trigger, [object]$Settings)
    Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Settings $Settings
}
function Remove-DpTask { param([string]$Name) Unregister-ScheduledTask -TaskName $Name -Confirm:$false }
function Start-DpTask { param([string]$Name) Start-ScheduledTask -TaskName $Name }
function Stop-DpTask { param([string]$Name) Stop-ScheduledTask -TaskName $Name }
function Update-DpTask { param([string]$Name, [object]$Settings) Set-ScheduledTask -TaskName $Name -Settings $Settings }
# This installer never calls Remove/Start/Stop/Update; they exist so any
# future authorized operation has one auditable seam.

$program = "powershell.exe"
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$DailyRunnerPath`""
if ($RunnerParameters) { $arguments += " $RunnerParameters" }

$definition = [ordered]@{
    schema           = "dp06-task-install/1"
    task_name        = $TaskName
    dry_run          = $true
    replaces_existing_task = $false
    action           = [ordered]@{
        program     = $program
        arguments   = $arguments
        working_directory = $WorkingDirectory
    }
    trigger          = [ordered]@{
        type        = "daily"
        at          = $DailyTime
    }
    settings         = [ordered]@{
        multiple_instances = "IgnoreNew"   # runtime single instance
        start_when_available = $false
        wake_to_run = $false
    }
    log_root         = $LogRoot
    authorization    = [ordered]@{
        required    = "explicit -Execute plus -AuthorizationReference"
        reference   = $(if ($AuthorizationReference) { $AuthorizationReference } else { $null })
    }
    notes            = @(
        "distinct AK-Datapipeline- prefix; never replaces the 17:30 ranking/observer tasks",
        "default mode is dry-run: zero register/delete/start/stop calls",
        "single instance enforced by run_datapipeline_daily.ps1 PID lock"
    )
}

function Write-Result([object]$payload) {
    if ($ResultJsonFile) {
        $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ResultJsonFile -Encoding UTF8
    }
}

if (-not $Execute) {
    # ---------------- DEFAULT: DRY-RUN, zero system contact ---------------
    $definition.dry_run = $true
    Write-Output "DRY-RUN: no scheduled task was created, deleted, started, stopped or modified."
    Write-Output "The following task definition WOULD be registered only with -Execute and -AuthorizationReference:"
    $definition | ConvertTo-Json -Depth 8 | Write-Output
    Write-Result ([ordered]@{ dry_run = $true; registered = $false; definition = $definition })
    exit 0
}

# ---------------- -Execute path (requires explicit authorization) ---------
if (-not $AuthorizationReference) {
    Write-Output "REFUSED: -Execute requires -AuthorizationReference recording the data-operations authorization."
    Write-Result ([ordered]@{ dry_run = $false; registered = $false; refused = "authorization_missing" })
    exit 10
}

$action = New-ScheduledTaskAction -Execute $program -Argument $arguments -WorkingDirectory $WorkingDirectory
$trigger = New-ScheduledTaskTrigger -Daily -At $DailyTime
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable:$false -WakeToRun:$false
$definition.dry_run = $false

try {
    $null = Register-DpTask -Name $TaskName -Action $action -Trigger $trigger -Settings $settings
} catch {
    Write-Output "REGISTRATION ERROR (no -Force, no replacement attempted): $($_.Exception.Message)"
    Write-Result ([ordered]@{ dry_run = $false; registered = $false; error = "registration_failed"; authorization = $AuthorizationReference })
    exit 11
}

Write-Output "REGISTERED task $TaskName (authorization $AuthorizationReference). No existing task was replaced."
Write-Result ([ordered]@{ dry_run = $false; registered = $true; task_name = $TaskName; authorization = $AuthorizationReference })
exit 0
