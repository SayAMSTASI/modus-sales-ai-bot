param(
    [string]$KeycloakClientId = 'modus-sales-telegram-bot',
    [string]$PublicBaseUrl = 'https://CHANGE-ME.modusbi.ru'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$deliveryDirectory = Join-Path $projectRoot 'delivery'
$dataDirectory = Join-Path $projectRoot 'data'
$secretStore = Join-Path $dataDirectory 'local-secrets.clixml'
$tokenKeyFile = Join-Path $dataDirectory 'token-encryption.key'
$databaseFile = Join-Path $dataDirectory 'sales_bot.db'
$passwordStore = Join-Path $dataDirectory 'devops-handoff-password.clixml'
$sevenZip = 'C:\Program Files\7-Zip\7z.exe'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

foreach ($required in @($secretStore, $tokenKeyFile, $databaseFile, $sevenZip, $python)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required handoff source not found: $required"
    }
}
if (-not $PublicBaseUrl.StartsWith('https://')) {
    throw 'PublicBaseUrl must use HTTPS.'
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

function Write-Utf8([string]$Path, [string]$Content) {
    [IO.File]::WriteAllText($Path, $Content, [Text.UTF8Encoding]::new($false))
}

New-Item -ItemType Directory -Force -Path $deliveryDirectory | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$packageName = "modus-sales-bot-devops-$stamp"
$stagingDirectory = Join-Path $deliveryDirectory ('.staging-' + [guid]::NewGuid().ToString('N'))
$archivePath = Join-Path $deliveryDirectory "$packageName.7z"
$archiveHashPath = "$archivePath.sha256"
$sourceArchive = Join-Path $stagingDirectory 'source.tar'
$sourceDirectory = Join-Path $stagingDirectory 'source'
$secretsDirectory = Join-Path $stagingDirectory 'secrets'
$handoffDataDirectory = Join-Path $stagingDirectory 'data'

New-Item -ItemType Directory -Path $stagingDirectory, $sourceDirectory, $secretsDirectory, $handoffDataDirectory | Out-Null

try {
    $head = (git -C $projectRoot rev-parse HEAD).Trim()
    if (-not $head) {
        throw 'Could not resolve the current Git revision.'
    }
    git -C $projectRoot archive --format=tar --output=$sourceArchive HEAD
    if ($LASTEXITCODE -ne 0) {
        throw 'git archive failed.'
    }
    tar -xf $sourceArchive -C $sourceDirectory
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not expand the source archive.'
    }
    Remove-Item -LiteralPath $sourceArchive

    Copy-Item -LiteralPath $databaseFile -Destination (Join-Path $handoffDataDirectory 'sales_bot.db')
    Copy-Item -LiteralPath (Join-Path $projectRoot 'docs\devops-handoff-package.md') -Destination (Join-Path $stagingDirectory 'DEPLOYMENT.md')

    $inventoryScript = @'
import json, sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
db.row_factory = sqlite3.Row
tables = [row[0] for row in db.execute(
    "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
)]
counts = {table: db.execute(f'select count(*) from "{table}"').fetchone()[0] for table in tables}
users = [dict(row) for row in db.execute(
    "select telegram_user_id, chat_id, telegram_username, corporate_name, corporate_email, "
    "status, role, request_number, approved_by, approved_at, created_at, updated_at "
    "from user_access order by telegram_user_id"
)]
print(json.dumps({"counts": counts, "users": users}, ensure_ascii=False))
'@
    $inventory = (& $python -c $inventoryScript $databaseFile) | ConvertFrom-Json
    $stored = Import-Clixml -LiteralPath $secretStore
    $adminIds = @(
        $stored.AdminTelegramIds.ToString().Split(',') |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ }
    )
    $pilotIds = @(
        $inventory.users |
            Where-Object { $_.status -eq 'active' } |
            ForEach-Object { $_.telegram_user_id.ToString() }
    )
    $pilotIds = @($adminIds + $pilotIds | Select-Object -Unique)
    $userSummary = @(
        $inventory.users | ForEach-Object {
            [ordered]@{
                telegram_user_id = $_.telegram_user_id
                chat_id = $_.chat_id
                telegram_username = $_.telegram_username
                corporate_name = $_.corporate_name
                corporate_email = $_.corporate_email
                status = $_.status
                role = $_.role
                request_number = $_.request_number
                bootstrap_admin = $adminIds -contains $_.telegram_user_id.ToString()
                approved_by = $_.approved_by
                approved_at = $_.approved_at
                created_at = $_.created_at
                updated_at = $_.updated_at
            }
        }
    )
    Write-Utf8 (Join-Path $handoffDataDirectory 'users-summary.json') (
        $userSummary | ConvertTo-Json -Depth 5
    )

    $postgresPassword = New-HexSecret 24
    $telegramBotToken = Reveal-Secret $stored.TelegramBotToken
    $openAIApiKey = Reveal-Secret $stored.OpenAIApiKey
    $tokenEncryptionKey = (Get-Content -Raw -LiteralPath $tokenKeyFile).Trim()
    $envContent = @(
        'ENV_FILE=/opt/modus-sales-bot/secrets/.env.production'
        'APP_ENV=production'
        'POSTGRES_DB=sales_bot'
        'POSTGRES_USER=sales_bot'
        "POSTGRES_PASSWORD=$postgresPassword"
        "DATABASE_URL=postgresql+psycopg://sales_bot:${postgresPassword}@db:5432/sales_bot"
        "PUBLIC_BASE_URL=$PublicBaseUrl"
        'HTTP_BIND_ADDRESS=127.0.0.1'
        'HTTP_PORT=8000'
        "TELEGRAM_BOT_TOKEN=$telegramBotToken"
        "TELEGRAM_WEBHOOK_SECRET=$(New-HexSecret 24)"
        'TELEGRAM_POLL_TIMEOUT_SECONDS=20'
        'TELEGRAM_DROP_PENDING_UPDATES=false'
        'LOCAL_AUTO_APPROVE_FIRST_USER=false'
        "ADMIN_TELEGRAM_IDS=$($adminIds -join ',')"
        "PILOT_TELEGRAM_IDS=$($pilotIds -join ',')"
        "SAFETY_IDENTIFIER_SECRET=$(New-HexSecret 32)"
        'AGENT_BACKEND=openai'
        "OPENAI_API_KEY=$openAIApiKey"
        'OPENAI_MODEL=gpt-5.6-luna'
        'OPENAI_REASONING_EFFORT=low'
        'OPENAI_MAX_OUTPUT_TOKENS=1200'
        'OPENAI_ENABLE_IMAGE_GENERATION=true'
        'OPENAI_ENABLE_CODE_INTERPRETER=true'
        'OPENAI_CODE_INTERPRETER_MEMORY_LIMIT=1g'
        'KEYCLOAK_FLOW=authorization_code'
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
        'ATTACHMENT_MAX_INPUT_BYTES=10485760'
        'ATTACHMENT_MAX_OUTPUT_BYTES=10485760'
        'ATTACHMENT_MAX_OUTPUT_COUNT=4'
        'ATTACHMENT_HTTP_TIMEOUT_SECONDS=30'
        'ATTACHMENT_ALLOWED_MIME_TYPES=image/jpeg,image/png,image/webp,application/pdf,text/plain,text/markdown,text/csv,application/json,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.openxmlformats-officedocument.presentationml.presentation'
        'ATTACHMENT_DOWNLOAD_ALLOWED_HOSTS='
        'MCP_SERVERS_FILE=config/mcp_servers.json'
    ) -join "`n"
    Write-Utf8 (Join-Path $secretsDirectory '.env.production') ($envContent + "`n")
    $telegramBotToken = $null
    $openAIApiKey = $null
    $envContent = $null
    $postgresPassword = $null
    $tokenEncryptionKey = $null

    $manifest = [ordered]@{
        package = $packageName
        created_at = [DateTimeOffset]::UtcNow.ToString('O')
        git_revision = $head
        keycloak_client_id = $KeycloakClientId
        public_base_url_requires_review = $PublicBaseUrl
        database_counts = $inventory.counts
        active_pilot_ids_count = $pilotIds.Count
        bootstrap_admin_ids_count = $adminIds.Count
        includes_production_secrets = $true
        includes_personal_data = $true
        token_encryption_key_matches_sqlite = $true
        deployment_host = 'selected by DevOps; not embedded in the package'
    }
    Write-Utf8 (Join-Path $stagingDirectory 'manifest.json') (
        $manifest | ConvertTo-Json -Depth 8
    )

    $checksumLines = @(
        Get-ChildItem -LiteralPath $stagingDirectory -Recurse -File |
            Where-Object { $_.Name -ne 'SHA256SUMS.txt' } |
            Sort-Object FullName |
            ForEach-Object {
                $relative = [IO.Path]::GetRelativePath($stagingDirectory, $_.FullName).Replace('\', '/')
                $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
                "$hash  $relative"
            }
    )
    Write-Utf8 (Join-Path $stagingDirectory 'SHA256SUMS.txt') (
        ($checksumLines -join "`n") + "`n"
    )

    $password = New-HexSecret 20
    ConvertTo-SecureString $password -AsPlainText -Force | Export-Clixml -LiteralPath $passwordStore -Force
    & $sevenZip a -t7z -mhe=on "-p$password" $archivePath "$stagingDirectory\*" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw '7-Zip archive creation failed.'
    }
    & $sevenZip t "-p$password" $archivePath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw '7-Zip archive verification failed.'
    }
    $password = $null

    $archiveHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToLowerInvariant()
    Write-Utf8 $archiveHashPath "$archiveHash  $([IO.Path]::GetFileName($archivePath))`n"
    [pscustomobject]@{
        Archive = $archivePath
        Sha256 = $archiveHash
        PasswordStore = $passwordStore
        GitRevision = $head
    } | ConvertTo-Json
}
finally {
    $resolvedDelivery = [IO.Path]::GetFullPath($deliveryDirectory).TrimEnd('\') + '\'
    $resolvedStaging = [IO.Path]::GetFullPath($stagingDirectory)
    if ($resolvedStaging.StartsWith($resolvedDelivery, [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolvedStaging)) {
        Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
    }
}
