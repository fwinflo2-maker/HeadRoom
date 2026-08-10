<#
.SYNOPSIS
    Start the central local Headroom proxy that Copilot CLI and VS Code share.

.DESCRIPTION
    One proxy, one dashboard, one savings total. Start this first and leave it
    running; Start-HeadroomCopilotCli.ps1 and Start-HeadroomVSCode.ps1 then
    attach to it instead of each starting their own.

    It runs `headroom wrap vscode-chat --no-configure`, which is the
    proxy-only watcher: it pins BOTH upstreams at the GitHub Copilot host and
    seeds this session's Copilot credential, but writes no VS Code config.
    Pinning both wires is what lets the Copilot CLI use Claude models
    (/v1/messages) as well as GPT models (/responses) in --native mode.

    Attaching is gated on identity: a wrapper joins only when the proxy is
    serving the same GitHub account. A different account is moved to its own
    port automatically.

    NOT for Claude Code. Because both upstreams point at Copilot, an Anthropic
    client attaching here would have its own API key forwarded to GitHub.
    Start-HeadroomClaudeCode.ps1 therefore uses its own port - by design.

.EXAMPLE
    .\Start-HeadroomProxy.ps1
.EXAMPLE
    .\Start-HeadroomProxy.ps1 -Port 8970 -Foreground
#>
[CmdletBinding()]
param(
    # Shared by the Copilot CLI and VS Code scripts - keep all three the same.
    [ValidateRange(1, 65535)]
    [int]$Port = 8970,

    # Defaults to the repo this script lives in.
    [string]$Path = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,

    # Run in this window instead of spawning one (useful for watching logs).
    [switch]$Foreground,

    [switch]$NoDashboard
)

$ErrorActionPreference = 'Stop'

function Write-Ok   { param($m) Write-Host "    OK  $m"  -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "    !   $m"  -ForegroundColor Yellow }
function Write-Bad  { param($m) Write-Host "    X   $m"  -ForegroundColor Red }
function Write-Step { param($m) Write-Host "`n$m" -ForegroundColor Cyan }

Write-Host "`n===============================================================" -ForegroundColor Magenta
Write-Host " Headroom CENTRAL PROXY  (port $Port)" -ForegroundColor Magenta
Write-Host " Shared by the Copilot CLI and VS Code Copilot Chat" -ForegroundColor Magenta
Write-Host "===============================================================" -ForegroundColor Magenta

Write-Step "[1] Preflight"

$headroom = (Get-Command headroom -ErrorAction SilentlyContinue)?.Source
if (-not $headroom) { Write-Bad "'headroom' is not on PATH."; exit 1 }
Write-Ok "headroom found: $headroom"
if (-not (Test-Path $Path)) { Write-Bad "Path not found: $Path"; exit 1 }

if (Test-NetConnection 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue) {
    Write-Ok "a proxy is ALREADY running on port $Port - nothing to do"
    Write-Host "        Just run Start-HeadroomCopilotCli.ps1 / Start-HeadroomVSCode.ps1;" -ForegroundColor DarkGray
    Write-Host "        they will attach to it." -ForegroundColor DarkGray
    if (-not $NoDashboard) { Start-Process "http://127.0.0.1:$Port/dashboard" }
    exit 0
}
Write-Ok "port $Port is free"

Write-Step "[2] Starting the central proxy"

$inner = "Set-Location '$Path'; headroom wrap vscode-chat --port $Port --no-configure"

if ($Foreground) {
    Write-Host "    running in THIS window - Ctrl+C stops it (and detaches every client)" -ForegroundColor DarkGray
    if (-not $NoDashboard) { Start-Process "http://127.0.0.1:$Port/dashboard" }
    Write-Host ""
    Set-Location $Path
    & headroom wrap vscode-chat --port $Port --no-configure
    exit $LASTEXITCODE
}

$cmd = @"
`$Host.UI.RawUI.WindowTitle = 'HEADROOM CENTRAL PROXY - port $Port'
Write-Host ''
Write-Host ' HEADROOM CENTRAL PROXY' -ForegroundColor Magenta
Write-Host ' Shared by the Copilot CLI and VS Code. Leave this window open.' -ForegroundColor DarkGray
Write-Host ' Closing it detaches every client.' -ForegroundColor DarkGray
Write-Host ''
$inner
"@
Start-Process powershell -ArgumentList '-NoExit', '-Command', $cmd

Write-Host "    waiting for the proxy..." -ForegroundColor DarkGray
$deadline = (Get-Date).AddSeconds(90); $ready = $false
while ((Get-Date) -lt $deadline) {
    try { if ((Invoke-WebRequest "http://127.0.0.1:$Port/health" -TimeoutSec 3 -UseBasicParsing).StatusCode -eq 200) { $ready = $true; break } }
    catch { Start-Sleep -Milliseconds 1500 }
}
if (-not $ready) { Write-Bad "proxy did not come up on port $Port - check its window."; exit 1 }
Write-Ok "central proxy healthy on http://127.0.0.1:$Port"

# Confirm it really is Copilot-pinned and credential-seeded, or the other
# scripts will quietly start their own instead of attaching.
try {
    $cfg = (Invoke-WebRequest "http://127.0.0.1:$Port/health" -TimeoutSec 10 -UseBasicParsing | ConvertFrom-Json).config
    if ($cfg.openai_api_url -like '*githubcopilot*')    { Write-Ok "OpenAI wire  -> $($cfg.openai_api_url)" }    else { Write-Warn "OpenAI wire is $($cfg.openai_api_url)" }
    if ($cfg.anthropic_api_url -like '*githubcopilot*') { Write-Ok "Anthropic wire -> $($cfg.anthropic_api_url) (lets the CLI use Claude models)" } else { Write-Warn "Anthropic wire is $($cfg.anthropic_api_url)" }
    if ($cfg.copilot_token_fingerprint) { Write-Ok "credential seeded ($($cfg.copilot_token_fingerprint)) - same-account clients will attach" }
    else { Write-Warn "no credential fingerprint - other scripts will start their own proxy instead of attaching" }
} catch { Write-Warn "could not read /health config: $($_.Exception.Message)" }

if (-not $NoDashboard) { Start-Process "http://127.0.0.1:$Port/dashboard"; Write-Ok "dashboard: http://127.0.0.1:$Port/dashboard" }

Write-Host @"

  NEXT - in other terminals, in any order:

     .\Start-HeadroomCopilotCli.ps1     # Copilot CLI, --native mode
     .\Start-HeadroomVSCode.ps1         # VS Code Copilot Chat

  Both attach to this proxy on port $Port. Everything shows up on the one
  dashboard above.

     .\Start-HeadroomClaudeCode.ps1     # Claude Code - SEPARATE port, on purpose
                                        # (this proxy would send an Anthropic
                                        #  key to GitHub; see that script)

  TO STOP: Ctrl+C in the proxy window.

"@ -ForegroundColor Gray
