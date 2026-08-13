# Design: headroom ↔ deepseek-harness (dsh) compatibility

Date: 2026-08-13
Status: Draft — pending user review

## Summary

Add `dsh` (DeepSeek Harness) to headroom's agent-compatibility matrix via a
`headroom wrap dsh` / `headroom unwrap dsh` runtime. The wrap starts the local
proxy, points dsh's DeepSeek provider at it, passes `DEEPSEEK_API_KEY` through,
and launches dsh (`web` by default, `headless` via a flag). The proxy compresses
all OpenAI-compatible chat-completions traffic and applies output shaping,
routing DeepSeek requests upstream to DeepSeek. All changes land in the headroom
repo; deepseek-harness is untouched.

## Scope

In scope:

- `headroom wrap dsh` / `headroom unwrap dsh` (launch-env-only; no durable config
  mutation in v1)
- First-class `deepseek` upstream target in the proxy
- DeepSeek request detection + upstream routing in the OpenAI chat handler
- Output shaping (verbosity steering + effort routing), which rides the existing
  OpenAI-compatible pipeline
- Tests, README compatibility matrix, and wiki/`llms.txt` docs

Out of scope (explicitly deferred by user):

- Cross-agent memory injection
- `headroom learn` → AGENTS.md
- Serena code navigation for dsh
- `headroom doctor` / dashboard acceptance bar
- dsh's secondary `llm-pi-ai` gateway provider
- Durable dsh settings patching (v1 uses a fail-loud guard instead)

## Architecture

### New code

1. `headroom/providers/dsh/` — mirrors the `zcode`/`opencode` provider packages:
   - `__init__.py`
   - `runtime.py` — `build_launch_env(port, environ)` returns the launch env with
     `DEEPSEEK_BASE_URL=http://127.0.0.1:{port}/v1` set and `DEEPSEEK_API_KEY`
     passed through; plus a `resolve_dsh_command(...)` helper that picks `web` vs
     `headless` and locates the `dsh` binary (`dsh` on `PATH`, then `pnpm dsh`,
     then an explicit `--command` override).
   - `install.py` — `build_install_env(port, backend, targets)` for `headroom
     deploy` parity, registered in `install_registry._ENV_BUILDERS`.

2. Wrap/unwrap CLI entries in `headroom/cli/wrap.py`:
   - `@wrap.command("dsh")` with `--port`, `--no-proxy`, `--profile` /
     `--command`, `--verbose`, `--prepare-only`, and passthrough args — following
     the `opencode`/`omp` pattern.
   - `@unwrap.command("dsh")` with `--port` / `--no-stop-proxy`.

### Modified code

3. `headroom/providers/registry.py` — add `deepseek` to the target/override surface:
   - `DEFAULT_DEEPSEEK_API_URL = "https://api.deepseek.com"`.
   - `ProviderApiOverrides.deepseek`, `ProviderApiTargets.deepseek`.
   - `resolve_api_overrides(..., deepseek_api_url=...)` reading the
     `DEEPSEEK_TARGET_API_URL` env var.
   - `resolve_api_targets(...)` normalizing it.
   - `ProxyProviderRuntime.deepseek_base_url` property (mirror of `openai_base_url`).

4. `headroom/proxy/handlers/openai.py` — the only proxy runtime change. In
   `_resolve_openai_upstream`, detect dsh traffic (present
   `x-deepseek-harness-user-id` header, or `model` starting `deepseek-`) and
   return the DeepSeek target instead of `self.OPENAI_API_URL`. Compression and
   output-shaping code is untouched — it already operates on OpenAI-compatible
   chat-completions bodies.

5. README agent-compatibility matrix + `wiki/` + `llms.txt` — add the `dsh` row.

## Data flow & upstream routing

### Happy path

```
dsh (web | headless)
  │  POST {baseURL}/chat/completions, baseURL = DEEPSEEK_BASE_URL
  │  Authorization: Bearer $DEEPSEEK_API_KEY
  │  x-deepseek-harness-user-id: …   (+ x-deepseek-harness-session-id when sessioned)
  ▼
headroom proxy — /v1/chat/completions (OPENAI_HANDLER_ROUTES → handle_openai_chat)
  │  compress tool outputs/logs/history · output shaping (verbosity/effort)
  ▼
https://api.deepseek.com  (or captured upstream)
  POST /v1/chat/completions, forwarded auth + x-deepseek-harness-* headers
```

### Wire-format detail

dsh appends `/chat/completions` directly to `baseURL` (no `/v1`). The wrap sets
`DEEPSEEK_BASE_URL = http://127.0.0.1:{port}/v1`, so dsh POSTs
`…/v1/chat/completions` — exactly the proxy's registered OpenAI route. No new
route is required. The handler's existing `/v1/chat/completions` upstream suffix
is accepted by DeepSeek's OpenAI-compatible API (verified in e2e, not assumed).

### Upstream detection

`_resolve_openai_upstream` currently returns the `x-headroom-base-url` header or
`self.OPENAI_API_URL`. It gains a DeepSeek branch detected by:

- primary: `x-deepseek-harness-user-id` header present (dsh sends it on every
  provider request after credential resolution), and
- fallback: `model` starts with `deepseek-` (already parsed in `handle_openai_chat`).

When DeepSeek is detected, return `self.DEEPSEEK_API_URL` (default
`https://api.deepseek.com`). Compression and output shaping are untouched.

### Upstream override & capture

The proxy's DeepSeek target resolves through the same `*_TARGET_API_URL` pattern
as the other providers: `deepseek_api_url` CLI arg / `DEEPSEEK_TARGET_API_URL`
env, defaulting to `https://api.deepseek.com`. The wrap captures the user's
pre-existing `DEEPSEEK_BASE_URL` (if set — dsh uses it for internal/custom
endpoints) and passes it to the proxy as the DeepSeek upstream override before
overwriting `DEEPSEEK_BASE_URL` to point at the proxy. If unset, the proxy routes
to the public API.

### Auth

`build_launch_env` passes `DEEPSEEK_API_KEY` through to dsh so dsh can resolve
it; the proxy forwards the `Authorization` header verbatim. No key injection or
storage on the proxy side.

## Wrap/unwrap, error handling, testing, docs

### Wrap/unwrap behavior

- `headroom wrap dsh [--profile web|headless] [--command CMD] [--port N]
  [--no-proxy] [--verbose] [--prepare-only] [dsh_args…]`. Default `web`;
  `--profile headless` runs `dsh --profile headless <task>`; `--command` covers
  `pnpm dsh` or a custom launcher.
- Binary resolution: `dsh` on `PATH` → use it; else `pnpm dsh` if available;
  else require `--command`. Fail loud if nothing resolves.
- Proxy start via the existing `_start_proxy` + readiness path, honoring
  `--no-proxy`.
- Launch env: `DEEPSEEK_BASE_URL=http://127.0.0.1:{port}/v1`, `DEEPSEEK_API_KEY`
  passed through.
- No durable config mutation in v1. `unwrap dsh` = stop the proxy (unless
  `--no-stop-proxy`).

### Error handling (fail loud, per dsh's own convention)

- Missing `dsh` binary → error with an install hint (`npm i -g
  @deepseek-ai/dsh`, or `pnpm`).
- Missing `DEEPSEEK_API_KEY` → warn (dsh surfaces `MISSING_CREDENTIAL`);
  headroom does not require it.
- **baseURL precedence guard**: dsh resolves `baseURL` as
  `config.baseURL ?? $DEEPSEEK_BASE_URL ?? public API`, where `config` is the
  merged settings/cordis.yml section (`llm-deepseek/src/index.ts:185-187`). A
  `baseURL` configured in dsh settings or cordis.yml therefore overrides the
  env var and would silently bypass the proxy. The wrap detects a conflicting
  effective `baseURL` (exact location pinned in the plan via dsh's
  `--dump-config` / settings file) and fails loud with remediation, rather than
  launching an uncompressed session. Documented as a v1 limitation; durable
  settings-patching is a possible follow-up.
- Proxy startup failure → existing error path.

### Testing

New unit tests, matching `tests/` conventions:

- `dsh/runtime.py` — `build_launch_env` (base URL + key passthrough),
  `resolve_dsh_command` (web vs headless vs `--command`), upstream capture.
- `registry.py` — `deepseek` target/override resolution (`DEEPSEEK_TARGET_API_URL`,
  default fallback).
- `handlers/openai.py` — DeepSeek detection matrix: `x-deepseek-harness-user-id`
  present → DeepSeek target; `deepseek-*` model prefix → DeepSeek target;
  neither → OpenAI target.

The full headroom pytest suite runs for no-regression; deepseek-harness's suites
are untouched (no dsh changes). Smoke test: wrap against a mock DeepSeek
endpoint and assert the proxy compresses and forwards to the correct upstream.

### Docs

- README agent-compatibility matrix: add `dsh` row (`web` + `headless`; routes
  via `DEEPSEEK_BASE_URL`).
- `wiki/` page + `llms.txt` entry. CHANGELOG is release-please-generated — no
  manual edit.

## Decisions

- **Routing approach**: first-class `deepseek` target + header detection
  (chosen over a dedicated `/deepseek` route — more surface for an identical
  wire format — and over reusing the OpenAI target via override — breaks the
  shared-proxy model).
- **baseURL precedence**: fail-loud guard for v1 (chosen over durable
  settings-patching).
- **Wire format**: `DEEPSEEK_BASE_URL=http://127.0.0.1:{port}/v1` so dsh hits the
  existing `/v1/chat/completions` route.

## Risks

- DeepSeek's public API accepting the `/v1/chat/completions` suffix is verified
  in e2e, not assumed.
- The exact location of dsh's effective settings/cordis.yml `baseURL` for the
  precedence guard is pinned during planning (via `dsh --dump-config` / the
  settings file), not guessed.
