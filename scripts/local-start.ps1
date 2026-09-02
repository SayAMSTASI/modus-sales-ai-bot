param(
    [switch]$Configure,
    [string]$KeycloakClientId = 'modus-sales-telegram-local',
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
    & (Join-Path $PSScriptRoot 'local-configure.ps1') @configureArguments
}

& (Join-Path $PSScriptRoot 'local-up.ps1') -OpenAI
