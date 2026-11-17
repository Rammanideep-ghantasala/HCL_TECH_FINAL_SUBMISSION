<#
.SYNOPSIS
    Regression tests for the configuration loader (Get-DeployConfig.ps1). Runs no AWS, Docker or git command.

.DESCRIPTION
    A plain Windows PowerShell 5.1 script; Pester is not needed. It writes small configuration
    files, derived from deploy.config.example.json, to a new folder under $env:TEMP, loads each
    one with "Get-DeployConfig -SkipIdentity" and checks whether it is accepted or refused.
    The folder is deleted at the end.

    Covered: the ticket storage rule. Saving resolved tickets with JSON storage is refused,
    because on Fargate the JSON store sits on each task's temporary disk (saved tickets are
    lost when a task is replaced, and a retried save routed to another task creates a second
    ticket). See DEPLOY_AWS.md, section "Persistence of saved tickets".

    Exit code 0 when every case passes, 1 otherwise.

.PARAMETER LoaderPath
    Loader to test. Default: Get-DeployConfig.ps1 in the parent folder.

.PARAMETER ExamplePath
    Example configuration the cases start from. Default: deploy.config.example.json in the
    parent folder.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\Test-DeployConfig.ps1
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string]$LoaderPath = '',

    [Parameter()]
    [string]$ExamplePath = ''
)

$ErrorActionPreference = 'Stop'
$deployAwsRoot = Split-Path -Parent $PSScriptRoot
if (-not $LoaderPath) {
    $LoaderPath = Join-Path $deployAwsRoot 'Get-DeployConfig.ps1'
}
if (-not $ExamplePath) {
    $ExamplePath = Join-Path $deployAwsRoot 'deploy.config.example.json'
}
. $LoaderPath

$script:TestDir = Join-Path ([System.IO.Path]::GetTempPath()) ('deploy-config-tests-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $script:TestDir -Force | Out-Null
$script:Failures = 0
$script:Cases = 0

function New-TestConfig {
    <# .SYNOPSIS Writes a copy of the example configuration with changes and returns its path. #>
    [CmdletBinding()]
    param(
        # File name without extension.
        [Parameter(Mandatory = $true)][string]$Name,
        # Environment entries to set (added or replaced).
        [Parameter()][hashtable]$Set = @{},
        # Environment entries to remove.
        [Parameter()][string[]]$Remove = @(),
        # Top-level fields to set, for example @{ maxTaskCount = 1 }.
        [Parameter()][hashtable]$TopLevel = @{}
    )
    $config = ConvertFrom-Json -InputObject (Get-Content -LiteralPath $ExamplePath -Raw -Encoding UTF8)
    $config | Add-Member -NotePropertyName 'region' -NotePropertyValue 'us-east-1' -Force
    $config | Add-Member -NotePropertyName 'accountId' -NotePropertyValue '123456789012' -Force
    foreach ($key in $Remove) {
        $config.environment.PSObject.Properties.Remove($key)
    }
    foreach ($key in $Set.Keys) {
        $config.environment | Add-Member -NotePropertyName $key -NotePropertyValue $Set[$key] -Force
    }
    foreach ($key in $TopLevel.Keys) {
        $config | Add-Member -NotePropertyName $key -NotePropertyValue $TopLevel[$key] -Force
    }
    $path = Join-Path $script:TestDir ($Name + '.json')
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, (ConvertTo-Json -InputObject $config -Depth 10), $encoding)
    return $path
}

function Assert-Config {
    <# .SYNOPSIS Loads one configuration file and records whether the outcome is the expected one. #>
    [CmdletBinding()]
    param(
        # Description printed with the result.
        [Parameter(Mandatory = $true)][string]$Name,
        # Configuration file.
        [Parameter(Mandatory = $true)][string]$Path,
        # Whether the loader should accept the file.
        [Parameter(Mandatory = $true)][bool]$ExpectAccepted,
        # Text the error message must contain when the file is refused.
        [Parameter()][string]$ExpectMessage = '',
        # Pass -SkipStorageCheck to the loader.
        [Parameter()][switch]$SkipStorageCheck
    )
    $script:Cases = $script:Cases + 1
    $accepted = $false
    $message = ''
    try {
        if ($SkipStorageCheck) {
            $null = Get-DeployConfig -ConfigPath $Path -SkipIdentity -SkipStorageCheck
        }
        else {
            $null = Get-DeployConfig -ConfigPath $Path -SkipIdentity
        }
        $accepted = $true
    }
    catch {
        $message = $_.Exception.Message
    }
    $ok = ($accepted -eq $ExpectAccepted)
    if ($ok -and (-not $ExpectAccepted) -and $ExpectMessage) {
        $ok = $message.Contains($ExpectMessage)
    }
    $expected = 'refused'
    if ($ExpectAccepted) {
        $expected = 'accepted'
    }
    $actual = 'refused'
    if ($accepted) {
        $actual = 'accepted'
    }
    if ($ok) {
        Write-Host ('[PASS] ' + $Name + ' (' + $actual + ')') -ForegroundColor Green
    }
    else {
        $script:Failures = $script:Failures + 1
        Write-Host ('[FAIL] ' + $Name + ': expected ' + $expected + ', got ' + $actual) -ForegroundColor Red
        if ($message) {
            Write-Host ('       message: ' + $message)
        }
    }
}

try {
    # ---- the shipped example
    $script:Cases = $script:Cases + 1
    $exampleOk = $false
    $exampleDetail = ''
    try {
        $example = Get-DeployConfig -ConfigPath $ExamplePath -SkipIdentity
        $exampleSave = [string]$example.Environment['SAVE_ENABLED']
        if ($exampleSave -eq 'false') {
            $exampleOk = $true
        }
        else {
            $exampleDetail = 'environment.SAVE_ENABLED is "' + $exampleSave + '", expected "false"'
        }
    }
    catch {
        $exampleDetail = $_.Exception.Message
    }
    if ($exampleOk) {
        Write-Host '[PASS] the shipped example sets SAVE_ENABLED=false and loads' -ForegroundColor Green
    }
    else {
        $script:Failures = $script:Failures + 1
        Write-Host ('[FAIL] the shipped example sets SAVE_ENABLED=false and loads: ' + $exampleDetail) -ForegroundColor Red
    }

    # ---- refused: saving with JSON storage
    Assert-Config -Name 'SAVE_ENABLED unset, TICKET_STORE unset (json), maxTaskCount 2' -ExpectAccepted $false -ExpectMessage 'SAVE_ENABLED' `
        -Path (New-TestConfig -Name 'unset-unset' -Remove @('SAVE_ENABLED', 'TICKET_STORE'))
    Assert-Config -Name 'SAVE_ENABLED unset with one task (Fargate disk is temporary either way)' -ExpectAccepted $false -ExpectMessage 'SAVE_ENABLED' `
        -Path (New-TestConfig -Name 'unset-one-task' -Remove @('SAVE_ENABLED') -TopLevel @{ minTaskCount = 1; maxTaskCount = 1 })
    Assert-Config -Name 'SAVE_ENABLED "true"' -ExpectAccepted $false -ExpectMessage 'SAVE_ENABLED' `
        -Path (New-TestConfig -Name 'true' -Set @{ SAVE_ENABLED = 'true' })
    Assert-Config -Name 'SAVE_ENABLED "true", TICKET_STORE "json"' -ExpectAccepted $false -ExpectMessage 'TICKET_STORE' `
        -Path (New-TestConfig -Name 'true-json' -Set @{ SAVE_ENABLED = 'true'; TICKET_STORE = 'json' })
    Assert-Config -Name 'SAVE_ENABLED " false" (the application rejects the space)' -ExpectAccepted $false -ExpectMessage 'SAVE_ENABLED' `
        -Path (New-TestConfig -Name 'space-false' -Set @{ SAVE_ENABLED = ' false' })
    Assert-Config -Name 'SAVE_ENABLED unset, TICKET_STORE "PostgreSQL" (not a value the application accepts)' -ExpectAccepted $false -ExpectMessage 'TICKET_STORE' `
        -Path (New-TestConfig -Name 'mixedcase-postgres' -Remove @('SAVE_ENABLED') -Set @{ TICKET_STORE = 'PostgreSQL' })
    Assert-Config -Name 'SAVE_ENABLED "false", TICKET_STORE "JSON" (not a value the application accepts)' -ExpectAccepted $false -ExpectMessage 'TICKET_STORE' `
        -Path (New-TestConfig -Name 'false-uppercase-json' -Set @{ SAVE_ENABLED = 'false'; TICKET_STORE = 'JSON' })

    # ---- accepted
    Assert-Config -Name 'SAVE_ENABLED "false", TICKET_STORE "json"' -ExpectAccepted $true `
        -Path (New-TestConfig -Name 'false-json' -Set @{ SAVE_ENABLED = 'false'; TICKET_STORE = 'json' })
    foreach ($spelling in @('False', 'FALSE', '0', 'no', 'off')) {
        Assert-Config -Name ('SAVE_ENABLED "' + $spelling + '" (read as false by the application)') -ExpectAccepted $true `
            -Path (New-TestConfig -Name ('spelling-' + $spelling) -Set @{ SAVE_ENABLED = $spelling })
    }
    Assert-Config -Name 'lowercase name save_enabled "false" (the application ignores case in names)' -ExpectAccepted $true `
        -Path (New-TestConfig -Name 'lower-false' -Remove @('SAVE_ENABLED') -Set @{ save_enabled = 'false' })
    Assert-Config -Name 'SAVE_ENABLED as the JSON boolean false' -ExpectAccepted $true `
        -Path (New-TestConfig -Name 'json-boolean-false' -Set @{ SAVE_ENABLED = $false })
    Assert-Config -Name 'SAVE_ENABLED "true", TICKET_STORE "postgresql", maxTaskCount 2' -ExpectAccepted $true `
        -Path (New-TestConfig -Name 'true-postgresql' -Set @{ SAVE_ENABLED = 'true'; TICKET_STORE = 'postgresql' })
    Assert-Config -Name 'SAVE_ENABLED unset, TICKET_STORE "postgresql"' -ExpectAccepted $true `
        -Path (New-TestConfig -Name 'unset-postgresql' -Remove @('SAVE_ENABLED') -Set @{ TICKET_STORE = 'postgresql' })
    Assert-Config -Name 'unsafe file with -SkipStorageCheck (05-verify, 99-cleanup can still run)' -ExpectAccepted $true -SkipStorageCheck `
        -Path (New-TestConfig -Name 'skip-check' -Remove @('SAVE_ENABLED'))
}
finally {
    Remove-Item -LiteralPath $script:TestDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host ($script:Cases.ToString() + ' cases, ' + $script:Failures + ' failed.')
if ($script:Failures -gt 0) {
    exit 1
}
exit 0
