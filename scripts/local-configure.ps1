param(
    [string]$KeycloakClientId = 'modus-sales-telegram-local',
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

if ($null -eq $AdminTelegramId) {
    $adminValue = Read-Host 'Your numeric Telegram user ID (pilot administrator)'
}
else {
    $adminValue = $AdminTelegramId.Value.ToString()
}
if ($adminValue -notmatch '^\d+$') {
    throw 'Telegram user ID must contain digits only.'
}
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
