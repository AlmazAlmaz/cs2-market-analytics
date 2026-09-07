param(
    [Parameter(Mandatory = $true)]
    [string]$VpsUser,

    [Parameter(Mandatory = $true)]
    [string]$VpsHost,

    [int]$LocalPort = 8010,
    [int]$RemotePort = 8010
)

$ErrorActionPreference = "Stop"

Write-Host "Reverse tunnel:"
Write-Host "  VPS:        $VpsUser@$VpsHost"
Write-Host "  VPS port:   127.0.0.1:$RemotePort"
Write-Host "  Local port: 127.0.0.1:$LocalPort"
Write-Host ""
Write-Host "Keep this window open. Press Ctrl+C to stop."
Write-Host ""

while ($true) {
    $startedAt = Get-Date
    Write-Host "[$($startedAt.ToString('yyyy-MM-dd HH:mm:ss'))] Starting SSH tunnel..."

    ssh `
        -N `
        -T `
        -o ExitOnForwardFailure=yes `
        -o ServerAliveInterval=30 `
        -o ServerAliveCountMax=3 `
        -R "127.0.0.1:${RemotePort}:127.0.0.1:${LocalPort}" `
        "$VpsUser@$VpsHost"

    $stoppedAt = Get-Date
    Write-Host "[$($stoppedAt.ToString('yyyy-MM-dd HH:mm:ss'))] Tunnel stopped. Reconnecting in 5 seconds..."
    Start-Sleep -Seconds 5
}
