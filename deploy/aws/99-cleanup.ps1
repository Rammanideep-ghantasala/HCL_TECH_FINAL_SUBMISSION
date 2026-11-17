<#
.SYNOPSIS
    Deletes everything this deployment created, in a safe order, and reports what is left.

.DESCRIPTION
    Order (it matters: Amazon ECS needs the infrastructure role while it deletes the service,
    so the IAM roles go last and only when the service is confirmed gone):

      1. Inventory: the Express Mode service, the resources Amazon ECS manages for it
         ("aws ecs describe-service-revisions"), the Amazon ECR repository and the IAM roles.
         The inventory is saved to deploy/aws/.state/managed-resources.json.
      2. Confirmation: you type the service name. (-Force skips this. -DryRun never asks.)
      3. "aws ecs delete-express-gateway-service", then wait until the service is gone.
         Express Mode deletes its own load balancer (when no other service shares it),
         listener rule, target groups, security groups, certificate, auto scaling target and
         policy, and alarm.
      4. Check every inventoried resource and wait up to -LeftoverWaitMinutes for them to disappear.
      5. Delete the CloudWatch log group. Express Mode keeps it when the service is deleted.
         (-KeepLogs leaves it.)
      6. "aws ecr delete-repository --force": the repository and every image in it.
         (-KeepEcrRepository leaves it.)
      7. Detach the managed policies and delete the two IAM roles, but only roles that carry
         the ManagedBy=deploy-aws-scripts tag, and only when steps 3 and 4 found nothing left.
         (-KeepIamRoles leaves them.)
      8. Report the ECS cluster. With -DeleteEmptyCluster it is deleted when it has no
         services and no tasks.
      9. Report anything still tagged AmazonECSManaged=true in the Region, the service-linked
         roles (never deleted here: they are shared and free) and the commands to confirm
         each item yourself.

    Nothing outside this list is touched. The default VPC is used, not created, and stays.

.PARAMETER ConfigPath
    Path of the configuration file. Default: deploy.config.json next to this script.

.PARAMETER KeepEcrRepository
    Do not delete the Amazon ECR repository and its images.

.PARAMETER KeepIamRoles
    Do not delete the IAM roles.

.PARAMETER KeepLogs
    Do not delete the CloudWatch log group.

.PARAMETER DeleteEmptyCluster
    Delete the ECS cluster when it has no services and no running or pending tasks.

.PARAMETER DeleteRolesAnyway
    Delete the IAM roles even when leftover resources were found. Use only after you have
    confirmed the leftovers belong to something else.

.PARAMETER TimeoutMinutes
    Minutes to wait for the service to be deleted. Default 30.

.PARAMETER LeftoverWaitMinutes
    Minutes to wait for the managed resources to disappear after the service is gone. Default 10.

.PARAMETER Force
    Do not ask for confirmation.

.PARAMETER DryRun
    Print the AWS commands without running them. Nothing is deleted. Alias: -WhatIf.

.EXAMPLE
    .\99-cleanup.ps1 -WhatIf

.EXAMPLE
    .\99-cleanup.ps1

.EXAMPLE
    .\99-cleanup.ps1 -KeepEcrRepository -KeepIamRoles

.NOTES
    Reviewed against AWS documentation; never executed against a live AWS account.
    Deleting is permanent. Read the printed plan before you confirm.
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string]$ConfigPath = '',

    [Parameter()]
    [switch]$KeepEcrRepository,

    [Parameter()]
    [switch]$KeepIamRoles,

    [Parameter()]
    [switch]$KeepLogs,

    [Parameter()]
    [switch]$DeleteEmptyCluster,

    [Parameter()]
    [switch]$DeleteRolesAnyway,

    [Parameter()]
    [ValidateRange(1, 240)]
    [int]$TimeoutMinutes = 30,

    [Parameter()]
    [ValidateRange(0, 120)]
    [int]$LeftoverWaitMinutes = 10,

    [Parameter()]
    [switch]$Force,

    [Parameter()]
    [Alias('WhatIf')]
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Get-DeployConfig.ps1')

function Get-ServicePresence {
    <#
    .SYNOPSIS
        Returns ACTIVE, DRAINING, INACTIVE, NOTFOUND or UNKNOWN for the Express Mode service.
    .DESCRIPTION
        UNKNOWN means the describe call failed for a reason other than "not found" (for
        example expired credentials). UNKNOWN is never treated as "deleted".
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config,
        # ARN of the Express Mode service.
        [Parameter(Mandatory = $true)][string]$ServiceArn,
        # Do not print the command.
        [Parameter()][switch]$Quiet
    )
    $result = Invoke-Aws -Config $Config -Arguments @('ecs', 'describe-express-gateway-service', '--service-arn', $ServiceArn) -AllowFailure -Quiet:$Quiet
    if ($result.ExitCode -eq 0) {
        $status = ''
        if ($null -ne $result.Json -and $null -ne $result.Json.service) {
            $status = [string]$result.Json.service.status.statusCode
        }
        if ($status) {
            return $status
        }
        return 'UNKNOWN'
    }
    if (Test-AwsNotFound -Result $result) {
        return 'NOTFOUND'
    }
    Write-Note ('describe-express-gateway-service failed: ' + $result.StdErr)
    return 'UNKNOWN'
}

function Test-ManagedResource {
    <#
    .SYNOPSIS
        Checks whether one inventoried resource still exists. Returns exists, gone or unknown.
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config,
        # Inventory entry with Type, Arn and Name.
        [Parameter(Mandatory = $true)][psobject]$Resource,
        # Print the command without running it.
        [Parameter()][switch]$DryRun,
        # Do not print the command.
        [Parameter()][switch]$Quiet
    )
    $arn = [string]$Resource.Arn
    $name = [string]$Resource.Name
    $scalingResourceId = 'service/' + $Config.ClusterName + '/' + $Config.ServiceName
    $arguments = $null
    $listProperty = ''
    switch ([string]$Resource.Type) {
        'load-balancer' {
            $arguments = @('elbv2', 'describe-load-balancers', '--load-balancer-arns', $arn)
            $listProperty = 'LoadBalancers'
        }
        'listener' {
            $arguments = @('elbv2', 'describe-listeners', '--listener-arns', $arn)
            $listProperty = 'Listeners'
        }
        'listener-rule' {
            $arguments = @('elbv2', 'describe-rules', '--rule-arns', $arn)
            $listProperty = 'Rules'
        }
        'target-group' {
            $arguments = @('elbv2', 'describe-target-groups', '--target-group-arns', $arn)
            $listProperty = 'TargetGroups'
        }
        'security-group' {
            $groupId = ($arn -split '/')[-1]
            $arguments = @('ec2', 'describe-security-groups', '--group-ids', $groupId)
            $listProperty = 'SecurityGroups'
        }
        'certificate' {
            $arguments = @('acm', 'describe-certificate', '--certificate-arn', $arn)
            $listProperty = 'Certificate'
        }
        'metric-alarm' {
            $alarmName = ($arn -split ':alarm:')[-1]
            $arguments = @('cloudwatch', 'describe-alarms', '--alarm-names', $alarmName)
            $listProperty = 'MetricAlarms'
        }
        'scalable-target' {
            $arguments = @('application-autoscaling', 'describe-scalable-targets', '--service-namespace', 'ecs', '--resource-ids', $scalingResourceId)
            $listProperty = 'ScalableTargets'
        }
        'scaling-policy' {
            $arguments = @('application-autoscaling', 'describe-scaling-policies', '--service-namespace', 'ecs', '--resource-id', $scalingResourceId)
            $listProperty = 'ScalingPolicies'
        }
        'log-group' {
            $arguments = @('logs', 'describe-log-groups', '--log-group-name-prefix', $name)
            $listProperty = 'logGroups'
        }
        'task-definition' {
            $arguments = @('ecs', 'describe-task-definition', '--task-definition', $arn)
            $listProperty = 'taskDefinition'
        }
        default {
            return 'unknown'
        }
    }
    $result = Invoke-Aws -Config $Config -Arguments $arguments -AllowFailure -DryRun:$DryRun -Quiet:$Quiet
    if ($result.Skipped) {
        return 'unknown'
    }
    if ($result.ExitCode -ne 0) {
        if (Test-AwsNotFound -Result $result) {
            return 'gone'
        }
        return 'unknown'
    }
    if ($null -eq $result.Json) {
        return 'unknown'
    }
    $value = $result.Json.PSObject.Properties[$listProperty]
    if ($null -eq $value) {
        return 'gone'
    }
    if ([string]$Resource.Type -eq 'log-group') {
        foreach ($logGroup in (ConvertTo-Array $value.Value)) {
            if ([string]$logGroup.logGroupName -eq $name) {
                return 'exists'
            }
        }
        return 'gone'
    }
    if ([string]$Resource.Type -eq 'task-definition') {
        if ([string]$value.Value.status -eq 'ACTIVE') {
            return 'exists'
        }
        return 'gone'
    }
    if ((ConvertTo-Array $value.Value).Count -gt 0) {
        return 'exists'
    }
    return 'gone'
}

function Remove-DeployRole {
    <#
    .SYNOPSIS
        Detaches the managed policies of one IAM role and deletes it, when the role was created by these scripts.
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config,
        # Role name.
        [Parameter(Mandatory = $true)][string]$RoleName,
        # Managed policy the scripts attached.
        [Parameter(Mandatory = $true)][string]$ManagedPolicyArn,
        # Print the commands without running them.
        [Parameter()][switch]$DryRun
    )
    $role = Invoke-Aws -Config $Config -Arguments @('iam', 'get-role', '--role-name', $RoleName) -AllowFailure -DryRun:$DryRun
    if ($role.Skipped) {
        $null = Invoke-Aws -Config $Config -Arguments @('iam', 'list-attached-role-policies', '--role-name', $RoleName) -DryRun
        $null = Invoke-Aws -Config $Config -Arguments @('iam', 'list-role-policies', '--role-name', $RoleName) -DryRun
        $null = Invoke-Aws -Config $Config -Arguments @('iam', 'detach-role-policy', '--role-name', $RoleName, '--policy-arn', $ManagedPolicyArn) -DryRun
        $null = Invoke-Aws -Config $Config -Arguments @('iam', 'delete-role', '--role-name', $RoleName) -DryRun
        return
    }
    if ($role.ExitCode -ne 0) {
        if ($role.StdErr -match 'NoSuchEntity') {
            Write-Info ('role "' + $RoleName + '" does not exist (already gone).')
            return
        }
        Write-Note ('could not read role "' + $RoleName + '": ' + $role.StdErr)
        return
    }
    $ownedByScripts = $false
    foreach ($tag in (ConvertTo-Array $role.Json.Role.Tags)) {
        if (([string]$tag.Key -eq 'ManagedBy') -and ([string]$tag.Value -eq $script:ManagedByTagValue)) {
            $ownedByScripts = $true
        }
    }
    if (-not $ownedByScripts) {
        Write-Note ('role "' + $RoleName + '" has no ManagedBy=' + $script:ManagedByTagValue + ' tag, so these scripts did not create it. It is left in place.')
        return
    }
    $inline = Invoke-Aws -Config $Config -Arguments @('iam', 'list-role-policies', '--role-name', $RoleName)
    if ((ConvertTo-Array $inline.Json.PolicyNames).Count -gt 0) {
        Write-Note ('role "' + $RoleName + '" has inline policies that these scripts did not add. It is left in place.')
        return
    }
    $attached = Invoke-Aws -Config $Config -Arguments @('iam', 'list-attached-role-policies', '--role-name', $RoleName)
    foreach ($policy in (ConvertTo-Array $attached.Json.AttachedPolicies)) {
        $null = Invoke-Aws -Config $Config -Arguments @('iam', 'detach-role-policy', '--role-name', $RoleName, '--policy-arn', [string]$policy.PolicyArn)
    }
    $null = Invoke-Aws -Config $Config -Arguments @('iam', 'delete-role', '--role-name', $RoleName)
    Write-Info ('deleted role "' + $RoleName + '".')
}

try {
    Write-Step 'Clean up the AWS deployment'
    if (-not $DryRun) {
        Assert-ToolAvailable -Name 'aws' -Hint 'Run .\00-check-prereqs.ps1 first.'
    }
    # Cleanup deploys nothing, so the ticket storage rule must not stop it from removing a
    # service that was deployed with an older configuration.
    $config = Get-DeployConfig -ConfigPath $ConfigPath -DryRun:$DryRun -SkipStorageCheck
    Show-DeployTarget -Config $config -DryRun:$DryRun
    $serviceArn = Get-TargetServiceArn -Config $config
    $scalingResourceId = 'service/' + $config.ClusterName + '/' + $config.ServiceName

    # ---- 1. inventory
    Write-Step '1. Inventory'
    $presence = 'UNKNOWN'
    $service = $null
    if ($DryRun) {
        $null = Get-ExpressService -Config $config -ServiceArn $serviceArn -DryRun
    }
    else {
        $presence = Get-ServicePresence -Config $config -ServiceArn $serviceArn
        Write-Info ('service ' + $serviceArn + ': ' + $presence)
        if ($presence -eq 'UNKNOWN') {
            throw 'The state of the service could not be determined. Fix the error above (are you signed in?) and run the script again. Nothing was deleted.'
        }
        if ($presence -ne 'NOTFOUND') {
            $service = Get-ExpressService -Config $config -ServiceArn $serviceArn -Quiet
        }
    }

    $inventory = New-Object System.Collections.Generic.List[object]
    $inventoryKeys = New-Object System.Collections.Generic.List[string]
    $liveResources = Get-ExpressManagedResources -Config $config -Service $service -DryRun:$DryRun
    $savedInventory = Read-State -Name 'managed-resources'
    $savedResources = @()
    if (Test-StateMatchesConfig $savedInventory -Config $config) {
        if ([string]$savedInventory.serviceName -eq [string]$config.ServiceName) {
            $savedResources = ConvertTo-Array $savedInventory.resources
        }
    }
    foreach ($resource in (@($liveResources) + @($savedResources))) {
        if ($null -eq $resource) { continue }
        $key = [string]$resource.Type + '|' + [string]$resource.Arn
        if ($inventoryKeys.Contains($key)) { continue }
        $inventoryKeys.Add($key)
        $inventory.Add([pscustomobject]@{ Type = [string]$resource.Type; Arn = [string]$resource.Arn; Name = [string]$resource.Name })
    }
    if ((-not $DryRun) -and ($inventory.Count -gt 0)) {
        Write-State -Name 'managed-resources' -Object ([ordered]@{
                accountId   = $config.AccountId
                region      = $config.Region
                serviceName = $config.ServiceName
                savedAtUtc  = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
                resources   = $inventory.ToArray()
            })
    }
    Write-Info ('resources managed by Express Mode for this service: ' + $inventory.Count)
    foreach ($resource in $inventory) {
        Write-Info ('  ' + ([string]$resource.Type).PadRight(16) + ' ' + $resource.Arn)
    }
    if ((-not $DryRun) -and ($inventory.Count -eq 0) -and ($presence -ne 'NOTFOUND')) {
        Write-Note 'The managed resources could not be listed. They are still deleted with the service; the final report uses the AmazonECSManaged tag instead.'
    }

    # ---- 2. plan and confirmation
    Write-Step '2. Plan'
    Write-Info ('DELETE  Express Mode service   ' + $config.ServiceName + '  (and, through Amazon ECS, its load balancer, listener rule, target groups, security groups, certificate, scaling and alarm)')
    if ($KeepLogs) {
        Write-Info 'KEEP    CloudWatch log group   (-KeepLogs)'
    }
    else {
        Write-Info 'DELETE  CloudWatch log group   of the service, with all application logs'
    }
    if ($KeepEcrRepository) {
        Write-Info ('KEEP    ECR repository         ' + $config.EcrRepositoryName + '  (-KeepEcrRepository)')
    }
    else {
        Write-Info ('DELETE  ECR repository         ' + $config.EcrRepositoryName + '  with every image in it')
    }
    if ($KeepIamRoles) {
        Write-Info 'KEEP    IAM roles              (-KeepIamRoles)'
    }
    else {
        Write-Info ('DELETE  IAM role               ' + $config.TaskExecutionRoleName)
        Write-Info ('DELETE  IAM role               ' + $config.InfrastructureRoleName)
    }
    if ($DeleteEmptyCluster) {
        Write-Info ('DELETE  ECS cluster            ' + $config.ClusterName + '  only if it is empty')
    }
    else {
        Write-Info ('KEEP    ECS cluster            ' + $config.ClusterName + '  (no charge for an empty cluster; -DeleteEmptyCluster removes it)')
    }
    Write-Info 'KEEP    service-linked roles, the default VPC, local Docker images'

    if ((-not $DryRun) -and (-not $Force)) {
        Write-Host ''
        Write-Host ('   This permanently deletes the items marked DELETE in account ' + $config.AccountId + ', Region ' + $config.Region + '.') -ForegroundColor Yellow
        $answer = Read-Host ('   Type the service name (' + $config.ServiceName + ') to continue')
        if ($answer -ne $config.ServiceName) {
            Write-Info 'Cancelled. Nothing was deleted.'
            exit 1
        }
    }

    # ---- 3. delete the service
    Write-Step '3. Delete the Express Mode service'
    $serviceGone = $false
    if ($DryRun) {
        $null = Invoke-Aws -Config $config -Arguments @('ecs', 'delete-express-gateway-service', '--service-arn', $serviceArn) -DryRun
        $null = Get-ExpressService -Config $config -ServiceArn $serviceArn -DryRun
    }
    elseif ($presence -eq 'NOTFOUND' -or $presence -eq 'INACTIVE') {
        Write-Info ('the service is already ' + $presence + '; no delete call is needed.')
        $serviceGone = $true
    }
    else {
        if ($presence -eq 'DRAINING') {
            Write-Info 'the service is already DRAINING (a delete is in progress).'
        }
        else {
            $null = Invoke-Aws -Config $config -Arguments @('ecs', 'delete-express-gateway-service', '--service-arn', $serviceArn)
            Write-Info 'delete accepted. Amazon ECS drains the tasks and removes the infrastructure.'
        }
        Write-Info ('waiting up to ' + $TimeoutMinutes + ' minutes; polling: aws ecs describe-express-gateway-service --service-arn ' + $serviceArn)
        $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 20
            $presence = Get-ServicePresence -Config $config -ServiceArn $serviceArn -Quiet
            Write-Info ((Get-Date).ToString('HH:mm:ss') + '  service=' + $presence)
            if ($presence -eq 'NOTFOUND' -or $presence -eq 'INACTIVE') {
                $serviceGone = $true
                break
            }
        }
        if (-not $serviceGone) {
            throw ('The service was not deleted within ' + $TimeoutMinutes + ' minutes (last state: ' + $presence + '). The IAM roles were NOT deleted, because Amazon ECS still needs the infrastructure role. Run this script again later.')
        }
    }

    # ---- 4. managed resources
    Write-Step '4. Check the resources Express Mode managed'
    $leftovers = New-Object System.Collections.Generic.List[object]
    $logGroupNames = New-Object System.Collections.Generic.List[string]
    $taskDefinitionArns = New-Object System.Collections.Generic.List[string]
    foreach ($resource in $inventory) {
        if ($resource.Type -eq 'log-group') {
            if ($resource.Name -and (-not $logGroupNames.Contains([string]$resource.Name))) {
                $logGroupNames.Add([string]$resource.Name)
            }
        }
        if ($resource.Type -eq 'task-definition') {
            $taskDefinitionArns.Add([string]$resource.Arn)
        }
    }
    if ($DryRun) {
        $samples = @(
            [pscustomobject]@{ Type = 'load-balancer'; Arn = '<LOAD_BALANCER_ARN>'; Name = '' },
            [pscustomobject]@{ Type = 'listener'; Arn = '<LISTENER_ARN>'; Name = '' },
            [pscustomobject]@{ Type = 'listener-rule'; Arn = '<LISTENER_RULE_ARN>'; Name = '' },
            [pscustomobject]@{ Type = 'target-group'; Arn = '<TARGET_GROUP_ARN>'; Name = '' },
            [pscustomobject]@{ Type = 'security-group'; Arn = 'security-group/<SECURITY_GROUP_ID>'; Name = '' },
            [pscustomobject]@{ Type = 'certificate'; Arn = '<CERTIFICATE_ARN>'; Name = '' },
            [pscustomobject]@{ Type = 'metric-alarm'; Arn = ':alarm:<ALARM_NAME>'; Name = '' },
            [pscustomobject]@{ Type = 'scalable-target'; Arn = '<SCALABLE_TARGET_ARN>'; Name = '' },
            [pscustomobject]@{ Type = 'scaling-policy'; Arn = '<SCALING_POLICY_ARN>'; Name = '' },
            [pscustomobject]@{ Type = 'log-group'; Arn = '<LOG_GROUP_ARN>'; Name = '<LOG_GROUP_NAME>' },
            [pscustomobject]@{ Type = 'task-definition'; Arn = '<TASK_DEFINITION_ARN>'; Name = '' }
        )
        foreach ($sample in $samples) {
            $null = Test-ManagedResource -Config $config -Resource $sample -DryRun
        }
    }
    else {
        $leftoverDeadline = (Get-Date).AddMinutes($LeftoverWaitMinutes)
        while ($true) {
            $leftovers.Clear()
            foreach ($resource in $inventory) {
                if ($resource.Type -eq 'log-group' -or $resource.Type -eq 'task-definition') {
                    continue
                }
                $state = Test-ManagedResource -Config $config -Resource $resource -Quiet
                if ($state -ne 'gone') {
                    $leftovers.Add([pscustomobject]@{ Type = $resource.Type; Arn = $resource.Arn; State = $state })
                }
            }
            if ($leftovers.Count -eq 0) {
                break
            }
            if ((Get-Date) -ge $leftoverDeadline) {
                break
            }
            Write-Info ((Get-Date).ToString('HH:mm:ss') + '  ' + $leftovers.Count + ' resource(s) not gone yet; checking again in 30 seconds.')
            Start-Sleep -Seconds 30
        }
        if ($leftovers.Count -eq 0) {
            Write-Info ('all ' + $inventory.Count + ' inventoried resources are gone (log group and task definition are handled below).')
        }
        else {
            Write-Note 'These resources still exist or could not be checked. A load balancer that other Express Mode services share is kept by design.'
            foreach ($leftover in $leftovers) {
                Write-Info ('  ' + ([string]$leftover.State).PadRight(8) + ([string]$leftover.Type).PadRight(16) + ' ' + $leftover.Arn)
            }
        }
    }

    # ---- 5. log group
    Write-Step '5. CloudWatch log group'
    if ($DryRun) {
        if (-not $KeepLogs) {
            $null = Invoke-Aws -Config $config -Arguments @('logs', 'delete-log-group', '--log-group-name', '<LOG_GROUP_NAME>') -DryRun
        }
    }
    elseif ($logGroupNames.Count -eq 0) {
        Write-Note ('No log group name was recorded. Look for one with: aws logs describe-log-groups --log-group-name-prefix /aws/ecs/' + $config.ClusterName + '/' + $config.ServiceName + ' --region ' + $config.Region)
    }
    else {
        foreach ($logGroupName in $logGroupNames) {
            $logResource = [pscustomobject]@{ Type = 'log-group'; Arn = ''; Name = $logGroupName }
            $logState = Test-ManagedResource -Config $config -Resource $logResource
            if ($logState -eq 'gone') {
                Write-Info ('log group ' + $logGroupName + ' is already gone.')
            }
            elseif ($KeepLogs) {
                Write-Info ('log group ' + $logGroupName + ' is kept (-KeepLogs). It costs storage until you delete it or set a retention period.')
            }
            else {
                $null = Invoke-Aws -Config $config -Arguments @('logs', 'delete-log-group', '--log-group-name', $logGroupName)
                Write-Info ('deleted log group ' + $logGroupName + '.')
            }
        }
    }

    # ---- 6. ECR repository
    Write-Step '6. Amazon ECR repository'
    if ($KeepEcrRepository) {
        Write-Info ('kept (-KeepEcrRepository). Stored images cost money until you delete them.')
    }
    else {
        $repositoryCheck = Invoke-Aws -Config $config -Arguments @('ecr', 'describe-repositories', '--repository-names', $config.EcrRepositoryName) -AllowFailure -DryRun:$DryRun
        if ($repositoryCheck.Skipped) {
            $null = Invoke-Aws -Config $config -Arguments @('ecr', 'delete-repository', '--repository-name', $config.EcrRepositoryName, '--force') -DryRun
        }
        elseif ($repositoryCheck.ExitCode -eq 0) {
            $null = Invoke-Aws -Config $config -Arguments @('ecr', 'delete-repository', '--repository-name', $config.EcrRepositoryName, '--force')
            Write-Info ('deleted repository ' + $config.EcrRepositoryName + ' and its images.')
        }
        elseif ($repositoryCheck.StdErr -match 'RepositoryNotFoundException') {
            Write-Info ('repository ' + $config.EcrRepositoryName + ' does not exist (already gone).')
        }
        else {
            Write-Note ('could not check the repository: ' + $repositoryCheck.StdErr)
        }
    }

    # ---- 7. IAM roles
    Write-Step '7. IAM roles'
    if ($KeepIamRoles) {
        Write-Info 'kept (-KeepIamRoles). IAM roles cost nothing.'
    }
    elseif ($DryRun) {
        Remove-DeployRole -Config $config -RoleName $config.TaskExecutionRoleName -ManagedPolicyArn $config.TaskExecutionPolicyArn -DryRun
        Remove-DeployRole -Config $config -RoleName $config.InfrastructureRoleName -ManagedPolicyArn $config.InfrastructurePolicyArn -DryRun
    }
    elseif (-not $serviceGone) {
        Write-Note 'The service is not confirmed gone. The IAM roles are kept.'
    }
    elseif (($leftovers.Count -gt 0) -and (-not $DeleteRolesAnyway)) {
        Write-Note 'Leftover resources were found, so the IAM roles are kept: Amazon ECS may still need the infrastructure role to finish deleting them.'
        Write-Note 'Run this script again in a few minutes, or use -DeleteRolesAnyway once you have confirmed the leftovers belong to something else.'
    }
    else {
        Remove-DeployRole -Config $config -RoleName $config.TaskExecutionRoleName -ManagedPolicyArn $config.TaskExecutionPolicyArn
        Remove-DeployRole -Config $config -RoleName $config.InfrastructureRoleName -ManagedPolicyArn $config.InfrastructurePolicyArn
    }

    # ---- 8. cluster
    Write-Step ('8. ECS cluster "' + $config.ClusterName + '"')
    $clusterResult = Invoke-Aws -Config $config -Arguments @('ecs', 'describe-clusters', '--clusters', $config.ClusterName) -AllowFailure -DryRun:$DryRun
    if ($clusterResult.Skipped) {
        if ($DeleteEmptyCluster) {
            $null = Invoke-Aws -Config $config -Arguments @('ecs', 'delete-cluster', '--cluster', $config.ClusterName) -DryRun
        }
    }
    elseif ($clusterResult.ExitCode -ne 0) {
        Write-Note ('could not describe the cluster: ' + $clusterResult.StdErr)
    }
    else {
        $cluster = (ConvertTo-Array $clusterResult.Json.clusters)[0]
        if ($null -eq $cluster -or [string]$cluster.status -eq 'INACTIVE') {
            Write-Info 'the cluster does not exist (or is INACTIVE).'
        }
        else {
            $busy = [int]$cluster.activeServicesCount + [int]$cluster.runningTasksCount + [int]$cluster.pendingTasksCount
            Write-Info ('status ' + $cluster.status + ', services ' + $cluster.activeServicesCount + ', running tasks ' + $cluster.runningTasksCount + ', pending tasks ' + $cluster.pendingTasksCount)
            if ($DeleteEmptyCluster -and ($busy -eq 0)) {
                $null = Invoke-Aws -Config $config -Arguments @('ecs', 'delete-cluster', '--cluster', $config.ClusterName)
                Write-Info 'deleted the empty cluster.'
            }
            elseif ($DeleteEmptyCluster) {
                Write-Note 'The cluster is not empty, so it is kept. Something else is running in it.'
            }
            else {
                Write-Info 'kept. An empty cluster costs nothing.'
            }
        }
    }

    # ---- 9. final report
    Write-Step '9. Final report'
    Write-Info 'Resources in this Region still tagged AmazonECSManaged=true (other Express Mode services, if you have any, also appear here):'
    $tagged = Invoke-Aws -Config $config -Arguments @('resourcegroupstaggingapi', 'get-resources', '--tag-filters', 'Key=AmazonECSManaged,Values=true') -AllowFailure -DryRun:$DryRun
    if (-not $tagged.Skipped) {
        if ($tagged.ExitCode -ne 0) {
            Write-Note ('the tag query failed: ' + $tagged.StdErr)
        }
        else {
            $taggedResources = ConvertTo-Array $tagged.Json.ResourceTagMappingList
            if ($taggedResources.Count -eq 0) {
                Write-Info '  none.'
            }
            foreach ($taggedResource in $taggedResources) {
                Write-Info ('  ' + $taggedResource.ResourceARN)
            }
            if ($taggedResources.Count -gt 0) {
                Write-Note 'The tag index can list a resource for a while after it was deleted. Confirm each one with the commands in DEPLOY_AWS.md, section 10.'
            }
        }
    }
    foreach ($taskDefinitionArn in $taskDefinitionArns) {
        $taskResource = [pscustomobject]@{ Type = 'task-definition'; Arn = $taskDefinitionArn; Name = '' }
        $taskState = Test-ManagedResource -Config $config -Resource $taskResource -DryRun:$DryRun
        if ($taskState -eq 'exists') {
            Write-Note ('Task definition still ACTIVE (no charge): ' + $taskDefinitionArn)
            Write-Note ('  remove with: aws ecs deregister-task-definition --task-definition ' + $taskDefinitionArn + ' --region ' + $config.Region)
        }
    }
    Write-Info 'Service-linked roles (AWSServiceRoleForECS, AWSServiceRoleForElasticLoadBalancing, AWSServiceRoleForApplicationAutoScaling_ECSService) are not deleted: they are shared by the whole account and cost nothing.'
    Write-Info 'Confirm with:'
    Write-Info ('  aws ecs describe-express-gateway-service --service-arn ' + $serviceArn + ' --region ' + $config.Region)
    Write-Info ('  aws elbv2 describe-load-balancers --region ' + $config.Region)
    Write-Info ('  aws ecr describe-repositories --region ' + $config.Region)
    Write-Info ('  aws application-autoscaling describe-scalable-targets --service-namespace ecs --resource-ids ' + $scalingResourceId + ' --region ' + $config.Region)
    Write-Info ('  aws iam get-role --role-name ' + $config.InfrastructureRoleName)

    if ($DryRun) {
        Write-Step 'Dry run finished. Nothing was deleted.'
        exit 0
    }

    $servicePath = Get-StateFilePath -Name 'service'
    if ($serviceGone -and (Test-Path -LiteralPath $servicePath)) {
        Remove-Item -LiteralPath $servicePath -Force
    }
    if (Test-Path -LiteralPath $script:GeneratedDir) {
        Remove-Item -LiteralPath $script:GeneratedDir -Recurse -Force
    }

    Write-Step 'Done'
    if ($leftovers.Count -gt 0) {
        Write-Host '   Cleanup finished with leftovers. Read the notes above and run the script again.' -ForegroundColor Yellow
        exit 2
    }
    Write-Info 'Cleanup finished. Check the Billing console tomorrow to confirm the charges stopped.'
    exit 0
}
catch {
    Write-Host ''
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
