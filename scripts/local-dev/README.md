# Running this PR locally

Scripts for developers who want to try PR #2643 before it merges. They install
the branch and run **one shared local proxy** serving the GitHub Copilot CLI,
VS Code Copilot Chat and Claude Code at the same time.

## What the PR adds

| Feature | Command |
| --- | --- |
| Route Copilot's own API through Headroom, keeping native model routing and mid-session model switching | `headroom wrap copilot --native` |
| The same for VS Code Copilot Chat: Copilot's normal picker, every model compressed — including models the *agent* picks for a subagent | `headroom wrap vscode-chat` |
| Catalog-driven model routing and wire selection | `headroom models` |

## Install

```powershell
.\Install-HeadroomPR.ps1
```

Clones or updates the repo, checks out the PR, builds a virtualenv, installs
Headroom editable, and verifies the commands above exist. Re-runnable; it stops
rather than discarding uncommitted work.

Then put the venv on PATH and confirm you are signed in to Copilot:

```powershell
$env:PATH = "<repo>\.venv\Scripts;$env:PATH"
headroom copilot-auth status
```

## Run

```powershell
.\Start-HeadroomProxy.ps1        # central proxy on 8970 - leave it open

# then, in any order, in other terminals:
.\Start-HeadroomCopilotCli.ps1   # Copilot CLI, --native
.\Start-HeadroomVSCode.ps1       # VS Code Copilot Chat
.\Start-HeadroomClaudeCode.ps1   # Claude Code
```

All three attach to the proxy on 8970, so everything lands on one dashboard at
`http://127.0.0.1:8970/dashboard` with one savings total.

Each script also works standalone — if no central proxy is running it starts its
own, which is fine for testing a single client.

## Why VS Code needed more than BYOK

The first attempt registered duplicate "(Headroom)" models as a Custom Endpoint
BYOK provider. That only ever covered models a **human** picked from the picker.
When the agent chose a model itself — a subagent, or auto model selection — it
picked from Copilot's own list and ran on Copilot's uncompressed endpoint.

`wrap vscode-chat` now redirects the Chat extension's whole API surface at the
proxy instead, via `github.copilot.advanced.debug.overrideCapiUrl` (user-scope
only). Every endpoint the extension uses derives from that one base URL, so the
picker is unchanged and everything through it is compressed — the same thing
`COPILOT_API_URL` does for the CLI in `--native` mode.

The BYOK provider is still available behind `--byok-models`, but it is redundant.

One consequence worth knowing: while VS Code is routed at the proxy, stopping
the proxy stops Copilot Chat. Run `headroom unwrap vscode-chat` and restart VS
Code to hand it back to GitHub.

## How one proxy serves all three

The proxy has **one** default destination per wire. The central proxy pins both
wires at the GitHub Copilot host, because `wrap copilot --native` drives Claude
models over `/v1/messages` and OpenAI models over `/responses`.

That is also why sharing needs care. The Anthropic handler forwards the client's
own `x-api-key` unchanged, so a naive share would send a Claude Code key to
GitHub. Two things prevent it:

- `wrap claude` pins **its own** upstream per request with
  `X-Headroom-Base-Url: https://api.anthropic.com`, which the proxy's
  `/v1/messages` route honours. Claude Code's traffic reaches Anthropic no
  matter where the shared proxy points.
- The proxy **refuses** to forward an `x-api-key` to a non-Anthropic upstream at
  all, so a dropped header fails loudly instead of leaking a credential.

Attaching is also identity-gated: a wrapper joins a running proxy only when it
is serving the same GitHub account, compared by a non-secret digest of the OAuth
token. A different account is moved to its own port automatically.

## Undo

```powershell
headroom unwrap copilot
headroom unwrap vscode-chat
headroom unwrap claude
```

`unwrap vscode-chat` removes only the provider block Headroom wrote — it proves
ownership from an out-of-band digest, so hand-edited entries are never touched.

## Not covered

VS Code inline (ghost-text) completions, semantic search and embeddings always
go straight to GitHub whatever model is selected, so they are neither compressed
nor counted.
