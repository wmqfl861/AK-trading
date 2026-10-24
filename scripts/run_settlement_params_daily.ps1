param(
    [string]$LogDir = '',
    [string]$Python = ''
)

# Daily runner for the settlement-parameter snapshot task (18:20 slot).
# Appends collector output to a dated log under data\settlement_params\task_logs\.

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable does not exist: $Python"
}
if ([string]::IsNullOrWhiteSpace($LogDir)) {
    $LogDir = Join-Path $Root 'data\settlement_params\task_logs'
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$log = Join-Path $LogDir ("settlement_params_{0}.log" -f $stamp)

"=== settlement-params daily run $stamp ===" | Add-Content -Path $log -Encoding UTF8
& $Python (Join-Path $Root 'scripts\collect_settlement_params.py') 2>&1 |
    Add-Content -Path $log -Encoding UTF8
$code = $LASTEXITCODE
"=== exit $code ===" | Add-Content -Path $log -Encoding UTF8

Get-ChildItem -Path $LogDir -Filter 'settlement_params_*.log' |
    Sort-Object Name -Descending |
    Select-Object -Skip 90 |
    Remove-Item -ErrorAction SilentlyContinue

exit $code
