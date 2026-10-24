param(
    [string]$LogDir = '',
    [string]$Python = ''
)

# Daily runner for the main-continuous minute fill task. Appends JSON-lines
# output to a dated log under data\downloads\kqm_fill_logs\.

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
if ([string]::IsNullOrWhiteSpace($LogDir)) {
    $LogDir = Join-Path $Root 'data\downloads\kqm_fill_logs'
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$log = Join-Path $LogDir ("kqm_fill_{0}.log" -f $stamp)

$env:AK_DATABASE_STATEMENT_TIMEOUT_MS = '60000'
$script = Join-Path $Root 'scripts\fill_recent_main_minute.py'

"=== KQ.m minute fill $stamp ===" | Add-Content -Path $log -Encoding UTF8
& $Python $script 2>&1 | Add-Content -Path $log -Encoding UTF8
$code = $LASTEXITCODE
"=== exit $code ===" | Add-Content -Path $log -Encoding UTF8

Get-ChildItem -Path $LogDir -Filter 'kqm_fill_*.log' |
    Sort-Object Name -Descending |
    Select-Object -Skip 90 |
    Remove-Item -ErrorAction SilentlyContinue

exit $code
