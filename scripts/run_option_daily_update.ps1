param(
    [Parameter(Mandatory = $true)]
    [string]$Python,
    [ValidateRange(1, 8)]
    [int]$ShardCount = 6,
    [ValidateRange(1, 10000)]
    [int]$Bars = 15,
    [int]$KeepLogsDays = 14
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Root 'scripts\update_option_bars_daily.py'
if (-not (Test-Path -LiteralPath $Script -PathType Leaf)) {
    throw "Updater script not found: $Script"
}
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:AK_DATABASE_STATEMENT_TIMEOUT_MS = '120000'

# The coordinator owns the cross-launch lock, run logs and child exit checks.
# KeepLogsDays remains accepted for existing callers; evidence is not deleted.
& $Python -B -u $Script --shard-count $ShardCount --bars $Bars
exit $LASTEXITCODE
