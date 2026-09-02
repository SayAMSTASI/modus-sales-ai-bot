param(
    [string]$KeycloakClientId = 'modus-sales-telegram-local',
    [string]$AdminTelegramIds = '',
    [Nullable[long]]$AdminTelegramId
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$dataDirectory = Join-Path $projectRoot 'data'
$secretFile = Join-Path $dataDirectory 'local-secrets.clixml'

if ($PSVersionTable.Platform -and $PSVersionTable.Platform -ne 'Win32NT') {
    throw 'Local DPAPI secret storage is supported only on Windows.'
}

$telegramToken = Read-Host 'Telegram bot token from @BotFather' -AsSecureString
$openAIKey = Read-Host 'OpenAI project API key' -AsSecureString
if ($telegramToken.Length -eq 0) {
    throw 'Telegram bot token is empty.'
}
if ($openAIKey.Length -eq 0) {
    throw 'OpenAI API key is empty.'
}

if ($AdminTelegramIds.Trim()) {
    $adminValue = $AdminTelegramIds
}
elseif ($null -ne $AdminTelegramId) {
    $adminValue = $AdminTelegramId.Value.ToString()
}
else {
    $adminValue = Read-Host 'Administrator Telegram IDs, comma-separated'
}
$adminItems = @(
    $adminValue.Split(',') |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ } |
        Select-Object -Unique
)
if (-not $adminItems -or ($adminItems | Where-Object { $_ -notmatch '^\d+$' })) {
    throw 'Administrator Telegram IDs must contain digits and be comma-separated.'
}
$adminValue = $adminItems -join ','
if (-not $KeycloakClientId.Trim()) {
    throw 'Keycloak client ID must not be empty.'
}

New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null
[pscustomobject]@{
    TelegramBotToken = $telegramToken
    OpenAIApiKey = $openAIKey
    AdminTelegramIds = $adminValue
    KeycloakClientId = $KeycloakClientId.Trim()
    SavedAt = [DateTimeOffset]::UtcNow.ToString('O')
} | Export-Clixml -LiteralPath $secretFile -Force

Write-Host 'Local secrets saved with Windows DPAPI in ignored data\local-secrets.clixml.'
Write-Host 'Start the bot next time with: .\scripts\local-start.ps1'
