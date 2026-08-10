<#
.SYNOPSIS
    Route VS Code's GitHub Copilot Chat through Headroom.

.DESCRIPTION
    Starts (or joins) the shared Headroom proxy, registers every model your
    Copilot subscription is entitled to as "... (Headroom)" in VS Code's chat
    model picker, then opens the dashboard and VS Code.

    Uses the SAME port as Start-HeadroomCopilotCli.ps1 on purpose: run either
    script first and the other joins the proxy already running, so the CLI and
    VS Code share one proxy, one dashboard and one savings total.

    This does NOT start or touch the Copilot CLI.

.EXAMPLE
    .\Start-HeadroomVSCode.ps1
.EXAMPLE
    .\Start-HeadroomVSCode.ps1 -Port 8970 -Path C:\git\my-repo
#>
[CmdletBinding()]
param(
    # Shared with Start-HeadroomCopilotCli.ps1 - keep them the same.
    [ValidateRange(1, 65535)]
    [int]$Port = 8970,

    # Defaults to the repo this script lives in.
    [string]$Path = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,

    # Configure the proxy but leave the editor alone.
    [switch]$NoVSCode
)

$ErrorActionPreference = 'Stop'

function Write-Ok   { param($m) Write-Host "    OK  $m"  -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "    !   $m"  -ForegroundColor Yellow }
function Write-Bad  { param($m) Write-Host "    X   $m"  -ForegroundColor Red }
function Write-Step { param($m) Write-Host "`n$m" -ForegroundColor Cyan }

Write-Host "`n===============================================================" -ForegroundColor Magenta
Write-Host " Headroom for the VS CODE Copilot Chat extension" -ForegroundColor Magenta
Write-Host " (this does NOT touch or start the Copilot CLI)" -ForegroundColor Magenta
Write-Host "===============================================================" -ForegroundColor Magenta

# --- 1. Preflight ----------------------------------------------------------
Write-Step "[1] Preflight"

$headroom = (Get-Command headroom -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
if (-not $headroom) {
    Write-Bad "'headroom' is not on PATH. Activate the venv or reinstall, then retry."
    exit 1
}
Write-Ok "headroom found: $headroom"

if (-not (Test-Path $Path)) { Write-Bad "Path not found: $Path"; exit 1 }

Push-Location $Path
try { $branch = (git rev-parse --abbrev-ref HEAD 2>$null) } catch { $branch = $null }
Pop-Location
# The VS Code work now lives on the PR branch; the old feature branch still
# works for anyone who has not merged.
$expectedBranches = @('fix/copilot-responses-mixed-model-routing', 'feat/vscode-copilot-chat-byok')
if ($expectedBranches -contains $branch) { Write-Ok "on branch $branch" }
elseif ($branch) { Write-Warn "on branch '$branch' (expected $($expectedBranches[0]))" }

if (-not (& headroom wrap vscode-chat --help 2>&1 | Select-String -Quiet 'vscode-chat|Usage')) {
    Write-Bad "'headroom wrap vscode-chat' is unavailable - wrong branch or stale install."
    exit 1
}
Write-Ok "wrap vscode-chat command available"

# A proxy already on this port is almost certainly the Copilot CLI script's.
# Same account => it is shared rather than duplicated.
$proxyLive = Test-NetConnection 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
if ($proxyLive) {
    Write-Ok "central proxy found on port $Port - this session will ATTACH to it"
    Write-Host "        (started by Start-HeadroomProxy.ps1, or another script)" -ForegroundColor DarkGray
} else {
    Write-Warn "no central proxy on port $Port"
    Write-Host "        This script will start one, but it dies with this script's window." -ForegroundColor DarkGray
    Write-Host "        For a proxy that outlives it, Ctrl+C now and run:" -ForegroundColor DarkGray
    Write-Host "            .\Start-HeadroomProxy.ps1" -ForegroundColor DarkGray
}

# --- 2. Back up the VS Code config we are about to edit --------------------
Write-Step "[2] Backing up your VS Code config"

$userDir = Join-Path $env:APPDATA 'Code\User'
$backup  = Join-Path $userDir ("headroom-backup-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$saved   = @()
if (Test-Path $userDir) {
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    foreach ($f in 'chatLanguageModels.json', 'settings.json') {
        $src = Join-Path $userDir $f
        if (Test-Path $src) { Copy-Item $src (Join-Path $backup $f) -Force; $saved += $f }
    }
}
if ($saved.Count) {
    Write-Ok ("backed up " + ($saved -join ', ') + " to:")
    Write-Host "        $backup" -ForegroundColor DarkGray
    Write-Host "        (or just run: headroom unwrap vscode-chat)" -ForegroundColor DarkGray
} else {
    Write-Ok "nothing to back up yet (first run)"
}

# --- 3. Start or join the shared proxy -------------------------------------
Write-Step "[3] $(if ($proxyLive) { 'Attaching to the central' } else { 'Starting a' }) Headroom proxy on port $Port"
if ($proxyLive) {
    Write-Host "    (registering the models against the proxy that is already there)" -ForegroundColor DarkGray
} else {
    Write-Host "    (no central proxy found - this window will own one instead." -ForegroundColor DarkGray
    Write-Host "     For a proxy that outlives this script, run .\Start-HeadroomProxy.ps1 first.)" -ForegroundColor DarkGray
}

$cmd = @"
`$Host.UI.RawUI.WindowTitle = 'HEADROOM - VS Code chat models - port $Port'
Write-Host ''
Write-Host ' HEADROOM - VS Code Copilot Chat' -ForegroundColor Magenta
Write-Host ' Registers the chat models and holds the proxy. Leave this open.' -ForegroundColor DarkGray
Write-Host ' This window does NOT run the Copilot CLI.' -ForegroundColor DarkGray
Write-Host ''
Set-Location '$Path'
headroom wrap vscode-chat --port $Port
"@
$shell = if (Get-Command pwsh -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell' }
Start-Process $shell -ArgumentList '-NoExit', '-Command', $cmd

Write-Host "    waiting for the proxy..." -ForegroundColor DarkGray
$deadline = (Get-Date).AddSeconds(90)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        if ((Invoke-WebRequest "http://127.0.0.1:$Port/health" -TimeoutSec 3 -UseBasicParsing).StatusCode -eq 200) { $ready = $true; break }
    } catch { Start-Sleep -Milliseconds 1500 }
}
if (-not $ready) {
    Write-Bad "proxy did not come up on port $Port. Check the proxy window for the error."
    exit 1
}
Write-Ok "proxy healthy on http://127.0.0.1:$Port"

# Health is NOT the condition this script needs. When attaching to a proxy that
# is already up, /health answers immediately while the wrap session is still
# resolving the model catalog and writing the config -- so reading the file here
# found nothing, and the script gave up before it ever opened VS Code. Wait for
# the models themselves, which is the thing step 4 actually checks.
$modelsFile = Join-Path $userDir 'chatLanguageModels.json'
Write-Host "    waiting for the chat models to be registered..." -ForegroundColor DarkGray
$deadline = (Get-Date).AddSeconds(120)
$registered = $false
while ((Get-Date) -lt $deadline) {
    $raw = Get-Content $modelsFile -Raw -ErrorAction SilentlyContinue
    if ($raw -and $raw -match 'Headroom') { $registered = $true; break }
    Start-Sleep -Milliseconds 1000
}

# --- 4. Verify what actually landed in the config --------------------------
Write-Step "[4] Verifying VS Code config"

if (-not $registered) {
    Write-Bad "the chat models were not registered within 2 minutes."
    Write-Host "        Look at the 'HEADROOM - VS Code chat models' window - it prints" -ForegroundColor DarkGray
    Write-Host "        the reason (auth, an unparseable config, or a BYOK entitlement)." -ForegroundColor DarkGray
    exit 1
}
if (-not (Test-Path $modelsFile)) {
    Write-Bad "chatLanguageModels.json was not written. Check the proxy window."
    exit 1
}
$providers = Get-Content $modelsFile -Raw | ConvertFrom-Json
$mine  = $providers | Where-Object { $_.name -like '*Headroom*' }
$other = $providers | Where-Object { $_.name -notlike '*Headroom*' } | ForEach-Object { $_.name }

if (-not $mine) { Write-Bad "no Headroom provider found in $modelsFile"; exit 1 }
Write-Ok "$(@($mine.models).Count) Headroom models registered"
if ($other) { Write-Ok "your other providers preserved: $($other -join ', ')" }

# Ids must be prefixed, or VS Code's picker collapses them into Copilot's own
# entry and the models you use most silently lose their Headroom twin.
$unprefixed = @($mine.models | Where-Object { $_.id -notlike 'headroom--*' })
if ($unprefixed.Count) { Write-Warn "$($unprefixed.Count) model(s) are missing the headroom-- id prefix" }
else                   { Write-Ok "model ids are prefixed (so they stay visible next to Copilot's own)" }

$settings = Get-Content (Join-Path $userDir 'settings.json') -Raw -ErrorAction SilentlyContinue
if ($settings -match 'chat\.agentHost\.byokModels\.enabled') { Write-Ok "chat.agentHost.byokModels.enabled is set (required on VS Code 1.132+)" }
else { Write-Warn "chat.agentHost.byokModels.enabled is NOT set - VS Code 1.132+ will hide every Headroom model" }

# --- 5. Open the dashboard and VS Code -------------------------------------
Write-Step "[5] Opening dashboard and VS Code"

Start-Process "http://127.0.0.1:$Port/dashboard"
Write-Ok "dashboard: http://127.0.0.1:$Port/dashboard"

if ($NoVSCode) {
    Write-Ok "skipping VS Code (-NoVSCode)"
} else {
    # Verified rather than assumed: silencing the error would make a failed
    # launch look exactly like a successful one.
    $before   = @(Get-Process Code -ErrorAction SilentlyContinue).Count
    $launched = $false
    try { Start-Process "code" -ArgumentList "`"$Path`"" -ErrorAction Stop; $launched = $true }
    catch { Write-Warn "the 'code' command failed: $($_.Exception.Message)" }

    if (-not $launched) {
        foreach ($exe in @(
            "$env:LOCALAPPDATA\Programs\Microsoft VS Code\Code.exe",
            "$env:ProgramFiles\Microsoft VS Code\Code.exe")) {
            if (Test-Path $exe) {
                try { Start-Process $exe -ArgumentList "`"$Path`"" -ErrorAction Stop; $launched = $true; break } catch { }
            }
        }
    }

    if ($launched) {
        $deadline = (Get-Date).AddSeconds(15); $up = $false
        while ((Get-Date) -lt $deadline) {
            if (@(Get-Process Code -ErrorAction SilentlyContinue).Count -gt $before) { $up = $true; break }
            Start-Sleep -Milliseconds 700
        }
        if ($up) { Write-Ok "VS Code opened at $Path" }
        else     { Write-Warn "VS Code was launched but no new window was detected - it may have reused an existing one" }
    } else {
        Write-Bad "could not start VS Code. Open it yourself, then continue below."
    }
}

# --- 6. What to test -------------------------------------------------------
Write-Host "`n=============================================" -ForegroundColor Magenta
Write-Host " Ready - here is what to test" -ForegroundColor Magenta
Write-Host "=============================================" -ForegroundColor Magenta
Write-Host @"

  In VS Code, open Copilot Chat and use the MODEL PICKER.
  Headroom models are under: Other Models > Headroom (GitHub Copilot)

   1. Pick any model ending "(Headroom)" and ask anything.
        -> confirms chat traffic flows through the proxy

   2. Check the models you actually use are there - Claude Opus 5,
      Claude Sonnet 5, Claude Opus 4.8, GPT-5.6 Sol, GPT-5.6 Terra.
        -> these five used to be missing; that was the id-collision bug

   3. SWITCH model mid-session to another "(Headroom)" model and ask again.
        -> model switching without restarting

   4. Pick a /responses model, e.g. "GPT-5.5 (Headroom)" or
      "MAI-Code-1-Flash (Headroom)", and ask something.

   5. Try AGENT mode with a tool-using request ("read README.md and
      summarise it").

   6. Watch the dashboard - savings should climb as you chat.

  RUNNING ALL THREE AT ONCE:
     In other terminals: .\Start-HeadroomCopilotCli.ps1
                         .\Start-HeadroomClaudeCode.ps1
     Both attach to this same proxy on port $Port - one dashboard, one total.

  EXPECTED DIFFERENCES from stock Copilot Chat (not bugs):
   - Headroom models are a separate group, suffixed "(Headroom)".
   - Built-in Copilot models still appear and BYPASS Headroom if selected.
   - Inline (ghost-text) completions always go straight to GitHub - never
     compressed or counted, whatever model you pick.
   - If no Headroom models appear: restart VS Code once (the agent host has
     to restart), then check the proxy window for warnings.

  WHEN DONE:
     headroom unwrap vscode-chat     # removes the models + setting
     (then Ctrl+C in the proxy window)

  Proxy log:
     $env:USERPROFILE\.headroom\logs\proxy.log

"@ -ForegroundColor Gray
