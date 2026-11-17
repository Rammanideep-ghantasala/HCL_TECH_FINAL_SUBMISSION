<#
.SYNOPSIS
    Deploys a new image (and the current settings in deploy.config.json) to the existing Express Mode service.

.DESCRIPTION
    Steps:
      1. Resolve the image (the -ImageUri argument, or the image pushed by 02-build-and-push.ps1).
      2. "aws ecs describe-express-gateway-service" to confirm the service is ACTIVE and to
         show the image it runs now.
      3. "aws ecs update-express-gateway-service" with --primary-container, --health-check-path,
         --cpu, --memory and --scaling-target. JSON parameters go through file:// documents.
      4. Wait for the deployment. Express Mode always uses a canary deployment: new tasks
         start next to the old ones, 5% of traffic moves to them, and after the bake time
         the rest follows. Amazon ECS rolls the deployment back by itself when its error
         alarm fires. The wait therefore takes several minutes even when all is well.

    The image deployed before this update is written to deploy/aws/.state/deploy-history.json,
    which 07-rollback.ps1 uses.

.PARAMETER ConfigPath
    Path of the configuration file. Default: deploy.config.json next to this script.

.PARAMETER ImageUri
    Image reference to deploy, for example <repository-uri>@sha256:<digest>.
    Default: the image recorded by 02-build-and-push.ps1.

.PARAMETER NoWait
    Return as soon as the update call is accepted.

.PARAMETER DryRun
    Print the AWS commands without running them. Alias: -WhatIf.

.EXAMPLE
    .\02-build-and-push.ps1
    .\06-update-service.ps1

.EXAMPLE
    .\06-update-service.ps1 -DryRun

.NOTES
    Reviewed against AWS documentation; never executed against a live AWS account.
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
    [Alias('WhatIf')]
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Get-DeployConfig.ps1')

try {
    Write-Step 'Update the Express Mode service'
    if (-not $DryRun) {
        Assert-ToolAvailable -Name 'aws' -Hint 'Run .\00-check-prereqs.ps1 first.'
    }
    $config = Get-DeployConfig -ConfigPath $ConfigPath -DryRun:$DryRun
    Show-DeployTarget -Config $config -DryRun:$DryRun
    $serviceArn = Get-TargetServiceArn -Config $config

    Write-Step '1. Image to deploy'
    $image = Resolve-ImageUri -Config $config -ImageUri $ImageUri -DryRun:$DryRun
    Write-Info $image

    Write-Step '2. Current state of the service'
    $service = Get-ExpressService -Config $config -ServiceArn $serviceArn -DryRun:$DryRun
    $previousImage = ''
    if (-not $DryRun) {
        if ($null -eq $service) {
            throw ('The service was not found: ' + $serviceArn + '. Create it with .\04-deploy-express-service.ps1. ' + $script:LastAwsError)
        }
        $serviceStatus = [string]$service.status.statusCode
        if ($serviceStatus -ne 'ACTIVE') {
            throw ('The service status is ' + $serviceStatus + ', not ACTIVE. It cannot be updated now.')
        }
        $current = Get-ExpressCurrentConfiguration $service
        if ($null -ne $current) {
            $previousImage = [string]$current.primaryContainer.image
        }
        Write-Info ('status        : ' + $serviceStatus)
        Write-Info ('running image : ' + $previousImage)
        if ($previousImage -eq $image) {
            Write-Note 'The service already runs this image. The update is sent anyway so that changed settings in deploy.config.json are applied.'
        }
        elseif ($previousImage) {
            Add-DeployHistory -Config $config -Action 'previous' -ImageUri $previousImage -Result 'succeeded' -Detail 'image that was running before an update'
        }
    }

    Write-Step '3. Update the service'
    Write-Info ('task size : cpu ' + $config.Cpu + ' units, memory ' + $config.Memory + ' MiB')
    Write-Info ('scaling   : ' + $config.MinTaskCount + ' to ' + $config.MaxTaskCount + ' tasks')
    $outcome = Invoke-ExpressUpdate -Config $config -ServiceArn $serviceArn -ImageUri $image -Action 'update' -DryRun:$DryRun -NoWait:$NoWait

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
    if ($previousImage -and ($previousImage -ne $image)) {
        Write-Info ('to go back : .\07-rollback.ps1 -ImageUri ' + $previousImage)
    }
    Write-Info 'Next step: .\05-verify.ps1'
    exit 0
}
catch {
    Write-Host ''
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '   If a deployment is still running you can stop it and return to the previous revision with: .\07-rollback.ps1 -Mode StopInProgress' -ForegroundColor Yellow
    exit 1
}
