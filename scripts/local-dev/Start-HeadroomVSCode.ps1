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
$modelsFile   = Join-Path $userDir 'chatLanguageModels.json'
$settingsFile = Join-Path $userDir 'settings.json'
Write-Host "    waiting for the routing settings to be written..." -ForegroundColor DarkGray
$deadline = (Get-Date).AddSeconds(120)
$registered = $false
while ((Get-Date) -lt $deadline) {
    $raw = Get-Content $settingsFile -Raw -ErrorAction SilentlyContinue
    if ($raw -and $raw -match 'overrideCapiUrl') { $registered = $true; break }
    Start-Sleep -Milliseconds 1000
}

# --- 4. Verify what actually landed in the config --------------------------
Write-Step "[4] Verifying VS Code config"

if (-not $registered) {
    Write-Bad "the routing settings were not written within 2 minutes."
    Write-Host "        Look at the 'HEADROOM - VS Code chat models' window - it prints" -ForegroundColor DarkGray
    Write-Host "        the reason (auth, an unparseable config, or a BYOK entitlement)." -ForegroundColor DarkGray
    exit 1
}
$settings = Get-Content (Join-Path $userDir 'settings.json') -Raw -ErrorAction SilentlyContinue
if ($settings -match 'overrideCapiUrl') {
    Write-Ok "Copilot Chat routing points at the proxy"
    if ($settings -match "overrideCapiUrl`"\s*:\s*`"([^`"]+)`"") {
        Write-Host "        $($Matches[1])" -ForegroundColor DarkGray
    }
} else {
    Write-Bad "the Copilot Chat routing setting was not written."
    Write-Host "        Look at the 'HEADROOM - VS Code chat models' window for the reason." -ForegroundColor DarkGray
    exit 1
}

# Optional: only present when wrap was run with --byok-models.
if (Test-Path $modelsFile) {
    $providers = Get-Content $modelsFile -Raw | ConvertFrom-Json
    $mine = $providers | Where-Object { $_.name -like '*Headroom*' }
    if ($mine) { Write-Ok "$(@($mine.models).Count) duplicate '(Headroom)' BYOK models also registered" }
    $other = $providers | Where-Object { $_.name -notlike '*Headroom*' } | ForEach-Object { $_.name }
    if ($other) { Write-Ok "your other providers preserved: $($other -join ', ')" }
}

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

  RESTART VS CODE FIRST - the routing setting is read at startup.

  Then use Copilot Chat's NORMAL model picker. There are no "(Headroom)"
  entries any more, and that is the point: Copilot's own models now go
  through Headroom.

   1. Ask anything with any model. Watch the dashboard - savings climb.

   2. Switch model mid-session and ask again.

   3. Ask the agent to use a different vendor's model ("use gemini").
        -> the SUBAGENT's traffic should now appear on the dashboard too.
           This is what BYOK could not do: the agent picks from Copilot's
           list, so a subagent used to bypass Headroom entirely.

   4. Try AGENT mode with a tool-using request ("read README.md and
      summarise it").

   5. Sanity check in the proxy window / log: you should see
      /models, /models/session and /chat/completions arriving.

  RUNNING ALL THREE AT ONCE:
     In other terminals: .\Start-HeadroomCopilotCli.ps1
                         .\Start-HeadroomClaudeCode.ps1
     Both attach to this same proxy on port $Port - one dashboard, one total.

  EXPECTED DIFFERENCES from stock Copilot Chat (not bugs):
   - The picker looks exactly as it always did. That is intentional.
   - Inline (ghost-text) completions always go straight to GitHub - never
     compressed or counted, whatever model you pick.
   - If the proxy is stopped while VS Code is routed at it, chat stops
     working until you `headroom unwrap vscode-chat` and restart VS Code.

  WHEN DONE:
     headroom unwrap vscode-chat     # removes the models + setting
     (then Ctrl+C in the proxy window)

  Proxy log:
     $env:USERPROFILE\.headroom\logs\proxy.log

"@ -ForegroundColor Gray
