param(
    [Parameter(Mandatory = $true)]
    [string]$Archive,

    [string]$PasswordStore = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$deliveryDirectory = Join-Path $projectRoot 'delivery'
$sevenZip = 'C:\Program Files\7-Zip\7z.exe'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not $PasswordStore) {
    $PasswordStore = Join-Path $projectRoot 'data\devops-handoff-password.clixml'
}
foreach ($required in @($Archive, $PasswordStore, $sevenZip, $python)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required verification input not found: $required"
    }
}

$securePassword = Import-Clixml -LiteralPath $PasswordStore
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
$password = $null
$verifyDirectory = Join-Path $deliveryDirectory (
    '.verify-' + [guid]::NewGuid().ToString('N')
)

try {
    $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    New-Item -ItemType Directory -Path $verifyDirectory | Out-Null
    & $sevenZip x -y "-p$password" "-o$verifyDirectory" $Archive | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Archive extraction failed.'
    }

    $checksumFile = Join-Path $verifyDirectory 'SHA256SUMS.txt'
    $mismatches = @()
    foreach ($line in Get-Content -LiteralPath $checksumFile) {
        if (-not $line.Trim()) {
            continue
        }
        $parts = $line -split '  ', 2
        $path = Join-Path $verifyDirectory $parts[1].Replace('/', '\')
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
        if ($actual -ne $parts[0]) {
            $mismatches += $parts[1]
        }
    }
    if ($mismatches) {
        throw "Checksum mismatch: $($mismatches -join ', ')"
    }

    $databaseFile = Join-Path $verifyDirectory 'data\sales_bot.db'
    $databaseResult = & $python (Join-Path $projectRoot 'scripts\import_sqlite_to_postgres.py') `
        $databaseFile --dry-run
    if ($LASTEXITCODE -ne 0) {
        throw 'Database verification failed.'
    }
    $envFile = Join-Path $verifyDirectory 'secrets\.env.production'
    $configResult = & $python -c @'
import sys
from app.config import Settings
s = Settings(_env_file=sys.argv[1])
print("config-ok", s.app_env, s.agent_backend, s.keycloak_flow, len(s.admin_ids()), len(s.pilot_ids()))
'@ $envFile
    if ($LASTEXITCODE -ne 0) {
        throw 'Production configuration verification failed.'
    }
    $manifest = Get-Content -Raw -LiteralPath (Join-Path $verifyDirectory 'manifest.json') |
        ConvertFrom-Json
    [pscustomobject]@{
        ArchiveTest = 'ok'
        Checksums = 'ok'
        Database = $databaseResult
        Configuration = $configResult
        Revision = $manifest.git_revision
        Files = (Get-ChildItem -LiteralPath $verifyDirectory -Recurse -File).Count
    } | ConvertTo-Json -Depth 4
}
finally {
    if ($pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    $password = $null
    $resolvedRoot = [IO.Path]::GetFullPath($deliveryDirectory).TrimEnd('\') + '\'
    $resolvedVerify = [IO.Path]::GetFullPath($verifyDirectory)
    if ($resolvedVerify.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolvedVerify)) {
        Remove-Item -LiteralPath $resolvedVerify -Recurse -Force
    }
}
