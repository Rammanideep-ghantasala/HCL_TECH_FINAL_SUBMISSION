<#
.SYNOPSIS
    Returns the service to an earlier image, or stops a deployment that is still in progress.

.DESCRIPTION
    Two modes.

    -Mode PreviousImage (default)
        Use this when the bad deployment has already finished. The script finds the image
        that ran before (from deploy/aws/.state/deploy-history.json, or the -ImageUri you
        pass) and deploys it again with "aws ecs update-express-gateway-service". This is an
        ordinary canary deployment of the older image, so it takes several minutes.
        Because images are referenced by digest and tags are immutable, the older image is
        exactly the one that ran before.

    -Mode StopInProgress
        Use this while a deployment is still PENDING or IN_PROGRESS. The script finds it with
        "aws ecs list-service-deployments" and runs
        "aws ecs stop-service-deployment --stop-type ROLLBACK", which returns the service to
        the previous service revision.
        VERIFY BEFORE RUNNING: this command is documented for Amazon ECS service deployments
        in general. The Express Mode pages do not mention it explicitly.

    Amazon ECS also rolls a failing Express Mode deployment back by itself when its error
    alarm fires during the canary phase. Check the deployment status before rolling back by hand.

.PARAMETER ConfigPath
    Path of the configuration file. Default: deploy.config.json next to this script.

.PARAMETER Mode
    PreviousImage or StopInProgress. Default: PreviousImage.

.PARAMETER ImageUri
    PreviousImage mode: the image to return to, for example <repository-uri>@sha256:<digest>.
    Default: the most recent different image in the deployment history.

.PARAMETER NoWait
    Return as soon as the request is accepted.

.PARAMETER Force
    Do not ask for confirmation.

.PARAMETER DryRun
    Print the AWS commands without running them. Alias: -WhatIf.

.EXAMPLE
    .\07-rollback.ps1 -DryRun

.EXAMPLE
    .\07-rollback.ps1 -Mode StopInProgress

.EXAMPLE
    .\07-rollback.ps1 -ImageUri 123456789012.dkr.ecr.eu-west-1.amazonaws.com/ticket-similarity@sha256:<digest>

.NOTES
    Reviewed against AWS documentation; never executed against a live AWS account.
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string]$ConfigPath = '',

    [Parameter()]
    [ValidateSet('PreviousImage', 'StopInProgress')]
    [string]$Mode = 'PreviousImage',

    [Parameter()]
    [string]$ImageUri = '',

    [Parameter()]
    [switch]$NoWait,

    [Parameter()]
    [switch]$Force,

    [Parameter()]
    [Alias('WhatIf')]
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Get-DeployConfig.ps1')

try {
    Write-Step ('Roll back the Express Mode service (mode: ' + $Mode + ')')
    if (-not $DryRun) {
        Assert-ToolAvailable -Name 'aws' -Hint 'Run .\00-check-prereqs.ps1 first.'
    }
    # PreviousImage sends the environment block from the configuration, so the ticket storage
    # rule applies. StopInProgress only stops a deployment and must work during an incident.
    $config = Get-DeployConfig -ConfigPath $ConfigPath -DryRun:$DryRun -SkipStorageCheck:($Mode -eq 'StopInProgress')
    Show-DeployTarget -Config $config -DryRun:$DryRun
    $serviceArn = Get-TargetServiceArn -Config $config

    Write-Step '1. Current state of the service'
    $service = Get-ExpressService -Config $config -ServiceArn $serviceArn -DryRun:$DryRun
    $currentImage = ''
    if (-not $DryRun) {
        if ($null -eq $service) {
            throw ('The service was not found: ' + $serviceArn + '. ' + $script:LastAwsError)
        }
        $current = Get-ExpressCurrentConfiguration $service
        if ($null -ne $current) {
            $currentImage = [string]$current.primaryContainer.image
        }
        Write-Info ('status        : ' + $service.status.statusCode)
        Write-Info ('newest image  : ' + $currentImage)
    }

    Write-Step '2. Deployments'
    $deployments = Get-ServiceDeployments -Config $config -ServiceArn $serviceArn -DryRun:$DryRun
    $inProgress = $null
    foreach ($deployment in $deployments) {
        Write-Info (([string]$deployment.status).PadRight(22) + ' created ' + $deployment.createdAt + '  ' + $deployment.serviceDeploymentArn)
        if (@('PENDING', 'IN_PROGRESS') -contains [string]$deployment.status) {
            if ($null -eq $inProgress -or ((Get-SortableTime $deployment.createdAt) -gt (Get-SortableTime $inProgress.createdAt))) {
                $inProgress = $deployment
            }
        }
    }

    if ($Mode -eq 'StopInProgress') {
        Write-Step '3. Stop the deployment in progress and roll back'
        $deploymentArn = '<SERVICE_DEPLOYMENT_ARN>'
        if (-not $DryRun) {
            if ($null -eq $inProgress) {
                throw 'No deployment is PENDING or IN_PROGRESS. Nothing to stop. To return to an earlier image run: .\07-rollback.ps1 -Mode PreviousImage'
            }
            $deploymentArn = [string]$inProgress.serviceDeploymentArn
            Write-Info ('deployment to stop: ' + $deploymentArn)
            if (-not $Force) {
                $answer = Read-Host '   Type yes to stop this deployment and roll back'
                if ($answer -ne 'yes') {
                    Write-Info 'Cancelled. Nothing was changed.'
                    exit 1
                }
            }
        }
        $null = Invoke-Aws -Config $config -Arguments @(
            'ecs', 'stop-service-deployment',
            '--service-deployment-arn', $deploymentArn,
            '--stop-type', 'ROLLBACK'
        ) -DryRun:$DryRun

        if ($DryRun) {
            $null = Invoke-Aws -Config $config -Arguments @('ecs', 'describe-service-deployments', '--service-deployment-arns', $deploymentArn) -DryRun
            Write-Step 'Dry run finished. Nothing was executed.'
            exit 0
        }
        Add-DeployHistory -Config $config -Action 'rollback' -ImageUri $currentImage -Result 'requested' -Detail ('stop-service-deployment ROLLBACK ' + $deploymentArn)
        if ($NoWait) {
            Write-Step 'Done (not waiting)'
            Write-Info ('check with: aws ecs describe-service-deployments --service-deployment-arns ' + $deploymentArn + ' --region ' + $config.Region)
            exit 0
        }

        Write-Step '4. Wait for the rollback to finish'
        $deadline = (Get-Date).AddMinutes($config.DeployTimeoutMinutes)
        $finalStatus = ''
        $finalReason = ''
        while ((Get-Date) -lt $deadline) {
            $described = Invoke-Aws -Config $config -Arguments @('ecs', 'describe-service-deployments', '--service-deployment-arns', $deploymentArn) -AllowFailure -Quiet
            $status = 'unknown'
            if ($described.ExitCode -eq 0 -and $null -ne $described.Json) {
                $entry = (ConvertTo-Array $described.Json.serviceDeployments)[0]
                if ($null -ne $entry) {
                    $status = [string]$entry.status
                    $finalReason = [string]$entry.statusReason
                }
            }
            Write-Info ((Get-Date).ToString('HH:mm:ss') + '  deployment=' + $status)
            if (@('ROLLBACK_SUCCESSFUL', 'ROLLBACK_FAILED', 'STOPPED', 'SUCCESSFUL') -contains $status) {
                $finalStatus = $status
                break
            }
            Start-Sleep -Seconds 20
        }
        if ($finalStatus -ne 'ROLLBACK_SUCCESSFUL') {
            if (-not $finalStatus) {
                $finalStatus = 'timed out'
            }
            throw ('The rollback did not finish successfully: ' + $finalStatus + '. ' + $finalReason)
        }
        Write-Step 'Done'
        Write-Info 'The deployment was stopped and the service is back on the previous revision.'
        Write-Info 'Next step: .\05-verify.ps1'
        exit 0
    }

    # ---- Mode PreviousImage
    Write-Step '3. Image to return to'
    if ((-not $DryRun) -and ($null -ne $inProgress)) {
        Write-Note ('A deployment is still ' + $inProgress.status + '. Consider: .\07-rollback.ps1 -Mode StopInProgress')
    }
    $target = $ImageUri
    if (-not $target) {
        $history = ConvertTo-Array (Read-State -Name 'deploy-history')
        for ($index = $history.Count - 1; $index -ge 0; $index--) {
            $entry = $history[$index]
            if ([string]$entry.accountId -ne [string]$config.AccountId) { continue }
            if ([string]$entry.region -ne [string]$config.Region) { continue }
            if ([string]$entry.serviceName -ne [string]$config.ServiceName) { continue }
            if ([string]$entry.result -ne 'succeeded') { continue }
            if (-not $entry.imageUri) { continue }
            if ([string]$entry.imageUri -eq $currentImage) { continue }
            $target = [string]$entry.imageUri
            Write-Info ('from history  : ' + $entry.timestampUtc + ' ' + $entry.action)
            break
        }
    }
    if (-not $target) {
        if ($DryRun) {
            $target = $config.RepositoryUri + '@sha256:<PREVIOUS_IMAGE_DIGEST>'
        }
        else {
            throw ('No earlier image is recorded in ' + (Get-StateFilePath -Name 'deploy-history') + '. List the images with "aws ecr describe-images --repository-name ' + $config.EcrRepositoryName + ' --region ' + $config.Region + '" and pass one with -ImageUri <repository-uri>@sha256:<digest>.')
        }
    }
    else {
        $problem = Test-ImageUri -Config $config -ImageUri $target
        if ($problem) {
            throw ($problem + ' Value: ' + $target)
        }
    }
    Write-Info ('newest image  : ' + $currentImage)
    Write-Info ('return to     : ' + $target)
    if ((-not $DryRun) -and ($target -eq $currentImage)) {
        throw 'The service already runs this image. Nothing to roll back.'
    }
    if ((-not $DryRun) -and (-not $Force)) {
        $answer = Read-Host '   Type yes to deploy the earlier image'
        if ($answer -ne 'yes') {
            Write-Info 'Cancelled. Nothing was changed.'
            exit 1
        }
    }

    Write-Step '4. Deploy the earlier image'
    $outcome = Invoke-ExpressUpdate -Config $config -ServiceArn $serviceArn -ImageUri $target -Action 'rollback' -DryRun:$DryRun -NoWait:$NoWait
    if ($DryRun) {
        Write-Step 'Dry run finished. Nothing was executed.'
        exit 0
    }
    if ($null -eq $outcome) {
        Write-Step 'Done (not waiting)'
        exit 0
    }
    Write-Step 'Done'
    Write-Info ('deployment : ' + $outcome.Status + ' ' + $outcome.DeploymentArn)
    Write-Info ('HTTPS URL  : ' + $outcome.Endpoint)
    Write-Info 'Next step: .\05-verify.ps1'
    exit 0
}
catch {
    Write-Host ''
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
