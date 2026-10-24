param(
    [string]$LogDir = '',
    [int]$Lookback = 5,
    # Single space-separated string (e.g. 'CFFEX CZCE GFEX') — positional
    # binding of repeated/array arguments is unreliable under powershell -File.
    [string]$Exchanges = '',
    [string]$Python = ''
)

# Daily runner for the member-rank scheduled task. Appends the collector's
# JSON-lines output to a dated log under data\member_ranks\task_logs\.
# $Python is resolved by the installer so the S4U task context does not
# depend on the interactive PATH.

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$bootstrap = Join-Path $Root 'data\member_ranks\task_logs\bootstrap.log'
try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $bootstrap) | Out-Null
    $who = '{0} @ {1} (user={2}, ps={3})' -f $PID, (Get-Date -Format 'HH:mm:ss'), [Environment]::UserName, $PSVersionTable.PSVersion
    Add-Content -Path $bootstrap -Encoding UTF8 -Value "started $who"
    Add-Content -Path $bootstrap -Encoding UTF8 -Value ("args: " + (($PSBoundParameters.GetEnumerator() | ForEach-Object { '{0}={1}' -f $_.Key, $_.Value }) -join ' '))
} catch {
    Add-Content -Path $bootstrap -Encoding UTF8 -Value ('bootstrap failed: ' + $_.Exception.Message)
}
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable does not exist: $Python"
}
if ([string]::IsNullOrWhiteSpace($LogDir)) {
    $LogDir = Join-Path $Root 'data\member_ranks\task_logs'
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$log = Join-Path $LogDir ("member_rank_{0}.log" -f $stamp)

$arguments = @(
    (Join-Path $Root 'scripts\collect_member_ranks.py'),
    '--lookback', "$Lookback"
)
$exchangeList = $Exchanges -split '\s+' | Where-Object { $_ }
foreach ($exchange in $exchangeList) {
    $arguments += @('--exchange', $exchange)
}

$python = $Python
"=== member-rank daily run $stamp ===" | Add-Content -Path $log -Encoding UTF8
& $python @arguments 2>&1 | Add-Content -Path $log -Encoding UTF8
$code = $LASTEXITCODE
"=== exit $code ===" | Add-Content -Path $log -Encoding UTF8

# Keep the newest 90 task logs.
Get-ChildItem -Path $LogDir -Filter 'member_rank_*.log' |
    Sort-Object Name -Descending |
    Select-Object -Skip 90 |
    Remove-Item -ErrorAction SilentlyContinue

exit $code
