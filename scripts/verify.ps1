param([Parameter(Mandatory=$true)][string]$Python)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $taskRoot
$taskPreviousBytecode = $env:PYTHONDONTWRITEBYTECODE
$taskPreviousStockPython = $env:STOCK_PYTHON
try {
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & $Python scripts/verify_engine_integration.py
    if ($LASTEXITCODE -ne 0) { throw 'Canonical engine comparison failed.' }
    & $Python scripts/verify_local.py
    if ($LASTEXITCODE -ne 0) { throw 'Local Python verification failed.' }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/verify-auth-input.ps1 -Python $Python
    if ($LASTEXITCODE -ne 0) { throw 'PowerShell credential input verification failed.' }
    & node --check web/app.js
    if ($LASTEXITCODE -ne 0) { throw 'Dashboard JavaScript syntax failed.' }
    & node --check web/live.js
    if ($LASTEXITCODE -ne 0) { throw 'Hermes dashboard JavaScript syntax failed.' }
    $env:STOCK_PYTHON = $Python
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File engine/scripts/verify.ps1
    if ($LASTEXITCODE -ne 0) { throw 'Stock engine verification failed.' }
    & git -c "safe.directory=$taskRoot" diff --check
    if ($LASTEXITCODE -ne 0) { throw 'Git whitespace check failed.' }
    Write-Output 'Finance verification passed: dashboard and canonical stock engine. Actual three-year dataset, external mobile access and Telegram delivery are unverified.'
}
finally {
    $env:PYTHONDONTWRITEBYTECODE = $taskPreviousBytecode
    $env:STOCK_PYTHON = $taskPreviousStockPython
    Pop-Location
}

