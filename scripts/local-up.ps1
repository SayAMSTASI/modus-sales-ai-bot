param(
    [switch]$OpenAI,
    [Nullable[long]]$OwnerTelegramUserId,
    [string]$KeycloakClientId = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $projectRoot '.venv'

function Read-Secret([string]$Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

if (-not (Test-Path -LiteralPath $venv)) {
    python -m venv $venv
}

$python = Join-Path $venv 'Scripts\python.exe'
& $python -c 'import app, httpx, openai, sqlalchemy' 2>$null
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install --disable-pip-version-check -e $projectRoot
}
if (-not $env:TELEGRAM_BOT_TOKEN) {
    $env:TELEGRAM_BOT_TOKEN = Read-Secret 'Telegram bot token from @BotFather'
}
if (-not $env:TELEGRAM_BOT_TOKEN) {
    throw 'Telegram bot token is empty.'
}

$env:APP_ENV = 'development'
$env:DATABASE_URL = 'sqlite:///./data/sales_bot.db'
$env:LOCAL_AUTO_APPROVE_FIRST_USER = 'false'
$dataDirectory = Join-Path $projectRoot 'data'
New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null
$adminIdPath = Join-Path $dataDirectory 'admin-telegram-ids.txt'
if ($null -ne $OwnerTelegramUserId) {
    $ownerId = $OwnerTelegramUserId.Value.ToString()
    $env:ADMIN_TELEGRAM_IDS = $ownerId
    $env:PILOT_TELEGRAM_IDS = $ownerId
    Set-Content -LiteralPath $adminIdPath -Value $ownerId -Encoding ascii
}
elseif (-not $env:ADMIN_TELEGRAM_IDS -and (Test-Path -LiteralPath $adminIdPath)) {
    $ownerId = (Get-Content -Raw -LiteralPath $adminIdPath).Trim()
    $env:ADMIN_TELEGRAM_IDS = $ownerId
    $env:PILOT_TELEGRAM_IDS = $ownerId
}
elseif (-not $env:ADMIN_TELEGRAM_IDS) {
    $ownerId = Read-Host 'Your numeric Telegram user ID (pilot administrator)'
    if ($ownerId -notmatch '^\d+$') {
        throw 'Telegram user ID must contain digits only.'
    }
    $env:ADMIN_TELEGRAM_IDS = $ownerId
    $env:PILOT_TELEGRAM_IDS = $ownerId
    Set-Content -LiteralPath $adminIdPath -Value $ownerId -Encoding ascii
}
if (-not $env:TOKEN_ENCRYPTION_KEY) {
    $keyPath = Join-Path $projectRoot 'data\token-encryption.key'
    if (Test-Path -LiteralPath $keyPath) {
        $env:TOKEN_ENCRYPTION_KEY = (Get-Content -Raw -LiteralPath $keyPath).Trim()
    }
    else {
        $keyDirectory = Split-Path -Parent $keyPath
        New-Item -ItemType Directory -Force -Path $keyDirectory | Out-Null
        $env:TOKEN_ENCRYPTION_KEY = & $python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
        Set-Content -LiteralPath $keyPath -Value $env:TOKEN_ENCRYPTION_KEY -Encoding ascii
        Write-Host 'Created ignored local OAuth encryption key: data\token-encryption.key'
    }
}
if ($KeycloakClientId) {
    $env:KEYCLOAK_CLIENT_ID = $KeycloakClientId
    Set-Content -LiteralPath (Join-Path $dataDirectory 'keycloak-client-id.txt') -Value $KeycloakClientId -Encoding ascii
}
elseif (-not $env:KEYCLOAK_CLIENT_ID) {
    $clientIdPath = Join-Path $dataDirectory 'keycloak-client-id.txt'
    if (Test-Path -LiteralPath $clientIdPath) {
        $env:KEYCLOAK_CLIENT_ID = (Get-Content -Raw -LiteralPath $clientIdPath).Trim()
    }
}

if ($OpenAI) {
    $env:AGENT_BACKEND = 'openai'
    if (-not $env:OPENAI_API_KEY) {
        $env:OPENAI_API_KEY = Read-Secret 'OpenAI project API key'
    }
    if (-not $env:OPENAI_API_KEY) {
        throw 'OpenAI API key is empty.'
    }
}
else {
    $env:AGENT_BACKEND = 'mock'
    Write-Host 'OpenAI is disabled: deterministic mock responses will be used.'
}

Push-Location $projectRoot
try {
    & $python -m app.local_bot
}
finally {
    $env:TELEGRAM_BOT_TOKEN = $null
    $env:OPENAI_API_KEY = $null
    $env:TOKEN_ENCRYPTION_KEY = $null
    Pop-Location
}
