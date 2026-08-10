<#
.SYNOPSIS
    Run Claude Code through Headroom.

.DESCRIPTION
    Joins the same central proxy as the Copilot CLI and VS Code.

    That takes care: a proxy has ONE destination for the Anthropic
    /v1/messages wire, and the central proxy pins it at the GitHub Copilot
    host so `wrap copilot --native` can drive Claude models. Claude Code sends
    its own Anthropic credential on that same wire, and the proxy forwards the
    client key unchanged - so a naive share would hand your Anthropic key to
    GitHub.

    `wrap claude` therefore pins THIS session's upstream per request
    (X-Headroom-Base-Url: https://api.anthropic.com), which the proxy honours,
    so your traffic reaches Anthropic no matter where the shared proxy points.
    The proxy also refuses outright to forward an x-api-key to a non-Anthropic
    host, so a dropped header fails loudly instead of leaking.

.EXAMPLE
    .\Start-HeadroomClaudeCode.ps1
.EXAMPLE
    .\Start-HeadroomClaudeCode.ps1 -Port 8972 -Path C:\git\my-repo
#>
[CmdletBinding()]
param(
    # Same central proxy as the Copilot CLI and VS Code scripts.
    [ValidateRange(1, 65535)]
    [int]$Port = 8970,

    # Defaults to the repo this script lives in.
    [string]$Path = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,

    [switch]$NoDashboard,

    # Extra args passed through to Claude Code.
    [string[]]$ClaudeArgs = @()
)

$ErrorActionPreference = 'Stop'

function Write-Ok   { param($m) Write-Host "    OK  $m"  -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "    !   $m"  -ForegroundColor Yellow }
function Write-Bad  { param($m) Write-Host "    X   $m"  -ForegroundColor Red }
function Write-Step { param($m) Write-Host "`n$m" -ForegroundColor Cyan }

Write-Host "`n===============================================================" -ForegroundColor Magenta
Write-Host " Headroom for CLAUDE CODE  (port $Port)" -ForegroundColor Magenta
Write-Host " Shares the central proxy with the Copilot CLI and VS Code" -ForegroundColor Magenta
Write-Host "===============================================================" -ForegroundColor Magenta

Write-Step "[1] Preflight"

$headroom = (Get-Command headroom -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
if (-not $headroom) { Write-Bad "'headroom' is not on PATH."; exit 1 }
Write-Ok "headroom found: $headroom"

$claude = (Get-Command claude -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
if ($claude) { Write-Ok "claude CLI found: $claude" }
else { Write-Warn "'claude' was not found on PATH - headroom will report it if it cannot launch" }

if (-not (Test-Path $Path)) { Write-Bad "Path not found: $Path"; exit 1 }
Set-Location $Path

Write-Step "[2] Central proxy on port $Port"

$proxyLive = Test-NetConnection 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
if ($proxyLive) {
    Write-Ok "central proxy found on port $Port - this session will ATTACH to it"
    Write-Host "        Your Anthropic traffic is pinned per request, so it reaches" -ForegroundColor DarkGray
    Write-Host "        Anthropic even though the shared proxy points at Copilot." -ForegroundColor DarkGray
} else {
    Write-Warn "no central proxy on port $Port - wrap will start its own"
    Write-Host "        For one shared proxy, run .\Start-HeadroomProxy.ps1 first." -ForegroundColor DarkGray
}

if (-not $NoDashboard) {
    Start-Process "http://127.0.0.1:$Port/dashboard"
    Write-Ok "dashboard: http://127.0.0.1:$Port/dashboard"
}

Write-Host "`n=============================================" -ForegroundColor Magenta
Write-Host " What to test once Claude Code starts" -ForegroundColor Magenta
Write-Host "=============================================" -ForegroundColor Magenta
Write-Host @"

   1. Ask anything, then check the dashboard - savings should climb.
   2. Switch model mid-session (/model) and continue.
   3. Ask for work that spawns subagents.

   4. Confirm the split: the banner should say this session's Anthropic
      traffic is pinned to https://api.anthropic.com.

  All three run at once on the CENTRAL proxy (port $Port):
     .\Start-HeadroomProxy.ps1   then   .\Start-HeadroomCopilotCli.ps1
                                 and    .\Start-HeadroomVSCode.ps1

  WHEN DONE:  Ctrl+C here, then:  headroom unwrap claude

  Proxy log:  $env:USERPROFILE\.headroom\logs\proxy.log

"@ -ForegroundColor Gray

Write-Step "[3] Starting Claude Code through Headroom"
Write-Host "    running: headroom wrap claude --port $Port$(if ($proxyLive) { ' --no-proxy' }) $($ClaudeArgs -join ' ')" -ForegroundColor DarkGray
Write-Host ""

# --no-proxy when the central proxy is already up: attach to it instead of
# tripping the upstream-mismatch guard, which exists for clients that do NOT
# pin their own upstream. This one does.
$argv = @('wrap', 'claude', '--port', $Port)
if ($proxyLive) { $argv += '--no-proxy' }
if ($ClaudeArgs.Count) { $argv += '--'; $argv += $ClaudeArgs }

& headroom @argv
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) { Write-Ok "Claude Code session ended cleanly" }
else             { Write-Warn "Claude Code exited with code $code" }
Write-Host "    Stop everything with: headroom unwrap claude" -ForegroundColor DarkGray
exit $code
