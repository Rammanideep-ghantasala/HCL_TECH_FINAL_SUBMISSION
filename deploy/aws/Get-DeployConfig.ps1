<#
.SYNOPSIS
    Shared configuration loader and helper functions for the AWS deployment scripts.

.DESCRIPTION
    Dot-source this file from a script in the same folder:

        . (Join-Path $PSScriptRoot 'Get-DeployConfig.ps1')

    It defines functions only. Loading it runs no AWS, Docker or git command.

    Compatible with Windows PowerShell 5.1: no pipeline chain operators, no ternary,
    no null-coalescing. Every native command goes through Invoke-NativeCaptured,
    Invoke-NativeStreaming or Invoke-DockerLogin, each of which reads $LASTEXITCODE
    immediately after the call.

    Conventions used by every script:
      * JSON is passed to the AWS CLI as file:// documents written without a byte-order mark.
      * -DryRun (alias -WhatIf) prints each command and runs none of them.
      * No account id or region is hardcoded. They come from deploy.config.json, from
        "aws sts get-caller-identity" and from "aws configure get region".

.NOTES
    These scripts were reviewed against AWS documentation but have never been executed
    against a live AWS account. See DEPLOY_AWS.md.
#>

$script:DeployAwsRoot = $PSScriptRoot
$script:RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$script:StateDir = Join-Path $PSScriptRoot '.state'
$script:GeneratedDir = Join-Path $PSScriptRoot '.generated'
$script:ManagedByTagValue = 'deploy-aws-scripts'
# 2.32.2 added the Express Mode commands, 2.32.22 the TEXT-ONLY monitor mode and
# 2.36.44 the --cpu-architecture option that 04-deploy-express-service.ps1 passes.
$script:MinimumAwsCliVersion = '2.36.44'
$script:LastAwsError = ''

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
}
catch {
    # Older .NET builds may not expose the enum value. HTTPS checks then use the system default.
}

# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

function Write-Step {
    <# .SYNOPSIS Prints a section heading. #>
    [CmdletBinding()]
    param(
        # Heading text.
        [Parameter(Mandatory = $true)][string]$Message
    )
    Write-Host ''
    Write-Host ('== ' + $Message) -ForegroundColor Cyan
}

function Write-Info {
    <# .SYNOPSIS Prints an indented information line. #>
    [CmdletBinding()]
    param(
        # Text to print. An empty string prints a blank line.
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Message
    )
    Write-Host ('   ' + $Message)
}

function Write-Note {
    <# .SYNOPSIS Prints an indented warning line. #>
    [CmdletBinding()]
    param(
        # Warning text.
        [Parameter(Mandatory = $true)][string]$Message
    )
    Write-Host ('   NOTE: ' + $Message) -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

function ConvertTo-Array {
    <#
    .SYNOPSIS
        Returns the value as an array. $null becomes an empty array (not an array holding $null).
    #>
    [CmdletBinding()]
    param(
        # Any value, array or $null.
        [Parameter(Position = 0)]$Value
    )
    if ($null -eq $Value) {
        return , @()
    }
    return , @($Value)
}

function Get-ConfigValue {
    <#
    .SYNOPSIS
        Reads a property from a parsed JSON object, returning a default when it is missing or empty.
    #>
    [CmdletBinding()]
    param(
        # Object produced by ConvertFrom-Json.
        [Parameter(Position = 0)]$Object,
        # Property name.
        [Parameter(Mandatory = $true, Position = 1)][string]$Name,
        # Value returned when the property is absent, null or an empty string.
        [Parameter(Position = 2)]$Default
    )
    if ($null -ne $Object) {
        $property = $Object.PSObject.Properties[$Name]
        if ($null -ne $property) {
            $value = $property.Value
            if ($null -ne $value) {
                if ($value -is [string]) {
                    if ($value.Trim() -ne '') {
                        return $value.Trim()
                    }
                }
                else {
                    return $value
                }
            }
        }
    }
    return $Default
}

function Format-CommandLine {
    <#
    .SYNOPSIS
        Builds a printable command line. Used for display only, never for execution.
    #>
    [CmdletBinding()]
    param(
        # Program name.
        [Parameter(Mandatory = $true)][string]$FilePath,
        # Arguments in order.
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$ArgumentList
    )
    $parts = New-Object System.Collections.Generic.List[string]
    $parts.Add($FilePath)
    foreach ($argument in $ArgumentList) {
        if ($argument -match '[\s"]') {
            $parts.Add('"' + ($argument -replace '"', '\"') + '"')
        }
        else {
            $parts.Add($argument)
        }
    }
    return ($parts -join ' ')
}

function Save-JsonFile {
    <#
    .SYNOPSIS
        Writes an object as JSON, UTF-8 without a byte-order mark.
    .DESCRIPTION
        Windows PowerShell 5.1 "Out-File -Encoding utf8" writes a byte-order mark that the
        AWS CLI rejects in file:// documents, so the file is written through
        [System.IO.File]::WriteAllText with UTF8Encoding($false).
    #>
    [CmdletBinding()]
    param(
        # Destination file. The folder is created when missing.
        [Parameter(Mandatory = $true)][string]$Path,
        # Object, hashtable or array to serialise.
        [Parameter(Mandatory = $true)][AllowEmptyCollection()]$Object
    )
    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $json = ConvertTo-Json -InputObject $Object -Depth 20
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $json, $encoding)
}

function Get-FileUri {
    <# .SYNOPSIS Converts a local path into the file:// form the AWS CLI reads. #>
    [CmdletBinding()]
    param(
        # Absolute path of an existing file.
        [Parameter(Mandatory = $true)][string]$Path
    )
    return ('file://' + ($Path -replace '\\', '/'))
}

function Save-GeneratedJson {
    <#
    .SYNOPSIS
        Writes a generated JSON document under deploy/aws/.generated and returns its file:// URI.
    #>
    [CmdletBinding()]
    param(
        # File name, for example 'primary-container.json'.
        [Parameter(Mandatory = $true)][string]$Name,
        # Object to serialise.
        [Parameter(Mandatory = $true)][AllowEmptyCollection()]$Object
    )
    $path = Join-Path $script:GeneratedDir $Name
    Save-JsonFile -Path $path -Object $Object
    Write-Info ('wrote ' + $path)
    return (Get-FileUri -Path $path)
}

function Get-SortableTime {
    <#
    .SYNOPSIS
        Converts an AWS CLI timestamp (ISO 8601 text or epoch seconds) into a DateTimeOffset.
    #>
    [CmdletBinding()]
    param(
        # Timestamp as returned by the AWS CLI.
        [Parameter(Position = 0)]$Value
    )
    if ($null -eq $Value) {
        return [DateTimeOffset]::MinValue
    }
    if ($Value -is [DateTime]) {
        return (New-Object System.DateTimeOffset($Value))
    }
    $text = [string]$Value
    $number = 0.0
    if ([double]::TryParse($text, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$number)) {
        return [DateTimeOffset]::FromUnixTimeMilliseconds([long]($number * 1000))
    }
    $parsed = [DateTimeOffset]::MinValue
    if ([DateTimeOffset]::TryParse($text, [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::AssumeUniversal, [ref]$parsed)) {
        return $parsed
    }
    return [DateTimeOffset]::MinValue
}

function Test-ToolAvailable {
    <# .SYNOPSIS Returns $true when a program is on PATH. Runs nothing. #>
    [CmdletBinding()]
    param(
        # Program name, for example 'aws'.
        [Parameter(Mandatory = $true)][string]$Name
    )
    $command = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue
    return ($null -ne $command)
}

function Assert-ToolAvailable {
    <# .SYNOPSIS Throws a clear error when a required program is not on PATH. #>
    [CmdletBinding()]
    param(
        # Program name.
        [Parameter(Mandatory = $true)][string]$Name,
        # Text appended to the error, for example where to install the tool.
        [Parameter()][string]$Hint = ''
    )
    if (-not (Test-ToolAvailable -Name $Name)) {
        throw ("'" + $Name + "' was not found on PATH. " + $Hint)
    }
}

# ---------------------------------------------------------------------------
# Native command execution
# ---------------------------------------------------------------------------

function Invoke-NativeCaptured {
    <#
    .SYNOPSIS
        Runs a native command and captures stdout, stderr and the exit code.
    .DESCRIPTION
        In Windows PowerShell 5.1 a native command that writes to a redirected stderr
        raises a terminating error when $ErrorActionPreference is 'Stop'. The preference
        is therefore set to 'Continue' inside this function only. The caller decides what
        to do with a non-zero exit code.
    #>
    [CmdletBinding()]
    param(
        # Program to run.
        [Parameter(Mandatory = $true)][string]$FilePath,
        # Arguments in order. Each element is passed as one argument.
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$ArgumentList
    )
    $ErrorActionPreference = 'Continue'
    $stdout = New-Object System.Collections.Generic.List[string]
    $stderr = New-Object System.Collections.Generic.List[string]
    # A stale exit code from an earlier command must never be mistaken for this command's result.
    $global:LASTEXITCODE = -1
    & $FilePath @ArgumentList 2>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) {
            $stderr.Add([string]$_.Exception.Message)
        }
        else {
            $stdout.Add([string]$_)
        }
    }
    $exitCode = $LASTEXITCODE
    return [pscustomobject]@{
        Skipped  = $false
        ExitCode = $exitCode
        StdOut   = ($stdout -join "`n")
        StdErr   = (($stderr -join "`n").Trim())
        Json     = $null
    }
}

function Invoke-NativeStreaming {
    <#
    .SYNOPSIS
        Runs a native command with its output going straight to the console (docker build, docker push).
    .DESCRIPTION
        Call this function as a statement and do not assign its result: the command's
        output is not captured. Throws when the exit code is not zero.
    #>
    [CmdletBinding()]
    param(
        # Program to run.
        [Parameter(Mandatory = $true)][string]$FilePath,
        # Arguments in order.
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$ArgumentList,
        # Print the command without running it.
        [Parameter()][switch]$DryRun
    )
    $line = Format-CommandLine -FilePath $FilePath -ArgumentList $ArgumentList
    if ($DryRun) {
        Write-Host ('   [dry-run] ' + $line) -ForegroundColor DarkGray
        return
    }
    Write-Host ('   > ' + $line) -ForegroundColor DarkGray
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = -1
    & $FilePath @ArgumentList
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw ($FilePath + ' ' + $ArgumentList[0] + ' failed with exit code ' + $exitCode + '. See the output above.')
    }
}

function Invoke-DockerLogin {
    <#
    .SYNOPSIS
        Signs Docker in to a registry, sending the password on stdin so it never appears on a command line.
    .DESCRIPTION
        When the console input code page is UTF-8 (65001), Windows PowerShell 5.1 writes a
        byte-order mark to a native command's stdin, which corrupts the password. The console
        input encoding is switched to UTF-8 without a preamble for the duration of the call
        and then restored.
    #>
    [CmdletBinding()]
    param(
        # Registry host, for example 123456789012.dkr.ecr.eu-west-1.amazonaws.com.
        [Parameter(Mandatory = $true)][string]$Registry,
        # User name. Amazon ECR always uses AWS.
        [Parameter()][string]$UserName = 'AWS',
        # Password or token. Never printed.
        [Parameter(Mandatory = $true)][string]$Password
    )
    $loginArguments = @('login', '--username', $UserName, '--password-stdin', $Registry)
    Write-Host ('   > (password on stdin) | ' + (Format-CommandLine -FilePath 'docker' -ArgumentList $loginArguments)) -ForegroundColor DarkGray
    $ErrorActionPreference = 'Continue'
    $OutputEncoding = New-Object System.Text.ASCIIEncoding
    $previousInputEncoding = $null
    try {
        $previousInputEncoding = [Console]::InputEncoding
        [Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
    }
    catch {
        $previousInputEncoding = $null
    }
    $exitCode = 1
    try {
        $global:LASTEXITCODE = -1
        $Password | & docker @loginArguments
        $exitCode = $LASTEXITCODE
    }
    finally {
        if ($null -ne $previousInputEncoding) {
            try { [Console]::InputEncoding = $previousInputEncoding } catch { }
        }
    }
    if ($exitCode -ne 0) {
        throw ('docker login failed with exit code ' + $exitCode + '. See the output above.')
    }
}

function Get-AwsCommonArguments {
    <# .SYNOPSIS Returns the global AWS CLI options appended to every call. #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config
    )
    $common = New-Object System.Collections.Generic.List[string]
    if ($Config.Region) {
        $common.Add('--region')
        $common.Add([string]$Config.Region)
    }
    if ($Config.AwsProfile) {
        $common.Add('--profile')
        $common.Add([string]$Config.AwsProfile)
    }
    $common.Add('--output')
    $common.Add('json')
    $common.Add('--no-cli-pager')
    return , $common.ToArray()
}

function Invoke-Aws {
    <#
    .SYNOPSIS
        Runs one AWS CLI command, prints it first, checks the exit code and parses JSON output.
    .DESCRIPTION
        Returns an object with Skipped, ExitCode, StdOut, StdErr and Json.
        With -DryRun the command is printed and not run (Skipped is $true).
        Without -AllowFailure a non-zero exit code throws, with the CLI error text.
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig (supplies region and profile).
        [Parameter(Mandatory = $true)][psobject]$Config,
        # Service, operation and options, for example @('ecr','describe-repositories').
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        # Return the failed result instead of throwing.
        [Parameter()][switch]$AllowFailure,
        # Print the command without running it.
        [Parameter()][switch]$DryRun,
        # Do not print the command (used inside polling loops).
        [Parameter()][switch]$Quiet,
        # Do not parse stdout as JSON (used for commands that print plain text).
        [Parameter()][switch]$PlainText
    )
    $commonArguments = Get-AwsCommonArguments -Config $Config
    $fullArguments = @($Arguments) + @($commonArguments)
    $line = Format-CommandLine -FilePath 'aws' -ArgumentList $fullArguments
    if ($DryRun) {
        Write-Host ('   [dry-run] ' + $line) -ForegroundColor DarkGray
        return [pscustomobject]@{ Skipped = $true; ExitCode = 0; StdOut = ''; StdErr = ''; Json = $null }
    }
    if (-not $Quiet) {
        Write-Host ('   > ' + $line) -ForegroundColor DarkGray
    }
    $result = Invoke-NativeCaptured -FilePath 'aws' -ArgumentList $fullArguments
    if ($result.ExitCode -ne 0) {
        $script:LastAwsError = $result.StdErr
        if ($AllowFailure) {
            return $result
        }
        throw ('aws ' + $Arguments[0] + ' ' + $Arguments[1] + ' failed with exit code ' + $result.ExitCode + ': ' + $result.StdErr)
    }
    if (-not $PlainText) {
        $text = $result.StdOut.Trim()
        if ($text.StartsWith('{') -or $text.StartsWith('[')) {
            try {
                $result.Json = ConvertFrom-Json -InputObject $text
            }
            catch {
                throw ('aws ' + $Arguments[0] + ' ' + $Arguments[1] + ' returned output that is not valid JSON: ' + $_.Exception.Message)
            }
        }
    }
    return $result
}

function Test-AwsNotFound {
    <#
    .SYNOPSIS
        Returns $true when a failed AWS CLI result looks like "the resource does not exist".
    #>
    [CmdletBinding()]
    param(
        # Result object from Invoke-Aws -AllowFailure.
        [Parameter(Mandatory = $true)][psobject]$Result
    )
    if ($Result.ExitCode -eq 0) {
        return $false
    }
    return ($Result.StdErr -match 'NotFound|NoSuchEntity|not found|does not exist|cannot be found|MISSING')
}

# ---------------------------------------------------------------------------
# Local state (deploy/aws/.state)
# ---------------------------------------------------------------------------

function Get-StateFilePath {
    <# .SYNOPSIS Returns the path of a named state file. #>
    [CmdletBinding()]
    param(
        # State name without extension, for example 'last-image'.
        [Parameter(Mandatory = $true)][string]$Name
    )
    return (Join-Path $script:StateDir ($Name + '.json'))
}

function Read-State {
    <# .SYNOPSIS Reads a state file. Returns $null when it does not exist. #>
    [CmdletBinding()]
    param(
        # State name without extension.
        [Parameter(Mandatory = $true)][string]$Name
    )
    $path = Get-StateFilePath -Name $Name
    if (-not (Test-Path -LiteralPath $path)) {
        return $null
    }
    $text = Get-Content -LiteralPath $path -Raw -Encoding UTF8
    if ($null -eq $text) {
        return $null
    }
    if ($text.Trim() -eq '') {
        return $null
    }
    return (ConvertFrom-Json -InputObject $text)
}

function Write-State {
    <# .SYNOPSIS Writes a state file as JSON without a byte-order mark. #>
    [CmdletBinding()]
    param(
        # State name without extension.
        [Parameter(Mandatory = $true)][string]$Name,
        # Object to store.
        [Parameter(Mandatory = $true)][AllowEmptyCollection()]$Object
    )
    Save-JsonFile -Path (Get-StateFilePath -Name $Name) -Object $Object
}

function Test-StateMatchesConfig {
    <#
    .SYNOPSIS
        Returns $true when a state record was written for the same account and region as the current configuration.
    #>
    [CmdletBinding()]
    param(
        # State record, or $null.
        [Parameter(Position = 0)]$State,
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config
    )
    if ($null -eq $State) {
        return $false
    }
    if ([string]$State.accountId -ne [string]$Config.AccountId) {
        return $false
    }
    if ([string]$State.region -ne [string]$Config.Region) {
        return $false
    }
    return $true
}

function Add-DeployHistory {
    <#
    .SYNOPSIS
        Appends one entry to deploy/aws/.state/deploy-history.json. 07-rollback.ps1 reads this file.
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config,
        # 'create', 'update' or 'rollback'.
        [Parameter(Mandatory = $true)][string]$Action,
        # Image reference that was deployed. May be empty when it could not be read.
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ImageUri,
        # 'requested', 'succeeded' or 'failed'.
        [Parameter(Mandatory = $true)][string]$Result,
        # Free text, for example a deployment status reason.
        [Parameter()][string]$Detail = ''
    )
    $items = New-Object System.Collections.Generic.List[object]
    foreach ($existing in (ConvertTo-Array (Read-State -Name 'deploy-history'))) {
        $items.Add($existing)
    }
    $items.Add([ordered]@{
            timestampUtc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
            accountId    = [string]$Config.AccountId
            region       = [string]$Config.Region
            serviceName  = [string]$Config.ServiceName
            action       = $Action
            imageUri     = $ImageUri
            result       = $Result
            detail       = $Detail
        })
    Write-State -Name 'deploy-history' -Object $items.ToArray()
}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

function Get-EnvironmentSetting {
    <#
    .SYNOPSIS
        Returns the value of one entry of the "environment" block, or $null when it is absent.
    .DESCRIPTION
        The name is matched without regard to case, because the application reads its
        settings that way (pydantic-settings with case_sensitive=False).
    #>
    [CmdletBinding()]
    param(
        # The Environment dictionary of a configuration object.
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.IDictionary]$Environment,
        # Variable name, for example 'SAVE_ENABLED'.
        [Parameter(Mandatory = $true)][string]$Name
    )
    foreach ($key in @($Environment.Keys)) {
        # -eq compares strings without regard to case.
        if ([string]$key -eq $Name) {
            return [string]$Environment[$key]
        }
    }
    return $null
}

function Get-TicketStorageProblem {
    <#
    .SYNOPSIS
        Returns the reasons the "environment" block would lose or duplicate saved tickets on Fargate (an empty array when it is safe).
    .DESCRIPTION
        The application saves resolved tickets unless SAVE_ENABLED is false, and keeps them in a
        JSON file under /app/state unless TICKET_STORE is postgresql. On Fargate that folder is on
        the task's temporary disk, which belongs to one task only:
          * saved tickets are lost whenever the task is replaced (a deployment, a rollback,
            scaling in, a failed health check);
          * with more than one task, a retried save can reach another task, which does not know
            the first save and creates a second ticket.
        The rule is therefore applied whatever maxTaskCount says: saving is refused unless
        TICKET_STORE is postgresql. Values are read the way the application reads them:
        SAVE_ENABLED is off for false, 0, no, off, f or n in any case (nothing else, not even
        with surrounding spaces); TICKET_STORE accepts exactly json or postgresql.
    #>
    [CmdletBinding()]
    param(
        # The Environment dictionary of a configuration object.
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.IDictionary]$Environment
    )
    $found = New-Object System.Collections.Generic.List[string]

    $store = Get-EnvironmentSetting -Environment $Environment -Name 'TICKET_STORE'
    $storeShown = 'not set, so json'
    if ($null -ne $store) {
        $storeShown = '"' + $store + '"'
        # -cnotcontains: the application accepts these two values in lowercase only.
        if (@('json', 'postgresql') -cnotcontains $store) {
            $found.Add('environment.TICKET_STORE is ' + $storeShown + '; the application accepts only "json" or "postgresql" (lowercase) and would not start.')
        }
    }

    $save = Get-EnvironmentSetting -Environment $Environment -Name 'SAVE_ENABLED'
    $saveShown = 'not set, so saving is on'
    $saveDisabled = $false
    if ($null -ne $save) {
        $saveShown = '"' + $save + '"'
        # -contains compares without regard to case, as the application does.
        if (@('false', '0', 'no', 'off', 'f', 'n') -contains $save) {
            $saveDisabled = $true
        }
    }

    if ((-not $saveDisabled) -and ($store -cne 'postgresql')) {
        $found.Add(
            'Saving resolved tickets is enabled (environment.SAVE_ENABLED is ' + $saveShown + ') with JSON ticket storage (environment.TICKET_STORE is ' + $storeShown + '). ' +
            'On Fargate the JSON store sits on each task''s temporary disk: saved tickets are lost whenever a task is replaced, and with more than one task a retried save can reach another task and create a second ticket. ' +
            'Either add "SAVE_ENABLED": "false" to the environment block (read-only demo; search is unchanged), ' +
            'or keep saving on PostgreSQL: create the database and run the migrations (POSTGRES.md), set "TICKET_STORE": "postgresql", and supply DATABASE_URL from AWS Secrets Manager through the container''s "secrets" field, never through "environment" (these scripts do not pass secrets yet). ' +
            'See DEPLOY_AWS.md, section "Persistence of saved tickets".')
    }
    return , $found.ToArray()
}

function Get-DeployConfig {
    <#
    .SYNOPSIS
        Loads deploy.config.json, applies defaults, validates values and resolves the account id and region.
    .DESCRIPTION
        Order used to resolve the region: the "region" field, then the AWS_REGION and
        AWS_DEFAULT_REGION environment variables, then "aws configure get region".
        The account id always comes from "aws sts get-caller-identity"; when the
        "accountId" field is set and differs from the signed-in account, loading stops.

        With -DryRun no command is run. Values that would have come from the AWS CLI are
        replaced by the placeholders <ACCOUNT_ID> and <REGION> unless the file sets them.

        When deploy.config.json does not exist, deploy.config.example.json is used and a
        note is printed.

        Loading stops when the environment block enables saving with JSON ticket storage,
        because Fargate's disk is temporary (see Get-TicketStorageProblem). Scripts that
        deploy nothing pass -SkipStorageCheck so they can still inspect or remove a service
        that was deployed with an older configuration.
    #>
    [CmdletBinding()]
    param(
        # Path of the configuration file. Default: deploy.config.json next to the scripts.
        [Parameter()][string]$ConfigPath = '',
        # Resolve nothing through the AWS CLI; use placeholders instead.
        [Parameter()][switch]$DryRun,
        # Validate the file only. Skips "aws sts get-caller-identity" and "aws configure get region".
        [Parameter()][switch]$SkipIdentity,
        # Do not apply the ticket storage rule. Only for scripts that send no environment to
        # Amazon ECS: 05-verify.ps1, 99-cleanup.ps1 and 07-rollback.ps1 -Mode StopInProgress.
        [Parameter()][switch]$SkipStorageCheck
    )

    if (-not $ConfigPath) {
        $ConfigPath = Join-Path $script:DeployAwsRoot 'deploy.config.json'
        if (-not (Test-Path -LiteralPath $ConfigPath)) {
            $examplePath = Join-Path $script:DeployAwsRoot 'deploy.config.example.json'
            Write-Note ('deploy.config.json not found; using the defaults in ' + $examplePath)
            Write-Note 'To customise: Copy-Item deploy.config.example.json deploy.config.json'
            $ConfigPath = $examplePath
        }
    }
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        throw ('Configuration file not found: ' + $ConfigPath)
    }
    $rawText = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8
    try {
        $raw = ConvertFrom-Json -InputObject $rawText
    }
    catch {
        throw ('Configuration file is not valid JSON: ' + $ConfigPath + ' (' + $_.Exception.Message + ')')
    }

    $environment = New-Object System.Collections.Specialized.OrderedDictionary
    $rawEnvironment = Get-ConfigValue $raw 'environment' $null
    if ($null -ne $rawEnvironment) {
        foreach ($property in $rawEnvironment.PSObject.Properties) {
            $environment[[string]$property.Name] = [string]$property.Value
        }
    }

    $config = [pscustomobject]@{
        ConfigPath              = $ConfigPath
        AwsProfile              = [string](Get-ConfigValue $raw 'awsProfile' '')
        Region                  = [string](Get-ConfigValue $raw 'region' '')
        AccountId               = [string](Get-ConfigValue $raw 'accountId' '')
        Partition               = 'aws'
        CallerArn               = ''
        ProjectTag              = [string](Get-ConfigValue $raw 'projectTag' 'ticket-similarity')
        EcrRepositoryName       = [string](Get-ConfigValue $raw 'ecrRepositoryName' 'ticket-similarity')
        EcrScanOnPush           = [bool](Get-ConfigValue $raw 'ecrScanOnPush' $true)
        EcrApplyLifecyclePolicy = [bool](Get-ConfigValue $raw 'ecrApplyLifecyclePolicy' $true)
        ServiceName             = [string](Get-ConfigValue $raw 'serviceName' 'ticket-similarity')
        ClusterName             = [string](Get-ConfigValue $raw 'clusterName' 'default')
        TaskExecutionRoleName   = [string](Get-ConfigValue $raw 'taskExecutionRoleName' 'ticket-similarity-ecs-task-execution')
        InfrastructureRoleName  = [string](Get-ConfigValue $raw 'infrastructureRoleName' 'ticket-similarity-ecs-express-infra')
        ContainerPort           = [int](Get-ConfigValue $raw 'containerPort' 8080)
        HealthCheckPath         = [string](Get-ConfigValue $raw 'healthCheckPath' '/ready')
        Cpu                     = [string](Get-ConfigValue $raw 'cpu' '1024')
        Memory                  = [string](Get-ConfigValue $raw 'memory' '2048')
        CpuArchitecture         = [string](Get-ConfigValue $raw 'cpuArchitecture' 'X86_64')
        MinTaskCount            = [int](Get-ConfigValue $raw 'minTaskCount' 1)
        MaxTaskCount            = [int](Get-ConfigValue $raw 'maxTaskCount' 2)
        AutoScalingMetric       = [string](Get-ConfigValue $raw 'autoScalingMetric' 'AVERAGE_CPU')
        AutoScalingTargetValue  = [int](Get-ConfigValue $raw 'autoScalingTargetValue' 60)
        Environment             = $environment
        LogGroupName            = [string](Get-ConfigValue $raw 'logGroupName' '')
        LogStreamPrefix         = [string](Get-ConfigValue $raw 'logStreamPrefix' 'ecs')
        LogRetentionDays        = [int](Get-ConfigValue $raw 'logRetentionDays' 14)
        DeployByDigest          = [bool](Get-ConfigValue $raw 'deployByDigest' $true)
        DeployTimeoutMinutes    = [int](Get-ConfigValue $raw 'deployTimeoutMinutes' 30)
        DockerfilePath          = [string](Get-ConfigValue $raw 'dockerfilePath' 'Dockerfile')
        DockerBuildContext      = [string](Get-ConfigValue $raw 'dockerBuildContext' '.')
        DockerPlatform          = [string](Get-ConfigValue $raw 'dockerPlatform' 'linux/amd64')
        DockerDisableProvenance = [bool](Get-ConfigValue $raw 'dockerDisableProvenance' $true)
        TaskExecutionRoleArn    = ''
        InfrastructureRoleArn   = ''
        TaskExecutionPolicyArn  = ''
        InfrastructurePolicyArn = ''
        ServiceArn              = ''
        RegistryHost            = ''
        RepositoryUri           = ''
    }

    # ---- validation of values that end up in resource names and API parameters
    $problems = New-Object System.Collections.Generic.List[string]
    if ($config.ServiceName -notmatch '^[a-z][a-z0-9-]{1,39}$') {
        $problems.Add('serviceName must be 2-40 characters: lowercase letters, digits and hyphens, starting with a letter (it becomes part of the public host name).')
    }
    if ($config.ClusterName -notmatch '^[A-Za-z0-9_-]{1,255}$') {
        $problems.Add('clusterName may contain only letters, digits, underscores and hyphens.')
    }
    if ($config.EcrRepositoryName -notmatch '^[a-z0-9]+((\.|_|__|-+)[a-z0-9]+)*(/[a-z0-9]+((\.|_|__|-+)[a-z0-9]+)*)*$') {
        $problems.Add('ecrRepositoryName must match the Amazon ECR pattern: lowercase letters, digits, and . _ - / as separators.')
    }
    foreach ($roleName in @($config.TaskExecutionRoleName, $config.InfrastructureRoleName)) {
        if ($roleName -notmatch '^[A-Za-z0-9+=,.@_-]{1,64}$') {
            $problems.Add('IAM role name "' + $roleName + '" must be 1-64 characters from the set A-Z a-z 0-9 + = , . @ _ -')
        }
    }
    if ($config.TaskExecutionRoleName -eq $config.InfrastructureRoleName) {
        $problems.Add('taskExecutionRoleName and infrastructureRoleName must be different roles.')
    }
    if ($config.ContainerPort -lt 1 -or $config.ContainerPort -gt 65535) {
        $problems.Add('containerPort must be between 1 and 65535.')
    }
    if (-not $config.HealthCheckPath.StartsWith('/')) {
        $problems.Add('healthCheckPath must start with a forward slash.')
    }
    if ($config.Cpu -notmatch '^[0-9]+$') {
        $problems.Add('cpu must be a whole number of CPU units, for example "1024" for 1 vCPU.')
    }
    if ($config.Memory -notmatch '^[0-9]+$') {
        $problems.Add('memory must be a whole number of MiB, for example "2048" for 2 GB.')
    }
    if (@('X86_64', 'ARM64') -notcontains $config.CpuArchitecture) {
        $problems.Add('cpuArchitecture must be X86_64 or ARM64.')
    }
    if (@('AVERAGE_CPU', 'AVERAGE_MEMORY', 'REQUEST_COUNT_PER_TARGET') -notcontains $config.AutoScalingMetric) {
        $problems.Add('autoScalingMetric must be AVERAGE_CPU, AVERAGE_MEMORY or REQUEST_COUNT_PER_TARGET.')
    }
    if ($config.MinTaskCount -lt 1) {
        $problems.Add('minTaskCount must be at least 1.')
    }
    if ($config.MaxTaskCount -lt $config.MinTaskCount) {
        $problems.Add('maxTaskCount must be greater than or equal to minTaskCount.')
    }
    if ($config.AutoScalingTargetValue -lt 1) {
        $problems.Add('autoScalingTargetValue must be a positive whole number.')
    }
    if ($config.LogRetentionDays -lt 0) {
        $problems.Add('logRetentionDays must be 0 (leave unchanged) or a positive number of days accepted by CloudWatch Logs.')
    }
    if ($config.DeployTimeoutMinutes -lt 1) {
        $problems.Add('deployTimeoutMinutes must be at least 1.')
    }
    if ($config.AccountId -and ($config.AccountId -notmatch '^[0-9]{12}$')) {
        $problems.Add('accountId must be empty or a 12-digit AWS account id.')
    }
    if ($config.Region -and ($config.Region -notmatch '^[a-z]{2}(-[a-z]+)+-[0-9]+$')) {
        $problems.Add('region must be empty or an AWS Region code such as eu-west-1.')
    }
    foreach ($name in @($config.Environment.Keys)) {
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
            $problems.Add('environment variable name "' + $name + '" is not valid.')
        }
    }
    if ($config.Environment.Contains('PORT')) {
        if ([string]$config.Environment['PORT'] -ne [string]$config.ContainerPort) {
            $problems.Add('environment.PORT (' + $config.Environment['PORT'] + ') must equal containerPort (' + $config.ContainerPort + ').')
        }
    }
    else {
        $config.Environment['PORT'] = [string]$config.ContainerPort
    }
    if (-not $SkipStorageCheck) {
        foreach ($storageProblem in (Get-TicketStorageProblem -Environment $config.Environment)) {
            $problems.Add($storageProblem)
        }
    }
    if ($problems.Count -gt 0) {
        throw ('Configuration problems in ' + $ConfigPath + ":`n - " + ($problems -join "`n - "))
    }
    if ($config.CpuArchitecture -eq 'ARM64' -and $config.DockerPlatform -eq 'linux/amd64') {
        Write-Note 'cpuArchitecture is ARM64 but dockerPlatform is linux/amd64. The image and the task architecture must match.'
    }

    # ---- region
    if (-not $config.Region) {
        if ($env:AWS_REGION) {
            $config.Region = [string]$env:AWS_REGION
        }
        elseif ($env:AWS_DEFAULT_REGION) {
            $config.Region = [string]$env:AWS_DEFAULT_REGION
        }
    }
    if (-not $config.Region) {
        $regionArguments = @('configure', 'get', 'region')
        if ($config.AwsProfile) {
            $regionArguments += @('--profile', $config.AwsProfile)
        }
        if ($DryRun -or $SkipIdentity) {
            if ($DryRun) {
                Write-Host ('   [dry-run] ' + (Format-CommandLine -FilePath 'aws' -ArgumentList $regionArguments)) -ForegroundColor DarkGray
            }
            $config.Region = '<REGION>'
        }
        else {
            Assert-ToolAvailable -Name 'aws' -Hint 'Install AWS CLI v2; see DEPLOY_AWS.md, section 1.'
            Write-Host ('   > ' + (Format-CommandLine -FilePath 'aws' -ArgumentList $regionArguments)) -ForegroundColor DarkGray
            $regionResult = Invoke-NativeCaptured -FilePath 'aws' -ArgumentList $regionArguments
            if ($regionResult.ExitCode -eq 0 -and $regionResult.StdOut.Trim() -ne '') {
                $config.Region = $regionResult.StdOut.Trim()
            }
            else {
                throw 'No AWS Region is configured. Set "region" in deploy.config.json, or run: aws configure set region <region-code> [--profile <name>]'
            }
        }
    }

    # ---- account
    if ($DryRun -or $SkipIdentity) {
        if ($DryRun) {
            $previewCommon = Get-AwsCommonArguments -Config $config
            $identityPreview = @('sts', 'get-caller-identity') + $previewCommon
            Write-Host ('   [dry-run] ' + (Format-CommandLine -FilePath 'aws' -ArgumentList $identityPreview)) -ForegroundColor DarkGray
        }
        if (-not $config.AccountId) {
            $config.AccountId = '<ACCOUNT_ID>'
        }
    }
    else {
        Assert-ToolAvailable -Name 'aws' -Hint 'Install AWS CLI v2; see DEPLOY_AWS.md, section 1.'
        $identity = Invoke-Aws -Config $config -Arguments @('sts', 'get-caller-identity')
        $signedInAccount = [string]$identity.Json.Account
        if ($signedInAccount -notmatch '^[0-9]{12}$') {
            throw 'aws sts get-caller-identity did not return an account id. Sign in first (aws sso login --profile <name>).'
        }
        if ($config.AccountId -and ($config.AccountId -ne $signedInAccount)) {
            throw ('You are signed in to account ' + $signedInAccount + ' but deploy.config.json expects account ' + $config.AccountId + '. Nothing was changed.')
        }
        $config.AccountId = $signedInAccount
        $config.CallerArn = [string]$identity.Json.Arn
        $arnParts = $config.CallerArn -split ':'
        if ($arnParts.Count -gt 1 -and $arnParts[1]) {
            $config.Partition = $arnParts[1]
        }
    }

    # ---- derived names
    $rolePrefix = 'arn:' + $config.Partition + ':iam::' + $config.AccountId + ':role/'
    $policyPrefix = 'arn:' + $config.Partition + ':iam::aws:policy/service-role/'
    $config.TaskExecutionRoleArn = $rolePrefix + $config.TaskExecutionRoleName
    $config.InfrastructureRoleArn = $rolePrefix + $config.InfrastructureRoleName
    $config.TaskExecutionPolicyArn = $policyPrefix + 'AmazonECSTaskExecutionRolePolicy'
    $config.InfrastructurePolicyArn = $policyPrefix + 'AmazonECSInfrastructureRoleforExpressGatewayServices'
    $config.ServiceArn = 'arn:' + $config.Partition + ':ecs:' + $config.Region + ':' + $config.AccountId + ':service/' + $config.ClusterName + '/' + $config.ServiceName
    $config.RegistryHost = $config.AccountId + '.dkr.ecr.' + $config.Region + '.amazonaws.com'
    $config.RepositoryUri = $config.RegistryHost + '/' + $config.EcrRepositoryName
    return $config
}

function Show-DeployTarget {
    <# .SYNOPSIS Prints the account, Region and names a script is about to act on. #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config,
        # Whether the script runs in dry-run mode.
        [Parameter()][switch]$DryRun
    )
    Write-Info ('configuration : ' + $Config.ConfigPath)
    Write-Info ('account       : ' + $Config.AccountId)
    Write-Info ('region        : ' + $Config.Region)
    if ($Config.AwsProfile) {
        Write-Info ('profile       : ' + $Config.AwsProfile)
    }
    if ($Config.CallerArn) {
        Write-Info ('signed in as  : ' + $Config.CallerArn)
    }
    Write-Info ('service       : ' + $Config.ServiceName + ' (cluster ' + $Config.ClusterName + ')')
    Write-Info ('repository    : ' + $Config.EcrRepositoryName)
    if ($DryRun) {
        Write-Note 'DRY RUN: commands are printed and not executed. Values in <ANGLE_BRACKETS> would come from AWS.'
    }
}

# ---------------------------------------------------------------------------
# Tags and JSON documents for the Express Mode API
# ---------------------------------------------------------------------------

function Get-TagList {
    <#
    .SYNOPSIS
        Returns the tags applied to every resource the scripts create.
    .DESCRIPTION
        Amazon ECS expects lowercase "key"/"value"; IAM and Amazon ECR expect "Key"/"Value".
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config,
        # Use lowercase member names (Amazon ECS).
        [Parameter()][switch]$LowerCaseKeys
    )
    $pairs = [ordered]@{
        Project   = [string]$Config.ProjectTag
        ManagedBy = $script:ManagedByTagValue
    }
    $list = New-Object System.Collections.Generic.List[object]
    foreach ($name in $pairs.Keys) {
        if ($LowerCaseKeys) {
            $list.Add([ordered]@{ key = [string]$name; value = [string]$pairs[$name] })
        }
        else {
            $list.Add([ordered]@{ Key = [string]$name; Value = [string]$pairs[$name] })
        }
    }
    return , $list.ToArray()
}

function New-PrimaryContainerDocument {
    <#
    .SYNOPSIS
        Builds the --primary-container document (ExpressGatewayContainer).
    .DESCRIPTION
        Shape, from the AWS CLI reference:
          { "image": string, "containerPort": integer,
            "awsLogsConfiguration": { "logGroup": string, "logStreamPrefix": string },
            "environment": [ { "name": string, "value": string } ] }
        awsLogsConfiguration is included only when logGroupName is set in the configuration;
        otherwise Express Mode creates and names the log group itself.
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config,
        # Full image reference, by digest or by tag.
        [Parameter(Mandatory = $true)][string]$ImageUri
    )
    $environmentList = New-Object System.Collections.Generic.List[object]
    foreach ($name in @($Config.Environment.Keys)) {
        $environmentList.Add([ordered]@{ name = [string]$name; value = [string]$Config.Environment[$name] })
    }
    $document = [ordered]@{
        image         = $ImageUri
        containerPort = [int]$Config.ContainerPort
        environment   = $environmentList.ToArray()
    }
    if ($Config.LogGroupName) {
        $document['awsLogsConfiguration'] = [ordered]@{
            logGroup        = [string]$Config.LogGroupName
            logStreamPrefix = [string]$Config.LogStreamPrefix
        }
    }
    return $document
}

function New-ScalingTargetDocument {
    <# .SYNOPSIS Builds the --scaling-target document (ExpressGatewayScalingTarget). #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config
    )
    return [ordered]@{
        minTaskCount           = [int]$Config.MinTaskCount
        maxTaskCount           = [int]$Config.MaxTaskCount
        autoScalingMetric      = [string]$Config.AutoScalingMetric
        autoScalingTargetValue = [int]$Config.AutoScalingTargetValue
    }
}

function Test-ImageUri {
    <#
    .SYNOPSIS
        Checks an image reference and returns a problem description, or an empty string when it is acceptable.
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config,
        # Image reference to check.
        [Parameter(Mandatory = $true)][string]$ImageUri
    )
    if ($ImageUri -match '\s') {
        return 'The image reference contains white space.'
    }
    $byDigest = ($ImageUri -match '@sha256:[0-9a-f]{64}$')
    $byTag = ($ImageUri -match ':[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$')
    if (-not ($byDigest -or $byTag)) {
        return 'The image reference must end in @sha256:<64 hex characters> or in :<tag>.'
    }
    if ($Config.DeployByDigest -and (-not $byDigest)) {
        return 'deployByDigest is true, so the image reference must end in @sha256:<digest>. 02-build-and-push.ps1 prints it.'
    }
    return ''
}

function Resolve-ImageUri {
    <#
    .SYNOPSIS
        Returns the image reference to deploy: the -ImageUri argument, or the image recorded by 02-build-and-push.ps1.
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config,
        # Explicit image reference. Empty means "use the last pushed image".
        [Parameter()][string]$ImageUri = '',
        # Whether the script runs in dry-run mode.
        [Parameter()][switch]$DryRun
    )
    $resolved = $ImageUri
    if (-not $resolved) {
        $lastImage = Read-State -Name 'last-image'
        if (Test-StateMatchesConfig $lastImage -Config $Config) {
            if ($Config.DeployByDigest) {
                $resolved = [string]$lastImage.imageUriByDigest
            }
            else {
                $resolved = [string]$lastImage.imageUriByTag
            }
        }
    }
    if (-not $resolved) {
        if ($DryRun) {
            return ($Config.RepositoryUri + '@sha256:<IMAGE_DIGEST>')
        }
        throw 'No image to deploy. Run 02-build-and-push.ps1 first, or pass -ImageUri <repository-uri>@sha256:<digest>.'
    }
    $problem = Test-ImageUri -Config $Config -ImageUri $resolved
    if ($problem) {
        throw ($problem + ' Value: ' + $resolved)
    }
    return $resolved
}

# ---------------------------------------------------------------------------
# Express Mode service helpers
# ---------------------------------------------------------------------------

function Get-TargetServiceArn {
    <#
    .SYNOPSIS
        Returns the ARN of the Express Mode service the scripts act on.
    .DESCRIPTION
        Uses the ARN that "create-express-gateway-service" returned (stored in
        deploy/aws/.state/service.json) when it was recorded for the same account, Region
        and service name. Otherwise builds
        arn:<partition>:ecs:<region>:<account>:service/<cluster>/<service-name>.
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config
    )
    $state = Read-State -Name 'service'
    if (Test-StateMatchesConfig $state -Config $Config) {
        if (([string]$state.serviceName -eq [string]$Config.ServiceName) -and $state.serviceArn) {
            return [string]$state.serviceArn
        }
    }
    return [string]$Config.ServiceArn
}

function Get-ExpressService {
    <#
    .SYNOPSIS
        Runs "aws ecs describe-express-gateway-service" and returns the service object, or $null when the call fails.
    .DESCRIPTION
        The CLI error text of a failed call is left in $script:LastAwsError.
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config,
        # ARN of the Express Mode service.
        [Parameter(Mandatory = $true)][string]$ServiceArn,
        # Print the command without running it.
        [Parameter()][switch]$DryRun,
        # Do not print the command.
        [Parameter()][switch]$Quiet
    )
    $script:LastAwsError = ''
    $result = Invoke-Aws -Config $Config -Arguments @('ecs', 'describe-express-gateway-service', '--service-arn', $ServiceArn) -AllowFailure -DryRun:$DryRun -Quiet:$Quiet
    if ($result.Skipped) {
        return $null
    }
    if ($result.ExitCode -ne 0) {
        return $null
    }
    if ($null -eq $result.Json) {
        return $null
    }
    return $result.Json.service
}

function Get-ExpressCurrentConfiguration {
    <#
    .SYNOPSIS
        Returns the most recently created entry of service.activeConfigurations, or $null.
    #>
    [CmdletBinding()]
    param(
        # Service object from Get-ExpressService.
        [Parameter(Position = 0)]$Service
    )
    if ($null -eq $Service) {
        return $null
    }
    $latest = $null
    $latestTime = [DateTimeOffset]::MinValue
    foreach ($configuration in (ConvertTo-Array $Service.activeConfigurations)) {
        $created = Get-SortableTime $configuration.createdAt
        if ($null -eq $latest -or $created -gt $latestTime) {
            $latest = $configuration
            $latestTime = $created
        }
    }
    return $latest
}

function Get-ExpressEndpoint {
    <#
    .SYNOPSIS
        Returns the public HTTPS base URL from service.activeConfigurations[].ingressPaths[].endpoint, or an empty string.
    .DESCRIPTION
        The URL is always read from the API. It is never constructed from the service name.
    #>
    [CmdletBinding()]
    param(
        # Service object from Get-ExpressService.
        [Parameter(Position = 0)]$Service
    )
    if ($null -eq $Service) {
        return ''
    }
    $fallback = ''
    foreach ($configuration in (ConvertTo-Array $Service.activeConfigurations)) {
        foreach ($path in (ConvertTo-Array $configuration.ingressPaths)) {
            $endpoint = [string]$path.endpoint
            if (-not $endpoint) {
                continue
            }
            $endpoint = $endpoint.Trim()
            if ($endpoint -notmatch '^https?://') {
                $endpoint = 'https://' + $endpoint
            }
            $endpoint = $endpoint.TrimEnd('/')
            if ([string]$path.accessType -eq 'PUBLIC') {
                return $endpoint
            }
            if (-not $fallback) {
                $fallback = $endpoint
            }
        }
    }
    return $fallback
}

function Get-ExpressManagedResources {
    <#
    .SYNOPSIS
        Lists the AWS resources Express Mode manages for a service, as objects with Type, Arn and Name.
    .DESCRIPTION
        Runs "aws ecs describe-service-revisions" for every service revision in
        service.activeConfigurations and flattens serviceRevisions[].ecsManagedResources:
        load balancer, listener, listener rule, target groups, security groups, certificate,
        scalable target, scaling policies, metric alarms and log groups, plus the task
        definition of each revision. Returns an empty array when nothing can be read.
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config,
        # Service object from Get-ExpressService.
        [Parameter(Position = 0)]$Service,
        # Print the command without running it.
        [Parameter()][switch]$DryRun
    )
    $found = New-Object System.Collections.Generic.List[object]
    $seen = New-Object System.Collections.Generic.List[string]
    $addResource = {
        param([string]$Type, [string]$Arn, [string]$Name)
        if (-not $Arn) { return }
        $key = $Type + '|' + $Arn
        if ($seen.Contains($key)) { return }
        $seen.Add($key)
        $found.Add([pscustomobject]@{ Type = $Type; Arn = $Arn; Name = $Name })
    }

    $revisionArns = New-Object System.Collections.Generic.List[string]
    if ($DryRun) {
        $revisionArns.Add('<SERVICE_REVISION_ARN>')
    }
    elseif ($null -ne $Service) {
        foreach ($configuration in (ConvertTo-Array $Service.activeConfigurations)) {
            $revisionArn = [string]$configuration.serviceRevisionArn
            if ($revisionArn -and (-not $revisionArns.Contains($revisionArn))) {
                $revisionArns.Add($revisionArn)
            }
        }
    }
    if ($revisionArns.Count -eq 0) {
        return , @()
    }
    $arguments = @('ecs', 'describe-service-revisions', '--service-revision-arns') + $revisionArns.ToArray()
    $result = Invoke-Aws -Config $Config -Arguments $arguments -AllowFailure -DryRun:$DryRun
    if ($result.Skipped) {
        return , @()
    }
    if ($result.ExitCode -ne 0) {
        Write-Note ('describe-service-revisions failed: ' + $result.StdErr)
        return , @()
    }
    if ($null -eq $result.Json) {
        return , @()
    }
    foreach ($revision in (ConvertTo-Array $result.Json.serviceRevisions)) {
        & $addResource 'task-definition' ([string]$revision.taskDefinition) ''
        $managed = $revision.ecsManagedResources
        if ($null -eq $managed) {
            continue
        }
        foreach ($ingress in (ConvertTo-Array $managed.ingressPaths)) {
            & $addResource 'load-balancer' ([string]$ingress.loadBalancer.arn) ''
            & $addResource 'listener' ([string]$ingress.listener.arn) ''
            & $addResource 'listener-rule' ([string]$ingress.rule.arn) ''
            & $addResource 'certificate' ([string]$ingress.certificate.arn) ([string]$ingress.certificate.domainName)
            foreach ($group in (ConvertTo-Array $ingress.loadBalancerSecurityGroups)) {
                & $addResource 'security-group' ([string]$group.arn) 'load balancer'
            }
            foreach ($targetGroup in (ConvertTo-Array $ingress.targetGroups)) {
                & $addResource 'target-group' ([string]$targetGroup.arn) ''
            }
        }
        if ($null -ne $managed.autoScaling) {
            & $addResource 'scalable-target' ([string]$managed.autoScaling.scalableTarget.arn) ''
            foreach ($policy in (ConvertTo-Array $managed.autoScaling.applicationAutoScalingPolicies)) {
                & $addResource 'scaling-policy' ([string]$policy.arn) ([string]$policy.metric)
            }
        }
        foreach ($alarm in (ConvertTo-Array $managed.metricAlarms)) {
            & $addResource 'metric-alarm' ([string]$alarm.arn) ''
        }
        foreach ($group in (ConvertTo-Array $managed.serviceSecurityGroups)) {
            & $addResource 'security-group' ([string]$group.arn) 'service'
        }
        foreach ($logGroup in (ConvertTo-Array $managed.logGroups)) {
            $logGroupArn = [string]$logGroup.arn
            if (-not $logGroupArn) {
                $logGroupArn = 'log-group:' + [string]$logGroup.logGroupName
            }
            & $addResource 'log-group' $logGroupArn ([string]$logGroup.logGroupName)
        }
    }
    return , $found.ToArray()
}

function Get-ServiceDeployments {
    <#
    .SYNOPSIS
        Runs "aws ecs list-service-deployments" and returns the deployments as an array (empty when the call fails).
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config,
        # ARN of the service.
        [Parameter(Mandatory = $true)][string]$ServiceArn,
        # Print the command without running it.
        [Parameter()][switch]$DryRun,
        # Do not print the command.
        [Parameter()][switch]$Quiet
    )
    $arguments = @('ecs', 'list-service-deployments', '--service', $ServiceArn, '--cluster', $Config.ClusterName)
    $result = Invoke-Aws -Config $Config -Arguments $arguments -AllowFailure -DryRun:$DryRun -Quiet:$Quiet
    if ($result.Skipped) {
        return , @()
    }
    if ($result.ExitCode -ne 0) {
        return , @()
    }
    if ($null -eq $result.Json) {
        return , @()
    }
    return , (ConvertTo-Array $result.Json.serviceDeployments)
}

function Get-HttpStatus {
    <#
    .SYNOPSIS
        Sends one GET request and returns the HTTP status code, or 0 when no response was received.
    #>
    [CmdletBinding()]
    param(
        # Full URL.
        [Parameter(Mandatory = $true)][string]$Url,
        # Request timeout in seconds.
        [Parameter()][int]$TimeoutSeconds = 15
    )
    $ProgressPreference = 'SilentlyContinue'
    try {
        $response = Invoke-WebRequest -Uri $Url -Method Get -UseBasicParsing -TimeoutSec $TimeoutSeconds
        return [int]$response.StatusCode
    }
    catch {
        $failedResponse = $_.Exception.Response
        if ($null -ne $failedResponse) {
            return [int]$failedResponse.StatusCode
        }
        return 0
    }
}

function Wait-ExpressDeployment {
    <#
    .SYNOPSIS
        Polls until the newest service deployment succeeds, fails, or the timeout passes.
    .DESCRIPTION
        Each poll runs "aws ecs describe-express-gateway-service" and
        "aws ecs list-service-deployments", then requests the health check path over HTTPS.

        A deployment whose ARN is in -KnownDeploymentArns is ignored, so an update waits
        for the deployment it started rather than an older one.

        Success: the newest deployment reports SUCCESSFUL. With -AcceptReadyEndpoint (first
        creation only) three consecutive HTTP 200 answers from the health check path are
        also accepted when no deployment record is visible.

        Failure: the newest deployment reports STOPPED, ROLLBACK_SUCCESSFUL or
        ROLLBACK_FAILED, or the timeout passes.
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config,
        # ARN of the Express Mode service.
        [Parameter(Mandatory = $true)][string]$ServiceArn,
        # Deployment ARNs that existed before the change was requested.
        [Parameter()][AllowEmptyCollection()][string[]]$KnownDeploymentArns = @(),
        # Minutes to wait before giving up.
        [Parameter()][int]$TimeoutMinutes = 30,
        # Seconds between polls.
        [Parameter()][int]$PollSeconds = 20,
        # Accept a healthy endpoint as success when no deployment record is visible.
        [Parameter()][switch]$AcceptReadyEndpoint
    )
    Write-Info ('waiting up to ' + $TimeoutMinutes + ' minutes; polling every ' + $PollSeconds + ' seconds with:')
    Write-Info ('  aws ecs describe-express-gateway-service --service-arn ' + $ServiceArn)
    Write-Info ('  aws ecs list-service-deployments --service ' + $ServiceArn + ' --cluster ' + $Config.ClusterName)
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    $readyStreak = 0
    $endpoint = ''
    while ((Get-Date) -lt $deadline) {
        $service = Get-ExpressService -Config $Config -ServiceArn $ServiceArn -Quiet
        $serviceStatus = 'unknown'
        if ($null -ne $service) {
            $serviceStatus = [string]$service.status.statusCode
            $found = Get-ExpressEndpoint $service
            if ($found) {
                $endpoint = $found
            }
        }

        $newest = $null
        $newestTime = [DateTimeOffset]::MinValue
        foreach ($deployment in (Get-ServiceDeployments -Config $Config -ServiceArn $ServiceArn -Quiet)) {
            if ($KnownDeploymentArns -contains [string]$deployment.serviceDeploymentArn) {
                continue
            }
            $created = Get-SortableTime $deployment.createdAt
            if ($null -eq $newest -or $created -gt $newestTime) {
                $newest = $deployment
                $newestTime = $created
            }
        }
        $deploymentStatus = 'not visible yet'
        $deploymentReason = ''
        $deploymentArn = ''
        if ($null -ne $newest) {
            $deploymentStatus = [string]$newest.status
            $deploymentReason = [string]$newest.statusReason
            $deploymentArn = [string]$newest.serviceDeploymentArn
        }

        $httpStatus = 0
        if ($endpoint) {
            $httpStatus = Get-HttpStatus -Url ($endpoint + $Config.HealthCheckPath)
        }
        $stamp = (Get-Date).ToString('HH:mm:ss')
        Write-Info ($stamp + '  service=' + $serviceStatus + '  deployment=' + $deploymentStatus + '  GET ' + $Config.HealthCheckPath + '=' + $httpStatus)

        if ($deploymentStatus -eq 'SUCCESSFUL') {
            return [pscustomobject]@{ Status = 'SUCCESSFUL'; Endpoint = $endpoint; DeploymentArn = $deploymentArn; Reason = $deploymentReason }
        }
        if (@('STOPPED', 'ROLLBACK_SUCCESSFUL', 'ROLLBACK_FAILED') -contains $deploymentStatus) {
            throw ('The deployment ended with status ' + $deploymentStatus + '. Reason: ' + $deploymentReason + ' Deployment: ' + $deploymentArn)
        }
        if ($httpStatus -eq 200) {
            $readyStreak = $readyStreak + 1
        }
        else {
            $readyStreak = 0
        }
        if ($AcceptReadyEndpoint -and ($null -eq $newest) -and ($readyStreak -ge 3)) {
            return [pscustomobject]@{ Status = 'READY'; Endpoint = $endpoint; DeploymentArn = ''; Reason = 'health check path answered 200 three times in a row' }
        }
        Start-Sleep -Seconds $PollSeconds
    }
    throw ('Timed out after ' + $TimeoutMinutes + ' minutes. The change may still be in progress. Check: aws ecs monitor-express-gateway-service --service-arn ' + $ServiceArn + ' --mode TEXT-ONLY --region ' + $Config.Region)
}

function Invoke-ExpressUpdate {
    <#
    .SYNOPSIS
        Runs "aws ecs update-express-gateway-service" with the image and the settings from the configuration, then waits.
    .DESCRIPTION
        Sends the complete primary container definition (image, port, environment), the health
        check path, CPU, memory and the scaling target on every update, so deploy.config.json
        stays the single source of truth. Express Mode deploys the change as a canary deployment.
    #>
    [CmdletBinding()]
    param(
        # Configuration object from Get-DeployConfig.
        [Parameter(Mandatory = $true)][psobject]$Config,
        # ARN of the Express Mode service.
        [Parameter(Mandatory = $true)][string]$ServiceArn,
        # Image reference to deploy.
        [Parameter(Mandatory = $true)][string]$ImageUri,
        # 'update' or 'rollback'; recorded in the deployment history.
        [Parameter(Mandatory = $true)][string]$Action,
        # Print the commands without running them.
        [Parameter()][switch]$DryRun,
        # Return as soon as the update is accepted.
        [Parameter()][switch]$NoWait
    )
    $knownArns = New-Object System.Collections.Generic.List[string]
    foreach ($deployment in (Get-ServiceDeployments -Config $Config -ServiceArn $ServiceArn -DryRun:$DryRun)) {
        $knownArns.Add([string]$deployment.serviceDeploymentArn)
    }

    $containerUri = Save-GeneratedJson -Name 'primary-container.json' -Object (New-PrimaryContainerDocument -Config $Config -ImageUri $ImageUri)
    $scalingUri = Save-GeneratedJson -Name 'scaling-target.json' -Object (New-ScalingTargetDocument -Config $Config)
    $arguments = @(
        'ecs', 'update-express-gateway-service',
        '--service-arn', $ServiceArn,
        '--primary-container', $containerUri,
        '--health-check-path', $Config.HealthCheckPath,
        '--cpu', $Config.Cpu,
        '--memory', $Config.Memory,
        '--scaling-target', $scalingUri
    )
    $result = Invoke-Aws -Config $Config -Arguments $arguments -DryRun:$DryRun
    if ($result.Skipped) {
        Write-Info '[dry-run] would then poll describe-express-gateway-service and list-service-deployments until the deployment finishes.'
        return $null
    }
    Add-DeployHistory -Config $Config -Action $Action -ImageUri $ImageUri -Result 'requested'
    Write-Info 'update accepted. Express Mode now runs a canary deployment (new tasks, 5% of traffic, bake time, then 100%).'
    if ($NoWait) {
        Write-Info ('not waiting. Follow progress with: aws ecs monitor-express-gateway-service --service-arn ' + $ServiceArn + ' --mode TEXT-ONLY --region ' + $Config.Region)
        return $null
    }
    try {
        $outcome = Wait-ExpressDeployment -Config $Config -ServiceArn $ServiceArn -KnownDeploymentArns $knownArns.ToArray() -TimeoutMinutes $Config.DeployTimeoutMinutes
    }
    catch {
        Add-DeployHistory -Config $Config -Action $Action -ImageUri $ImageUri -Result 'failed' -Detail $_.Exception.Message
        throw
    }
    Add-DeployHistory -Config $Config -Action $Action -ImageUri $ImageUri -Result 'succeeded' -Detail $outcome.Status
    return $outcome
}
