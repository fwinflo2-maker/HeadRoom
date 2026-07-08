---
name: safe-codex-codex-app
description: Operate safe-codex with Codex Desktop Actions and the headroom custom provider while avoiding secret, prompt, response, and stderr body persistence.
---

# safe-codex Codex App Operation

Use this skill when working with the headroom-safe-codex project and the goal is to operate Codex through the safe-codex proxy or verify the Codex custom provider.

## Scope

This skill covers:

- starting the safe-codex proxy on loopback
- checking proxy status and /health
- using the Codex custom provider named headroom
- verifying completion without saving prompt, response, or stderr bodies
- stopping the proxy safely

This skill does not cover:

- saving API keys
- modifying ~/.codex/config.toml unless explicitly requested
- enabling request/response body logging
- enabling Codex wire debug
- pushing changes to remote

## Safety rules

Always follow these rules:

- Bind the proxy only to 127.0.0.1.
- Do not use 0.0.0.0 or any non-loopback host.
- Do not save API keys, tokens, Authorization headers, prompt bodies, response bodies, or full stderr bodies.
- Do not use --log-messages.
- Do not use HEADROOM_LOG_MESSAGES=1.
- Do not use --codex-wire-debug.
- Do not use --codex-wire-debug-dir.
- Do not use HEADROOM_CODEX_WIRE_DEBUG=1.
- Do not commit phase*_investigation/ directories.
- Do not push unless explicitly instructed.

## Standard PowerShell setup

Use PowerShell 5.1-compatible commands.

    cd C:\dev\headroom-safe-codex

    $ErrorActionPreference = "Stop"
    $env:PYTHONUTF8 = "1"
    [Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

    if (Test-Path ".venv\Scripts\Activate.ps1") {
        . .venv\Scripts\Activate.ps1
    }

## Start proxy

Prefer the project script:

    .\scripts\start-safe-codex-proxy.ps1

Expected behavior:

- starts headroom proxy --profile safe-codex
- binds to 127.0.0.1:8787
- avoids message logging and wire debug
- avoids duplicate startup when port 8787 is already listening

## Check proxy

    .\scripts\check-safe-codex-status.ps1

Minimum expected result:

- proxy is listening on loopback
- /health returns 200

Do not record request body, response body, prompt body, or Authorization details.

## Codex custom provider

Expected provider settings:

    model_provider = "headroom"

    [model_providers.headroom]
    name = "headroom"
    base_url = "http://127.0.0.1:8787/v1"

Do not add API keys, tokens, or Authorization headers to project docs.

## Completion verification

When verifying codex exec:

- do not redirect full stdout or stderr to files
- do not save prompt text
- do not save response text
- do not save full stderr text
- record only derived facts, such as:
  - exit code
  - whether provider=headroom was observed
  - whether usage limit was observed
  - whether completion succeeded
  - whether temporary prompt-bearing artifacts were deleted

PowerShell 5.1 can treat native stderr as NativeCommandError; avoid preserving raw stderr artifacts.

## Stop proxy

    .\scripts\stop-safe-codex-env.ps1

Expected behavior:

- only stops the process that appears to be the safe-codex proxy
- removes temporary safe-codex environment variables
- avoids stopping unrelated processes

## Documentation sources

Before changing behavior, check:

- safe-codex-notes/00_README.md
- safe-codex-notes/01_PROJECT_CONTEXT.md
- safe-codex-notes/02_DESIGN_DECISIONS.md
- safe-codex-notes/03_ROADMAP_PROGRESS.md
- safe-codex-notes/04_SAFE_CODEX_OPERATION.md
- safe-codex-notes/05_CODEX_APP_OPERATION.md

## Final checks

Before commit:

    git status --short --branch
    git diff --check
    git diff --stat

Also verify that the changed Markdown files contain no NUL bytes.

Do not add or commit investigation directories.