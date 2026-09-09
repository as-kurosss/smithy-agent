#requires -Version 5.1
<#
Smithy agent one-line installer (Windows).

Usage (regular PowerShell, elevated not required - it will self-elevate):

  irm https://raw.githubusercontent.com/as-kurosss/smithy-agent/master/install-agent.ps1 | iex

or with parameters:

  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/as-kurosss/smithy-agent/master/install-agent.ps1))) `
      -Orchestrator https://cloud.example.com -Name prod-1 -JoinToken <TOKEN>
#>
param(
    [string]$Orchestrator = "",
    [string]$Name = "",
    [string]$JoinToken = "",
    [string]$AgentUrl = "",
    [string]$User = "",
    [string]$Python = "",
    # Supply-chain pinning: point this at a release tag instead of master
    # (e.g. https://raw.githubusercontent.com/as-kurosss/smithy-agent/v0.2.1/install-agent.ps1)
    [string]$RepoUrl = "https://raw.githubusercontent.com/as-kurosss/smithy-agent/master/install-agent.ps1"
)

$ErrorActionPreference = "Stop"
$VenvDir = Join-Path $env:LOCALAPPDATA "smithy-agent\venv"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Find-Python {
    if ($Python -and (Test-Path $Python)) { return $Python }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $v = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($v -and [version]$v -ge [version]"3.11") { return $cmd.Source }
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $v = & py -3 -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($v -and [version]$v -ge [version]"3.11") { return "py" }
    }
    return ""
}

# ---------------------------------------------------------------- elevate
if (-not (Test-Admin)) {
    Write-Host "Not running as administrator - relaunching elevated..."
    $tmp = Join-Path $env:TEMP "smithy-agent-install.ps1"
    if ($MyInvocation.MyCommand.Path -and (Test-Path $MyInvocation.MyCommand.Path)) {
        # Elevated pass must run the exact copy the user inspected - never a
        # fresh (possibly different) download from the network.
        Copy-Item $MyInvocation.MyCommand.Path $tmp -Force
    } else {
        # irm|iex entry: no file on disk - pin to whatever RepoUrl was set to.
        Invoke-WebRequest -UseBasicParsing -Uri $RepoUrl -OutFile $tmp
    }

    # Rebuild the invocation as an encoded command: values starting with "-"
    # (join tokens are base64-ish) cannot be passed as bare -File arguments.
    function Esc([string]$s) { return "'" + $s.Replace("'", "''") + "'" }
    $inner = "& '$tmp'"
    foreach ($p in @("Orchestrator", "Name", "JoinToken", "AgentUrl", "User", "Python")) {
        $val = Get-Variable -Name $p -ValueOnly
        if ($val) { $inner += " -$p $(Esc $val)" }
    }
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))
    $proc = Start-Process powershell -Verb RunAs -Wait -PassThru -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $enc)
    exit $proc.ExitCode
}

Write-Host "=== Smithy agent installer ==="

# ---------------------------------------------------------------- orchestrator
if (-not $Orchestrator) {
    $Orchestrator = Read-Host "Orchestrator URL (e.g. https://cloud.example.com)"
}
$Orchestrator = $Orchestrator.TrimEnd("/")

if (-not $Name) {
    $hostLabel = $env:COMPUTERNAME.ToLower() -replace "[^a-z0-9-]", "-"
    $Name = "agent-$hostLabel"
}
Write-Host "Orchestrator: $Orchestrator"
Write-Host "Agent name:   $Name"

# ---------------------------------------------------------------- python
$pyExe = Find-Python
if (-not $pyExe) {
    Write-Host "Python 3.11+ not found - installing via winget..."
    winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget failed to install Python; install it manually from https://python.org" }
    # winget adds Python to the machine PATH; pick it up for this session
    $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("PATH", "User")
    $pyExe = Find-Python
    if (-not $pyExe) { throw "Python installed but not found - reopen the terminal and retry" }
}
Write-Host "Python:       $pyExe"

# ---------------------------------------------------------------- venv
New-Item -ItemType Directory -Force -Path (Split-Path $VenvDir) | Out-Null
if (-not (Test-Path "$VenvDir\Scripts\python.exe")) {
    Write-Host "Creating venv at $VenvDir ..."
    & $pyExe -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
Write-Host "Installing smithy-agent[screenshot] (PyPI)..."
& "$VenvDir\Scripts\python.exe" -m pip install --upgrade pip --quiet
& "$VenvDir\Scripts\python.exe" -m pip install --upgrade "smithy-agent[screenshot]" --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install smithy-agent failed" }

# ---------------------------------------------------------------- register
$svc = "$VenvDir\Scripts\smithy-agent-service.exe"
if (-not (Test-Path $svc)) { $svc = "$VenvDir\Scripts\smithy-agent-service.bat" }

$installArgs = @("install", "--orchestrator", $Orchestrator, "--name", $Name)
if ($AgentUrl)  { $installArgs += @("--url", $AgentUrl) }
if ($JoinToken) { $installArgs += "--join-token=$JoinToken" }
if ($User)      { $installArgs += @("--user", $User) }

& $svc @installArgs
if ($LASTEXITCODE -ne 0) { throw "smithy-agent-service install failed (exit $LASTEXITCODE)" }

Write-Host "Starting the agent..."
& $svc start

Write-Host ""
Write-Host "=== Done ==="
Write-Host "Agent:      $Name"
Write-Host "Config:     $env:LOCALAPPDATA\smithy_agent\config.json"
Write-Host "Log:        $env:LOCALAPPDATA\smithy_agent\agent.log"
Write-Host "It starts automatically at logon. Check it in the orchestrator web UI."
