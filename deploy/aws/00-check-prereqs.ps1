<#
.SYNOPSIS
    Checks that this machine and the signed-in AWS account are ready for the deployment. Changes nothing.

.DESCRIPTION
    Read-only. Checks, in order:
      1. Windows PowerShell version.
      2. AWS CLI v2 is installed and new enough for the Express Mode commands.
      3. Docker is installed, the engine is running and it runs Linux containers.
      4. git (optional; used only to add a commit hash to the image tag).
      5. The Dockerfile exists at the repository root.
      6. deploy.config.json (or the example file) loads and validates.
      7. "aws sts get-caller-identity" works, and which account and Region will be used.
      8. The Region has a default VPC with at least two default subnets in two Availability
         Zones. Express Mode uses the default VPC unless you supply subnets.

    Exit code 0 when every required check passes, 1 otherwise.

.PARAMETER ConfigPath
    Path of the configuration file. Default: deploy.config.json next to this script, falling
    back to deploy.config.example.json.

.PARAMETER MinimumAwsCliVersion
    Lowest acceptable AWS CLI v2 version. Default 2.36.44, the first version with the
    --cpu-architecture option of "aws ecs create-express-gateway-service".

.PARAMETER DryRun
    Print the commands this script would run, and run none of them. Alias: -WhatIf.

.EXAMPLE
    .\00-check-prereqs.ps1

.EXAMPLE
    .\00-check-prereqs.ps1 -DryRun

.NOTES
    Reviewed against AWS documentation; never executed against a live AWS account.
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string]$ConfigPath = '',

    [Parameter()]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$MinimumAwsCliVersion = '2.36.44',

    [Parameter()]
    [Alias('WhatIf')]
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Get-DeployConfig.ps1')

$checkResults = New-Object System.Collections.Generic.List[object]

function Add-CheckResult {
    <# .SYNOPSIS Records and prints the outcome of one check. #>
    [CmdletBinding()]
    param(
        # Short name of the check.
        [Parameter(Mandatory = $true)][string]$Name,
        # 'PASS', 'FAIL', 'WARN' or 'SKIP'.
        [Parameter(Mandatory = $true)][ValidateSet('PASS', 'FAIL', 'WARN', 'SKIP')][string]$Outcome,
        # What was found, or what to do about it.
        [Parameter()][string]$Detail = ''
    )
    $checkResults.Add([pscustomobject]@{ Name = $Name; Outcome = $Outcome; Detail = $Detail })
    $color = 'Gray'
    if ($Outcome -eq 'PASS') { $color = 'Green' }
    if ($Outcome -eq 'FAIL') { $color = 'Red' }
    if ($Outcome -eq 'WARN') { $color = 'Yellow' }
    Write-Host ('   [' + $Outcome + '] ' + $Name + ': ' + $Detail) -ForegroundColor $color
}

function Show-NativeCommand {
    <# .SYNOPSIS Prints a command the way the other scripts do. #>
    [CmdletBinding()]
    param(
        # Program name.
        [Parameter(Mandatory = $true)][string]$FilePath,
        # Arguments in order.
        [Parameter(Mandatory = $true)][string[]]$ArgumentList
    )
    $prefix = '   > '
    if ($DryRun) {
        $prefix = '   [dry-run] '
    }
    Write-Host ($prefix + (Format-CommandLine -FilePath $FilePath -ArgumentList $ArgumentList)) -ForegroundColor DarkGray
}

try {
    Write-Step 'Prerequisite check (read-only)'
    if ($DryRun) {
        Write-Note 'DRY RUN: commands are printed and not executed.'
    }

    # ---- 1. PowerShell
    Write-Step '1. Windows PowerShell'
    $psVersion = $PSVersionTable.PSVersion
    if ($psVersion -ge [version]'5.1') {
        Add-CheckResult -Name 'PowerShell' -Outcome 'PASS' -Detail ('version ' + $psVersion.ToString())
    }
    else {
        Add-CheckResult -Name 'PowerShell' -Outcome 'FAIL' -Detail ('version ' + $psVersion.ToString() + ' found; 5.1 or later is required.')
    }

    # ---- 2. AWS CLI
    Write-Step '2. AWS CLI v2'
    $awsAvailable = $false
    $awsVersionArguments = @('--version')
    Show-NativeCommand -FilePath 'aws' -ArgumentList $awsVersionArguments
    if ($DryRun) {
        Add-CheckResult -Name 'AWS CLI' -Outcome 'SKIP' -Detail ('dry run; requires ' + $MinimumAwsCliVersion + ' or later.')
    }
    elseif (-not (Test-ToolAvailable -Name 'aws')) {
        Add-CheckResult -Name 'AWS CLI' -Outcome 'FAIL' -Detail 'not found on PATH. Install AWS CLI v2 (DEPLOY_AWS.md, section 1), then open a new PowerShell window.'
    }
    else {
        $awsVersionResult = Invoke-NativeCaptured -FilePath 'aws' -ArgumentList $awsVersionArguments
        $awsVersionText = ($awsVersionResult.StdOut + ' ' + $awsVersionResult.StdErr)
        $versionMatch = [regex]::Match($awsVersionText, 'aws-cli/(\d+\.\d+\.\d+)')
        if ($awsVersionResult.ExitCode -ne 0 -or (-not $versionMatch.Success)) {
            Add-CheckResult -Name 'AWS CLI' -Outcome 'FAIL' -Detail ('could not read the version (exit code ' + $awsVersionResult.ExitCode + '): ' + $awsVersionText.Trim())
        }
        else {
            $awsVersion = [version]$versionMatch.Groups[1].Value
            if ($awsVersion.Major -ne 2) {
                Add-CheckResult -Name 'AWS CLI' -Outcome 'FAIL' -Detail ('version ' + $awsVersion.ToString() + ' found; AWS CLI version 2 is required.')
            }
            elseif ($awsVersion -lt [version]$MinimumAwsCliVersion) {
                Add-CheckResult -Name 'AWS CLI' -Outcome 'FAIL' -Detail ('version ' + $awsVersion.ToString() + ' found; ' + $MinimumAwsCliVersion + ' or later is required. Update the AWS CLI.')
            }
            else {
                $awsAvailable = $true
                Add-CheckResult -Name 'AWS CLI' -Outcome 'PASS' -Detail ('version ' + $awsVersion.ToString() + ' (minimum ' + $MinimumAwsCliVersion + ')')
            }
        }
    }

    # ---- 3. Docker
    Write-Step '3. Docker'
    $dockerInfoArguments = @('info', '--format', '{{.OSType}}/{{.Architecture}}')
    Show-NativeCommand -FilePath 'docker' -ArgumentList $dockerInfoArguments
    if ($DryRun) {
        Add-CheckResult -Name 'Docker' -Outcome 'SKIP' -Detail 'dry run.'
    }
    elseif (-not (Test-ToolAvailable -Name 'docker')) {
        Add-CheckResult -Name 'Docker' -Outcome 'FAIL' -Detail 'not found on PATH. Install Docker Desktop and start it.'
    }
    else {
        $dockerInfo = Invoke-NativeCaptured -FilePath 'docker' -ArgumentList $dockerInfoArguments
        $engine = $dockerInfo.StdOut.Trim()
        if ($dockerInfo.ExitCode -ne 0) {
            Add-CheckResult -Name 'Docker' -Outcome 'FAIL' -Detail ('the engine is not reachable (exit code ' + $dockerInfo.ExitCode + '). Start Docker Desktop. ' + $dockerInfo.StdErr)
        }
        elseif (-not $engine.StartsWith('linux/')) {
            Add-CheckResult -Name 'Docker' -Outcome 'FAIL' -Detail ('engine reports "' + $engine + '". Switch Docker Desktop to Linux containers; the image must be linux/amd64.')
        }
        else {
            Add-CheckResult -Name 'Docker' -Outcome 'PASS' -Detail ('engine running, ' + $engine)
        }
    }

    # ---- 4. git (optional)
    Write-Step '4. git (optional)'
    if (Test-ToolAvailable -Name 'git') {
        Add-CheckResult -Name 'git' -Outcome 'PASS' -Detail 'found; the image tag will include the short commit hash when the repository has a commit.'
    }
    else {
        Add-CheckResult -Name 'git' -Outcome 'WARN' -Detail 'not found; the image tag will be a UTC timestamp only.'
    }

    # ---- 5 and 6. Configuration and Dockerfile
    Write-Step '5. Configuration file'
    $fileConfig = $null
    try {
        $fileConfig = Get-DeployConfig -ConfigPath $ConfigPath -SkipIdentity
        Add-CheckResult -Name 'Configuration' -Outcome 'PASS' -Detail ('valid: ' + $fileConfig.ConfigPath)
    }
    catch {
        Add-CheckResult -Name 'Configuration' -Outcome 'FAIL' -Detail $_.Exception.Message
    }

    Write-Step '6. Dockerfile'
    if ($null -ne $fileConfig) {
        $dockerfile = $fileConfig.DockerfilePath
        if (-not [System.IO.Path]::IsPathRooted($dockerfile)) {
            $dockerfile = Join-Path $script:RepoRoot $dockerfile
        }
        if (Test-Path -LiteralPath $dockerfile) {
            Add-CheckResult -Name 'Dockerfile' -Outcome 'PASS' -Detail $dockerfile
        }
        else {
            Add-CheckResult -Name 'Dockerfile' -Outcome 'WARN' -Detail ('not found at ' + $dockerfile + '. 02-build-and-push.ps1 needs it.')
        }
    }
    else {
        Add-CheckResult -Name 'Dockerfile' -Outcome 'SKIP' -Detail 'the configuration did not load.'
    }

    # ---- 7. Identity and Region
    Write-Step '7. AWS identity and Region'
    $config = $null
    if ($DryRun) {
        if ($null -ne $fileConfig) {
            $config = Get-DeployConfig -ConfigPath $ConfigPath -DryRun
        }
        Add-CheckResult -Name 'AWS identity' -Outcome 'SKIP' -Detail 'dry run.'
    }
    elseif (-not $awsAvailable) {
        Add-CheckResult -Name 'AWS identity' -Outcome 'SKIP' -Detail 'the AWS CLI check did not pass.'
    }
    elseif ($null -eq $fileConfig) {
        Add-CheckResult -Name 'AWS identity' -Outcome 'SKIP' -Detail 'the configuration did not load.'
    }
    else {
        try {
            $config = Get-DeployConfig -ConfigPath $ConfigPath
            Add-CheckResult -Name 'AWS identity' -Outcome 'PASS' -Detail ('account ' + $config.AccountId + ', signed in as ' + $config.CallerArn)
            Add-CheckResult -Name 'AWS Region' -Outcome 'PASS' -Detail $config.Region
        }
        catch {
            $config = $null
            Add-CheckResult -Name 'AWS identity' -Outcome 'FAIL' -Detail ($_.Exception.Message + ' If you use IAM Identity Center, run: aws sso login --profile <name>')
        }
    }

    # ---- 8. Default VPC
    Write-Step '8. Default VPC in the Region'
    if ($null -eq $config) {
        Add-CheckResult -Name 'Default VPC' -Outcome 'SKIP' -Detail 'no signed-in account to query.'
    }
    else {
        $vpcResult = Invoke-Aws -Config $config -Arguments @('ec2', 'describe-vpcs', '--filters', 'Name=is-default,Values=true') -AllowFailure -DryRun:$DryRun
        if ($vpcResult.Skipped) {
            $null = Invoke-Aws -Config $config -Arguments @('ec2', 'describe-subnets', '--filters', 'Name=vpc-id,Values=<DEFAULT_VPC_ID>') -DryRun
            Add-CheckResult -Name 'Default VPC' -Outcome 'SKIP' -Detail 'dry run.'
        }
        elseif ($vpcResult.ExitCode -ne 0) {
            Add-CheckResult -Name 'Default VPC' -Outcome 'WARN' -Detail ('could not be checked: ' + $vpcResult.StdErr)
        }
        else {
            $vpcs = ConvertTo-Array $vpcResult.Json.Vpcs
            if ($vpcs.Count -eq 0) {
                Add-CheckResult -Name 'Default VPC' -Outcome 'FAIL' -Detail ('Region ' + $config.Region + ' has no default VPC. Create one (aws ec2 create-default-vpc) or choose another Region; see DEPLOY_AWS.md, section 2.')
            }
            else {
                $vpcId = [string]$vpcs[0].VpcId
                $subnetResult = Invoke-Aws -Config $config -Arguments @('ec2', 'describe-subnets', '--filters', ('Name=vpc-id,Values=' + $vpcId)) -AllowFailure
                if ($subnetResult.ExitCode -ne 0) {
                    Add-CheckResult -Name 'Default VPC' -Outcome 'WARN' -Detail ('found ' + $vpcId + ' but its subnets could not be listed: ' + $subnetResult.StdErr)
                }
                else {
                    $usableSubnets = 0
                    $zones = New-Object System.Collections.Generic.List[string]
                    foreach ($subnet in (ConvertTo-Array $subnetResult.Json.Subnets)) {
                        $isPublicDefault = ($subnet.DefaultForAz -eq $true) -or ($subnet.MapPublicIpOnLaunch -eq $true)
                        if ($isPublicDefault -and ([int]$subnet.AvailableIpAddressCount -ge 8)) {
                            $usableSubnets = $usableSubnets + 1
                            if (-not $zones.Contains([string]$subnet.AvailabilityZone)) {
                                $zones.Add([string]$subnet.AvailabilityZone)
                            }
                        }
                    }
                    if ($usableSubnets -ge 2 -and $zones.Count -ge 2) {
                        Add-CheckResult -Name 'Default VPC' -Outcome 'PASS' -Detail ($vpcId + ' with ' + $usableSubnets + ' default subnets in ' + $zones.Count + ' Availability Zones')
                    }
                    else {
                        Add-CheckResult -Name 'Default VPC' -Outcome 'FAIL' -Detail ($vpcId + ' has ' + $usableSubnets + ' usable default subnets in ' + $zones.Count + ' Availability Zones; Express Mode needs at least 2 public subnets in 2 zones with 8 free addresses each.')
                    }
                }
            }
        }
    }

    # ---- Summary
    Write-Step 'Summary'
    $failed = 0
    $warned = 0
    foreach ($entry in $checkResults) {
        if ($entry.Outcome -eq 'FAIL') { $failed = $failed + 1 }
        if ($entry.Outcome -eq 'WARN') { $warned = $warned + 1 }
    }
    Write-Info ($checkResults.Count.ToString() + ' checks, ' + $failed + ' failed, ' + $warned + ' warnings.')
    if ($DryRun) {
        Write-Info 'Dry run finished. Nothing was executed.'
        exit 0
    }
    if ($failed -gt 0) {
        Write-Host '   Fix the failed checks and run this script again.' -ForegroundColor Red
        exit 1
    }
    Write-Info 'Ready. Next step: .\01-create-ecr.ps1   (add -DryRun to preview)'
    exit 0
}
catch {
    Write-Host ''
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
