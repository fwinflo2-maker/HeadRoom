[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8787,

    [switch]$NoActivate
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Get-RepoRoot {
    if (-not $PSScriptRoot) {
        throw "PSScriptRoot is not available. Run this script from a file."
    }

    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Get-ListenersOnPort {
    param([int]$TargetPort)

    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        return @(Get-NetTCPConnection -State Listen -LocalPort $TargetPort -ErrorAction SilentlyContinue)
    }

    return @()
}

$repoRoot = Get-RepoRoot
Set-Location $repoRoot

$env:PYTHONUTF8 = "1"
$env:HEADROOM_PROFILE = "safe-codex"
$env:HEADROOM_HOST = "127.0.0.1"
$env:HEADROOM_LOG_MESSAGES = "0"
$env:HEADROOM_CODEX_WIRE_DEBUG = "0"
Remove-Item Env:\HEADROOM_CODEX_WIRE_DEBUG_DIR -ErrorAction SilentlyContinue

if (-not $NoActivate) {
    $activate = Join-Path $repoRoot ".venv\Scripts\Activate.ps1"
    if (Test-Path $activate) {
        . $activate
    }
}

$listeners = @(Get-ListenersOnPort -TargetPort $Port)
if ($listeners.Count -gt 0) {
    Write-Host ("safe-codex proxy was not started because port {0} is already listening." -f $Port)

    foreach ($listener in $listeners) {
        $processName = "<unknown>"
        try {
            $processName = (Get-Process -Id $listener.OwningProcess -ErrorAction Stop).ProcessName
        } catch {
            $processName = "<unknown>"
        }

        Write-Host ("port={0} address={1} pid={2} process={3}" -f $listener.LocalPort, $listener.LocalAddress, $listener.OwningProcess, $processName)
    }

    exit 0
}

Write-Host ("Starting safe-codex proxy on 127.0.0.1:{0}" -f $Port)
Write-Host "Stop with Ctrl+C, or run scripts\stop-safe-codex-env.ps1 from another PowerShell window."

& headroom proxy `
    --profile safe-codex `
    --host 127.0.0.1 `
    --port $Port `
    --prompt-cache-key auto `
    --prompt-cache-retention in_memory

exit $LASTEXITCODE