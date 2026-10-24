[CmdletBinding()]
param(
    [string]$PythonBootstrap = $env:PYTHON_BOOTSTRAP,
    [switch]$SkipTests,
    [switch]$CpuOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectDir = [System.IO.Path]::GetFullPath($PSScriptRoot)
$VenvDir = Join-Path $ProjectDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Test-Python312 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$PrefixArguments = @()
    )

    try {
        $version = & $FilePath @PrefixArguments -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        return ($LASTEXITCODE -eq 0 -and ($version | Select-Object -Last 1) -eq "3.12")
    }
    catch {
        return $false
    }
}

function Find-Python312 {
    if (-not [string]::IsNullOrWhiteSpace($PythonBootstrap)) {
        $candidate = $PythonBootstrap
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $candidate = [System.IO.Path]::GetFullPath($candidate)
        }
        if (-not (Test-Python312 -FilePath $candidate)) {
            throw "PYTHON_BOOTSTRAP does not point to Python 3.12: $PythonBootstrap"
        }
        return [pscustomobject]@{
            FilePath = $candidate
            PrefixArguments = @()
            DisplayName = $candidate
        }
    }

    $launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $launcher -and (Test-Python312 -FilePath $launcher.Source -PrefixArguments @("-3.12"))) {
        return [pscustomobject]@{
            FilePath = $launcher.Source
            PrefixArguments = @("-3.12")
            DisplayName = "py -3.12"
        }
    }

    foreach ($name in @("python3.12.exe", "python.exe")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $command -and (Test-Python312 -FilePath $command.Source)) {
            return [pscustomobject]@{
                FilePath = $command.Source
                PrefixArguments = @()
                DisplayName = $command.Source
            }
        }
    }

    throw @"
Python 3.12 was not found.
Install 64-bit Python 3.12 from python.org, enable the Python launcher,
then run this script again.
"@
}

function Protect-PrivateFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }

    try {
        $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $systemSid = New-Object System.Security.Principal.SecurityIdentifier(
            [System.Security.Principal.WellKnownSidType]::LocalSystemSid,
            $null
        )
        $administratorsSid = New-Object System.Security.Principal.SecurityIdentifier(
            [System.Security.Principal.WellKnownSidType]::BuiltinAdministratorsSid,
            $null
        )
        $rights = [System.Security.AccessControl.FileSystemRights]::FullControl
        $allow = [System.Security.AccessControl.AccessControlType]::Allow
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $acl.SetOwner($currentSid)
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($sid in @($currentSid, $systemSid, $administratorsSid)) {
            $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, $rights, $allow)
            [void]$acl.AddAccessRule($rule)
        }
        [System.IO.File]::SetAccessControl($Path, $acl)
    }
    catch {
        Write-Warning "Could not restrict the ACL for $Path. Keep this file private. $($_.Exception.Message)"
    }
}

Push-Location -LiteralPath $ProjectDir
try {
    if ((Test-Path -LiteralPath $VenvDir -PathType Container) -and
        -not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        throw @"
.venv exists but is not a Windows Python environment.
Rename it first, for example:
  Rename-Item -LiteralPath .venv -NewName .venv.from-other-os
"@
    }

    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        $bootstrap = Find-Python312
        Write-Host "Creating .venv with $($bootstrap.DisplayName)"
        $createArguments = @($bootstrap.PrefixArguments) + @("-m", "venv", $VenvDir)
        Invoke-Checked -FilePath $bootstrap.FilePath -Arguments $createArguments -Description "Virtual environment creation"
    }

    $venvVersion = & $VenvPython -c "import platform, struct, sys; print('{}.{}|{}|{}'.format(sys.version_info.major, sys.version_info.minor, struct.calcsize('P') * 8, platform.system()))"
    if ($LASTEXITCODE -ne 0) {
        throw "The virtual environment Python could not be started: $VenvPython"
    }
    if (($venvVersion | Select-Object -Last 1) -ne "3.12|64|Windows") {
        throw ".venv must use 64-bit Python 3.12 for Windows (found: $venvVersion)."
    }

    Invoke-Checked -FilePath $VenvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip") -Description "pip upgrade"
    $runtimeRequirements = if ($CpuOnly) {
        "requirements.txt"
    }
    else {
        "requirements_windows_gpu.txt"
    }
    Invoke-Checked -FilePath $VenvPython -Arguments @("-m", "pip", "install", "-r", $runtimeRequirements) -Description "Runtime dependency installation"
    Invoke-Checked -FilePath $VenvPython -Arguments @("-m", "pip", "install", "-r", "requirements-dev.txt") -Description "Development dependency installation"

    $gpuCode = @"
import json
import torch
payload = {
    "torch": torch.__version__,
    "cuda_available": torch.cuda.is_available(),
    "cuda_runtime": torch.version.cuda,
    "device_count": torch.cuda.device_count() if torch.cuda.is_available() else 0,
    "devices": [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())] if torch.cuda.is_available() else [],
}
print(json.dumps(payload, ensure_ascii=False))
"@
    $gpuReport = $gpuCode | & $VenvPython -
    if ($LASTEXITCODE -ne 0) {
        throw "PyTorch could not be imported after installation."
    }
    $gpu = ($gpuReport | Select-Object -Last 1) | ConvertFrom-Json
    if (-not $CpuOnly -and -not $gpu.cuda_available) {
        throw @"
The CUDA-enabled PyTorch wheel was installed, but CUDA is unavailable.
Update the NVIDIA driver and rerun .\setup_windows.cmd, or use -CpuOnly explicitly.
"@
    }

    foreach ($pair in @(
        @("config.local.env.example", "config.local.env"),
        @("hosting.local.env.example", "hosting.local.env")
    )) {
        $source = Join-Path $ProjectDir $pair[0]
        $destination = Join-Path $ProjectDir $pair[1]
        if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {
            Copy-Item -LiteralPath $source -Destination $destination
        }
    }

    $trainingDevice = if ($CpuOnly) { "cpu" } else { "cuda" }
    Invoke-Checked -FilePath $VenvPython -Arguments @(
        "windows_training_config.py",
        "--config",
        (Join-Path $ProjectDir "config.local.env"),
        "--device",
        $trainingDevice,
        "--cuda-devices",
        "0"
    ) -Description "Training accelerator configuration"

    foreach ($relativePath in @(
        "config.local.env",
        "hosting.local.env",
        "key",
        "key2",
        "key3",
        "key4",
        "key5",
        "key_for_friend",
        "api_keys_proxies.txt",
        "dashboard_token.txt",
        "telegram_bot_token.txt",
        "steamwebapi_key.txt",
        "api_token.txt"
    )) {
        Protect-PrivateFile -Path (Join-Path $ProjectDir $relativePath)
    }

    Invoke-Checked -FilePath $VenvPython -Arguments @("-m", "pip", "check") -Description "Dependency check"

    if (-not $SkipTests) {
        Invoke-Checked -FilePath $VenvPython -Arguments @("-m", "pytest", "-q") -Description "Test suite"
    }

    Write-Host ""
    Write-Host "Setup complete."
    Write-Host "Python: $VenvPython"
    Write-Host "PyTorch: $($gpu.torch)"
    if (-not $CpuOnly) {
        Write-Host "CUDA is ready: runtime $($gpu.cuda_runtime); GPU(s): $($gpu.devices -join ', ')"
        Invoke-Checked -FilePath $VenvPython -Arguments @("gpu_smoke_test.py", "--json") -Description "CUDA training smoke test"
    }
    else {
        Write-Host "CPU-only mode configured explicitly; GPU smoke testing was skipped."
        Write-Host "Nova and Igor will use CPU until setup is rerun without -CpuOnly."
    }
    Write-Host "Start the configured project with:"
    Write-Host "  .\start_all.cmd"
}
finally {
    Pop-Location
}
