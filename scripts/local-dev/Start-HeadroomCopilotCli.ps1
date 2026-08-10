<#
.SYNOPSIS
    Run the GitHub Copilot CLI through Headroom in --native mode.

.DESCRIPTION
    Launches `headroom wrap copilot --native`, which redirects Copilot's own
    API surface through the local proxy. Copilot keeps its native model
    routing, so /model and the full picker still work and the main agent can
    be switched mid-session - unlike BYOK mode, which pins one model.

    Uses the SAME port as Start-HeadroomVSCode.ps1 on purpose: run either
    script first and the other joins the proxy already running, so the CLI and
    VS Code share one proxy, one dashboard and one savings total.

    The Copilot CLI runs in THIS window. Ctrl+C ends the session.

.EXAMPLE
    .\Start-HeadroomCopilotCli.ps1
.EXAMPLE
    .\Start-HeadroomCopilotCli.ps1 -Port 8970 -Path C:\git\my-repo
#>
[CmdletBinding()]
param(
    # Shared with Start-HeadroomVSCode.ps1 - keep them the same.
    [ValidateRange(1, 65535)]
    [int]$Port = 8970,

    # Defaults to the repo this script lives in.
    [string]$Path = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,

    # Don't open the dashboard in a browser.
    [switch]$NoDashboard,

    # Extra args passed through to the Copilot CLI, e.g. -CopilotArgs '--model','gpt-5.5'
    [string[]]$CopilotArgs = @()
)

$ErrorActionPreference = 'Stop'

function Write-Ok   { param($m) Write-Host "    OK  $m"  -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "    !   $m"  -ForegroundColor Yellow }
function Write-Bad  { param($m) Write-Host "    X   $m"  -ForegroundColor Red }
function Write-Step { param($m) Write-Host "`n$m" -ForegroundColor Cyan }

Write-Host "`n===============================================================" -ForegroundColor Magenta
Write-Host " Headroom for the COPILOT CLI  (--native mode)" -ForegroundColor Magenta
Write-Host " The CLI runs in THIS window. VS Code is not touched." -ForegroundColor Magenta
Write-Host "===============================================================" -ForegroundColor Magenta

# --- 1. Preflight ----------------------------------------------------------
Write-Step "[1] Preflight"

$headroom = (Get-Command headroom -ErrorAction SilentlyContinue)?.Source
if (-not $headroom) {
    Write-Bad "'headroom' is not on PATH. Activate the venv or reinstall, then retry."
    exit 1
}
Write-Ok "headroom found: $headroom"

$copilot = (Get-Command copilot -ErrorAction SilentlyContinue)?.Source
if ($copilot) { Write-Ok "copilot CLI found: $copilot" }
else { Write-Warn "'copilot' was not found on PATH - headroom will report it if it cannot launch" }

if (-not (Test-Path $Path)) { Write-Bad "Path not found: $Path"; exit 1 }
Set-Location $Path

try { $branch = (git rev-parse --abbrev-ref HEAD 2>$null) } catch { $branch = $null }
if ($branch) { Write-Ok "on branch $branch" }

# --- 2. Shared proxy -------------------------------------------------------
Write-Step "[2] Shared Headroom proxy on port $Port"

$proxyLive = Test-NetConnection 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
if ($proxyLive) {
    Write-Ok "central proxy found on port $Port - this session will ATTACH to it"
    Write-Host "        Same GitHub account => attaches. A different account is moved" -ForegroundColor DarkGray
    Write-Host "        to its own port automatically." -ForegroundColor DarkGray
} else {
    Write-Warn "no central proxy on port $Port - wrap will start its own"
    Write-Host "        For a proxy shared with VS Code, run .\Start-HeadroomProxy.ps1 first." -ForegroundColor DarkGray
}

if (-not $NoDashboard) {
    # Opened before the CLI takes over the window; harmless if not up yet.
    Start-Process "http://127.0.0.1:$Port/dashboard"
    Write-Ok "dashboard: http://127.0.0.1:$Port/dashboard"
}

# --- 3. What to test -------------------------------------------------------
Write-Host "`n=============================================" -ForegroundColor Magenta
Write-Host " What to test once the CLI starts" -ForegroundColor Magenta
Write-Host "=============================================" -ForegroundColor Magenta
Write-Host @"

   1. Ask anything, then check the dashboard - savings should climb.

   2. Run /model  ->  the FULL Copilot model list should be there, not a
      single pinned model. That is what --native buys over BYOK.

   3. Switch the main model mid-session (/model, pick another) and continue.

   4. Ask for work that spawns subagents - they should be able to use
      other models too.

   5. Try a Claude model AND a GPT model in the same session: they ride
      different wires (/v1/messages vs /responses) and both must work.

  RUNNING BOTH AT ONCE:
     Open another terminal and run .\Start-HeadroomVSCode.ps1
     It attaches to the same proxy on port $Port - one dashboard, one total.
     (Claude Code is separate on port 8972 - see Start-HeadroomClaudeCode.ps1)

  WHEN DONE:
     Ctrl+C here, then:  headroom unwrap copilot

  Proxy log:
     $env:USERPROFILE\.headroom\logs\proxy.log

"@ -ForegroundColor Gray

# --- 4. Launch -------------------------------------------------------------
Write-Step "[3] Starting Copilot CLI through Headroom (--native)"
Write-Host "    running: headroom wrap copilot --native --port $Port $($CopilotArgs -join ' ')" -ForegroundColor DarkGray
Write-Host ""

# --native implies --subscription. Extra CLI args go after `--`.
$argv = @('wrap', 'copilot', '--native', '--port', $Port)
if ($CopilotArgs.Count) { $argv += '--'; $argv += $CopilotArgs }

& headroom @argv
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) { Write-Ok "Copilot CLI session ended cleanly" }
else             { Write-Warn "Copilot CLI exited with code $code" }
Write-Host "    The shared proxy may still be running for VS Code - that is expected." -ForegroundColor DarkGray
Write-Host "    Stop everything with: headroom unwrap copilot" -ForegroundColor DarkGray
exit $code
