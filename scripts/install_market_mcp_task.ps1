param(
    [string]$TaskName = 'AK-trading Market MCP Server',
    [int]$Port = 17779,
    [string]$Host_ = '',
    [switch]$Apply
)

# Registers the read-only market data MCP server as a boot-time background
# task (restart on failure, single instance). The server itself reads
# AK_MCP_TOKEN / AK_MCP_HOST / AK_MCP_PORT from the project .env.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Python = (Get-Command python -ErrorAction Stop).Source
$Runner = Join-Path $Root 'scripts\run_market_mcp.py'

foreach ($path in @($Python, $Runner)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required file does not exist: $path"
    }
}

# Task Scheduler disallows -Host (reserved), so the variable keeps the underscore.
$bindHost = if ($Host_) { $Host_ } else { '127.0.0.1' }

# Execute python directly (no powershell parent): Stop-ScheduledTask then
# kills the server itself instead of orphaning it.
$action = New-ScheduledTaskAction -Execute $Python `
    -Argument "-X utf8 `"$Runner`" --host $bindHost --port $Port" -WorkingDirectory $Root

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

$definition = [ordered]@{
    task_name = $TaskName
    bind = "${bindHost}:$Port"
    trigger = 'at logon + at boot'
    restart_on_failure = 3
    action = if ($Apply) { 'register' } else { 'dry_run' }
}
Write-Output ('MCP_SCHEDULER ' + ($definition | ConvertTo-Json -Compress))

if (-not $Apply) {
    Write-Output 'MCP_SCHEDULER_DRY_RUN Re-run with -Apply to register.'
    exit 0
}

$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn),
    (New-ScheduledTaskTrigger -AtStartup)
)
Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal `
    -Description 'Read-only market data MCP server (Streamable HTTP + bearer token) for remote agents.' -Force | Out-Null
Write-Output "MCP_SCHEDULER_REGISTERED $TaskName on ${bindHost}:$Port."
