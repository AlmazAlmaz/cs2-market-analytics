[CmdletBinding()]
param(
    [switch]$AutoStart,
    [switch]$BuildOnly,
    [switch]$SelfTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$projectDirectory = [System.IO.Path]::GetFullPath($PSScriptRoot)
$runDirectory = Join-Path $projectDirectory ".run"
New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null
$sourcePath = Join-Path $projectDirectory "windows_launcher.cs"
$sourceHash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.Substring(0, 16)
$executablePath = Join-Path $runDirectory "CS2Launcher-$sourceHash.exe"
$iconPath = Join-Path $runDirectory "cs2_launcher.ico"
$buildLock = $null

try {
    $deadline = [datetime]::UtcNow.AddSeconds(30)
    while ($null -eq $buildLock) {
        try {
            $buildLock = [System.IO.File]::Open((Join-Path $runDirectory "launcher.build.lock"), 'OpenOrCreate', 'ReadWrite', 'None')
        }
        catch [System.IO.IOException] {
            if ([datetime]::UtcNow -ge $deadline) { throw "Another launcher build did not finish." }
            Start-Sleep -Milliseconds 100
        }
    }
    if (-not (Test-Path -LiteralPath $iconPath)) {
        Add-Type -AssemblyName System.Drawing
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CS2IconHandle {
    [DllImport("user32.dll")] public static extern bool DestroyIcon(IntPtr icon);
}
'@
        $bitmap = New-Object System.Drawing.Bitmap(64, 64)
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        $font = New-Object System.Drawing.Font("Segoe UI", 27, [System.Drawing.FontStyle]::Bold)
        $brush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(70, 180, 235))
        $iconHandle = [IntPtr]::Zero
        try {
            $graphics.Clear([System.Drawing.Color]::FromArgb(24, 28, 36))
            $graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
            $graphics.DrawString("C2", $font, $brush, [single]1, [single]7)
            $iconHandle = $bitmap.GetHicon()
            $icon = [System.Drawing.Icon]::FromHandle($iconHandle)
            $stream = [System.IO.File]::Create($iconPath)
            try { $icon.Save($stream) } finally { $stream.Dispose(); $icon.Dispose() }
        }
        finally {
            if ($iconHandle -ne [IntPtr]::Zero) { [void][CS2IconHandle]::DestroyIcon($iconHandle) }
            $brush.Dispose(); $font.Dispose(); $graphics.Dispose(); $bitmap.Dispose()
        }
    }
    if (-not (Test-Path -LiteralPath $executablePath)) {
        $compiler = Join-Path $env:SystemRoot "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
        if (-not (Test-Path -LiteralPath $compiler)) { throw ".NET Framework C# compiler is unavailable." }
        $temporaryExecutable = Join-Path $runDirectory "CS2Launcher-$sourceHash.$PID.exe"
        try {
            & $compiler /nologo /target:winexe /optimize+ "/out:$temporaryExecutable" "/win32icon:$iconPath" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll $sourcePath
            if ($LASTEXITCODE -ne 0) { throw "CS2 launcher compilation failed." }
            Move-Item -LiteralPath $temporaryExecutable -Destination $executablePath -Force
        }
        finally { Remove-Item -LiteralPath $temporaryExecutable -Force -ErrorAction SilentlyContinue }
    }
}
finally { if ($null -ne $buildLock) { $buildLock.Dispose() } }

if ($BuildOnly) { Write-Output $executablePath; exit 0 }
if ($SelfTest) {
    $resultPath = Join-Path $runDirectory "launcher-self-test.txt"
    $testProcess = Start-Process -FilePath $executablePath -ArgumentList @("--self-test", ('"' + $resultPath + '"')) -WindowStyle Hidden -PassThru
    if (-not $testProcess.WaitForExit(15000)) { $testProcess.Kill(); throw "Launcher self-test timed out." }
    Get-Content -LiteralPath $resultPath -Encoding UTF8
    exit $testProcess.ExitCode
}
$launcherArguments = @('"' + $projectDirectory + '"')
if ($AutoStart) { $launcherArguments += "--autostart" }
# This is the interactive control window requested by the user. Its background
# mode hides to the notification area; child services never open console windows.
Start-Process -FilePath $executablePath -ArgumentList $launcherArguments -WindowStyle Normal | Out-Null
