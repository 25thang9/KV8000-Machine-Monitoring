$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Missing .venv. Run scripts\setup.ps1 first." }

Push-Location (Join-Path $Root "dashboard")
try {
    Write-Host "[1/5] Django system check"
    & $Python manage.py check
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[2/5] Migration consistency"
    & $Python manage.py makemigrations --check --dry-run
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[3/5] Unapplied migrations"
    & $Python manage.py migrate --check
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[4/5] Monitoring configuration"
    & $Python manage.py validate_monitoring --strict
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[5/5] Read-only PLC check"
    & $Python manage.py check_plc
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}
