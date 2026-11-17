<#
.SYNOPSIS
    Retrieves the HTTPS URL of the service and checks /health, /ready and one POST /api/search.

.DESCRIPTION
    Steps:
      1. "aws ecs describe-express-gateway-service" to read the service status, the
         configuration Amazon ECS reports and the public endpoint
         (service.activeConfigurations[].ingressPaths[].endpoint).
      2. GET <url>/health    expects HTTP 200 (liveness).
      3. GET <url>/ready     expects HTTP 200 (readiness; 503 until the indexes are loaded).
      4. POST <url>/api/search with a small JSON body, using Invoke-RestMethod. Only the
         outcome fields are printed (status, message, result counts); ticket text is not.

    The search request is a real request and appears in the application log like any other.
    Exit code 0 when every check passes, 1 otherwise.

.PARAMETER ConfigPath
    Path of the configuration file. Default: deploy.config.json next to this script.

.PARAMETER BaseUrl
    Check this URL instead of asking AWS for the endpoint, for example http://localhost:8080
    to test a local container. With -BaseUrl no AWS command is run.

.PARAMETER Subject
    Subject of the sample ticket sent to POST /api/search.

.PARAMETER Description
    Description of the sample ticket sent to POST /api/search.

.PARAMETER SkipSearch
    Check /health and /ready only.

.PARAMETER DryRun
    Print the AWS commands and HTTP requests without sending them. Alias: -WhatIf.

.EXAMPLE
    .\05-verify.ps1

.EXAMPLE
    .\05-verify.ps1 -BaseUrl http://localhost:8080

.NOTES
    Reviewed against AWS documentation; never executed against a live AWS account.
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string]$ConfigPath = '',

    [Parameter()]
    [ValidatePattern('^$|^https?://')]
    [string]$BaseUrl = '',

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$Subject = 'Cannot sign in after changing my password',

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$Description = 'Since I changed my password this morning the sign-in page shows an error and I cannot reach my account. I have tried two browsers.',

    [Parameter()]
    [switch]$SkipSearch,

    [Parameter()]
    [Alias('WhatIf')]
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Get-DeployConfig.ps1')

$script:FailedChecks = 0

function Test-Endpoint {
    <# .SYNOPSIS Sends one GET request and reports whether the expected status code came back. #>
    [CmdletBinding()]
    param(
        # Full URL.
        [Parameter(Mandatory = $true)][string]$Url,
        # Expected HTTP status code.
        [Parameter()][int]$Expected = 200
    )
    $status = Get-HttpStatus -Url $Url -TimeoutSeconds 30
    if ($status -eq $Expected) {
        Write-Host ('   [PASS] GET ' + $Url + ' -> ' + $status) -ForegroundColor Green
    }
    else {
        $script:FailedChecks = $script:FailedChecks + 1
        $shown = [string]$status
        if ($status -eq 0) {
            $shown = 'no response'
        }
        Write-Host ('   [FAIL] GET ' + $Url + ' -> ' + $shown + ' (expected ' + $Expected + ')') -ForegroundColor Red
    }
}

try {
    Write-Step 'Verify the deployed service'
    $base = $BaseUrl.TrimEnd('/')

    if ($base) {
        Write-Info ('using -BaseUrl ' + $base + '; no AWS command is run.')
    }
    else {
        if (-not $DryRun) {
            Assert-ToolAvailable -Name 'aws' -Hint 'Run .\00-check-prereqs.ps1 first.'
        }
        # Verification deploys nothing; the ticket storage rule applies to the deploying scripts.
        $config = Get-DeployConfig -ConfigPath $ConfigPath -DryRun:$DryRun -SkipStorageCheck
        Show-DeployTarget -Config $config -DryRun:$DryRun
        $serviceArn = Get-TargetServiceArn -Config $config

        Write-Step '1. Service status and endpoint'
        $service = Get-ExpressService -Config $config -ServiceArn $serviceArn -DryRun:$DryRun
        if ($DryRun) {
            $base = 'https://<SERVICE_ENDPOINT>'
        }
        else {
            if ($null -eq $service) {
                throw ('The service was not found: ' + $serviceArn + '. ' + $script:LastAwsError)
            }
            Write-Info ('status            : ' + $service.status.statusCode + ' ' + $service.status.statusReason)
            $current = Get-ExpressCurrentConfiguration $service
            if ($null -ne $current) {
                Write-Info ('image             : ' + $current.primaryContainer.image)
                Write-Info ('cpu / memory      : ' + $current.cpu + ' / ' + $current.memory)
                Write-Info ('health check path : ' + $current.healthCheckPath)
                Write-Info ('tasks (min-max)   : ' + $current.scalingTarget.minTaskCount + '-' + $current.scalingTarget.maxTaskCount)
            }
            $base = Get-ExpressEndpoint $service
            if (-not $base) {
                throw 'The service has no endpoint yet. It may still be provisioning; run this script again in a few minutes.'
            }
        }
    }
    Write-Info ('HTTPS URL         : ' + $base)

    $searchBody = [ordered]@{ subject = $Subject; description = $Description }
    $searchJson = ConvertTo-Json -InputObject $searchBody -Depth 5 -Compress

    if ($DryRun) {
        Write-Step '2-4. HTTP checks (dry run)'
        Write-Info ('[dry-run] GET  ' + $base + '/health   expect 200')
        Write-Info ('[dry-run] GET  ' + $base + '/ready    expect 200')
        if (-not $SkipSearch) {
            Write-Info ('[dry-run] POST ' + $base + '/api/search   Content-Type: application/json')
            Write-Info ('[dry-run]      body: ' + $searchJson)
        }
        Write-Step 'Dry run finished. Nothing was sent.'
        exit 0
    }

    Write-Step '2. Liveness'
    Test-Endpoint -Url ($base + '/health')

    Write-Step '3. Readiness'
    Test-Endpoint -Url ($base + '/ready')
    $ProgressPreference = 'SilentlyContinue'
    try {
        $ready = Invoke-RestMethod -Uri ($base + '/ready') -Method Get -TimeoutSec 30
        Write-Info ('status=' + $ready.status + '  mode=' + $ready.mode + '  degraded=' + $ready.degraded + '  corpus_size=' + $ready.corpus_size)
    }
    catch {
        Write-Info ('readiness details not available: ' + $_.Exception.Message)
    }

    if (-not $SkipSearch) {
        Write-Step '4. Sample search'
        Write-Info ('POST ' + $base + '/api/search')
        try {
            $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($searchJson)
            $response = Invoke-RestMethod -Uri ($base + '/api/search') -Method Post -ContentType 'application/json; charset=utf-8' -Body $bodyBytes -TimeoutSec 60
            $resultCount = (ConvertTo-Array $response.results).Count
            $relatedCount = (ConvertTo-Array $response.related).Count
            Write-Host ('   [PASS] POST /api/search -> 200') -ForegroundColor Green
            Write-Info ('status   : ' + $response.status)
            Write-Info ('message  : ' + $response.message)
            Write-Info ('results  : ' + $resultCount + ' useful, ' + $relatedCount + ' related')
            Write-Info ('request  : ' + $response.request_id)
        }
        catch {
            $script:FailedChecks = $script:FailedChecks + 1
            $failedStatus = 'no response'
            $failedResponse = $_.Exception.Response
            if ($null -ne $failedResponse) {
                $failedStatus = [string][int]$failedResponse.StatusCode
            }
            Write-Host ('   [FAIL] POST /api/search -> ' + $failedStatus + ': ' + $_.Exception.Message) -ForegroundColor Red
        }
    }

    Write-Step 'Summary'
    if ($script:FailedChecks -gt 0) {
        Write-Host ('   ' + $script:FailedChecks + ' check(s) failed. See DEPLOY_AWS.md, section 8 (troubleshooting).') -ForegroundColor Red
        exit 1
    }
    Write-Info 'All checks passed.'
    Write-Info ('Open in a browser: ' + $base + '/')
    exit 0
}
catch {
    Write-Host ''
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
