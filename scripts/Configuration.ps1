$modelKeys = @('X_VALIDATOR_FAST_API_KEY', 'X_VALIDATOR_REVIEW_API_KEY', 'X_VALIDATOR_WEB_API_KEY')
$secretNames = @('X_BEARER_TOKEN', 'DEEPSEEK_API_KEY') + $modelKeys
$settingNames = @(
    'X_VALIDATOR_FAST_PROVIDER', 'X_VALIDATOR_FAST_BASE_URL', 'X_VALIDATOR_FAST_MODEL',
    'X_VALIDATOR_REVIEW_PROVIDER', 'X_VALIDATOR_REVIEW_BASE_URL', 'X_VALIDATOR_REVIEW_MODEL',
    'X_VALIDATOR_REVIEW_VISION_MODEL', 'X_VALIDATOR_WEB_PROVIDER', 'X_VALIDATOR_ARCHIVE'
)

function Read-XValidatorSettings {
    if (Test-Path -LiteralPath $credentialPath -PathType Leaf) {
        return Import-Clixml -LiteralPath $credentialPath
    }
    return @{}
}

function Save-XValidatorSettings($Settings) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $credentialPath) -Force | Out-Null
    $temporary = "$credentialPath.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $Settings | Export-Clixml -LiteralPath $temporary
        [IO.File]::Move($temporary, $credentialPath, $true)
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function With-XValidatorSettings($Settings, [scriptblock] $Command) {
    $applied = @()
    try {
        foreach ($name in $secretNames + $settingNames) {
            if ([Environment]::GetEnvironmentVariable($name)) { continue }
            $value = $Settings[$name]
            if ($name -in $secretNames) {
                if ($value -isnot [securestring] -or $value.Length -eq 0) { continue }
                if ($Settings[$name + '_ROUTE']) {
                    $prefix = $name.Substring(0, $name.Length - 'API_KEY'.Length)
                    $changedAddress = $false
                    foreach ($field in 'PROVIDER', 'BASE_URL') {
                        $override = [Environment]::GetEnvironmentVariable($prefix + $field)
                        if ($override -and $override.TrimEnd('/') -cne $Settings[$prefix + $field]) { $changedAddress = $true }
                    }
                    if ($changedAddress) { continue }
                }
                $value = [Net.NetworkCredential]::new('', $value).Password
            }
            if ($value -isnot [string] -or -not $value) { continue }
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
            $applied += $name
        }
        & $Command
    } finally {
        foreach ($name in $applied) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
    }
}

function Read-Setting([string] $Prompt, [string] $Default) {
    $value = (Read-Host "$Prompt [$Default]").Trim()
    if ($value) { return $value }
    return $Default
}

function Read-Choice([string] $Prompt, [string[]] $Choices, [string] $Default) {
    while ($true) {
        $choice = (Read-Setting "$Prompt ($($Choices -join ' / '))" $Default).ToLowerInvariant()
        if ($choice -in $Choices) { return $choice }
        Write-Host '请选择列表中的一项。'
    }
}

function Set-MaskedKey($Settings, [string] $Name, [string] $Prompt, [switch] $Required) {
    if ([Environment]::GetEnvironmentVariable($Name)) {
        Write-Host "$Prompt 使用当前环境提供的值。"
        return
    }
    $hint = if ($Settings[$Name]) { '回车保留已保存的值' } else { '回车跳过' }
    if ($Required -and -not $Settings[$Name]) { $hint = '必填，输入不回显' }
    $key = Read-Host "$Prompt（$hint）" -AsSecureString
    if ($key.Length -gt 0) { $Settings[$Name] = $key }
    elseif ($Required -and -not $Settings[$Name]) { throw '缺少必填凭证，配置尚未保存。' }
}

function Set-ModelRoute($Settings, $Catalog, [string] $Role, [string] $Label, [string] $DefaultProvider) {
    $prefix = "X_VALIDATOR_${Role}_"
    $oldProvider = $Settings[$prefix + 'PROVIDER']
    if (-not $oldProvider) { $oldProvider = 'deepseek' }
    $oldUrl = $Settings[$prefix + 'BASE_URL']
    $provider = Read-Choice "$Label 厂商" @($Catalog.Keys) $DefaultProvider
    $sameProvider = $provider -eq $oldProvider
    $defaultUrl = if ($sameProvider -and $oldUrl) { $oldUrl } else { $Catalog[$provider].url }
    $url = Read-Setting "$Label Base URL，填厂商基础地址" $defaultUrl
    if (-not $url) { throw '该厂商需要 Base URL，配置尚未保存。' }
    $url = $url.TrimEnd('/')
    $savedUrl = if ($oldUrl) { $oldUrl.TrimEnd('/') } else { $Catalog[$oldProvider].url }
    if (-not $sameProvider -or $url -cne $savedUrl) {
        $Settings.Remove($prefix + 'API_KEY')
        $Settings.Remove($prefix + 'API_KEY_ROUTE')
        if ($Role -eq 'REVIEW') { $Settings.Remove('X_VALIDATOR_REVIEW_VISION_MODEL') }
    }
    $Settings[$prefix + 'PROVIDER'] = $provider
    $Settings[$prefix + 'BASE_URL'] = $url
    $defaultModel = if ($sameProvider) { $Settings[$prefix + 'MODEL'] }
    if (-not $defaultModel -and $provider -eq 'deepseek') { $defaultModel = $Catalog['deepseek'].models[$Role] }
    $model = Read-Setting "$Label 模型 ID" $defaultModel
    if (-not $model) { throw '请输入模型 ID，配置尚未保存。' }
    $Settings[$prefix + 'MODEL'] = $model
}

function Invoke-XValidatorWizard([string] $Python, [string] $Diagnostics) {
    $catalogOutput = & $Python $Diagnostics --catalog
    if ($LASTEXITCODE -ne 0) { throw '无法加载厂商列表。' }
    $catalog = $catalogOutput | ConvertFrom-Json -AsHashtable
    $selected = Read-XValidatorSettings
    Write-Host 'X Validator 首次配置。输入的 Key 由当前 Windows 用户加密保存。'
    Write-Host '指定的环境变量优先于此处保存的设置。已有默认配置可按回车保留。'
    Set-MaskedKey $selected 'X_BEARER_TOKEN' 'X App-only Bearer Token' -Required
    $archiveDefault = if ($selected['X_VALIDATOR_ARCHIVE'] -eq '1') { 'y' } else { 'n' }
    $archive = Read-Choice '使用 X 全历史搜索权限（没有该权限请选择 n）' @('y', 'n') $archiveDefault
    $selected['X_VALIDATOR_ARCHIVE'] = if ($archive -eq 'y') { '1' } else { '0' }

    $fastDefault = if ($selected['X_VALIDATOR_FAST_PROVIDER']) { $selected['X_VALIDATOR_FAST_PROVIDER'] } else { 'deepseek' }
    Set-ModelRoute $selected $catalog 'FAST' '识图与初判' $fastDefault
    $reviewDefault = if ($selected['X_VALIDATOR_REVIEW_PROVIDER']) { $selected['X_VALIDATOR_REVIEW_PROVIDER'] } else { 'deepseek' }
    Set-ModelRoute $selected $catalog 'REVIEW' '文字复核' $reviewDefault
    $oldWeb = if ($selected['X_VALIDATOR_WEB_PROVIDER']) { $selected['X_VALIDATOR_WEB_PROVIDER'] } else { 'deepseek' }
    $webProvider = Read-Choice '网页取证厂商' @('deepseek', 'kimi') $oldWeb
    if ($webProvider -ne $oldWeb) {
        $selected.Remove('X_VALIDATOR_WEB_API_KEY')
        $selected.Remove('X_VALIDATOR_WEB_API_KEY_ROUTE')
    }
    $selected['X_VALIDATOR_WEB_PROVIDER'] = $webProvider

    if ('deepseek' -in @($selected['X_VALIDATOR_FAST_PROVIDER'], $selected['X_VALIDATOR_REVIEW_PROVIDER'], $webProvider)) {
        Set-MaskedKey $selected 'DEEPSEEK_API_KEY' '共享 DeepSeek API Key'
    }
    foreach ($role in 'FAST', 'REVIEW', 'WEB') {
        $prefix = "X_VALIDATOR_${role}_"
        $officialDefault = $selected[$prefix + 'PROVIDER'] -eq 'deepseek' -and (
            $role -eq 'WEB' -or $selected[$prefix + 'BASE_URL'] -eq $catalog['deepseek'].url
        )
        if (-not $officialDefault -or $selected[$prefix + 'API_KEY']) {
            Set-MaskedKey $selected ($prefix + 'API_KEY') "$role 专用 Key（可复用官方接口 Key 时留空）"
            if ($selected[$prefix + 'API_KEY']) { $selected[$prefix + 'API_KEY_ROUTE'] = $true }
        }
    }
    $vision = Read-Setting '可选的视觉复读模型（留空使用默认，输入 - 清除）' $selected['X_VALIDATOR_REVIEW_VISION_MODEL']
    if ($vision -eq '-') { $selected.Remove('X_VALIDATOR_REVIEW_VISION_MODEL') }
    elseif ($vision) { $selected['X_VALIDATOR_REVIEW_VISION_MODEL'] = $vision }
    With-XValidatorSettings $selected { & $Python $Diagnostics --require-review }
    if ($LASTEXITCODE -ne 0) { throw '请补全上述设置，配置尚未保存。' }
    Save-XValidatorSettings $selected
    Write-Host '已保存。可以用 Start 启动并扫码，用 Restart 加载新配置。'

    $online = Read-Choice '运行在线自检（最多 7 项检查，厂商可能扣费）' @('y', 'n') 'n'
    if ($online -eq 'y') {
        With-XValidatorSettings $selected { & $Python $Diagnostics --online }
        if ($LASTEXITCODE -ne 0) {
            throw '配置已保存。请解决上面的接口问题，或确认相应功能可以暂时不可用，再启动。'
        }
    }
}
