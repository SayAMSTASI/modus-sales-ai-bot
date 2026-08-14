param(
    [switch]$OpenAI,
    [switch]$Foreground,
    [Nullable[long]]$OwnerTelegramUserId
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
    return $Content -replace "(?m)^$([regex]::Escape($Name))=.*$", "$Name=$escaped"
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
    $content = Set-EnvValue $content 'ADMIN_PASSWORD' ([guid]::NewGuid().ToString('N'))
    $content = Set-EnvValue $content 'SAFETY_IDENTIFIER_SECRET' ([guid]::NewGuid().ToString('N'))
    $content = Set-EnvValue $content 'LOCAL_AUTO_APPROVE_FIRST_USER' 'true'
    Set-Content -LiteralPath $envFile -Value $content -Encoding utf8NoBOM
    Write-Host 'Created ignored local configuration: .env.docker.local'
}

$content = Get-Content -Raw -Encoding utf8 -LiteralPath $envFile
$telegramToken = Read-Secret 'Telegram bot token from @BotFather'
if (-not $telegramToken) {
    throw 'Telegram bot token is empty.'
}
$content = Set-EnvValue $content 'TELEGRAM_BOT_TOKEN' $telegramToken

if ($null -ne $OwnerTelegramUserId) {
    $content = Set-EnvValue $content 'LOCAL_OWNER_TELEGRAM_USER_ID' $OwnerTelegramUserId.Value.ToString()
}

if ($OpenAI) {
    $openAIKey = Read-Secret 'OpenAI project API key'
    if (-not $openAIKey) {
        throw 'OpenAI API key is empty.'
    }
    $content = Set-EnvValue $content 'AGENT_BACKEND' 'openai'
    $content = Set-EnvValue $content 'OPENAI_API_KEY' $openAIKey
}
else {
    $content = Set-EnvValue $content 'AGENT_BACKEND' 'mock'
    $content = Set-EnvValue $content 'OPENAI_API_KEY' ''
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
