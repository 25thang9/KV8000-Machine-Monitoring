$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$CollectorScript = Join-Path $Root "scripts\start_collector.ps1"
$WebScript = Join-Path $Root "scripts\start_web.ps1"

Start-Process powershell.exe -WorkingDirectory $Root -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", ('"' + $CollectorScript + '"')
)
Start-Sleep -Seconds 1
Start-Process powershell.exe -WorkingDirectory $Root -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", ('"' + $WebScript + '"')
)

Write-Host "Collector và Web đã được mở ở hai cửa sổ riêng."
