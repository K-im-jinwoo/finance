$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot

try {
    & npm.cmd test
    if ($LASTEXITCODE -ne 0) {
        throw "Node tests failed with exit code $LASTEXITCODE."
    }

    & npm.cmd run audit:workflows
    if ($LASTEXITCODE -ne 0) {
        throw "Workflow audit failed with exit code $LASTEXITCODE."
    }

    Write-Output 'Verification passed.'
}
finally {
    Pop-Location
}
