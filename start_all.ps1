[CmdletBinding()]
param(
    [string]$PythonBin = $env:PYTHON_BIN,
    [string]$PublicTunnel = $env:PUBLIC_TUNNEL,
    [string]$OpenBrowser = $env:OPEN_BROWSER,
    [string]$KeepAwake = $env:KEEP_AWAKE,
    [string]$StartParser = $env:START_PARSER,
    [string]$StartScanner = $env:START_SCANNER,
    [string]$StartTrend = $env:START_TREND,
    [string]$StartMarketHistory = $env:START_MARKET_HISTORY,
    [string]$StartReverseTunnel = $env:START_REVERSE_TUNNEL,
    [string]$StartApi = $env:START_API,
    [string]$StartTelegram = $env:START_TELEGRAM,
    [string]$PublicSiteUrl = $env:PUBLIC_SITE_URL,
    [string]$QuietDashboardUrl = $env:QUIET_DASHBOARD_URL
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Redirected Python services otherwise inherit the active Windows ANSI code page
# (for example, cp1251) and can crash while logging valid Unicode item names.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$ProjectDir = [System.IO.Path]::GetFullPath($PSScriptRoot)
$LogDir = Join-Path $ProjectDir "logs"
$RunDir = Join-Path $ProjectDir ".run"
$SupervisorPidFile = Join-Path $RunDir "start_all.pid"
$SupervisorStateFile = Join-Path $RunDir "start_all.windows.json"
$SupervisorLockFile = Join-Path $RunDir "start_all.windows.lock"
$StopRequestFile = Join-Path $RunDir "stop_all.windows.request"
$LauncherReadyFile = Join-Path $RunDir "launcher.ready.json"
$PublicUrlFile = Join-Path $RunDir "public_url.txt"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
. (Join-Path $ProjectDir "windows_service_shutdown.ps1")

$script:StartedServices = New-Object System.Collections.Generic.List[string]
$script:CoreServices = New-Object System.Collections.Generic.List[string]
$script:KnownServiceNames = @(
    "dashboard",
    "trend_worker",
    "market_history_worker",
    "reverse_tunnel",
    "parser_final",
    "deal_scanner",
    "api_server",
    "telegram_bot",
    "public_tunnel"
)
$script:StartupLock = $null
$script:KeepAwakeEnabled = $false
$script:RecoverableSupervisorInstance = ""
$script:InstanceId = [guid]::NewGuid().ToString("N")
$script:SupervisorStartUtc = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString(
    "o",
    [System.Globalization.CultureInfo]::InvariantCulture
)

function Resolve-Option {
    param(
        [AllowEmptyString()]
        [string]$Value,
        [Parameter(Mandatory = $true)]
        [string]$Default,
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string[]]$Allowed
    )

    $resolved = if ([string]::IsNullOrWhiteSpace($Value)) { $Default } else { $Value.Trim().ToLowerInvariant() }
    if ($Allowed -notcontains $resolved) {
        throw "$Name must be $($Allowed -join ', ') (got: $resolved)."
    }
    return $resolved
}

$PublicTunnel = Resolve-Option -Value $PublicTunnel -Default "0" -Name "PUBLIC_TUNNEL" -Allowed @("0", "1")
$OpenBrowser = Resolve-Option -Value $OpenBrowser -Default "1" -Name "OPEN_BROWSER" -Allowed @("0", "1")
$KeepAwake = Resolve-Option -Value $KeepAwake -Default "1" -Name "KEEP_AWAKE" -Allowed @("0", "1")
$StartParser = Resolve-Option -Value $StartParser -Default "1" -Name "START_PARSER" -Allowed @("0", "1")
$StartScanner = Resolve-Option -Value $StartScanner -Default "1" -Name "START_SCANNER" -Allowed @("0", "1")
$StartTrend = Resolve-Option -Value $StartTrend -Default "1" -Name "START_TREND" -Allowed @("0", "1")
$StartMarketHistory = Resolve-Option -Value $StartMarketHistory -Default "auto" -Name "START_MARKET_HISTORY" -Allowed @("0", "1", "auto")
$StartReverseTunnel = Resolve-Option -Value $StartReverseTunnel -Default "auto" -Name "START_REVERSE_TUNNEL" -Allowed @("0", "1", "auto")
$StartApi = Resolve-Option -Value $StartApi -Default "auto" -Name "START_API" -Allowed @("0", "1", "auto")
$StartTelegram = Resolve-Option -Value $StartTelegram -Default "auto" -Name "START_TELEGRAM" -Allowed @("0", "1", "auto")
$QuietDashboardUrl = Resolve-Option -Value $QuietDashboardUrl -Default "1" -Name "QUIET_DASHBOARD_URL" -Allowed @("0", "1")

if ([string]::IsNullOrWhiteSpace($PythonBin)) {
    $PythonBin = Join-Path $ProjectDir ".venv\Scripts\python.exe"
}
elseif (Test-Path -LiteralPath $PythonBin -PathType Leaf) {
    $PythonBin = [System.IO.Path]::GetFullPath($PythonBin)
}

function Write-AtomicText {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Value
    )

    $temporary = "$Path.$PID.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [System.IO.File]::WriteAllText($temporary, $Value, $Utf8NoBom)
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Read-JsonFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
    }
    catch {
        Write-Warning "Ignoring malformed state file: $Path"
        return $null
    }
}

function Resolve-ExecutablePath {
    param([Parameter(Mandatory = $true)][string]$FilePath)

    if (Test-Path -LiteralPath $FilePath -PathType Leaf) {
        return [System.IO.Path]::GetFullPath($FilePath)
    }
    $command = Get-Command $FilePath -ErrorAction Stop
    if ([string]::IsNullOrWhiteSpace($command.Source)) {
        throw "Executable could not be resolved: $FilePath"
    }
    return [System.IO.Path]::GetFullPath($command.Source)
}

function Get-LiveProcess {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    return Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
}

function Test-ProcessIdentity {
    param([Parameter(Mandatory = $true)]$State)

    $required = @("pid", "started_utc", "executable")
    foreach ($property in $required) {
        if ($State.PSObject.Properties.Name -notcontains $property) {
            return $false
        }
    }

    $processIdValue = 0
    if (-not [int]::TryParse([string]$State.pid, [ref]$processIdValue) -or $processIdValue -le 0) {
        return $false
    }
    $process = Get-LiveProcess -ProcessId $processIdValue
    if ($null -eq $process) {
        return $false
    }

    try {
        $expectedStart = [datetime]::Parse(
            [string]$State.started_utc,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::RoundtripKind
        ).ToUniversalTime()
        $actualStart = $process.StartTime.ToUniversalTime()
        if ([math]::Abs(($actualStart - $expectedStart).TotalSeconds) -gt 1.0) {
            return $false
        }

        $expectedExecutable = [System.IO.Path]::GetFullPath([string]$State.executable)
        $actualExecutable = [System.IO.Path]::GetFullPath($process.Path)
        return [string]::Equals(
            $expectedExecutable,
            $actualExecutable,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    }
    catch {
        return $false
    }
}

function Test-SupervisorProcessIdentity {
    param([Parameter(Mandatory = $true)]$State)

    foreach ($property in @("pid", "process_started_utc", "executable", "script_path")) {
        if ($State.PSObject.Properties.Name -notcontains $property) {
            return $false
        }
    }

    $processIdValue = 0
    if (-not [int]::TryParse([string]$State.pid, [ref]$processIdValue) -or $processIdValue -le 0) {
        return $false
    }
    $process = Get-LiveProcess -ProcessId $processIdValue
    if ($null -eq $process) {
        return $false
    }

    try {
        $expectedStart = [datetime]::Parse(
            [string]$State.process_started_utc,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::RoundtripKind
        ).ToUniversalTime()
        $actualStart = $process.StartTime.ToUniversalTime()
        $expectedExecutable = [System.IO.Path]::GetFullPath([string]$State.executable)
        $actualExecutable = [System.IO.Path]::GetFullPath($process.Path)
        $expectedScript = [System.IO.Path]::GetFullPath([string]$State.script_path)
        $actualScript = [System.IO.Path]::GetFullPath($PSCommandPath)
        return (
            [math]::Abs(($actualStart - $expectedStart).TotalSeconds) -le 1.0 -and
            [string]::Equals($expectedExecutable, $actualExecutable, [System.StringComparison]::OrdinalIgnoreCase) -and
            [string]::Equals($expectedScript, $actualScript, [System.StringComparison]::OrdinalIgnoreCase)
        )
    }
    catch {
        return $false
    }
}

function Test-ServiceDefinition {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$Arguments = @()
    )

    foreach ($property in @("name", "supervisor_instance", "arguments")) {
        if ($State.PSObject.Properties.Name -notcontains $property) {
            return $false
        }
    }
    try {
        $stateExecutable = [System.IO.Path]::GetFullPath([string]$State.executable)
    }
    catch {
        return $false
    }
    if (-not [string]::Equals([string]$State.name, $Name, [System.StringComparison]::Ordinal) -or
        -not [string]::Equals($stateExecutable, $Executable, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }

    $recordedArguments = @($State.arguments)
    if ($recordedArguments.Count -ne $Arguments.Count) {
        return $false
    }
    for ($index = 0; $index -lt $Arguments.Count; $index += 1) {
        if (-not [string]::Equals(
            [string]$recordedArguments[$index],
            [string]$Arguments[$index],
            [System.StringComparison]::Ordinal
        )) {
            return $false
        }
    }
    return $true
}

function Get-ServiceStatePath {
    param([Parameter(Mandatory = $true)][string]$Name)
    return (Join-Path $RunDir "$Name.windows.json")
}

function Get-ServicePidPath {
    param([Parameter(Mandatory = $true)][string]$Name)
    return (Join-Path $RunDir "$Name.pid")
}

function Read-ServiceState {
    param([Parameter(Mandatory = $true)][string]$Name)
    return (Read-JsonFile -Path (Get-ServiceStatePath -Name $Name))
}

function Remove-ServiceFiles {
    param([Parameter(Mandatory = $true)][string]$Name)
    Remove-CooperativeStopFile -State (Read-ServiceState -Name $Name) -Name $Name
    Remove-Item -LiteralPath (Get-ServicePidPath -Name $Name) -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Get-ServiceStatePath -Name $Name) -Force -ErrorAction SilentlyContinue
}

function ConvertTo-WindowsArgument {
    param([AllowEmptyString()][string]$Value)

    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') {
        return $Value
    }

    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append([char]34)
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]92) {
            $backslashes += 1
            continue
        }
        if ($character -eq [char]34) {
            if ($backslashes -gt 0) {
                [void]$builder.Append((("\" * (2 * $backslashes)) -join ""))
            }
            [void]$builder.Append([char]92)
            [void]$builder.Append([char]34)
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append((("\" * $backslashes) -join ""))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append((("\" * (2 * $backslashes)) -join ""))
    }
    [void]$builder.Append([char]34)
    return $builder.ToString()
}

function ConvertTo-WindowsCommandLine {
    param([string[]]$Arguments)
    return (($Arguments | ForEach-Object { ConvertTo-WindowsArgument -Value $_ }) -join " ")
}

function Rotate-Log {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ((Test-Path -LiteralPath $Path -PathType Leaf) -and (Get-Item -LiteralPath $Path).Length -gt 20MB) {
        $archive = "$Path.1"
        Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $Path -Destination $archive
    }
}

function Write-ServiceState {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$Arguments,
        [string]$StopToken = ""
    )

    $state = [ordered]@{
        schema_version = 1
        name = $Name
        pid = $Process.Id
        started_utc = $Process.StartTime.ToUniversalTime().ToString("o", [System.Globalization.CultureInfo]::InvariantCulture)
        executable = $Executable
        arguments = @($Arguments)
        supervisor_instance = $script:InstanceId
        stop_token = $StopToken
    }
    Write-AtomicText -Path (Get-ServiceStatePath -Name $Name) -Value ($state | ConvertTo-Json -Compress -Depth 4)
    Write-AtomicText -Path (Get-ServicePidPath -Name $Name) -Value "$($Process.Id)`r`n"
}

function Start-ServiceProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )

    Assert-StopNotRequested
    $executable = Resolve-ExecutablePath -FilePath $FilePath
    $state = Read-ServiceState -Name $Name
    if ($null -ne $state -and (Test-ProcessIdentity -State $state)) {
        if (-not (Test-ServiceDefinition -State $state -Name $Name -Executable $executable -Arguments $Arguments)) {
            throw "Refusing live state for $Name because its executable or arguments do not match this service."
        }

        $ownerInstance = [string]$state.supervisor_instance
        if ($ownerInstance -eq $script:InstanceId) {
            Write-Host "$Name already running under this supervisor: pid $($state.pid)"
        }
        elseif (-not [string]::IsNullOrWhiteSpace($script:RecoverableSupervisorInstance) -and
            $ownerInstance -eq $script:RecoverableSupervisorInstance) {
            $adoptedProcess = Get-LiveProcess -ProcessId ([int]$state.pid)
            $adoptedToken = if ($state.PSObject.Properties.Name -contains "stop_token") { [string]$state.stop_token } else { "" }
            Write-ServiceState `
                -Name $Name `
                -Process $adoptedProcess `
                -Executable $executable `
                -Arguments $Arguments `
                -StopToken $adoptedToken
            Write-Host "$Name adopted from stopped supervisor: pid $($state.pid)"
        }
        else {
            throw "Refusing to adopt live $Name PID $($state.pid) from an unverified supervisor."
        }
        if (-not $script:StartedServices.Contains($Name)) {
            [void]$script:StartedServices.Add($Name)
        }
        return [pscustomobject]@{ Started = $false; ProcessId = [int]$state.pid }
    }
    if ($null -ne $state) {
        $recordedProcess = Get-LiveProcess -ProcessId ([int]$state.pid)
        if ($null -eq $recordedProcess) {
            Remove-ServiceFiles -Name $Name
        }
        else {
            throw "Refusing to replace unverified live state for $Name (PID $($state.pid))."
        }
    }

    $pidPath = Get-ServicePidPath -Name $Name
    if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
        $rawProcessId = (Get-Content -LiteralPath $pidPath -Raw).Trim()
        $oldProcessId = 0
        if ([int]::TryParse($rawProcessId, [ref]$oldProcessId) -and $oldProcessId -gt 0) {
            $oldProcess = Get-LiveProcess -ProcessId $oldProcessId
            if ($null -ne $oldProcess) {
                throw "Refusing to overwrite $pidPath because PID $oldProcessId is alive without verified Windows state."
            }
        }
        Remove-Item -LiteralPath $pidPath -Force
    }

    $outputLog = Join-Path $LogDir "$Name.log"
    $errorLog = Join-Path $LogDir "$Name.error.log"
    Rotate-Log -Path $outputLog
    Rotate-Log -Path $errorLog
    Write-Host "starting $Name -> $outputLog (stderr: $errorLog)"

    $launchArguments = $Arguments
    $stopToken = ""
    if ($Arguments.Count -ge 2 -and $Arguments[0] -eq "-u" -and $Arguments[1] -like "*.py") {
        $stopToken = [guid]::NewGuid().ToString("N")
        $stopPath = Join-Path $RunDir "$Name.$stopToken.stop"
        $launchArguments = @("-u", (Join-Path $ProjectDir "windows_service_runner.py"),
            "--stop-file", $stopPath, "--stop-token", $stopToken) + $Arguments[1..($Arguments.Count - 1)]
    }
    $argumentLine = ConvertTo-WindowsCommandLine -Arguments $launchArguments
    $process = Start-Process `
        -FilePath $executable `
        -ArgumentList $argumentLine `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $outputLog `
        -RedirectStandardError $errorLog `
        -WindowStyle Hidden `
        -PassThru

    try {
        Write-ServiceState -Name $Name -Process $process -Executable $executable -Arguments $Arguments -StopToken $stopToken
    }
    catch {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw
    }
    if (-not $script:StartedServices.Contains($Name)) {
        [void]$script:StartedServices.Add($Name)
    }
    Write-Host "$Name started: pid $($process.Id)"
    return [pscustomobject]@{ Started = $true; ProcessId = $process.Id }
}

function Show-ServiceLogTail {
    param([Parameter(Mandatory = $true)][string]$Name)

    foreach ($suffix in @(".log", ".error.log")) {
        $path = Join-Path $LogDir "$Name$suffix"
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Write-Warning "Last lines from $path"
            Get-Content -LiteralPath $path -Tail 40 -ErrorAction SilentlyContinue | Write-Warning
        }
    }
}

function Wait-ServiceStable {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [int]$Seconds = 5
    )

    for ($attempt = 0; $attempt -lt $Seconds; $attempt += 1) {
        Assert-StopNotRequested
        $state = Read-ServiceState -Name $Name
        if ($null -eq $state -or -not (Test-ProcessIdentity -State $state)) {
            Show-ServiceLogTail -Name $Name
            throw "$Name exited during startup."
        }
        Start-Sleep -Seconds 1
    }
    Assert-StopNotRequested
    Write-Host "$Name is stable"
}

function Invoke-TaskKillBestEffort {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [switch]$Force
    )

    $taskArguments = @("/PID", [string]$ProcessId, "/T")
    if ($Force) {
        $taskArguments += "/F"
    }
    try {
        # taskkill can report that a console-host child disappeared while it
        # was walking the tree.  That race is harmless; identity checks below
        # decide whether another forceful attempt is needed.
        & "$env:SystemRoot\System32\taskkill.exe" @taskArguments 2>$null | Out-Null
    }
    catch {
        # Best effort by design; never let native stderr bypass cleanup.
    }
}

function Stop-ServiceProcess {
    param([Parameter(Mandatory = $true)][string]$Name)

    $state = Read-ServiceState -Name $Name
    if ($null -eq $state) {
        $pidPath = Get-ServicePidPath -Name $Name
        if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
            $rawProcessId = (Get-Content -LiteralPath $pidPath -Raw).Trim()
            $staleProcessId = 0
            if ([int]::TryParse($rawProcessId, [ref]$staleProcessId) -and
                $null -eq (Get-LiveProcess -ProcessId $staleProcessId)) {
                Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
            }
        }
        return
    }

    $stateOwner = if ($state.PSObject.Properties.Name -contains "supervisor_instance") {
        [string]$state.supervisor_instance
    }
    else {
        ""
    }
    $stateName = if ($state.PSObject.Properties.Name -contains "name") {
        [string]$state.name
    }
    else {
        ""
    }
    $ownedByCurrent = [string]::Equals(
        $stateOwner,
        $script:InstanceId,
        [System.StringComparison]::OrdinalIgnoreCase
    )
    $ownedByRecoverable = (
        -not [string]::IsNullOrWhiteSpace($script:RecoverableSupervisorInstance) -and
        [string]::Equals(
            $stateOwner,
            $script:RecoverableSupervisorInstance,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    )
    if (-not [string]::Equals($stateName, $Name, [System.StringComparison]::Ordinal) -or
        (-not $ownedByCurrent -and -not $ownedByRecoverable)) {
        Write-Warning "Refusing to stop $Name because its state is not owned by this or the recoverable supervisor."
        return
    }

    $recordedProcessId = 0
    if ($state.PSObject.Properties.Name -notcontains "pid" -or
        -not [int]::TryParse([string]$state.pid, [ref]$recordedProcessId) -or
        $recordedProcessId -le 0) {
        Write-Warning "Refusing invalid PID state for $Name; no process was stopped."
        return
    }

    if (-not (Test-ProcessIdentity -State $state)) {
        $recordedProcess = Get-LiveProcess -ProcessId $recordedProcessId
        if ($null -eq $recordedProcess) {
            Remove-ServiceFiles -Name $Name
        }
        else {
            Write-Warning "Refusing to stop unverified PID $($state.pid) for $Name."
        }
        return
    }

    $serviceProcessId = $recordedProcessId
    Write-Host "stopping $Name`: pid $serviceProcessId"
    $graceDeadline = Request-CooperativeServiceStop -State $state -Name $Name
    if ($null -ne $graceDeadline) {
        while ([datetime]::UtcNow -lt $graceDeadline -and (Test-ProcessIdentity -State $state)) {
            Start-Sleep -Milliseconds 100
        }
    }
    else {
        Invoke-TaskKillBestEffort -ProcessId $serviceProcessId
        for ($attempt = 0; $attempt -lt 8; $attempt += 1) {
            if (-not (Test-ProcessIdentity -State $state)) { break }
            Start-Sleep -Milliseconds 250
        }
    }
    if (Test-ProcessIdentity -State $state) {
        Write-Warning "$Name did not finish within its stop grace period; terminating its verified process tree."
        Invoke-TaskKillBestEffort -ProcessId $serviceProcessId -Force
    }
    for ($attempt = 0; $attempt -lt 12; $attempt += 1) {
        if (-not (Test-ProcessIdentity -State $state)) {
            break
        }
        Start-Sleep -Milliseconds 250
    }

    if (Test-ProcessIdentity -State $state) {
        Write-Warning "$Name PID $serviceProcessId did not stop; its state files were preserved."
    }
    else {
        Remove-ServiceFiles -Name $Name
    }
}

function Stop-StartedServices {
    if ($script:StartedServices.Count -eq 0) {
        return
    }
    Write-Host ""
    Write-Host "stopping services started by this command..."
    foreach ($serviceName in $script:StartedServices) {
        $serviceState = Read-ServiceState -Name $serviceName
        if ($null -ne $serviceState -and
            $serviceState.PSObject.Properties.Name -contains "supervisor_instance" -and
            [string]$serviceState.supervisor_instance -eq $script:InstanceId -and
            (Test-ProcessIdentity -State $serviceState)) {
            $null = Request-CooperativeServiceStop -State $serviceState -Name $serviceName
        }
    }
    for ($index = $script:StartedServices.Count - 1; $index -ge 0; $index -= 1) {
        Stop-ServiceProcess -Name $script:StartedServices[$index]
    }
}

function Stop-RecoverableServices {
    if ([string]::IsNullOrWhiteSpace($script:RecoverableSupervisorInstance)) {
        return
    }
    foreach ($serviceName in $script:KnownServiceNames) {
        $serviceState = Read-ServiceState -Name $serviceName
        if ($null -ne $serviceState -and
            $serviceState.PSObject.Properties.Name -contains "supervisor_instance" -and
            [string]$serviceState.supervisor_instance -eq $script:RecoverableSupervisorInstance -and
            (Test-ProcessIdentity -State $serviceState)) {
            $null = Request-CooperativeServiceStop -State $serviceState -Name $serviceName
        }
    }

    for ($index = $script:KnownServiceNames.Count - 1; $index -ge 0; $index -= 1) {
        $serviceName = $script:KnownServiceNames[$index]
        $statePath = Get-ServiceStatePath -Name $serviceName
        if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
            continue
        }
        $state = Read-ServiceState -Name $serviceName
        if ($null -eq $state) {
            throw "Cannot recover malformed service state: $statePath"
        }
        if ($state.PSObject.Properties.Name -notcontains "supervisor_instance" -or
            -not [string]::Equals(
                [string]$state.supervisor_instance,
                $script:RecoverableSupervisorInstance,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
            Write-Warning "Preserving $serviceName because it does not belong to the stopped supervisor."
            continue
        }

        Stop-ServiceProcess -Name $serviceName
        if (Test-Path -LiteralPath $statePath -PathType Leaf) {
            throw "Could not safely stop stale $serviceName; its state was preserved."
        }
    }
    $script:RecoverableSupervisorInstance = ""
}

function Invoke-PythonCapture {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $output = & $PythonBin @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $detail = ($output | Out-String).Trim()
        throw "Python command failed with exit code $exitCode. $detail"
    }
    return (($output | Out-String).Trim())
}

function Invoke-PythonCodeCapture {
    param([Parameter(Mandatory = $true)][string]$Code)

    $output = $Code | & $PythonBin - 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $detail = ($output | Out-String).Trim()
        throw "Python code failed with exit code $exitCode. $detail"
    }
    return (($output | Out-String).Trim())
}

function Test-HttpService {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$ExpectedService,
        [int]$TimeoutSeconds = 3
    )

    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSeconds
        $payload = $response.Content | ConvertFrom-Json
        return ([string]$payload.service -eq $ExpectedService)
    }
    catch {
        return $false
    }
}

function Test-TcpPort {
    param(
        [Parameter(Mandatory = $true)][string]$HostName,
        [Parameter(Mandatory = $true)][int]$Port
    )

    $client = New-Object System.Net.Sockets.TcpClient
    $asyncResult = $null
    try {
        $asyncResult = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $asyncResult.AsyncWaitHandle.WaitOne(500)) {
            return $false
        }
        $client.EndConnect($asyncResult)
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $asyncResult) {
            $asyncResult.AsyncWaitHandle.Close()
        }
        $client.Close()
    }
}

function Wait-HttpService {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$ExpectedService,
        [int]$Attempts
    )

    for ($attempt = 0; $attempt -lt $Attempts; $attempt += 1) {
        Assert-StopNotRequested
        if (Test-HttpService -Url $Url -ExpectedService $ExpectedService) {
            Write-Host "$Name is healthy: $Url"
            return
        }
        $state = Read-ServiceState -Name $Name
        if ($null -ne $state -and -not (Test-ProcessIdentity -State $state)) {
            Show-ServiceLogTail -Name $Name
            throw "$Name exited during startup."
        }
        Start-Sleep -Seconds 1
    }
    throw "$Name did not become healthy. Check $(Join-Path $LogDir "$Name.error.log")."
}

function Enable-KeepAwake {
    if ($KeepAwake -ne "1") {
        return
    }
    try {
        if ($null -eq ("Cs2Windows.PowerState" -as [type])) {
            Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
namespace Cs2Windows {
    public static class PowerState {
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern uint SetThreadExecutionState(uint executionState);
    }
}
"@
        }
        $result = [Cs2Windows.PowerState]::SetThreadExecutionState([uint32]2147483649)
        if ($result -eq 0) {
            throw "SetThreadExecutionState returned zero"
        }
        $script:KeepAwakeEnabled = $true
        Write-Host "Windows sleep prevention is active while this script is running."
    }
    catch {
        Write-Warning "Could not enable Windows sleep prevention. $($_.Exception.Message)"
    }
}

function Disable-KeepAwake {
    if (-not $script:KeepAwakeEnabled) {
        return
    }
    try {
        [void][Cs2Windows.PowerState]::SetThreadExecutionState([uint32]2147483648)
    }
    catch {
        Write-Warning "Could not restore the Windows sleep policy. $($_.Exception.Message)"
    }
    $script:KeepAwakeEnabled = $false
}

function Read-HostingConfiguration {
    $configPathValue = [Environment]::GetEnvironmentVariable("HOSTING_CONFIG_FILE", "Process")
    $configPath = if ([string]::IsNullOrWhiteSpace($configPathValue)) {
        Join-Path $ProjectDir "hosting.local.env"
    }
    elseif ([System.IO.Path]::IsPathRooted($configPathValue)) {
        [System.IO.Path]::GetFullPath($configPathValue)
    }
    else {
        [System.IO.Path]::GetFullPath((Join-Path $ProjectDir $configPathValue))
    }
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw "Missing $configPath. Copy hosting.local.env.example and fill VPS_HOST."
    }

    $allowedKeys = @(
        "VPS_HOST", "VPS_USER", "VPS_SSH_PORT", "VPS_IDENTITY_FILE", "VPS_PASSWORD",
        "REMOTE_BIND_HOST", "REMOTE_PORT", "LOCAL_HOST", "LOCAL_PORT", "PUBLIC_SITE_URL"
    )
    $settings = @{}
    foreach ($rawLine in [System.IO.File]::ReadAllLines($configPath)) {
        $line = $rawLine.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith("#")) {
            continue
        }
        $separator = $line.IndexOf("=")
        if ($separator -le 0) {
            throw "Invalid line in $configPath`: expected KEY=VALUE."
        }
        $keyName = $line.Substring(0, $separator).Trim()
        if ($keyName -notmatch '^[A-Z][A-Z0-9_]*$' -or $allowedKeys -notcontains $keyName) {
            throw "Unknown or invalid setting in $configPath`: $keyName"
        }
        if ($settings.ContainsKey($keyName)) {
            throw "Duplicate setting in $configPath`: $keyName"
        }
        $settingValue = $line.Substring($separator + 1).Trim()
        if ($settingValue.Length -ge 2 -and
            (($settingValue[0] -eq [char]34 -and $settingValue[$settingValue.Length - 1] -eq [char]34) -or
             ($settingValue[0] -eq [char]39 -and $settingValue[$settingValue.Length - 1] -eq [char]39))) {
            $settingValue = $settingValue.Substring(1, $settingValue.Length - 2)
        }
        $settings[$keyName] = $settingValue
    }

    $vpsHost = [string]$settings["VPS_HOST"]
    if ([string]::IsNullOrWhiteSpace($vpsHost) -or $vpsHost -eq "YOUR_SERVER_IP") {
        throw "Set VPS_HOST in $configPath."
    }
    if ($vpsHost -match '[:][/][/]|[/]|\s') {
        throw "VPS_HOST must contain only a hostname or IP address."
    }
    $vpsUser = if ([string]::IsNullOrWhiteSpace([string]$settings["VPS_USER"])) { "root" } else { [string]$settings["VPS_USER"] }
    if ($vpsUser -match '\s') {
        throw "VPS_USER must not contain spaces."
    }

    $ports = @{}
    foreach ($portSetting in @(
        @("VPS_SSH_PORT", 22),
        @("REMOTE_PORT", 8010),
        @("LOCAL_PORT", 8010)
    )) {
        $rawPort = [string]$settings[$portSetting[0]]
        $portValue = if ([string]::IsNullOrWhiteSpace($rawPort)) { [int]$portSetting[1] } else { 0 }
        if (-not [string]::IsNullOrWhiteSpace($rawPort) -and
            (-not [int]::TryParse($rawPort, [ref]$portValue) -or $portValue -lt 1 -or $portValue -gt 65535)) {
            throw "$($portSetting[0]) must be an integer from 1 to 65535."
        }
        $ports[$portSetting[0]] = $portValue
    }

    $identityValue = [string]$settings["VPS_IDENTITY_FILE"]
    $identityPath = ""
    if (-not [string]::IsNullOrWhiteSpace($identityValue)) {
        $userProfile = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
        if ($identityValue -eq "~") {
            $identityValue = $userProfile
        }
        elseif ($identityValue.StartsWith("~\") -or $identityValue.StartsWith("~/")) {
            $identityValue = Join-Path $userProfile $identityValue.Substring(2)
        }
        $identityPath = if ([System.IO.Path]::IsPathRooted($identityValue)) {
            [System.IO.Path]::GetFullPath($identityValue)
        }
        else {
            [System.IO.Path]::GetFullPath((Join-Path $ProjectDir $identityValue))
        }
        if (-not (Test-Path -LiteralPath $identityPath -PathType Leaf)) {
            throw "VPS_IDENTITY_FILE does not point to a readable private key."
        }
    }

    $hasPassword = -not [string]::IsNullOrWhiteSpace([string]$settings["VPS_PASSWORD"])
    $settings["VPS_PASSWORD"] = ""
    $usePassword = [string]::IsNullOrWhiteSpace($identityPath) -and $hasPassword
    $askPassPath = ""
    if ([string]::IsNullOrWhiteSpace($identityPath)) {
        if (-not $hasPassword) {
            throw "Set VPS_IDENTITY_FILE (recommended) or VPS_PASSWORD for one-command reverse-tunnel startup."
        }
        $askPassPath = Join-Path $ProjectDir "windows_ssh_askpass.cmd"
        if (-not (Test-Path -LiteralPath $askPassPath -PathType Leaf)) {
            throw "Windows SSH askpass helper is missing: $askPassPath"
        }
    }

    $remoteBindHost = if ([string]::IsNullOrWhiteSpace([string]$settings["REMOTE_BIND_HOST"])) { "127.0.0.1" } else { [string]$settings["REMOTE_BIND_HOST"] }
    $localHost = if ([string]::IsNullOrWhiteSpace([string]$settings["LOCAL_HOST"])) { "127.0.0.1" } else { [string]$settings["LOCAL_HOST"] }
    if ($remoteBindHost -match '\s' -or $localHost -match '\s') {
        throw "Tunnel bind hosts must not contain spaces."
    }

    return [pscustomobject]@{
        SshExecutable = Resolve-ExecutablePath -FilePath "ssh.exe"
        VpsHost = $vpsHost
        VpsUser = $vpsUser
        VpsSshPort = [int]$ports["VPS_SSH_PORT"]
        IdentityPath = $identityPath
        UsePassword = $usePassword
        AskPassPath = $askPassPath
        HostingConfigPath = $configPath
        RemoteBindHost = $remoteBindHost
        RemotePort = [int]$ports["REMOTE_PORT"]
        LocalHost = $localHost
        LocalPort = [int]$ports["LOCAL_PORT"]
        PublicSiteUrl = [string]$settings["PUBLIC_SITE_URL"]
    }
}

function Start-ReverseTunnelService {
    param([Parameter(Mandatory = $true)]$Configuration)

    $forward = "$($Configuration.RemoteBindHost):$($Configuration.RemotePort):$($Configuration.LocalHost):$($Configuration.LocalPort)"
    $destination = "$($Configuration.VpsUser)@$($Configuration.VpsHost)"
    $tunnelArguments = [System.Collections.Generic.List[string]]@(
        "-N", "-T",
        "-p", [string]$Configuration.VpsSshPort,
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=accept-new",
        "-R", $forward
    )
    if ($Configuration.UsePassword) {
        [void]$tunnelArguments.Add("-o")
        [void]$tunnelArguments.Add("BatchMode=no")
        [void]$tunnelArguments.Add("-o")
        [void]$tunnelArguments.Add("NumberOfPasswordPrompts=1")
        [void]$tunnelArguments.Add("-o")
        [void]$tunnelArguments.Add("PubkeyAuthentication=no")
        [void]$tunnelArguments.Add("-o")
        [void]$tunnelArguments.Add("PreferredAuthentications=password,keyboard-interactive")
    }
    else {
        [void]$tunnelArguments.Add("-o")
        [void]$tunnelArguments.Add("BatchMode=yes")
        [void]$tunnelArguments.Add("-i")
        [void]$tunnelArguments.Add([string]$Configuration.IdentityPath)
    }
    [void]$tunnelArguments.Add($destination)

    if (-not $Configuration.UsePassword) {
        [void](Start-ServiceProcess -Name "reverse_tunnel" -FilePath $Configuration.SshExecutable -Arguments $tunnelArguments.ToArray())
        return
    }

    $environmentNames = @("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "DISPLAY", "CS2_SSH_ASKPASS_CONFIG")
    $previousEnvironment = @{}
    foreach ($environmentName in $environmentNames) {
        $previousEnvironment[$environmentName] = [Environment]::GetEnvironmentVariable($environmentName, "Process")
    }
    try {
        [Environment]::SetEnvironmentVariable("SSH_ASKPASS", [string]$Configuration.AskPassPath, "Process")
        [Environment]::SetEnvironmentVariable("SSH_ASKPASS_REQUIRE", "force", "Process")
        [Environment]::SetEnvironmentVariable("DISPLAY", "cs2-ssh-askpass", "Process")
        [Environment]::SetEnvironmentVariable("CS2_SSH_ASKPASS_CONFIG", [string]$Configuration.HostingConfigPath, "Process")
        [void](Start-ServiceProcess -Name "reverse_tunnel" -FilePath $Configuration.SshExecutable -Arguments $tunnelArguments.ToArray())
    }
    finally {
        foreach ($environmentName in $environmentNames) {
            [Environment]::SetEnvironmentVariable($environmentName, $previousEnvironment[$environmentName], "Process")
        }
    }
}

function Wait-PublicSite {
    param([Parameter(Mandatory = $true)][string]$BaseUrl)
    for ($attempt = 0; $attempt -lt 6; $attempt += 1) {
        Assert-StopNotRequested
        if (Test-HttpService -Url "$BaseUrl/health" -ExpectedService "cs2-dashboard") {
            Write-Host "public site is healthy: $BaseUrl/health"
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Wait-QuickTunnel {
    for ($attempt = 0; $attempt -lt 120; $attempt += 1) {
        Assert-StopNotRequested
        $state = Read-ServiceState -Name "public_tunnel"
        if ($null -eq $state -or -not (Test-ProcessIdentity -State $state)) {
            Show-ServiceLogTail -Name "public_tunnel"
            return ""
        }
        foreach ($path in @(
            (Join-Path $LogDir "public_tunnel.log"),
            (Join-Path $LogDir "public_tunnel.error.log")
        )) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
                continue
            }
            $content = Get-Content -LiteralPath $path -Raw -ErrorAction SilentlyContinue
            $matches = [regex]::Matches([string]$content, 'https://[-a-zA-Z0-9]+[.]trycloudflare[.]com')
            if ($matches.Count -gt 0) {
                $candidate = $matches[$matches.Count - 1].Value
                if (Test-HttpService -Url "$candidate/health" -ExpectedService "cs2-dashboard" -TimeoutSeconds 5) {
                    Write-AtomicText -Path $PublicUrlFile -Value "$candidate`r`n"
                    Write-Host "temporary public site: $candidate"
                    return $candidate
                }
            }
        }
        Start-Sleep -Seconds 1
    }
    return ""
}

function Write-SupervisorState {
    $state = [ordered]@{
        schema_version = 1
        instance_id = $script:InstanceId
        pid = $PID
        process_started_utc = $script:SupervisorStartUtc
        executable = (Get-Process -Id $PID).Path
        script_path = [System.IO.Path]::GetFullPath($PSCommandPath)
        created_utc = [datetime]::UtcNow.ToString("o", [System.Globalization.CultureInfo]::InvariantCulture)
    }
    Write-AtomicText -Path $SupervisorStateFile -Value ($state | ConvertTo-Json -Compress)
    Write-AtomicText -Path $SupervisorPidFile -Value "$PID`r`n"
}

function Test-StopRequested {
    if (-not [string]::IsNullOrWhiteSpace($env:CS2_LAUNCHER_STOP_FILE) -and
        (Test-Path -LiteralPath $env:CS2_LAUNCHER_STOP_FILE -PathType Leaf)) {
        return $true
    }
    if (-not (Test-Path -LiteralPath $StopRequestFile -PathType Leaf)) {
        return $false
    }
    try {
        $request = Read-JsonFile -Path $StopRequestFile
        return ($null -ne $request -and
            $request.PSObject.Properties.Name -contains "instance_id" -and
            [string]$request.instance_id -eq $script:InstanceId)
    }
    catch {
        return $false
    }
}

function Assert-StopNotRequested {
    if (Test-StopRequested) {
        throw [System.OperationCanceledException]::new("Stop requested by stop_all.cmd.")
    }
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
New-Item -ItemType Directory -Path $RunDir -Force | Out-Null

Push-Location -LiteralPath $ProjectDir
try {
    Assert-StopNotRequested
    try {
        $script:StartupLock = [System.IO.File]::Open(
            $SupervisorLockFile,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    }
    catch [System.IO.IOException] {
        throw "Another start_all.ps1 command is already running."
    }

    $lockPayload = $Utf8NoBom.GetBytes("$PID $($script:InstanceId)`r`n")
    $script:StartupLock.SetLength(0)
    $script:StartupLock.Write($lockPayload, 0, $lockPayload.Length)
    $script:StartupLock.Flush()

    $previousSupervisorState = Read-JsonFile -Path $SupervisorStateFile
    if ($null -ne $previousSupervisorState) {
        if (Test-SupervisorProcessIdentity -State $previousSupervisorState) {
            throw "A verified start_all.cmd supervisor is still running despite the startup lock."
        }
        $previousInstance = if ($previousSupervisorState.PSObject.Properties.Name -contains "instance_id") {
            [string]$previousSupervisorState.instance_id
        }
        else {
            ""
        }
        $previousScript = if ($previousSupervisorState.PSObject.Properties.Name -contains "script_path") {
            [string]$previousSupervisorState.script_path
        }
        else {
            ""
        }
        $scriptMatches = $false
        try {
            $scriptMatches = (
                -not [string]::IsNullOrWhiteSpace($previousScript) -and
                [string]::Equals(
                    [System.IO.Path]::GetFullPath($previousScript),
                    [System.IO.Path]::GetFullPath($PSCommandPath),
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            )
        }
        catch {
            $scriptMatches = $false
        }
        if ($previousInstance -match '^[0-9a-fA-F]{32}$' -and $scriptMatches) {
            $script:RecoverableSupervisorInstance = $previousInstance
            Write-Host "Cleaning up services from stopped supervisor instance $previousInstance."
        }
        else {
            Write-Warning "Previous supervisor state is not trustworthy; live service processes will not be adopted."
        }
    }
    Stop-RecoverableServices
    Remove-Item -LiteralPath $StopRequestFile -Force -ErrorAction SilentlyContinue
    Write-SupervisorState
    Assert-StopNotRequested

    if (-not (Test-Path -LiteralPath $PythonBin -PathType Leaf)) {
        throw "Python environment not found: $PythonBin. Run .\setup_windows.cmd first."
    }
    & $PythonBin -m pip check | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "The Python environment has broken dependencies. Run .\setup_windows.cmd again."
    }
    Assert-StopNotRequested

    & $PythonBin model_training_activation.py --activate-pending --offline-confirm
    if ($LASTEXITCODE -ne 0) {
        throw "Pending model activation/recovery failed; services were not started."
    }
    Assert-StopNotRequested

    $preflightCode = @"
import os
import app_config
import model_generations

required = {
    "database": app_config.path("CS2_DB_PATH", "csfloat_history.db"),
    "skin catalog": app_config.path("CS2_SKINS_PATH", "ALL_SKINS.json"),
    "Almaz model": app_config.PROJECT_DIR / "models" / "csfloat_price_model.joblib",
    "Igor point model": app_config.PROJECT_DIR / "IGORMODEL" / "artifacts" / "price_model_point.cbm",
    "Igor low model": app_config.PROJECT_DIR / "IGORMODEL" / "artifacts" / "price_model_q15.cbm",
    "Igor high model": app_config.PROJECT_DIR / "IGORMODEL" / "artifacts" / "price_model_q85.cbm",
    "Igor metadata": app_config.PROJECT_DIR / "IGORMODEL" / "artifacts" / "feature_metadata.pkl",
    "Nova model": app_config.PROJECT_DIR / "NEURALMODEL" / "artifacts" / "nova_price_model.pt",
    "Nova metadata": app_config.PROJECT_DIR / "NEURALMODEL" / "artifacts" / "nova_metadata.json",
}
vega_dir = None
vega_pointer = model_generations.read_active_pointer(
    "vega", project_dir=app_config.PROJECT_DIR
)
if vega_pointer is not None:
    vega_dir = model_generations.active_or_legacy_bundle_dir(
        "vega",
        app_config.PROJECT_DIR / "VEGAMODEL" / "artifacts",
        project_dir=app_config.PROJECT_DIR,
        verify_files=True,
    )
    required.update({
        "Vega model": vega_dir / "vega_price_model.pt",
        "Vega metadata": vega_dir / "vega_metadata.json",
        "Vega metrics": vega_dir / "vega_metrics.json",
    })
missing = [f"{label}: {path}" for label, path in required.items() if not path.is_file()]
if missing:
    raise SystemExit("Missing required project files:\n- " + "\n- ".join(missing))
if vega_dir is not None:
    import vega_model_runtime
    vega_bundle = vega_model_runtime.load_bundle_from_directory(vega_dir)
    vega_model_runtime.smoke_bundle(vega_bundle)
if os.environ.get("CS2_CHECK_CSFLOAT_KEYS") == "1":
    from csfloat_listing_fetcher import load_key_records
    if not load_key_records(strict=True):
        raise SystemExit("No usable CSFloat API routes were found")
print("preflight: database, skins, models and CSFloat routes are ready")
"@
    $oldCsfloatCheck = [Environment]::GetEnvironmentVariable("CS2_CHECK_CSFLOAT_KEYS", "Process")
    try {
        $checkKeys = if ($StartParser -eq "1" -or $StartScanner -eq "1") { "1" } else { "0" }
        [Environment]::SetEnvironmentVariable("CS2_CHECK_CSFLOAT_KEYS", $checkKeys, "Process")
        $preflightCode | & $PythonBin -
        if ($LASTEXITCODE -ne 0) {
            throw "Project preflight failed."
        }
        Assert-StopNotRequested
    }
    finally {
        [Environment]::SetEnvironmentVariable("CS2_CHECK_CSFLOAT_KEYS", $oldCsfloatCheck, "Process")
    }

    if ($StartTrend -eq "1") {
        & $PythonBin -c "import TRENDMODEL, trend_worker"
        if ($LASTEXITCODE -ne 0) {
            throw "Trend worker dependencies could not be imported."
        }
    }

    $dashboardConfig = Invoke-PythonCodeCapture -Code @"
import app_config, json
print(json.dumps({'host': app_config.get('DASHBOARD_HOST', '127.0.0.1'), 'port': app_config.get_int('DASHBOARD_PORT', 8010, minimum=1, maximum=65535)}))
"@ | ConvertFrom-Json
    $dashboardHost = [string]$dashboardConfig.host
    $dashboardPort = [int]$dashboardConfig.port
    $healthHost = if ($dashboardHost -eq "0.0.0.0" -or $dashboardHost -eq "::") { "127.0.0.1" } else { $dashboardHost }
    $healthUrl = "http://${healthHost}:$dashboardPort/health"

    $apiStatus = Invoke-PythonCodeCapture -Code @"
import app_config
token = app_config.read_secret("API_TOKEN", app_config.path("API_TOKEN_PATH", "api_token.txt"))
database = app_config.path("API_DB_PATH", "csfloat_sales_history.db")
print("missing_token" if not token else "missing_database" if not database.is_file() else "ready")
"@
    $apiMessage = switch ($apiStatus) {
        "ready" { "ready" }
        "missing_token" { "API_TOKEN is empty" }
        "missing_database" { "API_DB_PATH does not exist" }
        default { $apiStatus }
    }
    $apiEnabled = $false
    if ($StartApi -eq "1" -and $apiStatus -ne "ready") {
        throw "START_API=1, but the export API is not configured ($apiMessage)."
    }
    elseif ($StartApi -eq "1" -or ($StartApi -eq "auto" -and $apiStatus -eq "ready")) {
        $apiEnabled = $true
    }
    elseif ($StartApi -eq "auto") {
        Write-Host "export API: skipped ($apiMessage; it is optional for the site)"
    }

    $telegramStatus = Invoke-PythonCodeCapture -Code @"
import app_config
token = app_config.read_secret("TELEGRAM_BOT_TOKEN", "telegram_bot_token.txt")
allowed = [value.strip() for value in app_config.get("TELEGRAM_ALLOWED_CHAT_IDS").split(",") if value.strip().lstrip("-").isdigit()]
print("missing_token" if not token else "missing_allowed_chat_ids" if not allowed and not app_config.get_bool("TELEGRAM_ALLOW_ALL", False) else "ready")
"@
    $telegramMessage = switch ($telegramStatus) {
        "ready" { "ready" }
        "missing_token" { "TELEGRAM_BOT_TOKEN is empty" }
        "missing_allowed_chat_ids" { "TELEGRAM_ALLOWED_CHAT_IDS is empty" }
        default { $telegramStatus }
    }
    $telegramEnabled = $false
    if ($StartTelegram -eq "1" -and $telegramStatus -ne "ready") {
        throw "START_TELEGRAM=1, but Telegram is not configured ($telegramMessage)."
    }
    elseif ($StartTelegram -eq "1" -or ($StartTelegram -eq "auto" -and $telegramStatus -eq "ready")) {
        $telegramEnabled = $true
    }
    elseif ($StartTelegram -eq "auto") {
        Write-Host "Telegram bot: skipped ($telegramMessage; access remains locked)"
    }

    $marketHistoryStatus = Invoke-PythonCodeCapture -Code @"
import app_config
enabled = app_config.get_bool("CS2_MARKET_HISTORY_ENABLED", False)
license_confirmed = app_config.get_bool("CS2_MARKET_HISTORY_LICENSE_CONFIRMED", False)
api_key = app_config.read_secret("CSPRICEAPI_KEY")
fetch_snapshots = app_config.get_bool("CS2_MARKET_HISTORY_FETCH_SNAPSHOTS", True)
fetch_sales = app_config.get_bool("CS2_MARKET_HISTORY_FETCH_SALES", True)
print("disabled" if not enabled else "license_unconfirmed" if not license_confirmed else "missing_key" if not api_key else "no_fetches" if not fetch_snapshots and not fetch_sales else "ready")
"@
    $marketHistoryMessage = switch ($marketHistoryStatus) {
        "ready" { "ready" }
        "disabled" { "CS2_MARKET_HISTORY_ENABLED is false" }
        "license_unconfirmed" { "written provider license is not confirmed" }
        "missing_key" { "CSPRICEAPI_KEY is empty" }
        "no_fetches" { "all market history fetches are disabled" }
        default { $marketHistoryStatus }
    }
    $marketHistoryEnabled = $false
    if ($StartMarketHistory -eq "1" -and $marketHistoryStatus -ne "ready") {
        throw "START_MARKET_HISTORY=1, but automatic market history is not configured ($marketHistoryMessage)."
    }
    elseif ($StartMarketHistory -eq "1" -or ($StartMarketHistory -eq "auto" -and $marketHistoryStatus -eq "ready")) {
        & $PythonBin -c "import market_history, market_history_worker"
        if ($LASTEXITCODE -ne 0) {
            throw "Market history worker dependencies could not be imported."
        }
        $marketHistoryEnabled = $true
    }
    elseif ($StartMarketHistory -eq "auto") {
        Write-Host "market history worker: skipped ($marketHistoryMessage; no provider request was made)"
    }

    $reverseTunnelEnabled = $false
    $reverseConfiguration = $null
    if ($StartReverseTunnel -ne "0") {
        try {
            $reverseConfiguration = Read-HostingConfiguration
            $reverseTunnelEnabled = $true
            Write-Host "reverse tunnel configuration: ready"
        }
        catch {
            if ($StartReverseTunnel -eq "1") {
                throw
            }
            Write-Host "reverse tunnel: skipped ($($_.Exception.Message))"
        }
    }

    Assert-StopNotRequested
    Enable-KeepAwake

    if (Test-HttpService -Url $healthUrl -ExpectedService "cs2-dashboard") {
        Write-Host "dashboard already healthy: $healthUrl"
    }
    else {
        if (Test-TcpPort -HostName $healthHost -Port $dashboardPort) {
            throw "Port $dashboardPort is already in use by something other than the CS2 dashboard."
        }
        [void](Start-ServiceProcess -Name "dashboard" -FilePath $PythonBin -Arguments @("-u", "dashboard_server.py"))
    }
    Wait-HttpService -Name "dashboard" -Url $healthUrl -ExpectedService "cs2-dashboard" -Attempts 180
    [void]$script:CoreServices.Add("dashboard")

    if ($StartTrend -eq "1") {
        [void](Start-ServiceProcess -Name "trend_worker" -FilePath $PythonBin -Arguments @("-u", "trend_worker.py"))
        Wait-ServiceStable -Name "trend_worker" -Seconds 3
        [void]$script:CoreServices.Add("trend_worker")
    }

    if ($marketHistoryEnabled) {
        [void](Start-ServiceProcess -Name "market_history_worker" -FilePath $PythonBin -Arguments @("-u", "market_history_worker.py", "--loop"))
        Wait-ServiceStable -Name "market_history_worker" -Seconds 3
        [void]$script:CoreServices.Add("market_history_worker")
    }

    $publicSiteVerified = $false
    if ($reverseTunnelEnabled) {
        Start-ReverseTunnelService -Configuration $reverseConfiguration
        Wait-ServiceStable -Name "reverse_tunnel" -Seconds 8
        if ([string]::IsNullOrWhiteSpace($PublicSiteUrl)) {
            $PublicSiteUrl = [string]$reverseConfiguration.PublicSiteUrl
        }
        if ([string]::IsNullOrWhiteSpace($PublicSiteUrl)) {
            $PublicSiteUrl = "https://csskinsprediction.online"
        }
        $PublicSiteUrl = $PublicSiteUrl.TrimEnd("/")
        if ($PublicSiteUrl -notmatch '^https?://[A-Za-z0-9._:-]+$') {
            throw "PUBLIC_SITE_URL must look like https://example.com (without a path)."
        }
        $publicSiteVerified = Wait-PublicSite -BaseUrl $PublicSiteUrl
        if (-not $publicSiteVerified) {
            Write-Warning "SSH is alive, but public /health could not be confirmed from this PC."
        }
    }

    if ($StartParser -eq "1") {
        [void](Start-ServiceProcess -Name "parser_final" -FilePath $PythonBin -Arguments @("-u", "parser_final.py"))
        Wait-ServiceStable -Name "parser_final" -Seconds 3
        [void]$script:CoreServices.Add("parser_final")
    }

    if ($StartScanner -eq "1") {
        [void](Start-ServiceProcess -Name "deal_scanner" -FilePath $PythonBin -Arguments @("-u", "deal_scanner.py"))
        Wait-ServiceStable -Name "deal_scanner" -Seconds 3
        [void]$script:CoreServices.Add("deal_scanner")
    }

    if ($apiEnabled) {
        $apiConfig = Invoke-PythonCodeCapture -Code @"
import app_config, json
print(json.dumps({'host': app_config.get('API_HOST', '127.0.0.1'), 'port': app_config.get_int('API_PORT', 8000, minimum=1, maximum=65535)}))
"@ | ConvertFrom-Json
        $apiHost = [string]$apiConfig.host
        $apiPort = [int]$apiConfig.port
        $apiHealthHost = if ($apiHost -eq "0.0.0.0" -or $apiHost -eq "::") { "127.0.0.1" } else { $apiHost }
        $apiHealthUrl = "http://${apiHealthHost}:$apiPort/health"
        [void](Start-ServiceProcess -Name "api_server" -FilePath $PythonBin -Arguments @("-u", "api_server.py"))
        Wait-HttpService -Name "api_server" -Url $apiHealthUrl -ExpectedService "cs2-export-api" -Attempts 60
        [void]$script:CoreServices.Add("api_server")
    }

    if ($telegramEnabled) {
        [void](Start-ServiceProcess -Name "telegram_bot" -FilePath $PythonBin -Arguments @("-u", "tg_deals_bot.py"))
        Wait-ServiceStable -Name "telegram_bot" -Seconds 3
        [void]$script:CoreServices.Add("telegram_bot")
    }

    $quickTunnelUrl = ""
    if ($PublicTunnel -eq "1") {
        if ($reverseTunnelEnabled) {
            Write-Warning "Both permanent reverse SSH and temporary Quick Tunnel are enabled."
        }
        $cloudflared = Get-Command "cloudflared.exe" -ErrorAction SilentlyContinue
        if ($null -eq $cloudflared) {
            Write-Warning "cloudflared is not installed; other services remain running."
        }
        else {
            [void](Start-ServiceProcess -Name "public_tunnel" -FilePath $cloudflared.Source -Arguments @(
                "tunnel", "--url", "http://${healthHost}:$dashboardPort", "--no-autoupdate", "--protocol", "http2"
            ))
            $quickTunnelUrl = Wait-QuickTunnel
            if ([string]::IsNullOrWhiteSpace($quickTunnelUrl)) {
                Write-Warning "Quick Tunnel failed; other services remain running."
                Stop-ServiceProcess -Name "public_tunnel"
                [void]$script:StartedServices.Remove("public_tunnel")
            }
        }
    }

    $dashboardAccess = Invoke-PythonCodeCapture -Code @"
import dashboard_server, json
print(json.dumps({'token': dashboard_server.DASHBOARD_TOKEN, 'path': str(dashboard_server.TOKEN_PATH)}))
"@ | ConvertFrom-Json
    $encodedToken = [uri]::EscapeDataString([string]$dashboardAccess.token)
    $localDashboardUrl = "http://${healthHost}:$dashboardPort/?token=$encodedToken"
    $dashboardUrl = $localDashboardUrl
    if ($publicSiteVerified -and -not [string]::IsNullOrWhiteSpace($PublicSiteUrl)) {
        $dashboardUrl = "$PublicSiteUrl/?token=$encodedToken"
    }
    elseif (-not [string]::IsNullOrWhiteSpace($quickTunnelUrl)) {
        $dashboardUrl = "$quickTunnelUrl/?token=$encodedToken"
    }
    elseif ($reverseTunnelEnabled) {
        Write-Warning "The public site was not verified; opening the healthy local dashboard instead."
    }

    Write-Host ""
    Write-Host "All configured services are running with: $PythonBin"
    $launcherReady = [ordered]@{
        instance_id = $script:InstanceId
        url = "http://${healthHost}:$dashboardPort/"
        token_path = [string]$dashboardAccess.path
    }
    Write-AtomicText -Path $LauncherReadyFile -Value ($launcherReady | ConvertTo-Json -Compress)
    if ($QuietDashboardUrl -eq "1") {
        Write-Host "Dashboard is running (private token URL hidden)."
        Write-Host "Admin token file: $($dashboardAccess.path)"
    }
    else {
        Write-Host "Dashboard: $dashboardUrl"
    }
    Write-Host "Logs: $LogDir"
    Write-Host "Keep this window open. Press Ctrl+C or run .\stop_all.cmd in another window."

    if ($OpenBrowser -eq "1") {
        Start-Process -FilePath $dashboardUrl | Out-Null
    }

    while ($true) {
        Start-Sleep -Seconds 1
        if (Test-StopRequested) {
            Write-Host "Stop requested by stop_all.cmd."
            break
        }

        if ($reverseTunnelEnabled) {
            $reverseState = Read-ServiceState -Name "reverse_tunnel"
            if ($null -eq $reverseState -or -not (Test-ProcessIdentity -State $reverseState)) {
                Write-Warning "Reverse tunnel disconnected; reconnecting..."
                try {
                    Start-ReverseTunnelService -Configuration $reverseConfiguration
                    Wait-ServiceStable -Name "reverse_tunnel" -Seconds 8
                }
                catch [System.OperationCanceledException] {
                    throw
                }
                catch {
                    Write-Warning "Reconnect failed; local services remain online and another attempt will follow. $($_.Exception.Message)"
                }
            }
        }

        foreach ($serviceName in $script:CoreServices) {
            if (-not $script:StartedServices.Contains($serviceName)) {
                continue
            }
            $serviceState = Read-ServiceState -Name $serviceName
            if ($null -eq $serviceState -or -not (Test-ProcessIdentity -State $serviceState)) {
                throw "$serviceName stopped unexpectedly. Check $(Join-Path $LogDir "$serviceName.error.log")."
            }
        }
    }
}
catch [System.OperationCanceledException] {
    Write-Host $_.Exception.Message
}
finally {
    Stop-StartedServices
    $remainingServiceNames = @($script:StartedServices | Where-Object {
        Test-Path -LiteralPath (Get-ServiceStatePath -Name $_) -PathType Leaf
    })
    $launcherReady = Read-JsonFile -Path $LauncherReadyFile
    if ($null -ne $launcherReady -and
        $launcherReady.PSObject.Properties.Name -contains "instance_id" -and
        [string]$launcherReady.instance_id -eq $script:InstanceId) {
        Remove-Item -LiteralPath $LauncherReadyFile -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $PublicUrlFile -Force -ErrorAction SilentlyContinue
    Disable-KeepAwake

    $supervisorState = Read-JsonFile -Path $SupervisorStateFile
    if ($null -ne $supervisorState -and
        $remainingServiceNames.Count -eq 0 -and
        $supervisorState.PSObject.Properties.Name -contains "instance_id" -and
        [string]$supervisorState.instance_id -eq $script:InstanceId) {
        Remove-Item -LiteralPath $SupervisorStateFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $SupervisorPidFile -Force -ErrorAction SilentlyContinue
    }
    $request = Read-JsonFile -Path $StopRequestFile
    if ($null -ne $request -and
        $remainingServiceNames.Count -eq 0 -and
        $request.PSObject.Properties.Name -contains "instance_id" -and
        [string]$request.instance_id -eq $script:InstanceId) {
        Remove-Item -LiteralPath $StopRequestFile -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $script:StartupLock) {
        $script:StartupLock.Dispose()
        $script:StartupLock = $null
    }
    Pop-Location
    if ($remainingServiceNames.Count -gt 0) {
        throw "Shutdown incomplete; supervisor ownership state was preserved for: $($remainingServiceNames -join ', ')."
    }
}
