param(
    [int]$Days = 90,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
Push-Location (Join-Path $Root "dashboard")
try {
    if ($DryRun) {
        & $Python manage.py purge_readings --days $Days --dry-run
    } else {
        & $Python manage.py purge_readings --days $Days
    }
}
finally {
    Pop-Location
}
