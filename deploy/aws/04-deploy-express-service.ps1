<#
.SYNOPSIS
    Creates the Amazon ECS Express Mode service (first deployment) and waits until it serves traffic.

.DESCRIPTION
    Runs "aws ecs create-express-gateway-service" once. Express Mode then creates, in your
    account: an ECS service on Fargate, a task definition, an internet-facing Application
    Load Balancer with an HTTPS listener and an ACM certificate, target groups, two security
    groups, an auto scaling target and policy, a CloudWatch alarm and a log group. These
    resources cost money for as long as they exist. See DEPLOY_AWS.md, sections 9 and 10.

    Steps:
      1. Resolve the image (the -ImageUri argument, or the image pushed by 02-build-and-push.ps1).
      2. Check both IAM roles exist ("aws iam get-role").
      3. Check the service does not already exist ("aws ecs describe-express-gateway-service").
         When it does, this script stops: use 06-update-service.ps1 instead.
      4. Write the JSON parameter documents to deploy/aws/.generated.
      5. Ask for confirmation, then create the service. When Amazon ECS cannot yet assume a
         newly created IAM role the call is retried after 60 seconds, up to 3 times.
      6. Wait for the first deployment and for GET <healthCheckPath> to answer 200.
      7. Set the retention of the log group (logRetentionDays).
      8. Print the HTTPS URL.

    Parameters sent (values from deploy.config.json):
      --service-name, --cluster (omitted when it is "default"), --execution-role-arn,
      --infrastructure-role-arn, --primary-container file://..., --health-check-path,
      --cpu, --memory, --cpu-architecture, --scaling-target file://..., --tags file://...
    No --task-role-arn is sent. No --network-configuration is sent, so the default VPC and
    its public subnets are used.

.PARAMETER ConfigPath
    Path of the configuration file. Default: deploy.config.json next to this script.

.PARAMETER ImageUri
    Image reference to deploy, for example <repository-uri>@sha256:<digest>.
    Default: the image recorded by 02-build-and-push.ps1.

.PARAMETER NoWait
    Return as soon as the create call is accepted.

.PARAMETER Yes
    Do not ask for confirmation before creating billable resources.

.PARAMETER DryRun
    Print the AWS commands without running them. Alias: -WhatIf.

.EXAMPLE
    .\04-deploy-express-service.ps1 -DryRun

.EXAMPLE
    .\04-deploy-express-service.ps1

.NOTES
    Reviewed against AWS documentation; never executed against a live AWS account.
    VERIFY BEFORE RUNNING: the unit of --cpu and --memory. The CLI and API references say
    CPU units and MiB (1024 and 2048 here); one AWS tutorial shows "--cpu 2 --memory 4".
    After creation this script prints the values Amazon ECS reports so you can compare.
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string]$ConfigPath = '',

    [Parameter()]
    [string]$ImageUri = '',

    [Parameter()]
    [switch]$NoWait,

    [Parameter()]
    [switch]$Yes,

    [Parameter()]
    [Alias('WhatIf')]
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Get-DeployConfig.ps1')

try {
    Write-Step 'Create the Amazon ECS Express Mode service'
    if (-not $DryRun) {
        Assert-ToolAvailable -Name 'aws' -Hint 'Run .\00-check-prereqs.ps1 first.'
    }
    $config = Get-DeployConfig -ConfigPath $ConfigPath -DryRun:$DryRun
    Show-DeployTarget -Config $config -DryRun:$DryRun

    # ---- 1. image
    Write-Step '1. Image to deploy'
    $image = Resolve-ImageUri -Config $config -ImageUri $ImageUri -DryRun:$DryRun
    Write-Info $image

    # ---- 2. roles
    Write-Step '2. IAM roles'
    foreach ($roleName in @($config.TaskExecutionRoleName, $config.InfrastructureRoleName)) {
        $role = Invoke-Aws -Config $config -Arguments @('iam', 'get-role', '--role-name', $roleName) -AllowFailure -DryRun:$DryRun
        if ((-not $role.Skipped) -and ($role.ExitCode -ne 0)) {
            throw ('IAM role "' + $roleName + '" is not available. Run .\03-create-iam-roles.ps1 first. ' + $role.StdErr)
        }
    }
    Write-Info ('execution role      : ' + $config.TaskExecutionRoleArn)
    Write-Info ('infrastructure role : ' + $config.InfrastructureRoleArn)

    # ---- 3. existing service
    Write-Step '3. Does the service already exist?'
    $serviceArn = $config.ServiceArn
    $existing = Get-ExpressService -Config $config -ServiceArn $serviceArn -DryRun:$DryRun
    if ($null -ne $existing) {
        $existingStatus = [string]$existing.status.statusCode
        if ($existingStatus -ne 'INACTIVE') {
            throw ('Service "' + $config.ServiceName + '" already exists with status ' + $existingStatus + '. To deploy a new image run .\06-update-service.ps1. To start over run .\99-cleanup.ps1 first.')
        }
        Write-Note 'A deleted (INACTIVE) service with this name exists. Creating a new one with the same name.'
    }
    elseif (-not $DryRun) {
        Write-Info 'no (the describe call returned no service).'
        if ($script:LastAwsError) {
            Write-Info ('CLI message: ' + $script:LastAwsError)
        }
    }

    # ---- 4. parameter documents
    Write-Step '4. Parameter documents'
    $containerUri = Save-GeneratedJson -Name 'primary-container.json' -Object (New-PrimaryContainerDocument -Config $config -ImageUri $image)
    $scalingUri = Save-GeneratedJson -Name 'scaling-target.json' -Object (New-ScalingTargetDocument -Config $config)
    $tagsUri = Save-GeneratedJson -Name 'service-tags.json' -Object (Get-TagList -Config $config -LowerCaseKeys)

    $createArguments = @('ecs', 'create-express-gateway-service', '--service-name', $config.ServiceName)
    if ($config.ClusterName -ne 'default') {
        $createArguments += @('--cluster', $config.ClusterName)
    }
    $createArguments += @(
        '--execution-role-arn', $config.TaskExecutionRoleArn,
        '--infrastructure-role-arn', $config.InfrastructureRoleArn,
        '--primary-container', $containerUri,
        '--health-check-path', $config.HealthCheckPath,
        '--cpu', $config.Cpu,
        '--memory', $config.Memory,
        '--cpu-architecture', $config.CpuArchitecture,
        '--scaling-target', $scalingUri,
        '--tags', $tagsUri
    )

    # ---- 5. create
    Write-Step '5. Create the service'
    Write-Info ('service name   : ' + $config.ServiceName)
    Write-Info ('task size      : cpu ' + $config.Cpu + ' units, memory ' + $config.Memory + ' MiB, ' + $config.CpuArchitecture)
    Write-Info ('container port : ' + $config.ContainerPort + ', health check path ' + $config.HealthCheckPath)
    Write-Info ('scaling        : ' + $config.MinTaskCount + ' to ' + $config.MaxTaskCount + ' tasks on ' + $config.AutoScalingMetric + ' at ' + $config.AutoScalingTargetValue)
    Write-Info ('environment    : ' + ((@($config.Environment.Keys)) -join ', '))
    Write-Note 'This creates billable resources (Fargate task, Application Load Balancer, public IPv4 addresses, logs) that run until you delete them with .\99-cleanup.ps1.'

    if ((-not $DryRun) -and (-not $Yes)) {
        $answer = Read-Host '   Type yes to create the service'
        if ($answer -ne 'yes') {
            Write-Info 'Cancelled. Nothing was created.'
            exit 1
        }
    }

    $created = $null
    $attempt = 0
    while ($true) {
        $attempt = $attempt + 1
        $created = Invoke-Aws -Config $config -Arguments $createArguments -AllowFailure -DryRun:$DryRun
        if ($created.Skipped) {
            break
        }
        if ($created.ExitCode -eq 0) {
            break
        }
        if (($created.StdErr -match 'assume') -and ($attempt -lt 4)) {
            Write-Note ('Amazon ECS could not assume a role yet (attempt ' + $attempt + ' of 4): ' + $created.StdErr)
            Write-Info 'New IAM roles need about a minute to propagate. Waiting 60 seconds before retrying.'
            Start-Sleep -Seconds 60
            continue
        }
        throw ('aws ecs create-express-gateway-service failed with exit code ' + $created.ExitCode + ': ' + $created.StdErr)
    }

    if ($DryRun) {
        Write-Step '6. Wait for the deployment (dry run)'
        $null = Get-ExpressService -Config $config -ServiceArn $serviceArn -DryRun
        $null = Get-ServiceDeployments -Config $config -ServiceArn $serviceArn -DryRun
        Write-Step '7. Log retention (dry run)'
        $null = Get-ExpressManagedResources -Config $config -DryRun
        if ($config.LogRetentionDays -gt 0) {
            $null = Invoke-Aws -Config $config -Arguments @('logs', 'put-retention-policy', '--log-group-name', '<LOG_GROUP_NAME>', '--retention-in-days', [string]$config.LogRetentionDays) -DryRun
        }
        Write-Step 'Dry run finished. Nothing was executed.'
        exit 0
    }

    $createdService = $created.Json.service
    if ($null -ne $createdService -and $createdService.serviceArn) {
        $serviceArn = [string]$createdService.serviceArn
    }
    Write-Info ('service ARN: ' + $serviceArn)
    if ($serviceArn -ne $config.ServiceArn) {
        Write-Note ('The ARN returned by AWS differs from the expected ' + $config.ServiceArn + '. The returned ARN is stored and used from now on.')
    }
    Write-State -Name 'service' -Object ([ordered]@{
            accountId    = $config.AccountId
            region       = $config.Region
            serviceName  = $config.ServiceName
            clusterName  = $config.ClusterName
            serviceArn   = $serviceArn
            createdAtUtc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
        })
    Add-DeployHistory -Config $config -Action 'create' -ImageUri $image -Result 'requested'

    if ($NoWait) {
        Write-Step 'Done (not waiting)'
        Write-Info ('follow progress: aws ecs monitor-express-gateway-service --service-arn ' + $serviceArn + ' --mode TEXT-ONLY --region ' + $config.Region)
        Write-Info 'then run        : .\05-verify.ps1'
        exit 0
    }

    # ---- 6. wait
    Write-Step '6. Wait for the first deployment'
    Write-Info 'Provisioning the load balancer and certificate, pulling the image and loading the search indexes takes several minutes.'
    try {
        $outcome = Wait-ExpressDeployment -Config $config -ServiceArn $serviceArn -TimeoutMinutes $config.DeployTimeoutMinutes -AcceptReadyEndpoint
    }
    catch {
        Add-DeployHistory -Config $config -Action 'create' -ImageUri $image -Result 'failed' -Detail $_.Exception.Message
        throw
    }
    Add-DeployHistory -Config $config -Action 'create' -ImageUri $image -Result 'succeeded' -Detail $outcome.Status

    # ---- 7. what ECS reports, and log retention
    Write-Step '7. Configuration reported by Amazon ECS'
    $service = Get-ExpressService -Config $config -ServiceArn $serviceArn
    $current = Get-ExpressCurrentConfiguration $service
    if ($null -ne $current) {
        Write-Info ('cpu / memory      : ' + $current.cpu + ' / ' + $current.memory + '   (requested ' + $config.Cpu + ' / ' + $config.Memory + ')')
        Write-Info ('cpu architecture  : ' + $current.cpuArchitecture)
        Write-Info ('health check path : ' + $current.healthCheckPath)
        Write-Info ('tasks (min-max)   : ' + $current.scalingTarget.minTaskCount + '-' + $current.scalingTarget.maxTaskCount)
        Write-Info ('image             : ' + $current.primaryContainer.image)
        if (([string]$current.cpu -ne $config.Cpu) -or ([string]$current.memory -ne $config.Memory)) {
            Write-Note 'The reported CPU or memory differs from the requested values. Check the task size in the ECS console before relying on it.'
        }
    }
    $logGroupNames = New-Object System.Collections.Generic.List[string]
    foreach ($resource in (Get-ExpressManagedResources -Config $config -Service $service)) {
        if ($resource.Type -eq 'log-group' -and $resource.Name) {
            $logGroupNames.Add([string]$resource.Name)
        }
    }
    foreach ($logGroupName in $logGroupNames) {
        Write-Info ('log group         : ' + $logGroupName)
        if ($config.LogRetentionDays -gt 0) {
            $retention = Invoke-Aws -Config $config -Arguments @('logs', 'put-retention-policy', '--log-group-name', $logGroupName, '--retention-in-days', [string]$config.LogRetentionDays) -AllowFailure
            if ($retention.ExitCode -eq 0) {
                Write-Info ('log retention set to ' + $config.LogRetentionDays + ' days.')
            }
            else {
                Write-Note ('Could not set log retention (the logs are kept forever until you change it): ' + $retention.StdErr)
            }
        }
    }
    if ($logGroupNames.Count -eq 0) {
        Write-Note 'The log group name could not be read. Find it in the ECS console (service, Resources tab); it is kept forever until you set a retention period.'
    }

    # ---- 8. URL
    $endpoint = $outcome.Endpoint
    if (-not $endpoint) {
        $endpoint = Get-ExpressEndpoint $service
    }
    Write-Step 'Done'
    Write-Info ('service ARN : ' + $serviceArn)
    Write-Info ('HTTPS URL   : ' + $endpoint)
    Write-Info 'Next step: .\05-verify.ps1'
    Write-Note 'The service keeps running and costing money until you run .\99-cleanup.ps1.'
    exit 0
}
catch {
    Write-Host ''
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
