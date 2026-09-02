$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$pythonName = 'python.exe'

$botProcesses = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -ieq $pythonName -and
            $_.CommandLine -match '(?:^|\s)-m\s+app\.local_bot(?:\s|$)' -and
            ($_.ExecutablePath -like "$projectRoot\*" -or
                $_.CommandLine -like "*$projectRoot*")
        }
)

if (-not $botProcesses) {
    Write-Host 'Local Sales Bot is not running.'
    return
}

$botProcesses |
    Sort-Object ProcessId -Descending |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host "Local Sales Bot stopped (processes: $($botProcesses.ProcessId -join ', '))."
