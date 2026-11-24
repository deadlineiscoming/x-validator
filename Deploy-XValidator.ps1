[CmdletBinding()]
param([switch] $PrepareOnly)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

function Install-RequiredPackage([string] $Id) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "Install $Id or enable WinGet (App Installer), then rerun deployment."
    }
    & $winget.Source install --id $Id --exact --source winget --silent --disable-interactivity --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "WinGet could not install $Id (exit $LASTEXITCODE)."
    }
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = ($env:Path, $machinePath, $userPath) -join ';'
}

if ($PSVersionTable.PSVersion.Major -lt 7) {
    $pwsh = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
    if (-not $pwsh) {
        Install-RequiredPackage 'Microsoft.PowerShell'
        $pwsh = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
        if (-not $pwsh) { $pwsh = Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe' }
    }
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath)
    if ($PrepareOnly) { $arguments += '-PrepareOnly' }
    & $pwsh @arguments
    exit $LASTEXITCODE
}

function Find-Python {
    $candidates = @((Get-Command python.exe -All -ErrorAction SilentlyContinue).Source)
    $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
    $candidates += Join-Path $env:ProgramFiles 'Python313\python.exe'
    foreach ($candidate in $candidates | Where-Object { $_ } | Select-Object -Unique) {
        if ($candidate -like '*\Microsoft\WindowsApps\*' -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        & $candidate -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' 2>$null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
}

function Find-Node {
    $candidates = @((Get-Command node.exe -All -ErrorAction SilentlyContinue).Source)
    $candidates += Join-Path $env:ProgramFiles 'nodejs\node.exe'
    foreach ($candidate in $candidates | Where-Object { $_ } | Select-Object -Unique) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        $release = (& $candidate --version 2>$null) -replace '^v', ''
        $version = [version]'0.0'
        if ([version]::TryParse($release, [ref]$version) -and (
            ($version -ge [version]'24.16' -and $version -lt [version]'25.0') -or $version -ge [version]'26.1'
        )) { return $candidate }
    }
}

function Test-Browser {
    $locations = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA) | Where-Object { $_ }
    foreach ($location in $locations) {
        foreach ($relativePath in 'Microsoft\Edge\Application\msedge.exe', 'Google\Chrome\Application\chrome.exe') {
            if (Test-Path -LiteralPath (Join-Path $location $relativePath) -PathType Leaf) { return $true }
        }
    }
    return $false
}

$entryScript = Join-Path $PSScriptRoot 'Start-XValidator.ps1'
$workerPath = Join-Path $PSScriptRoot 'verify_screenshot.py'
$root = Join-Path $env:LOCALAPPDATA 'X Validator'
$state = Join-Path $root 'openclaw'
$configPath = Join-Path $state 'openclaw.json'
$openclaw = Join-Path $root 'runtime\node_modules\openclaw\openclaw.mjs'
$weixin = Join-Path $root 'runtime\node_modules\@tencent-weixin\openclaw-weixin'
$port = 18647

function Invoke-Launcher([string] $Action) {
    & $entryScript -Action $Action
    if ($LASTEXITCODE -ne 0) { throw "X Validator $Action failed (exit $LASTEXITCODE)." }
}

function Test-WeChatPairing {
    $directory = Join-Path $state 'openclaw-weixin'
    $index = Join-Path $directory 'accounts.json'
    if (-not (Test-Path -LiteralPath $index -PathType Leaf)) { return $false }
    foreach ($id in (Get-Content -Raw -LiteralPath $index | ConvertFrom-Json)) {
        if ($id -isnot [string] -or $id -notmatch '^[a-zA-Z0-9_.-]+$') { continue }
        $account = Join-Path $directory "accounts\$id.json"
        if (Test-Path -LiteralPath $account -PathType Leaf) {
            $record = Get-Content -Raw -LiteralPath $account | ConvertFrom-Json
            if ($record.token) { return $true }
        }
    }
    return $false
}

$mutex = [System.Threading.Mutex]::new($false, 'Local\XValidator-Deploy-18647')
$locked = $false
try {
    try { $locked = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'An X Validator deployment is already running.' }

    $config = if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
    }
    $configuredWorker = $config.plugins.entries.'x-validator-wechat'.config.worker
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
        if ($configuredWorker -ne $workerPath -or $process.CommandLine -notlike '*gateway run*' -or
            ([string]$process.CommandLine).IndexOf($openclaw, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            throw "Port $port is already in use. Stop the other gateway or listener before deploying from this checkout."
        }
        Write-Host 'This checkout is configured and its gateway is running.'
        return
    }

    $python = Find-Python
    if (-not $python) {
        Install-RequiredPackage 'Python.Python.3.13'
        $python = Find-Python
        if (-not $python) { throw 'Python 3.10 or later was not found after installation.' }
    }
    $node = Find-Node
    if (-not $node) {
        Install-RequiredPackage 'OpenJS.NodeJS.LTS'
        $node = Find-Node
        if (-not $node) { throw 'OpenClaw needs Node.js 24.16-24.x or 26.1 and later.' }
    }
    $env:Path = "$(Split-Path -Parent $python);$(Split-Path -Parent $node);$env:Path"
    if (-not (Test-Browser)) {
        Install-RequiredPackage 'Microsoft.Edge'
        if (-not (Test-Browser)) { throw 'Microsoft Edge or Google Chrome is required.' }
    }

    $pythonConfigured = $config.plugins.entries.'x-validator-wechat'.config.python
    & $python -c 'from playwright.sync_api import sync_playwright' 2>$null
    if ($configuredWorker -ne $workerPath -or $pythonConfigured -ne $python -or $LASTEXITCODE -ne 0 -or
        -not (Test-Path -LiteralPath $openclaw -PathType Leaf) -or -not (Test-Path -LiteralPath $weixin -PathType Container)) {
        Invoke-Launcher 'Setup'
    }
    if ($PrepareOnly) {
        Write-Host 'Deployment files and runtime are ready.'
        return
    }

    $credentialPath = Join-Path $root 'credentials.xml'
    if (-not (Test-Path -LiteralPath $credentialPath -PathType Leaf)) {
        Invoke-Launcher 'Credentials'
    }
    $stored = Import-Clixml -LiteralPath $credentialPath
    foreach ($name in 'X_BEARER_TOKEN', 'DEEPSEEK_API_KEY') {
        if ($stored[$name] -isnot [securestring] -or $stored[$name].Length -eq 0) {
            throw 'Re-enter the local API credentials with Start-XValidator.ps1 -Action Credentials.'
        }
    }
    if (-not (Test-WeChatPairing)) {
        Invoke-Launcher 'Pair'
        if (-not (Test-WeChatPairing)) { throw 'WeChat pairing did not finish. Run deployment again to retry.' }
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $stdout = Join-Path $root "gateway-$stamp.log"
    $stderr = Join-Path $root "gateway-$stamp-error.log"
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $entryScript + '"'), '-Action', 'Run')
    $gateway = Start-Process -FilePath (Join-Path $PSHOME 'pwsh.exe') -ArgumentList $arguments `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Set-Content -LiteralPath (Join-Path $root 'gateway.pid') -Value $gateway.Id
    try {
        $deadline = (Get-Date).AddSeconds(90)
        while (-not (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
            if ($gateway.HasExited -or (Get-Date) -ge $deadline) { throw 'Gateway is not listening.' }
            Start-Sleep -Seconds 1
        }
        Invoke-Launcher 'Status'
    } catch {
        throw "Gateway startup could not be verified. Logs: $stdout and $stderr. $($_.Exception.Message)"
    }
    Write-Host 'X Validator is running in the background.'
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
