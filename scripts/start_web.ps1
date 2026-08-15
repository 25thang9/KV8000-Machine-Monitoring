$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Missing .venv. Run scripts\setup.ps1 first." }
Set-Location $Root
& $Python (Join-Path $Root "scripts\run_web.py")
