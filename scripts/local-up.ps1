param(
    [switch]$OpenAI,
    [switch]$Mcp,
    [Nullable[long]]$OwnerTelegramUserId
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
$env:LOCAL_AUTO_APPROVE_FIRST_USER = 'true'
if ($null -ne $OwnerTelegramUserId) {
    $env:LOCAL_OWNER_TELEGRAM_USER_ID = $OwnerTelegramUserId.Value.ToString()
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

$temporaryMcpTokenNames = @()
if ($Mcp) {
    if (-not $OpenAI) {
        throw 'MCP requires -OpenAI because remote MCP tools are called through Responses API.'
    }
    $registryPath = Join-Path $projectRoot 'config\mcp_servers.json'
    $enabledServers = @(
        Get-Content -LiteralPath $registryPath -Raw |
            ConvertFrom-Json |
            Where-Object { @($_.allowed_tools).Count -gt 0 }
    )
    if ($enabledServers.Count -eq 0) {
        throw 'No MCP servers have an approved allowed_tools list.'
    }
    foreach ($server in $enabledServers) {
        $tokenName = [string]$server.authorization_env
        if (-not $tokenName) {
            continue
        }
        $token = [Environment]::GetEnvironmentVariable($tokenName, 'Process')
        if (-not $token) {
            $token = Read-Secret "OAuth access token for MCP $($server.server_label)"
            if (-not $token) {
                throw "OAuth access token for MCP $($server.server_label) is empty."
            }
            [Environment]::SetEnvironmentVariable($tokenName, $token, 'Process')
            $temporaryMcpTokenNames += $tokenName
        }
    }
}

Push-Location $projectRoot
try {
    & $python -m app.local_bot
}
finally {
    $env:TELEGRAM_BOT_TOKEN = $null
    $env:OPENAI_API_KEY = $null
    foreach ($tokenName in $temporaryMcpTokenNames) {
        [Environment]::SetEnvironmentVariable($tokenName, $null, 'Process')
    }
    Pop-Location
}
