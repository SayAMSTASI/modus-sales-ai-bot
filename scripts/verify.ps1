$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $projectRoot '.venv'

function Assert-LastExitCode([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $venv)) {
    python -m venv $venv
}
$python = Join-Path $venv 'Scripts\python.exe'
& $python -m pip install --disable-pip-version-check -e "$projectRoot[test]"
Assert-LastExitCode 'Dependency installation'
& $python -m ruff check $projectRoot
Assert-LastExitCode 'Ruff'
& $python -m pytest $projectRoot
Assert-LastExitCode 'Pytest'
& $python "$projectRoot\scripts\secret_scan.py"
Assert-LastExitCode 'Secret scan'
Push-Location $projectRoot
try {
    & $python .\scripts\http_smoke.py
    Assert-LastExitCode 'HTTP smoke'
}
finally {
    Pop-Location
}
