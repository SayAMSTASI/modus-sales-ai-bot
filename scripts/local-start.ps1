param(
    [switch]$Configure,
    [switch]$Restart,
    [string]$KeycloakClientId = 'modus-sales-telegram-local',
    [string]$AdminTelegramIds = '',
    [Nullable[long]]$AdminTelegramId
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$secretFile = Join-Path $projectRoot 'data\local-secrets.clixml'

if ($Configure -or -not (Test-Path -LiteralPath $secretFile)) {
    $configureArguments = @{
        KeycloakClientId = $KeycloakClientId
    }
    if ($null -ne $AdminTelegramId) {
        $configureArguments.AdminTelegramId = $AdminTelegramId.Value
    }
    if ($AdminTelegramIds.Trim()) {
        $configureArguments.AdminTelegramIds = $AdminTelegramIds
    }
    & (Join-Path $PSScriptRoot 'local-configure.ps1') @configureArguments
}

$botProcesses = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -ieq 'python.exe' -and
            $_.CommandLine -match '(?:^|\s)-m\s+app\.local_bot(?:\s|$)' -and
            ($_.ExecutablePath -like "$projectRoot\*" -or
                $_.CommandLine -like "*$projectRoot*")
        }
)
if ($botProcesses) {
    if (-not $Restart) {
        throw 'Local Sales Bot is already running. Use -Restart to apply the latest code.'
    }
    & (Join-Path $PSScriptRoot 'local-stop.ps1')
}

& (Join-Path $PSScriptRoot 'local-up.ps1') -OpenAI
