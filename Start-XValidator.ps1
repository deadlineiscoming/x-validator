[CmdletBinding()]
param(
    [ValidateSet('Setup', 'Pair', 'Run', 'Status', 'Credentials')]
    [string] $Action = 'Run'
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$root = Join-Path $env:LOCALAPPDATA 'X Validator'
$runtime = Join-Path $root 'runtime'
$state = Join-Path $root 'openclaw'
$configPath = Join-Path $state 'openclaw.json'
$gatewayPort = 18647
$modules = Join-Path $runtime 'node_modules'
$openclaw = Join-Path $modules 'openclaw\openclaw.mjs'
$weixin = Join-Path $modules '@tencent-weixin\openclaw-weixin'

$env:OPENCLAW_STATE_DIR = $state
$env:OPENCLAW_CONFIG_PATH = $configPath

if ($Action -eq 'Credentials') {
    $xToken = Read-Host 'X App-only Bearer Token' -AsSecureString
    $deepSeekKey = Read-Host 'DeepSeek API Key' -AsSecureString
    if ($xToken.Length -eq 0 -or $deepSeekKey.Length -eq 0) {
        throw 'Both credentials are required.'
    }
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    @{ X_BEARER_TOKEN = $xToken; DEEPSEEK_API_KEY = $deepSeekKey } |
        Export-Clixml -LiteralPath (Join-Path $root 'credentials.xml')
    Write-Host 'Credentials encrypted for the current Windows user.'
    exit 0
}

if ($Action -eq 'Setup') {
    New-Item -ItemType Directory -Path $runtime,$state -Force | Out-Null
    if (-not (Test-Path -LiteralPath $openclaw) -or -not (Test-Path -LiteralPath $weixin)) {
        npm.cmd install --prefix $runtime --no-audit --no-fund openclaw@2026.11.7 @tencent-weixin/openclaw-weixin@2.4.10
        if ($LASTEXITCODE -ne 0) {
            throw 'OpenClaw runtime installation failed.'
        }
    }
    $token = if (Test-Path -LiteralPath $configPath) {
        (Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json).gateway.auth.token
    } else {
        [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    }
    $python = (Get-Command python.exe -ErrorAction Stop).Source
    & $python -c 'from playwright.sync_api import sync_playwright' 2>$null
    if ($LASTEXITCODE -ne 0) {
        & $python -m pip install playwright
        if ($LASTEXITCODE -ne 0) {
            throw 'Playwright installation failed.'
        }
    }
    $config = @{
        gateway = @{ mode = 'local'; bind = 'loopback'; port = $gatewayPort; auth = @{ mode = 'token'; token = $token } }
        agents = @{ defaults = @{ workspace = (Join-Path $state 'workspace'); skipBootstrap = $true } }
        session = @{ dmScope = 'per-account-channel-peer' }
        commands = @{ native = $false; nativeSkills = $false; text = $false; bash = $false; config = $false }
        channels = @{ 'openclaw-weixin' = @{ enabled = $true } }
        plugins = @{
            allow = @('openclaw-weixin', 'x-validator-wechat')
            load = @{ paths = @($weixin, (Join-Path $PSScriptRoot 'wechat-plugin')) }
            entries = @{
                'openclaw-weixin' = @{ enabled = $true }
                'x-validator-wechat' = @{
                    enabled = $true
                    config = @{
                        python = $python
                        worker = (Join-Path $PSScriptRoot 'verify_screenshot.py')
                        mediaRoot = (Join-Path $state 'media\inbound')
                    }
                }
            }
        }
        tools = @{ deny = @('group:runtime', 'group:fs', 'group:automation', 'group:messaging') }
    }
    $config | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $configPath -Encoding utf8NoBOM
    & node.exe $openclaw config validate
    exit $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath $configPath)) {
    throw 'X Validator setup has not been completed.'
}

switch ($Action) {
    'Pair' {
        $Host.UI.RawUI.WindowTitle = 'X Validator - WeChat QR'
        & node.exe $openclaw channels login --channel openclaw-weixin
    }
    'Status' {
        & node.exe $openclaw channels status --probe
    }
    'Run' {
        $credentialPath = Join-Path $root 'credentials.xml'
        if (Test-Path -LiteralPath $credentialPath) {
            $stored = Import-Clixml -LiteralPath $credentialPath
            foreach ($name in @('X_BEARER_TOKEN', 'DEEPSEEK_API_KEY')) {
                if (-not [Environment]::GetEnvironmentVariable($name) -and $stored[$name]) {
                    $value = [System.Net.NetworkCredential]::new('', $stored[$name]).Password
                    [Environment]::SetEnvironmentVariable($name, $value, 'Process')
                }
            }
        }
        & node.exe $openclaw gateway run --bind loopback --port $gatewayPort
    }
}
exit $LASTEXITCODE
