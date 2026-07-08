[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8787,

    [switch]$SkipProcessStop
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Get-ListenersOnPort {
    param([int]$TargetPort)

    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        return @(Get-NetTCPConnection -State Listen -LocalPort $TargetPort -ErrorAction SilentlyContinue)
    }

    return @()
}

function Get-ProcessCommandLine {
    param([int]$TargetProcessId)

    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $TargetProcessId" -ErrorAction Stop
        return [string]$proc.CommandLine
    } catch {
        try {
            $proc = Get-WmiObject Win32_Process -Filter "ProcessId = $TargetProcessId" -ErrorAction Stop
            return [string]$proc.CommandLine
        } catch {
            return ""
        }
    }
}

function Test-SafeCodexProxyProcess {
    param([int]$TargetProcessId)

    $commandLine = Get-ProcessCommandLine -TargetProcessId $TargetProcessId

    if ([string]::IsNullOrWhiteSpace($commandLine)) {
        return $false
    }

    return ($commandLine -match "(?i)headroom(\.exe)?\s+proxy" -and $commandLine -match "(?i)safe-codex")
}

function Clear-SafeCodexEnvironment {
    $names = @(
        "HEADROOM_PROFILE",
        "HEADROOM_MODE",
        "HEADROOM_LOSSLESS",
        "HEADROOM_DISABLE_KOMPRESS",
        "HEADROOM_LOG_MESSAGES",
        "HEADROOM_HOST",
        "HEADROOM_CODEX_WIRE_DEBUG",
        "HEADROOM_CODEX_WIRE_DEBUG_DIR",
        "OPENAI_BASE_URL"
    )

    foreach ($name in $names) {
        Remove-Item "Env:\$name" -ErrorAction SilentlyContinue
    }
}

if (-not $SkipProcessStop) {
    $listeners = @(Get-ListenersOnPort -TargetPort $Port)

    if ($listeners.Count -eq 0) {
        Write-Host ("No listener found on port {0}." -f $Port)
    } else {
        $candidatePids = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)

        foreach ($pidValue in $candidatePids) {
            if (Test-SafeCodexProxyProcess -TargetProcessId $pidValue) {
                Write-Host ("Stopping safe-codex proxy process pid={0}" -f $pidValue)
                Stop-Process -Id $pidValue -ErrorAction Stop
            } else {
                Write-Host ("Skipped pid={0}; command line does not look like safe-codex proxy." -f $pidValue)
            }
        }
    }
}

Clear-SafeCodexEnvironment
Write-Host "safe-codex environment variables cleared for this PowerShell process."
exit 0