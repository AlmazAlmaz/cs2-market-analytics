# Shared by start_all.ps1 and stop_all.ps1. Callers must verify the service
# name, owner, executable and process creation time before sending a request.
function Get-CooperativeStopPath {
    param($State, [string]$Name)
    if ($null -eq $State -or
        $State.PSObject.Properties.Name -notcontains "stop_token" -or
        [string]$State.stop_token -notmatch '^[0-9a-f]{32}$' -or
        $Name -notmatch '^[a-z_]+$') {
        return $null
    }
    return (Join-Path $RunDir "$Name.$($State.stop_token).stop")
}

function Request-CooperativeServiceStop {
    param($State, [string]$Name)
    $path = Get-CooperativeStopPath -State $State -Name $Name
    if ($null -eq $path) {
        return $null
    }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        $temporary = "$path.$PID.tmp"
        try {
            [System.IO.File]::WriteAllText($temporary, [string]$State.stop_token, $Utf8NoBom)
            Move-Item -LiteralPath $temporary -Destination $path -Force
        }
        finally {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
    # Signal all services before waiting: even a slow network request gets
    # the same 45-second grace period, instead of multiplying it per process.
    return [System.IO.File]::GetLastWriteTimeUtc($path).AddSeconds(45)
}

function Remove-CooperativeStopFile {
    param($State, [string]$Name)
    $path = Get-CooperativeStopPath -State $State -Name $Name
    if ($null -ne $path) {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
}
