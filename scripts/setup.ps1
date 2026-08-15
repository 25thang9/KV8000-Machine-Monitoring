$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating .venv ..."
    py -3.13 -m venv (Join-Path $Root ".venv")
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r (Join-Path $Root "requirements.txt")

Push-Location (Join-Path $Root "dashboard")
try {
    & $VenvPython manage.py migrate
    & $VenvPython manage.py collectstatic --noinput
    & $VenvPython manage.py check
}
finally {
    Pop-Location
}

Write-Host "SETUP DONE"
