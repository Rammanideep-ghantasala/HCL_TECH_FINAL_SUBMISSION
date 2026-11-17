<#
.SYNOPSIS
    Builds the application image for linux/amd64, pushes it to Amazon ECR under a unique tag and prints the image digest.

.DESCRIPTION
    Steps:
      1. Look up the repository URI ("aws ecr describe-repositories").
      2. Choose a unique tag: UTC timestamp, plus the short git commit hash when available,
         for example 20261117-101500-a1b2c3d. The repository is immutable, so a tag is
         never reused.
      3. "docker build --platform linux/amd64" from the Dockerfile at the repository root.
      4. Confirm the built image really is linux/amd64 ("docker image inspect").
      5. Sign Docker in to Amazon ECR: the token from "aws ecr get-login-password" is sent
         to "docker login --password-stdin". It is never printed or written to disk by this
         script. It is valid for 12 hours.
      6. "docker push".
      7. Read the digest back from Amazon ECR ("aws ecr describe-images") and print it.

    The result is recorded in deploy/aws/.state/last-image.json, which the deploy and update
    scripts read.

.PARAMETER ConfigPath
    Path of the configuration file. Default: deploy.config.json next to this script.

.PARAMETER Tag
    Image tag to use instead of the generated one. Must not already exist in the repository.

.PARAMETER NoCache
    Pass --no-cache to docker build.

.PARAMETER DryRun
    Print the commands without running them. Alias: -WhatIf.

.EXAMPLE
    .\02-build-and-push.ps1 -DryRun

.EXAMPLE
    .\02-build-and-push.ps1

.NOTES
    Reviewed against AWS and Docker documentation; never executed against a live AWS account.
    Run it in a normal PowerShell console window (not the ISE) so Docker can draw progress output.
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string]$ConfigPath = '',

    [Parameter()]
    [ValidatePattern('^$|^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$')]
    [string]$Tag = '',

    [Parameter()]
    [switch]$NoCache,

    [Parameter()]
    [Alias('WhatIf')]
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Get-DeployConfig.ps1')

try {
    Write-Step 'Build the image and push it to Amazon ECR'
    if (-not $DryRun) {
        Assert-ToolAvailable -Name 'aws' -Hint 'Run .\00-check-prereqs.ps1 first.'
        Assert-ToolAvailable -Name 'docker' -Hint 'Install Docker Desktop and start it.'
    }
    $config = Get-DeployConfig -ConfigPath $ConfigPath -DryRun:$DryRun
    Show-DeployTarget -Config $config -DryRun:$DryRun

    $dockerfile = $config.DockerfilePath
    if (-not [System.IO.Path]::IsPathRooted($dockerfile)) {
        $dockerfile = Join-Path $script:RepoRoot $dockerfile
    }
    $buildContext = $config.DockerBuildContext
    if (-not [System.IO.Path]::IsPathRooted($buildContext)) {
        $buildContext = Join-Path $script:RepoRoot $buildContext
    }
    $buildContext = [System.IO.Path]::GetFullPath($buildContext)
    Write-Info ('Dockerfile    : ' + $dockerfile)
    Write-Info ('build context : ' + $buildContext)
    if (-not (Test-Path -LiteralPath $dockerfile)) {
        if ($DryRun) {
            Write-Note 'The Dockerfile does not exist yet. A real run stops here.'
        }
        else {
            throw ('Dockerfile not found: ' + $dockerfile)
        }
    }

    # ---- 1. repository URI
    Write-Step '1. Repository URI'
    $repositoryUri = $config.RepositoryUri
    $describe = Invoke-Aws -Config $config -Arguments @('ecr', 'describe-repositories', '--repository-names', $config.EcrRepositoryName) -AllowFailure -DryRun:$DryRun
    if (-not $describe.Skipped) {
        if ($describe.ExitCode -ne 0) {
            throw ('The repository "' + $config.EcrRepositoryName + '" is not available. Run .\01-create-ecr.ps1 first. ' + $describe.StdErr)
        }
        $repository = (ConvertTo-Array $describe.Json.repositories)[0]
        $repositoryUri = [string]$repository.repositoryUri
        if ([string]$repository.imageTagMutability -ne 'IMMUTABLE') {
            Write-Note 'This repository allows tags to be overwritten. The generated tag is still unique.'
        }
    }
    $registryHost = ($repositoryUri -split '/')[0]
    Write-Info ('repository URI: ' + $repositoryUri)

    # ---- 2. tag
    Write-Step '2. Image tag'
    $imageTag = $Tag
    if (-not $imageTag) {
        $imageTag = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')
        $gitArguments = @('-C', $script:RepoRoot, 'rev-parse', '--short', 'HEAD')
        if ($DryRun) {
            Write-Host ('   [dry-run] ' + (Format-CommandLine -FilePath 'git' -ArgumentList $gitArguments)) -ForegroundColor DarkGray
        }
        elseif (Test-ToolAvailable -Name 'git') {
            Write-Host ('   > ' + (Format-CommandLine -FilePath 'git' -ArgumentList $gitArguments)) -ForegroundColor DarkGray
            $gitResult = Invoke-NativeCaptured -FilePath 'git' -ArgumentList $gitArguments
            $shortHash = $gitResult.StdOut.Trim()
            if ($gitResult.ExitCode -eq 0 -and $shortHash -match '^[0-9a-f]{4,40}$') {
                $imageTag = $imageTag + '-' + $shortHash
            }
            else {
                Write-Info 'no git commit found; the tag is the timestamp only.'
            }
        }
    }
    $imageByTag = $repositoryUri + ':' + $imageTag
    Write-Info ('tag           : ' + $imageTag)
    Write-Info ('image         : ' + $imageByTag)

    # ---- 3. build
    Write-Step ('3. docker build for ' + $config.DockerPlatform)
    $buildArguments = @('build', '--platform', $config.DockerPlatform)
    if ($config.DockerDisableProvenance) {
        # One manifest per tag: no extra attestation manifest shows up as an untagged image in ECR.
        $buildArguments += '--provenance=false'
    }
    if ($NoCache) {
        $buildArguments += '--no-cache'
    }
    $buildArguments += @('-f', $dockerfile, '-t', $imageByTag, $buildContext)
    Invoke-NativeStreaming -FilePath 'docker' -ArgumentList $buildArguments -DryRun:$DryRun

    # ---- 4. architecture check
    Write-Step '4. Check the image platform'
    $inspectArguments = @('image', 'inspect', $imageByTag, '--format', '{{.Os}}/{{.Architecture}}')
    if ($DryRun) {
        Write-Host ('   [dry-run] ' + (Format-CommandLine -FilePath 'docker' -ArgumentList $inspectArguments)) -ForegroundColor DarkGray
    }
    else {
        Write-Host ('   > ' + (Format-CommandLine -FilePath 'docker' -ArgumentList $inspectArguments)) -ForegroundColor DarkGray
        $inspect = Invoke-NativeCaptured -FilePath 'docker' -ArgumentList $inspectArguments
        if ($inspect.ExitCode -ne 0) {
            throw ('docker image inspect failed with exit code ' + $inspect.ExitCode + ': ' + $inspect.StdErr)
        }
        $builtPlatform = $inspect.StdOut.Trim()
        if ($builtPlatform -ne $config.DockerPlatform) {
            throw ('The image was built for "' + $builtPlatform + '" but "' + $config.DockerPlatform + '" is required. Nothing was pushed.')
        }
        Write-Info ('platform      : ' + $builtPlatform)
    }

    # ---- 5. registry sign-in
    Write-Step '5. Sign Docker in to Amazon ECR'
    $loginPreview = @('ecr', 'get-login-password')
    if ($DryRun) {
        $null = Invoke-Aws -Config $config -Arguments $loginPreview -DryRun
        Write-Host ('   [dry-run] (token on stdin) | docker login --username AWS --password-stdin ' + $registryHost) -ForegroundColor DarkGray
    }
    else {
        $tokenResult = Invoke-Aws -Config $config -Arguments $loginPreview -PlainText
        $token = $tokenResult.StdOut.Trim()
        $tokenResult = $null
        if (-not $token) {
            throw 'aws ecr get-login-password returned an empty token.'
        }
        try {
            Invoke-DockerLogin -Registry $registryHost -Password $token
        }
        finally {
            $token = $null
        }
    }

    # ---- 6. push
    Write-Step '6. docker push'
    Invoke-NativeStreaming -FilePath 'docker' -ArgumentList @('push', $imageByTag) -DryRun:$DryRun

    # ---- 7. digest
    Write-Step '7. Read the image digest from Amazon ECR'
    $imageDetails = Invoke-Aws -Config $config -Arguments @(
        'ecr', 'describe-images',
        '--repository-name', $config.EcrRepositoryName,
        '--image-ids', ('imageTag=' + $imageTag)
    ) -DryRun:$DryRun

    if ($DryRun) {
        Write-Step 'Dry run finished. Nothing was executed.'
        Write-Info ('a real run would print: ' + $repositoryUri + '@sha256:<IMAGE_DIGEST>')
        exit 0
    }

    $detail = (ConvertTo-Array $imageDetails.Json.imageDetails)[0]
    $digest = [string]$detail.imageDigest
    if ($digest -notmatch '^sha256:[0-9a-f]{64}$') {
        throw ('Amazon ECR did not return a digest for tag ' + $imageTag + '.')
    }
    $imageByDigest = $repositoryUri + '@' + $digest
    $sizeMegabytes = [math]::Round(([double]$detail.imageSizeInBytes) / 1048576, 0)

    Write-State -Name 'last-image' -Object ([ordered]@{
            accountId        = $config.AccountId
            region           = $config.Region
            repositoryName   = $config.EcrRepositoryName
            repositoryUri    = $repositoryUri
            tag              = $imageTag
            digest           = $digest
            imageUriByTag    = $imageByTag
            imageUriByDigest = $imageByDigest
            sizeBytes        = $detail.imageSizeInBytes
            pushedAtUtc      = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
        })

    Write-Step 'Done'
    Write-Info ('image tag     : ' + $imageTag)
    Write-Info ('image digest  : ' + $digest)
    Write-Info ('by tag        : ' + $imageByTag)
    Write-Info ('by digest     : ' + $imageByDigest)
    Write-Info ('size in ECR   : about ' + $sizeMegabytes + ' MB (compressed)')
    if ($config.EcrScanOnPush) {
        Write-Info ('scan findings : aws ecr describe-image-scan-findings --repository-name ' + $config.EcrRepositoryName + ' --image-id imageTag=' + $imageTag + ' --region ' + $config.Region)
    }
    Write-Info 'Next step: .\03-create-iam-roles.ps1 (first deployment) or .\06-update-service.ps1 (service already exists).'
    exit 0
}
catch {
    Write-Host ''
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
