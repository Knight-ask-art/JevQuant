# Full local verification: backend tests + frontend production build.
# Usage:  pwsh -File scripts/check.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& (Join-Path $root ".venv\Scripts\python.exe") -m pytest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Push-Location (Join-Path $root "frontend")
npm run build
$code = $LASTEXITCODE
Pop-Location
exit $code
