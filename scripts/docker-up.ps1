param(
    [switch]$OpenAI,
    [switch]$Configure,
    [switch]$Foreground,
    [Nullable[long]]$OwnerTelegramUserId,
    [string]$AdminTelegramIds = '',
    [string]$KeycloakClientId = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot '.env.docker.local'
$exampleFile = Join-Path $projectRoot '.env.example'

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

function Set-EnvValue([string]$Content, [string]$Name, [string]$Value) {
    $escaped = $Value.Replace('$', '$$')
    $pattern = "(?m)^$([regex]::Escape($Name))=.*$"
    if ($Content -match $pattern) {
        return $Content -replace $pattern, "$Name=$escaped"
    }
    return $Content.TrimEnd() + "`r`n$Name=$escaped`r`n"
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI not found. Install Docker Desktop and start it.'
}
docker compose version | Out-Null

if (-not (Test-Path -LiteralPath $envFile)) {
    $postgresPassword = [guid]::NewGuid().ToString('N')
    $content = Get-Content -Raw -Encoding utf8 -LiteralPath $exampleFile
    $content = Set-EnvValue $content 'APP_ENV' 'development'
    $content = Set-EnvValue $content 'POSTGRES_PASSWORD' $postgresPassword
    $content = Set-EnvValue $content 'DATABASE_URL' "postgresql+psycopg://sales_bot:${postgresPassword}@db:5432/sales_bot"
    $content = Set-EnvValue $content 'TELEGRAM_WEBHOOK_SECRET' ([guid]::NewGuid().ToString('N'))
    $content = Set-EnvValue $content 'SAFETY_IDENTIFIER_SECRET' ([guid]::NewGuid().ToString('N'))
    $tokenBytes = [byte[]]::new(32)
    [Security.Cryptography.RandomNumberGenerator]::Fill($tokenBytes)
    $tokenKey = [Convert]::ToBase64String($tokenBytes).Replace('+', '-').Replace('/', '_')
    $content = Set-EnvValue $content 'TOKEN_ENCRYPTION_KEY' $tokenKey
    $content = Set-EnvValue $content 'LOCAL_AUTO_APPROVE_FIRST_USER' 'false'
    Set-Content -LiteralPath $envFile -Value $content -Encoding utf8NoBOM
    Write-Host 'Created ignored local configuration: .env.docker.local'
}

$content = Get-Content -Raw -Encoding utf8 -LiteralPath $envFile
if ($content -notmatch '(?m)^TOKEN_ENCRYPTION_KEY=.+$') {
    $tokenBytes = [byte[]]::new(32)
    [Security.Cryptography.RandomNumberGenerator]::Fill($tokenBytes)
    $tokenKey = [Convert]::ToBase64String($tokenBytes).Replace('+', '-').Replace('/', '_')
    $content = Set-EnvValue $content 'TOKEN_ENCRYPTION_KEY' $tokenKey
}
if ($Configure -or $content -notmatch '(?m)^TELEGRAM_BOT_TOKEN=\s*\S.+$') {
    $telegramToken = Read-Secret 'Telegram bot token from @BotFather'
    if (-not $telegramToken) {
        throw 'Telegram bot token is empty.'
    }
    $content = Set-EnvValue $content 'TELEGRAM_BOT_TOKEN' $telegramToken
}

if ($AdminTelegramIds.Trim()) {
    if ($AdminTelegramIds -notmatch '^\d+(\s*,\s*\d+)*$') {
        throw 'Administrator Telegram IDs must contain digits and be comma-separated.'
    }
    $ownerId = (($AdminTelegramIds -split ',') | ForEach-Object { $_.Trim() }) -join ','
    $content = Set-EnvValue $content 'ADMIN_TELEGRAM_IDS' $ownerId
    $content = Set-EnvValue $content 'PILOT_TELEGRAM_IDS' $ownerId
}
elseif ($null -ne $OwnerTelegramUserId) {
    $ownerId = $OwnerTelegramUserId.Value.ToString()
    $content = Set-EnvValue $content 'ADMIN_TELEGRAM_IDS' $ownerId
    $content = Set-EnvValue $content 'PILOT_TELEGRAM_IDS' $ownerId
}
elseif ($content -notmatch '(?m)^ADMIN_TELEGRAM_IDS=\s*\d') {
    $ownerId = Read-Host 'Administrator Telegram IDs, comma-separated'
    if ($ownerId -notmatch '^\d+(\s*,\s*\d+)*$') {
        throw 'Administrator Telegram IDs must contain digits and be comma-separated.'
    }
    $ownerId = (($ownerId -split ',') | ForEach-Object { $_.Trim() }) -join ','
    $content = Set-EnvValue $content 'ADMIN_TELEGRAM_IDS' $ownerId
    $content = Set-EnvValue $content 'PILOT_TELEGRAM_IDS' $ownerId
}

if ($KeycloakClientId) {
    $content = Set-EnvValue $content 'KEYCLOAK_CLIENT_ID' $KeycloakClientId
}
elseif ($content -notmatch '(?m)^KEYCLOAK_CLIENT_ID=\s*\S.+$') {
    $content = Set-EnvValue $content 'KEYCLOAK_CLIENT_ID' 'modus-sales-telegram-local'
}

if ($OpenAI -or $content -match '(?m)^OPENAI_API_KEY=\s*\S.+$') {
    if ($Configure -or $content -notmatch '(?m)^OPENAI_API_KEY=\s*\S.+$') {
        $openAIKey = Read-Secret 'OpenAI project API key'
        if (-not $openAIKey) {
            throw 'OpenAI API key is empty.'
        }
        $content = Set-EnvValue $content 'OPENAI_API_KEY' $openAIKey
    }
    $content = Set-EnvValue $content 'AGENT_BACKEND' 'openai'
}
else {
    $content = Set-EnvValue $content 'AGENT_BACKEND' 'mock'
}

Set-Content -LiteralPath $envFile -Value $content -Encoding utf8NoBOM

Push-Location $projectRoot
try {
    $arguments = @(
        'compose',
        '--env-file', $envFile,
        '-f', 'docker-compose.local.yml',
        'up', '--build'
    )
    if (-not $Foreground) {
        $arguments += '-d'
    }
    & docker @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed with exit code $LASTEXITCODE"
    }
    if (-not $Foreground) {
        docker compose --env-file $envFile -f docker-compose.local.yml logs --tail 30 bot
        Write-Host 'Bot container started. Send /start in a private Telegram chat.'
    }
}
finally {
    $telegramToken = $null
    $openAIKey = $null
    $content = $null
    Pop-Location
}
