<#
.SYNOPSIS
    Install Headroom PR #2643 locally so you can run it against Copilot CLI,
    VS Code Copilot Chat and Claude Code.

.DESCRIPTION
    For developers who want to try the PR before it merges. It clones (or
    updates) the repo, checks out the PR branch, builds an isolated virtualenv,
    installs Headroom in editable mode, and verifies the commands this PR adds.

    Safe to re-run: it updates an existing checkout instead of duplicating it,
    and it never touches your global Python or your existing Headroom install
    beyond putting this venv's `headroom` on PATH for the shell you launch from
    the run scripts.

    What the PR contains:
      * `wrap copilot --native` - routes Copilot's own API through Headroom, so
        the CLI keeps its native model routing and mid-session model switching.
      * `wrap vscode-chat`      - registers every entitled model in VS Code's
        chat picker via the Custom Endpoint BYOK provider.
      * one shared local proxy serving all three clients at once.

.EXAMPLE
    .\Install-HeadroomPR.ps1
.EXAMPLE
    .\Install-HeadroomPR.ps1 -InstallDir C:\src -Pr 2643
#>
[CmdletBinding()]
param(
    # Parent directory the repo is cloned into.
    [string]$InstallDir = "$env:USERPROFILE\headroom-dev",

    [int]$Pr = 2643,

    # Branch to use when the gh CLI is unavailable.
    [string]$Branch = "fix/copilot-responses-mixed-model-routing",

    [string]$RepoUrl = "https://github.com/headroomlabs-ai/headroom.git",

    # Skip the test run at the end (faster, less confidence).
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'

function Write-Ok   { param($m) Write-Host "    OK  $m"  -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "    !   $m"  -ForegroundColor Yellow }
function Write-Bad  { param($m) Write-Host "    X   $m"  -ForegroundColor Red }
function Write-Step { param($m) Write-Host "`n$m" -ForegroundColor Cyan }

Write-Host "`n===============================================================" -ForegroundColor Magenta
Write-Host " Install Headroom PR #$Pr locally" -ForegroundColor Magenta
Write-Host " Copilot CLI (native) + VS Code Copilot Chat + Claude Code" -ForegroundColor Magenta
Write-Host "===============================================================" -ForegroundColor Magenta

# --- 1. Prerequisites ------------------------------------------------------
Write-Step "[1] Prerequisites"

foreach ($tool in 'git', 'python') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Bad "'$tool' is not on PATH. Install it and re-run."
        exit 1
    }
}
Write-Ok "git: $((Get-Command git).Source)"

# 3.11+ is required; 3.13 is what CI runs.
$pyVersion = (& python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null)
$pyMajorMinor = [version]($pyVersion -replace '^(\d+\.\d+).*', '$1')
if ($pyMajorMinor -lt [version]'3.11') {
    Write-Bad "Python $pyVersion found, but 3.11+ is required."
    exit 1
}
Write-Ok "python $pyVersion"

$gh = Get-Command gh -ErrorAction SilentlyContinue
if ($gh) { Write-Ok "gh CLI available - will check out PR #$Pr directly" }
else     { Write-Warn "gh CLI not found - falling back to branch '$Branch'" }

# --- 2. Clone or update ----------------------------------------------------
Write-Step "[2] Source"

$repo = Join-Path $InstallDir 'headroom'
if (Test-Path (Join-Path $repo '.git')) {
    Write-Ok "existing checkout: $repo"
    Push-Location $repo
    # Never discard local work - report it and stop rather than surprise anyone.
    if ((git status --porcelain) -and -not (git status --porcelain).Trim().Length -eq 0) {
        $dirty = (git status --porcelain) | Measure-Object | Select-Object -ExpandProperty Count
        if ($dirty -gt 0) {
            Write-Bad "$dirty uncommitted change(s) in $repo. Commit or stash them, then re-run."
            Pop-Location; exit 1
        }
    }
    git fetch origin --prune 2>&1 | Out-Null
} else {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    Write-Host "    cloning $RepoUrl ..." -ForegroundColor DarkGray
    git clone $RepoUrl $repo 2>&1 | Out-Null
    if (-not (Test-Path (Join-Path $repo '.git'))) { Write-Bad "clone failed"; exit 1 }
    Write-Ok "cloned to $repo"
    Push-Location $repo
}

if ($gh) {
    gh pr checkout $Pr 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "gh pr checkout failed (not authenticated?); falling back to '$Branch'"
        git checkout $Branch 2>&1 | Out-Null
        git pull --ff-only 2>&1 | Out-Null
    }
} else {
    git checkout $Branch 2>&1 | Out-Null
    git pull --ff-only 2>&1 | Out-Null
}

$current = (git rev-parse --abbrev-ref HEAD)
$sha     = (git rev-parse --short HEAD)
if ($LASTEXITCODE -ne 0 -or -not $current) { Write-Bad "could not check out the PR branch"; Pop-Location; exit 1 }
Write-Ok "on branch $current @ $sha"

# --- 3. Virtualenv + install ----------------------------------------------
Write-Step "[3] Virtualenv and install"

$venv = Join-Path $repo '.venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    Write-Host "    creating venv ..." -ForegroundColor DarkGray
    & python -m venv $venv
}
$py = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $py)) { Write-Bad "venv creation failed"; Pop-Location; exit 1 }
Write-Ok "venv: $venv"

Write-Host "    installing (editable, this takes a few minutes) ..." -ForegroundColor DarkGray
& $py -m pip install --upgrade pip 2>&1 | Out-Null
& $py -m pip install -e ".[dev]" 2>&1 | Select-Object -Last 3 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
if ($LASTEXITCODE -ne 0) {
    Write-Warn "editable install with [dev] extras failed; retrying without extras"
    & $py -m pip install -e . 2>&1 | Select-Object -Last 3 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    if ($LASTEXITCODE -ne 0) { Write-Bad "install failed"; Pop-Location; exit 1 }
}
Write-Ok "Headroom installed in editable mode"

# --- 4. Verify the PR's commands actually exist ---------------------------
Write-Step "[4] Verifying this PR's features"

$headroomExe = Join-Path $venv 'Scripts\headroom.exe'
if (-not (Test-Path $headroomExe)) { Write-Bad "'headroom' was not installed into the venv"; Pop-Location; exit 1 }
Write-Ok "headroom: $headroomExe"

$checks = @(
    @{ Name = 'wrap copilot --native (native mode)'; Args = @('wrap','copilot','--help');     Match = '--native' },
    @{ Name = 'wrap vscode-chat (VS Code chat)';     Args = @('wrap','vscode-chat','--help'); Match = 'Usage|vscode-chat' },
    @{ Name = 'unwrap vscode-chat';                  Args = @('unwrap','vscode-chat','--help');Match = 'Usage|vscode-chat' },
    @{ Name = 'wrap claude (Claude Code)';           Args = @('wrap','claude','--help');      Match = 'Usage|claude' },
    @{ Name = 'headroom models (catalog)';           Args = @('models','--help');             Match = 'Usage|models' }
)
$failed = 0
foreach ($c in $checks) {
    $out = & $headroomExe @($c.Args) 2>&1 | Out-String
    if ($out -match $c.Match) { Write-Ok $c.Name } else { Write-Bad "$($c.Name) - NOT available"; $failed++ }
}
if ($failed) { Write-Bad "$failed feature check(s) failed - the checkout may be wrong"; Pop-Location; exit 1 }

# --- 5. Tests --------------------------------------------------------------
if (-not $SkipTests) {
    Write-Step "[5] Running the PR's own tests"
    Write-Host "    (a focused subset - pass -SkipTests to skip)" -ForegroundColor DarkGray
    & $py -m pytest tests/test_vscode_chat_byok.py tests/test_copilot_model_catalog.py tests/test_copilot_native_mode.py -q 2>&1 |
        Select-Object -Last 3 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    if ($LASTEXITCODE -eq 0) { Write-Ok "tests passed" } else { Write-Warn "some tests failed - see output above" }
}

Pop-Location

# --- 6. What to do next ----------------------------------------------------
$scripts = Join-Path $repo 'scripts\local-dev'
Write-Host "`n=============================================" -ForegroundColor Magenta
Write-Host " Installed - here is how to run it" -ForegroundColor Magenta
Write-Host "=============================================" -ForegroundColor Magenta
Write-Host @"

  FIRST, put this venv on PATH for the shell you run the scripts from:

      `$env:PATH = "$venv\Scripts;`$env:PATH"

  You must be signed in to GitHub Copilot already:

      headroom copilot-auth status      # and `copilot-auth login` if needed

  THEN, from $scripts :

   1. Start the central proxy (leave it open):
          .\Start-HeadroomProxy.ps1

   2. In other terminals, in any order - all three attach to that one proxy:
          .\Start-HeadroomCopilotCli.ps1     # Copilot CLI, native mode
          .\Start-HeadroomVSCode.ps1         # VS Code Copilot Chat
          .\Start-HeadroomClaudeCode.ps1     # Claude Code

   3. Watch one dashboard for all of them:
          http://127.0.0.1:8970/dashboard

  WHAT TO LOOK FOR:
   * Copilot CLI: /model lists the FULL model set, not one pinned model,
     and you can switch mid-session.
   * VS Code: Copilot Chat's picker has a "Headroom (GitHub Copilot)" group
     under "Other Models", including Claude Opus 5 / Sonnet 5 / Opus 4.8.
   * Claude Code: its banner confirms Anthropic traffic is pinned to
     https://api.anthropic.com even though the proxy points at Copilot.
   * Savings climb on the shared dashboard as you use any of them.

  TO UNDO EVERYTHING:
      headroom unwrap copilot
      headroom unwrap vscode-chat
      headroom unwrap claude

"@ -ForegroundColor Gray
