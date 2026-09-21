$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot

try {
    if ($env:STOCK_PYTHON) {
        $python = $env:STOCK_PYTHON
    }
    else {
        $command = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $command) {
            throw 'Python 3.12+ was not found. Set STOCK_PYTHON to an absolute python.exe path.'
        }
        $python = $command.Source
    }

    $versionOutput = & $python -c 'import sys; print(sys.version_info[:2]); raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.12+ is required. Found: $versionOutput"
    }

    $previousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = Join-Path $projectRoot 'src'

    & $python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw "Python tests failed with exit code $LASTEXITCODE."
    }

    & $python -m compileall -q src
    if ($LASTEXITCODE -ne 0) {
        throw "Python compilation failed with exit code $LASTEXITCODE."
    }

    $fixtureFiles = Get-ChildItem -LiteralPath (Join-Path $projectRoot 'tests\fixtures') -Filter '*.json'
    foreach ($fixtureFile in $fixtureFiles) {
        Get-Content -LiteralPath $fixtureFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null
    }
    Write-Output 'Fixture JSON validation passed.'

    $secretMatches = & rg -n --glob '!docs/**' --glob '!tests/**' --glob '!workflows/**' '(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{20,})' . 2>$null
    if ($LASTEXITCODE -eq 0) {
        throw "Potential secret signature found outside tests/docs/workflows: $secretMatches"
    }
    if ($LASTEXITCODE -notin @(0, 1)) {
        throw "Secret scan failed with exit code $LASTEXITCODE."
    }

    git diff --check
    if ($LASTEXITCODE -ne 0) {
        throw "git diff --check failed with exit code $LASTEXITCODE."
    }

    Write-Output "Verification passed with Python $versionOutput."
}
finally {
    $env:PYTHONPATH = $previousPythonPath
    Pop-Location
}
