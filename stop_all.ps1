[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectDir = [System.IO.Path]::GetFullPath($PSScriptRoot)
$RunDir = Join-Path $ProjectDir ".run"
$StateFile = Join-Path $RunDir "start_all.windows.json"
$PidFile = Join-Path $RunDir "start_all.pid"
$RequestFile = Join-Path $RunDir "stop_all.windows.request"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
. (Join-Path $ProjectDir "windows_service_shutdown.ps1")
$ServiceNames = @(
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

function Remove-StaleSupervisorFiles {
    Remove-Item -LiteralPath $StateFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $RequestFile -Force -ErrorAction SilentlyContinue
}

function Get-ServiceStatePath {
    param([Parameter(Mandatory = $true)][string]$Name)
    return (Join-Path $RunDir "$Name.windows.json")
}

function Get-ServicePidPath {
    param([Parameter(Mandatory = $true)][string]$Name)
    return (Join-Path $RunDir "$Name.pid")
}

function Remove-ServiceFiles {
    param([Parameter(Mandatory = $true)][string]$Name)
    Remove-CooperativeStopFile -State (Read-ServiceState -Name $Name) -Name $Name
    Remove-Item -LiteralPath (Get-ServiceStatePath -Name $Name) -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Get-ServicePidPath -Name $Name) -Force -ErrorAction SilentlyContinue
}

function Read-ServiceState {
    param([Parameter(Mandatory = $true)][string]$Name)

    $path = Get-ServiceStatePath -Name $Name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return $null
    }
    try {
        return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json)
    }
    catch {
        Write-Warning "Malformed service state was preserved: $path"
        return $null
    }
}

function Test-ServiceProcessIdentity {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][string]$ExpectedName
    )

    foreach ($property in @("name", "pid", "started_utc", "executable", "supervisor_instance")) {
        if ($State.PSObject.Properties.Name -notcontains $property) {
            return $false
        }
    }
    if (-not [string]::Equals([string]$State.name, $ExpectedName, [System.StringComparison]::Ordinal)) {
        return $false
    }

    $serviceProcessId = 0
    if (-not [int]::TryParse([string]$State.pid, [ref]$serviceProcessId) -or $serviceProcessId -le 0) {
        return $false
    }
    $process = Get-Process -Id $serviceProcessId -ErrorAction SilentlyContinue
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
        $expectedExecutable = [System.IO.Path]::GetFullPath([string]$State.executable)
        $actualExecutable = [System.IO.Path]::GetFullPath($process.Path)
        return (
            [math]::Abs(($actualStart - $expectedStart).TotalSeconds) -le 1.0 -and
            [string]::Equals($expectedExecutable, $actualExecutable, [System.StringComparison]::OrdinalIgnoreCase)
        )
    }
    catch {
        return $false
    }
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
        # A console-host child may disappear while taskkill walks the tree.
        # The verified process identity below is the authoritative result.
        & "$env:SystemRoot\System32\taskkill.exe" @taskArguments 2>$null | Out-Null
    }
    catch {
        # Best effort by design; retry forcefully only if the owner still lives.
    }
}

function Stop-OwnedService {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$OwnerInstance
    )

    $statePath = Get-ServiceStatePath -Name $Name
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        return $true
    }
    $state = Read-ServiceState -Name $Name
    if ($null -eq $state) {
        return $false
    }
    if ($state.PSObject.Properties.Name -notcontains "supervisor_instance" -or
        -not [string]::Equals(
            [string]$state.supervisor_instance,
            $OwnerInstance,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        Write-Warning "Preserving $Name state because it belongs to another supervisor instance."
        return $true
    }

    $serviceProcessId = 0
    if ($state.PSObject.Properties.Name -notcontains "pid" -or
        -not [int]::TryParse([string]$state.pid, [ref]$serviceProcessId) -or
        $serviceProcessId -le 0) {
        Write-Warning "Preserving invalid $Name state; no process was stopped."
        return $false
    }
    if ($null -eq (Get-Process -Id $serviceProcessId -ErrorAction SilentlyContinue)) {
        Remove-ServiceFiles -Name $Name
        return $true
    }
    if (-not (Test-ServiceProcessIdentity -State $state -ExpectedName $Name)) {
        Write-Warning "Refusing to stop unverified $Name PID $serviceProcessId; its state was preserved."
        return $false
    }

    Write-Host "stopping orphaned $Name`: pid $serviceProcessId"
    $graceDeadline = Request-CooperativeServiceStop -State $state -Name $Name
    if ($null -ne $graceDeadline) {
        while ([datetime]::UtcNow -lt $graceDeadline -and (Test-ServiceProcessIdentity -State $state -ExpectedName $Name)) {
            Start-Sleep -Milliseconds 100
        }
    }
    else {
        Invoke-TaskKillBestEffort -ProcessId $serviceProcessId
        for ($attempt = 0; $attempt -lt 8; $attempt += 1) {
            if (-not (Test-ServiceProcessIdentity -State $state -ExpectedName $Name)) { break }
            Start-Sleep -Milliseconds 250
        }
    }
    if (Test-ServiceProcessIdentity -State $state -ExpectedName $Name) {
        Write-Warning "$Name did not finish within its stop grace period; terminating its verified process tree."
        Invoke-TaskKillBestEffort -ProcessId $serviceProcessId -Force
    }
    for ($attempt = 0; $attempt -lt 12; $attempt += 1) {
        if (-not (Test-ServiceProcessIdentity -State $state -ExpectedName $Name)) {
            break
        }
        Start-Sleep -Milliseconds 250
    }

    if (Test-ServiceProcessIdentity -State $state -ExpectedName $Name) {
        Write-Warning "$Name PID $serviceProcessId did not stop; its state was preserved."
        return $false
    }
    Remove-ServiceFiles -Name $Name
    return $true
}

function Stop-OrphanedServices {
    param([Parameter(Mandatory = $true)][string]$OwnerInstance)

    $allStopped = $true
    foreach ($serviceName in $ServiceNames) {
        $serviceState = Read-ServiceState -Name $serviceName
        if ($null -ne $serviceState -and
            $serviceState.PSObject.Properties.Name -contains "supervisor_instance" -and
            [string]$serviceState.supervisor_instance -eq $OwnerInstance -and
            (Test-ServiceProcessIdentity -State $serviceState -ExpectedName $serviceName)) {
            $null = Request-CooperativeServiceStop -State $serviceState -Name $serviceName
        }
    }
    for ($index = $ServiceNames.Count - 1; $index -ge 0; $index -= 1) {
        if (-not (Stop-OwnedService -Name $ServiceNames[$index] -OwnerInstance $OwnerInstance)) {
            $allStopped = $false
        }
    }
    return $allStopped
}

if (-not (Test-Path -LiteralPath $StateFile -PathType Leaf)) {
    if (Test-Path -LiteralPath $PidFile -PathType Leaf) {
        $rawProcessId = (Get-Content -LiteralPath $PidFile -Raw).Trim()
        $staleProcessId = 0
        if ([int]::TryParse($rawProcessId, [ref]$staleProcessId) -and
            $null -ne (Get-Process -Id $staleProcessId -ErrorAction SilentlyContinue)) {
            throw "Refusing to use an unverified live PID from $PidFile."
        }
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    }

    $unanchoredStates = New-Object System.Collections.Generic.List[string]
    foreach ($serviceName in $ServiceNames) {
        $serviceStatePath = Get-ServiceStatePath -Name $serviceName
        if (-not (Test-Path -LiteralPath $serviceStatePath -PathType Leaf)) {
            continue
        }
        $serviceState = Read-ServiceState -Name $serviceName
        if ($null -eq $serviceState) {
            [void]$unanchoredStates.Add($serviceName)
            continue
        }
        $serviceProcessId = 0
        if ($serviceState.PSObject.Properties.Name -contains "pid" -and
            [int]::TryParse([string]$serviceState.pid, [ref]$serviceProcessId) -and
            $serviceProcessId -gt 0 -and
            $null -ne (Get-Process -Id $serviceProcessId -ErrorAction SilentlyContinue)) {
            [void]$unanchoredStates.Add($serviceName)
        }
        else {
            Remove-ServiceFiles -Name $serviceName
        }
    }
    if ($unanchoredStates.Count -gt 0) {
        throw "Supervisor state is missing; refusing to stop unanchored live service state: $($unanchoredStates -join ', ')."
    }
    Write-Host "CS2 services are already stopped."
    exit 0
}

try {
    $state = Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    throw "Refusing malformed supervisor state: $StateFile"
}

foreach ($property in @("instance_id", "pid", "process_started_utc", "executable", "script_path")) {
    if ($state.PSObject.Properties.Name -notcontains $property) {
        throw "Refusing incomplete supervisor state: missing $property."
    }
}
$ownerInstance = [string]$state.instance_id
if ($ownerInstance -notmatch '^[0-9a-fA-F]{32}$') {
    throw "Refusing invalid supervisor instance ID in $StateFile."
}

$supervisorProcessId = 0
if (-not [int]::TryParse([string]$state.pid, [ref]$supervisorProcessId) -or
    $supervisorProcessId -le 0) {
    throw "Refusing invalid supervisor PID in $StateFile."
}

$process = Get-Process -Id $supervisorProcessId -ErrorAction SilentlyContinue
if ($null -eq $process) {
    if (Stop-OrphanedServices -OwnerInstance $ownerInstance) {
        Remove-StaleSupervisorFiles
        Write-Host "Stopped services owned by the stale supervisor and removed its state."
        exit 0
    }
    throw "The supervisor is stopped, but at least one owned service could not be verified or stopped. State was preserved."
}

try {
    $expectedStart = [datetime]::Parse(
        [string]$state.process_started_utc,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::RoundtripKind
    ).ToUniversalTime()
    $actualStart = $process.StartTime.ToUniversalTime()
    $expectedExecutable = [System.IO.Path]::GetFullPath([string]$state.executable)
    $actualExecutable = [System.IO.Path]::GetFullPath($process.Path)
    $expectedScript = [System.IO.Path]::GetFullPath([string]$state.script_path)
    $expectedScriptName = [System.IO.Path]::GetFileName($expectedScript)
    $commandLine = [string](Get-CimInstance Win32_Process -Filter "ProcessId = $supervisorProcessId").CommandLine
}
catch {
    throw "Could not verify the recorded start_all.ps1 supervisor; nothing was stopped."
}

if ([math]::Abs(($actualStart - $expectedStart).TotalSeconds) -gt 1.0 -or
    -not [string]::Equals($expectedExecutable, $actualExecutable, [System.StringComparison]::OrdinalIgnoreCase) -or
    $commandLine.IndexOf($expectedScriptName, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
    throw "PID $supervisorProcessId no longer matches the recorded start_all.ps1 supervisor; nothing was stopped."
}

$request = [ordered]@{
    schema_version = 1
    instance_id = [string]$state.instance_id
    requested_utc = [datetime]::UtcNow.ToString("o", [System.Globalization.CultureInfo]::InvariantCulture)
}
$temporary = "$RequestFile.$PID.$([guid]::NewGuid().ToString('N')).tmp"
try {
    [System.IO.File]::WriteAllText(
        $temporary,
        ($request | ConvertTo-Json -Compress),
        $Utf8NoBom
    )
    Move-Item -LiteralPath $temporary -Destination $RequestFile -Force
}
finally {
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
}

Write-Host "Stopping CS2 supervisor PID $supervisorProcessId..."
$deadline = [datetime]::UtcNow.AddSeconds([math]::Max(1, $TimeoutSeconds))
while ([datetime]::UtcNow -lt $deadline) {
    if ($null -eq (Get-Process -Id $supervisorProcessId -ErrorAction SilentlyContinue)) {
        if (Stop-OrphanedServices -OwnerInstance $ownerInstance) {
            Remove-StaleSupervisorFiles
            Write-Host "All services owned by start_all.cmd have stopped."
            exit 0
        }
        throw "The supervisor exited, but at least one owned service could not be verified or stopped. State was preserved."
    }
    Start-Sleep -Milliseconds 250
}

throw @"
Supervisor PID $supervisorProcessId did not stop within $TimeoutSeconds seconds.
State was preserved. Check $ProjectDir\logs and run this command again.
"@
