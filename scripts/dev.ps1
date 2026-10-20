# Local paper session: venv, install, offline check, API + UI.
# Usage:  pwsh -File scripts/dev.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
$python = Join-Path $root ".venv\Scripts\python.exe"
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -e ".[dev]"

Write-Host "== jev doctor ==" -ForegroundColor Cyan
& (Join-Path $root ".venv\Scripts\jev.exe") doctor

if (-not (Test-Path "frontend\node_modules")) {
    Push-Location frontend; npm install; Pop-Location
}

Write-Host "Starting API on http://127.0.0.1:8000 and UI on http://127.0.0.1:5173" -ForegroundColor Cyan
Start-Process -FilePath (Join-Path $root ".venv\Scripts\jev.exe") -ArgumentList "serve" -WorkingDirectory $root
Start-Process -FilePath "npm" -ArgumentList "run", "dev" -WorkingDirectory (Join-Path $root "frontend")
