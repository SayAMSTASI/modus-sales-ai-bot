param(
    [Parameter(Mandatory = $true)]
    [string]$PuttySession,

    [Parameter(Mandatory = $true)]
    [string]$SshUser,

    [string]$RemoteDirectory = '',
    [string]$KeycloakClientId = 'modus-sales-telegram-bot'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$secretFile = Join-Path $projectRoot 'data\local-secrets.clixml'
$plink = 'C:\Program Files\PuTTY\plink.exe'
$pscp = 'C:\Program Files\PuTTY\pscp.exe'

if (-not $RemoteDirectory) {
    $RemoteDirectory = "/home/$SshUser/modus-sales-bot"
}
if ($RemoteDirectory -notmatch '^/[A-Za-z0-9._/-]+$') {
    throw 'RemoteDirectory must be an absolute Linux path without spaces.'
}
foreach ($tool in @($plink, $pscp)) {
    if (-not (Test-Path -LiteralPath $tool)) {
        throw "PuTTY tool not found: $tool"
    }
}
if (-not (Test-Path -LiteralPath $secretFile)) {
    throw "Local DPAPI secret store not found: $secretFile"
}

function Invoke-Remote([string]$Command) {
    & $plink -batch -load $PuttySession -l $SshUser $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Remote command failed with exit code $LASTEXITCODE"
    }
}

function Copy-Remote([string]$LocalPath, [string]$RemotePath) {
    & $pscp -batch -load $PuttySession $LocalPath "${SshUser}@${PuttySession}:$RemotePath"
    if ($LASTEXITCODE -ne 0) {
        throw "File transfer failed with exit code $LASTEXITCODE"
    }
}

function Reveal-Secret([Security.SecureString]$Value) {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function New-HexSecret([int]$ByteCount) {
    $bytes = [byte[]]::new($ByteCount)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToHexString($bytes).ToLowerInvariant()
}

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) (
    'modus-sales-deploy-' + [guid]::NewGuid().ToString('N')
)
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null

try {
    $archivePath = Join-Path $temporaryDirectory 'source.tar'
    Push-Location $projectRoot
    try {
        git archive --format=tar --output=$archivePath HEAD
        if ($LASTEXITCODE -ne 0) {
            throw 'git archive failed.'
        }
    }
    finally {
        Pop-Location
    }

    Invoke-Remote "mkdir -p '$RemoteDirectory'"
    Copy-Remote $archivePath "$RemoteDirectory/source.tar"
    Invoke-Remote "tar -xf '$RemoteDirectory/source.tar' -C '$RemoteDirectory'"

    $envState = (& $plink -batch -load $PuttySession -l $SshUser `
        "test -s '$RemoteDirectory/.env.production' && echo PRESENT || echo MISSING").Trim()
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not inspect the remote production env file.'
    }

    if ($envState -eq 'MISSING') {
        $stored = Import-Clixml -LiteralPath $secretFile
        $postgresPassword = New-HexSecret 24
        $fernetBytes = [byte[]]::new(32)
        [Security.Cryptography.RandomNumberGenerator]::Fill($fernetBytes)
        $tokenEncryptionKey = [Convert]::ToBase64String($fernetBytes).Replace('+', '-').Replace('/', '_')
        $telegramBotToken = Reveal-Secret $stored.TelegramBotToken
        $openAIApiKey = Reveal-Secret $stored.OpenAIApiKey
        $envContent = @(
            "ENV_FILE=$RemoteDirectory/.env.production"
            'APP_ENV=production'
            'POSTGRES_DB=sales_bot'
            'POSTGRES_USER=sales_bot'
            "POSTGRES_PASSWORD=$postgresPassword"
            "DATABASE_URL=postgresql+psycopg://sales_bot:${postgresPassword}@db:5432/sales_bot"
            'PUBLIC_BASE_URL=http://127.0.0.1:8000'
            'HTTP_BIND_ADDRESS=127.0.0.1'
            'HTTP_PORT=8000'
            "TELEGRAM_BOT_TOKEN=$telegramBotToken"
            "TELEGRAM_WEBHOOK_SECRET=$(New-HexSecret 24)"
            'TELEGRAM_POLL_TIMEOUT_SECONDS=20'
            'TELEGRAM_DROP_PENDING_UPDATES=false'
            'LOCAL_AUTO_APPROVE_FIRST_USER=false'
            "ADMIN_TELEGRAM_IDS=$($stored.AdminTelegramIds)"
            "PILOT_TELEGRAM_IDS=$($stored.AdminTelegramIds)"
            "SAFETY_IDENTIFIER_SECRET=$(New-HexSecret 32)"
            'AGENT_BACKEND=openai'
            "OPENAI_API_KEY=$openAIApiKey"
            'OPENAI_MODEL=gpt-5.6-luna'
            'OPENAI_REASONING_EFFORT=low'
            'OPENAI_MAX_OUTPUT_TOKENS=1200'
            'KEYCLOAK_FLOW=device'
            'KEYCLOAK_ISSUER=https://auth.modusbi.ru/realms/master'
            "KEYCLOAK_CLIENT_ID=$KeycloakClientId"
            'KEYCLOAK_SCOPES=openid profile email offline_access'
            'KEYCLOAK_RESOURCE=https://mcp.modusbi.ru'
            "TOKEN_ENCRYPTION_KEY=$tokenEncryptionKey"
            'CONTEXT_TTL_HOURS=24'
            'CONTEXT_MAX_MESSAGES=12'
            'CONTEXT_MAX_CHARS=24000'
            'MAX_MESSAGE_CHARS=12000'
            'JOB_PAYLOAD_TTL_HOURS=24'
            'MAX_JOB_ATTEMPTS=3'
            'WORKER_POLL_SECONDS=1'
            'GLOBAL_DAILY_REQUEST_LIMIT=1000'
            'GLOBAL_DAILY_COST_LIMIT_USD=50'
            'QUESTION_AUDIT_RETENTION_DAYS=30'
            'QUESTION_AUDIT_MAX_CHARS=4000'
            'ADMIN_USERS_PAGE_SIZE=10'
            'MCP_SERVERS_FILE=config/mcp_servers.json'
        ) -join "`n"
        $envPath = Join-Path $temporaryDirectory 'production.env'
        [IO.File]::WriteAllText(
            $envPath,
            $envContent + "`n",
            [Text.UTF8Encoding]::new($false)
        )
        Copy-Remote $envPath "$RemoteDirectory/.env.production.upload"
        Invoke-Remote (
            "chmod 600 '$RemoteDirectory/.env.production.upload' && " +
            "mv '$RemoteDirectory/.env.production.upload' '$RemoteDirectory/.env.production'"
        )
        $telegramBotToken = $null
        $openAIApiKey = $null
        $envContent = $null
        Write-Host 'Created the protected production env file on the server.'
    }
    else {
        Write-Host 'Kept the existing production env file unchanged.'
    }

    Invoke-Remote (
        "cd '$RemoteDirectory' && " +
        "docker compose --env-file .env.production -f docker-compose.local.yml config --quiet && " +
        "docker compose --env-file .env.production -f docker-compose.local.yml up -d --build"
    )
    Invoke-Remote (
        "cd '$RemoteDirectory' && " +
        "docker compose --env-file .env.production -f docker-compose.local.yml ps && " +
        "docker compose --env-file .env.production -f docker-compose.local.yml logs --tail 30 bot"
    )
}
finally {
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}
