<#
.SYNOPSIS
    Creates the two IAM roles Amazon ECS Express Mode requires. Idempotent: safe to run again.

.DESCRIPTION
    Role 1, task execution role
      trusted service : ecs-tasks.amazonaws.com        (iam/task-execution-trust-policy.json)
      managed policy  : arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
      used by         : the ECS agent, to pull the image from Amazon ECR and write container
                        logs to CloudWatch Logs.

    Role 2, infrastructure role
      trusted service : ecs.amazonaws.com              (iam/infrastructure-trust-policy.json)
      managed policy  : arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices
      used by         : Amazon ECS, to create, update and delete the load balancer, target
                        groups, security groups, certificate, auto scaling, alarm and log
                        group of the Express Mode service.

    No task role is created. The application calls no AWS API, so it needs none.

    For each role the script:
      1. runs "aws iam get-role";
      2. creates the role with "aws iam create-role" when it does not exist, or checks that
         the existing role trusts the expected service and stops if it does not (an
         existing role is never modified apart from attaching the managed policy);
      3. runs "aws iam attach-role-policy" (attaching an already attached policy is a no-op).

    Your own identity additionally needs iam:PassRole on both roles to create the service.

.PARAMETER ConfigPath
    Path of the configuration file. Default: deploy.config.json next to this script.

.PARAMETER DryRun
    Print the AWS commands without running them. Alias: -WhatIf.

.EXAMPLE
    .\03-create-iam-roles.ps1 -DryRun

.EXAMPLE
    .\03-create-iam-roles.ps1

.NOTES
    Reviewed against AWS documentation; never executed against a live AWS account.
    IAM changes can take about a minute to propagate. 04-deploy-express-service.ps1 retries
    when Amazon ECS cannot yet assume a new role.
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

function Confirm-IamRole {
    <#
    .SYNOPSIS
        Ensures one IAM role exists, trusts the expected service and has the managed policy attached.
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config,
        # Role name.
        [Parameter(Mandatory = $true)][string]$RoleName,
        # Service principal that must be trusted, for example ecs-tasks.amazonaws.com.
        [Parameter(Mandatory = $true)][string]$ServicePrincipal,
        # Trust policy file under deploy/aws/iam.
        [Parameter(Mandatory = $true)][string]$TrustPolicyFile,
        # ARN of the AWS managed policy to attach.
        [Parameter(Mandatory = $true)][string]$ManagedPolicyArn,
        # Role description. Plain ASCII, no quotation marks.
        [Parameter(Mandatory = $true)][string]$Description,
        # file:// URI of the tag list.
        [Parameter(Mandatory = $true)][string]$TagsUri,
        # Print the commands without running them.
        [Parameter()][switch]$DryRun
    )
    $trustPolicyPath = Join-Path (Join-Path $script:DeployAwsRoot 'iam') $TrustPolicyFile
    if (-not (Test-Path -LiteralPath $trustPolicyPath)) {
        throw ('Trust policy file not found: ' + $trustPolicyPath)
    }

    $existing = Invoke-Aws -Config $Config -Arguments @('iam', 'get-role', '--role-name', $RoleName) -AllowFailure -DryRun:$DryRun
    $mustCreate = $false
    if ($existing.Skipped) {
        $mustCreate = $true
    }
    elseif ($existing.ExitCode -eq 0) {
        Write-Info ('role exists: ' + $existing.Json.Role.Arn)
        $trusted = $false
        foreach ($statement in (ConvertTo-Array $existing.Json.Role.AssumeRolePolicyDocument.Statement)) {
            if ([string]$statement.Effect -ne 'Allow') {
                continue
            }
            foreach ($service in (ConvertTo-Array $statement.Principal.Service)) {
                if ([string]$service -eq $ServicePrincipal) {
                    $trusted = $true
                }
            }
        }
        if (-not $trusted) {
            throw ('Role "' + $RoleName + '" exists but does not trust ' + $ServicePrincipal + '. Choose another role name in deploy.config.json; this script does not change the trust policy of an existing role.')
        }
        Write-Info ('trust policy allows ' + $ServicePrincipal)
    }
    elseif ($existing.StdErr -match 'NoSuchEntity') {
        Write-Info 'role does not exist; it will be created.'
        $mustCreate = $true
    }
    else {
        throw ('Could not check role "' + $RoleName + '": ' + $existing.StdErr)
    }

    if ($mustCreate) {
        $null = Invoke-Aws -Config $Config -Arguments @(
            'iam', 'create-role',
            '--role-name', $RoleName,
            '--assume-role-policy-document', (Get-FileUri -Path $trustPolicyPath),
            '--description', $Description,
            '--tags', $TagsUri
        ) -DryRun:$DryRun
    }

    $null = Invoke-Aws -Config $Config -Arguments @(
        'iam', 'attach-role-policy',
        '--role-name', $RoleName,
        '--policy-arn', $ManagedPolicyArn
    ) -DryRun:$DryRun

    if (-not $DryRun) {
        $attached = Invoke-Aws -Config $Config -Arguments @('iam', 'list-attached-role-policies', '--role-name', $RoleName)
        $found = $false
        foreach ($policy in (ConvertTo-Array $attached.Json.AttachedPolicies)) {
            if ([string]$policy.PolicyArn -eq $ManagedPolicyArn) {
                $found = $true
            }
        }
        if (-not $found) {
            throw ('The managed policy ' + $ManagedPolicyArn + ' is not attached to role "' + $RoleName + '".')
        }
        Write-Info ('managed policy attached: ' + $ManagedPolicyArn)
    }
}

try {
    Write-Step 'Create the IAM roles for Amazon ECS Express Mode'
    if (-not $DryRun) {
        Assert-ToolAvailable -Name 'aws' -Hint 'Run .\00-check-prereqs.ps1 first.'
    }
    $config = Get-DeployConfig -ConfigPath $ConfigPath -DryRun:$DryRun
    Show-DeployTarget -Config $config -DryRun:$DryRun

    $tagsUri = Save-GeneratedJson -Name 'iam-tags.json' -Object (Get-TagList -Config $config)

    Write-Step ('1. Task execution role: ' + $config.TaskExecutionRoleName)
    $executionRole = @{
        Config           = $config
        RoleName         = $config.TaskExecutionRoleName
        ServicePrincipal = 'ecs-tasks.amazonaws.com'
        TrustPolicyFile  = 'task-execution-trust-policy.json'
        ManagedPolicyArn = $config.TaskExecutionPolicyArn
        Description      = 'ECS task execution role - pull the image from ECR and write container logs'
        TagsUri          = $tagsUri
        DryRun           = $DryRun
    }
    Confirm-IamRole @executionRole

    Write-Step ('2. Infrastructure role: ' + $config.InfrastructureRoleName)
    $infrastructureRole = @{
        Config           = $config
        RoleName         = $config.InfrastructureRoleName
        ServicePrincipal = 'ecs.amazonaws.com'
        TrustPolicyFile  = 'infrastructure-trust-policy.json'
        ManagedPolicyArn = $config.InfrastructurePolicyArn
        Description      = 'ECS infrastructure role for Express Mode - load balancer, security groups, certificate, scaling'
        TagsUri          = $tagsUri
        DryRun           = $DryRun
    }
    Confirm-IamRole @infrastructureRole

    if ($DryRun) {
        Write-Step 'Dry run finished. Nothing was executed.'
        exit 0
    }

    Write-State -Name 'iam-roles' -Object ([ordered]@{
            accountId             = $config.AccountId
            region                = $config.Region
            taskExecutionRoleArn  = $config.TaskExecutionRoleArn
            infrastructureRoleArn = $config.InfrastructureRoleArn
        })

    Write-Step 'Done'
    Write-Info ('task execution role : ' + $config.TaskExecutionRoleArn)
    Write-Info ('infrastructure role : ' + $config.InfrastructureRoleArn)
    Write-Info 'no task role was created (the application calls no AWS API).'
    Write-Note 'New IAM roles can take about a minute to become usable.'
    Write-Info 'Next step: .\04-deploy-express-service.ps1   (add -DryRun to preview)'
    exit 0
}
catch {
    Write-Host ''
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
