[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8787
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

function Redact-CommandLine {
    param([string]$Value)

    if ([string]::IsNullOrEmpty($Value)) {
        return ""
    }

    $redacted = $Value
    $redacted = [regex]::Replace($redacted, "sk-[A-Za-z0-9_\-]+", "sk-<redacted>")
    $redacted = [regex]::Replace($redacted, "ghp_[A-Za-z0-9_]+", "ghp_<redacted>")
    $redacted = [regex]::Replace($redacted, "github_pat_[A-Za-z0-9_]+", "github_pat_<redacted>")
    $redacted = [regex]::Replace($redacted, "(?i)Authorization:\s*Bearer\s+[A-Za-z0-9_\.\-]+", "Authorization: Bearer <redacted>")
    return $redacted
}

$listeners = @(Get-ListenersOnPort -TargetPort $Port)

if ($listeners.Count -eq 0) {
    Write-Host ("safe-codex proxy status: stopped. No listener on port {0}." -f $Port)
    exit 1
}

$risky = @()
$loopback = @("127.0.0.1", "::1")

foreach ($listener in $listeners) {
    $processName = "<unknown>"
    try {
        $processName = (Get-Process -Id $listener.OwningProcess -ErrorAction Stop).ProcessName
    } catch {
        $processName = "<unknown>"
    }

    $commandLine = Redact-CommandLine -Value (Get-ProcessCommandLine -TargetProcessId $listener.OwningProcess)

    Write-Host ("port={0} address={1} pid={2} process={3}" -f $listener.LocalPort, $listener.LocalAddress, $listener.OwningProcess, $processName)

    if ($commandLine) {
        Write-Host ("command={0}" -f $commandLine)
    }

    if ($loopback -notcontains [string]$listener.LocalAddress) {
        $risky += $listener
    }
}

if ($risky.Count -gt 0) {
    Write-Host "safe-codex proxy status: unsafe listener address detected."
    exit 2
}

Write-Host "safe-codex proxy status: listening on loopback."
exit 0