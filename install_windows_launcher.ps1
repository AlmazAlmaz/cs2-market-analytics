[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$DesktopOnly,
    [string]$DesktopDirectory,
    [string]$StartupDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectDir = [System.IO.Path]::GetFullPath($PSScriptRoot)
$LauncherPath = Join-Path $ProjectDir "windows_launcher.ps1"
$PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$hasDesktopOverride = -not [string]::IsNullOrWhiteSpace($DesktopDirectory)
$hasStartupOverride = -not [string]::IsNullOrWhiteSpace($StartupDirectory)
if ($hasDesktopOverride -ne $hasStartupOverride) {
    throw "Specify both -DesktopDirectory and -StartupDirectory for a local test."
}

function Resolve-TestDirectory {
    param([Parameter(Mandatory = $true)][string]$Directory)

    $fullPath = [System.IO.Path]::GetFullPath($Directory)
    $prefix = $ProjectDir.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Test shortcut directories must be inside this project: $fullPath"
    }
    # A junction must not turn a local test into writes to the real Desktop/Startup.
    $ancestor = $fullPath
    while (-not [string]::IsNullOrWhiteSpace($ancestor)) {
        if (Test-Path -LiteralPath $ancestor) {
            $entry = Get-Item -LiteralPath $ancestor -Force
            if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Test shortcut directories cannot use junctions or symbolic links: $ancestor"
            }
        }
        $ancestor = [System.IO.Path]::GetDirectoryName($ancestor)
    }
    return $fullPath
}

if ($hasDesktopOverride) {
    $DesktopDirectory = Resolve-TestDirectory -Directory $DesktopDirectory
    $StartupDirectory = Resolve-TestDirectory -Directory $StartupDirectory
}
else {
    $DesktopDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
    if (-not $DesktopOnly) {
        $StartupDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::Startup)
    }
    if ([string]::IsNullOrWhiteSpace($DesktopDirectory) -or
        (-not $DesktopOnly -and [string]::IsNullOrWhiteSpace($StartupDirectory))) {
        throw "The current user's Desktop or Startup folder could not be found."
    }
}

if (-not $Remove -and -not (Test-Path -LiteralPath $LauncherPath -PathType Leaf)) {
    throw "The launcher script was not found: $LauncherPath"
}

$hashProvider = [System.Security.Cryptography.SHA256]::Create()
try {
    $projectHash = [BitConverter]::ToString($hashProvider.ComputeHash(
        [Text.Encoding]::UTF8.GetBytes($ProjectDir.ToLowerInvariant())
    )).Replace('-', '').ToLowerInvariant()
}
finally {
    $hashProvider.Dispose()
}
$OwnerDescription = "CS2 Dashboard launcher; project=$projectHash"
$ShortcutBaseName = "CS2 Dashboard"
$Shell = New-Object -ComObject WScript.Shell

function Test-OwnedShortcut {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    try {
        $shortcut = $Shell.CreateShortcut($Path)
        try {
            return $shortcut.Description -eq $OwnerDescription
        }
        finally {
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
        }
    }
    catch {
        return $false
    }
}

function Get-ShortcutPath {
    param([Parameter(Mandatory = $true)][string]$Directory)

    # Reuse an existing owned shortcut even if a former naming collision disappeared.
    foreach ($entry in @(Get-ChildItem -LiteralPath $Directory -Filter "$ShortcutBaseName*.lnk" -File)) {
        if (Test-OwnedShortcut -Path $entry.FullName) {
            return $entry.FullName
        }
    }
    $candidate = Join-Path $Directory "$ShortcutBaseName.lnk"
    if (-not (Test-Path -LiteralPath $candidate)) {
        return $candidate
    }
    for ($suffix = 0; $suffix -lt 100; $suffix++) {
        $name = "$ShortcutBaseName ($($projectHash.Substring(0, 8)))"
        if ($suffix -gt 0) { $name += " $suffix" }
        $candidate = Join-Path $Directory "$name.lnk"
        if (-not (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    throw "Could not choose an unused shortcut name in $Directory."
}

try {
    $shortcutFolders = @([pscustomobject]@{ Directory = $DesktopDirectory; AutoStart = $false })
    if (-not $DesktopOnly) {
        $shortcutFolders += [pscustomobject]@{ Directory = $StartupDirectory; AutoStart = $true }
    }
    foreach ($folder in $shortcutFolders) {
        if ($Remove) {
            if (Test-Path -LiteralPath $folder.Directory -PathType Container) {
                foreach ($entry in @(Get-ChildItem -LiteralPath $folder.Directory -Filter "$ShortcutBaseName*.lnk" -File)) {
                    if (Test-OwnedShortcut -Path $entry.FullName) {
                        Remove-Item -LiteralPath $entry.FullName -Force
                        Write-Host "Removed: $($entry.FullName)"
                    }
                }
            }
            continue
        }

        [void][System.IO.Directory]::CreateDirectory($folder.Directory)
        $shortcutPath = Get-ShortcutPath -Directory $folder.Directory
        $shortcut = $Shell.CreateShortcut($shortcutPath)
        try {
            $shortcut.TargetPath = $PowerShellPath
            $shortcut.Arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $LauncherPath + '"'
            if ($folder.AutoStart) { $shortcut.Arguments += " -AutoStart" }
            $shortcut.WorkingDirectory = $ProjectDir
            $shortcut.Description = $OwnerDescription
            $shortcut.WindowStyle = 7
            $iconPath = Join-Path $ProjectDir ".run\cs2_launcher.ico"
            if (Test-Path -LiteralPath $iconPath -PathType Leaf) {
                $shortcut.IconLocation = "$iconPath,0"
            }
            else {
                $shortcut.IconLocation = "$(Join-Path $env:SystemRoot 'System32\shell32.dll'),13"
            }
            $shortcut.Save()
            Write-Host "Installed: $shortcutPath"
        }
        finally {
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
        }
    }
}
finally {
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($Shell)
}
