<#
.SYNOPSIS
    Creates the private Amazon ECR repository for the application image. Safe to run again.

.DESCRIPTION
    Steps:
      1. "aws ecr describe-repositories" to see whether the repository already exists.
      2. If not, "aws ecr create-repository" with:
           - image tag immutability (IMMUTABLE), so a pushed tag can never be overwritten;
           - scan on push for basic vulnerability scanning (when ecrScanOnPush is true);
           - AES256 encryption at rest;
           - the Project and ManagedBy tags.
      3. If ecrApplyLifecyclePolicy is true, "aws ecr put-lifecycle-policy" with
         ecr/lifecycle-policy.json (expire untagged images after 7 days, keep the 10 newest).

    An Amazon ECR repository is private unless a repository policy grants wider access.
    This script sets no repository policy.

    JSON parameters are written to deploy/aws/.generated and passed as file:// documents.

.PARAMETER ConfigPath
    Path of the configuration file. Default: deploy.config.json next to this script.

.PARAMETER DryRun
    Print the AWS commands without running them. Alias: -WhatIf.

.EXAMPLE
    .\01-create-ecr.ps1 -DryRun

.EXAMPLE
    .\01-create-ecr.ps1

.NOTES
    Reviewed against AWS documentation; never executed against a live AWS account.
    The repository-level scanOnPush setting is documented as "being deprecated" in favour
    of the registry-level scanning configuration. It is still accepted by the CLI reference
    used for this review. Set ecrScanOnPush to false to leave it out.
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string]$ConfigPath = '',

    [Parameter()]
    [Alias('WhatIf')]
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Get-DeployConfig.ps1')

try {
    Write-Step 'Create the private Amazon ECR repository'
    if (-not $DryRun) {
        Assert-ToolAvailable -Name 'aws' -Hint 'Run .\00-check-prereqs.ps1 first.'
    }
    $config = Get-DeployConfig -ConfigPath $ConfigPath -DryRun:$DryRun
    Show-DeployTarget -Config $config -DryRun:$DryRun
    $repositoryName = $config.EcrRepositoryName

    Write-Step ('1. Does repository "' + $repositoryName + '" exist?')
    $describe = Invoke-Aws -Config $config -Arguments @('ecr', 'describe-repositories', '--repository-names', $repositoryName) -AllowFailure -DryRun:$DryRun
    $repository = $null
    $mustCreate = $false
    if ($describe.Skipped) {
        $mustCreate = $true
    }
    elseif ($describe.ExitCode -eq 0) {
        $repository = (ConvertTo-Array $describe.Json.repositories)[0]
        Write-Info ('yes: ' + $repository.repositoryUri)
    }
    elseif ($describe.StdErr -match 'RepositoryNotFoundException') {
        Write-Info 'no; it will be created.'
        $mustCreate = $true
    }
    else {
        throw ('Could not check the repository: ' + $describe.StdErr)
    }

    if ($mustCreate) {
        Write-Step '2. Create the repository'
        $createArguments = @(
            'ecr', 'create-repository',
            '--repository-name', $repositoryName,
            '--image-tag-mutability', 'IMMUTABLE'
        )
        if ($config.EcrScanOnPush) {
            $scanUri = Save-GeneratedJson -Name 'ecr-image-scanning-configuration.json' -Object ([ordered]@{ scanOnPush = $true })
            $createArguments += @('--image-scanning-configuration', $scanUri)
        }
        $encryptionUri = Save-GeneratedJson -Name 'ecr-encryption-configuration.json' -Object ([ordered]@{ encryptionType = 'AES256' })
        $tagsUri = Save-GeneratedJson -Name 'ecr-tags.json' -Object (Get-TagList -Config $config)
        $createArguments += @('--encryption-configuration', $encryptionUri, '--tags', $tagsUri)
        $created = Invoke-Aws -Config $config -Arguments $createArguments -DryRun:$DryRun
        if (-not $created.Skipped) {
            $repository = $created.Json.repository
            Write-Info ('created: ' + $repository.repositoryUri)
        }
    }
    else {
        Write-Step '2. Existing repository settings'
        Write-Info ('image tag mutability : ' + $repository.imageTagMutability)
        Write-Info ('scan on push         : ' + $repository.imageScanningConfiguration.scanOnPush)
        if ([string]$repository.imageTagMutability -ne 'IMMUTABLE') {
            Write-Note 'Tags in this repository can be overwritten. To make them immutable run:'
            Write-Note ('  aws ecr put-image-tag-mutability --repository-name ' + $repositoryName + ' --image-tag-mutability IMMUTABLE --region ' + $config.Region)
        }
    }

    Write-Step '3. Lifecycle policy'
    if ($config.EcrApplyLifecyclePolicy) {
        $policyPath = Join-Path (Join-Path $script:DeployAwsRoot 'ecr') 'lifecycle-policy.json'
        if (-not (Test-Path -LiteralPath $policyPath)) {
            throw ('Lifecycle policy file not found: ' + $policyPath)
        }
        $null = Invoke-Aws -Config $config -Arguments @(
            'ecr', 'put-lifecycle-policy',
            '--repository-name', $repositoryName,
            '--lifecycle-policy-text', (Get-FileUri -Path $policyPath)
        ) -DryRun:$DryRun
        Write-Info 'policy: untagged images expire after 7 days; only the 10 newest images are kept.'
        Write-Note 'An image that the service still runs must stay among the 10 newest, or new tasks cannot pull it.'
    }
    else {
        Write-Info 'skipped (ecrApplyLifecyclePolicy is false). Old images keep costing storage until you delete them.'
    }

    if ($DryRun) {
        Write-Step 'Dry run finished. Nothing was executed.'
        exit 0
    }

    Write-State -Name 'ecr' -Object ([ordered]@{
            accountId      = $config.AccountId
            region         = $config.Region
            repositoryName = $repositoryName
            repositoryUri  = [string]$repository.repositoryUri
            repositoryArn  = [string]$repository.repositoryArn
        })

    Write-Step 'Done'
    Write-Info ('repository URI: ' + $repository.repositoryUri)
    Write-Info ('confirm with  : aws ecr describe-repositories --repository-names ' + $repositoryName + ' --region ' + $config.Region)
    Write-Info 'Next step: .\02-build-and-push.ps1   (add -DryRun to preview)'
    exit 0
}
catch {
    Write-Host ''
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
