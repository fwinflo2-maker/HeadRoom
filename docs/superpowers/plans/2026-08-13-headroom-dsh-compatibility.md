# headroom ↔ deepseek-harness (dsh) Compatibility — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `dsh` (DeepSeek Harness) to headroom's agent-compatibility matrix via `headroom wrap dsh` / `unwrap dsh`, routing dsh's DeepSeek chat-completions traffic through the compression proxy and upstream to DeepSeek.

**Architecture:** A new `headroom/providers/dsh/` package provides the launch env + command resolution. A first-class `deepseek` upstream target is threaded through the proxy config/registry (mirroring the existing `openai`/`vertex` targets). The OpenAI chat handler gains a DeepSeek detection branch (header `x-deepseek-harness-user-id` or `deepseek-*` model prefix) that selects the DeepSeek target; compression and output shaping are unchanged. The wrap CLI gains `wrap dsh` / `unwrap dsh`, reusing the existing `_launch_tool`/`_ensure_proxy`/`_start_proxy` machinery.

**Tech Stack:** Python 3.10+ (headroom package), FastAPI/httpx proxy, pytest, Click CLI. deepseek-harness (TypeScript) is NOT modified — it is only the integration target.

## Global Constraints

- All changes are in the `headroom` repo; do not modify `deepseek-harness`.
- Follow the existing `openai`/`vertex` upstream-target pattern exactly at every site (registry, models, server, wrap plumbing).
- Default DeepSeek upstream is `https://api.deepseek.com`; override via `DEEPSEEK_TARGET_API_URL` env or `--deepseek-api-url` flag.
- dsh appends `/chat/completions` to its `baseURL` (no `/v1`); the wrap sets `DEEPSEEK_BASE_URL=http://127.0.0.1:{port}/v1` so dsh hits the existing `/v1/chat/completions` route.
- Launch-env-only wrap: no durable dsh settings/cordis.yml mutation in v1. `unwrap dsh` only stops the proxy.
- Test runner: `uv run python -m pytest <path> -v` from the repo root (the `headroom` package must be importable; `uv sync` or `pip install -e .` as needed).
- New tests live in `tests/` (flat, `test_*.py`), using pytest `monkeypatch` and Click `CliRunner` conventions already in the repo.
- Conventional Commits for commit messages (the repo enforces commitlint).

---

## File Structure

- **Create** `headroom/providers/dsh/__init__.py` — re-exports runtime symbols.
- **Create** `headroom/providers/dsh/runtime.py` — `DEFAULT_API_URL`, `proxy_base_url`, `build_launch_env`, `resolve_dsh_command`.
- **Create** `headroom/providers/dsh/install.py` — `build_install_env`.
- **Create** `tests/test_dsh_runtime.py` — unit tests for the provider runtime.
- **Create** `tests/test_deepseek_routing.py` — unit tests for the proxy routing detection.
- **Create** `tests/test_cli/test_wrap_dsh.py` — CLI wrap/unwrap tests.
- **Modify** `headroom/providers/install_registry.py` — register the dsh env builder.
- **Modify** `headroom/providers/registry.py` — `deepseek` target/override surface.
- **Modify** `headroom/proxy/models.py` — `ProxyConfig.deepseek_api_url` + `provider_api_overrides`.
- **Modify** `headroom/proxy/server.py` — `DEEPSEEK_API_URL` class attr, env read, banner, config debug.
- **Modify** `headroom/proxy/handlers/openai.py` — DeepSeek detection in `_resolve_openai_upstream`.
- **Modify** `headroom/cli/wrap.py` — thread `deepseek_api_url`; add `wrap dsh` / `unwrap dsh`.
- **Modify** `README.md`, `wiki/`, `llms.txt` — documentation.

---

### Task 1: dsh provider runtime package

**Files:**
- Create: `headroom/providers/dsh/__init__.py`
- Create: `headroom/providers/dsh/runtime.py`
- Test: `tests/test_dsh_runtime.py`

**Interfaces:**
- Produces: `DEFAULT_API_URL: str`, `proxy_base_url(port: int) -> str`, `build_launch_env(port: int, environ: Mapping[str, str] | None = None) -> tuple[dict[str, str], list[str]]`, `resolve_dsh_command(*, profile: str = "web", command: str | None = None, task_args: tuple[str, ...] = ()) -> list[str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_dsh_runtime.py`:

```python
"""Tests for the DeepSeek Harness (dsh) provider runtime."""

from __future__ import annotations

import pytest

from headroom.providers.dsh.runtime import (
    DEFAULT_API_URL,
    build_launch_env,
    proxy_base_url,
    resolve_dsh_command,
)


def test_proxy_base_url_includes_v1() -> None:
    assert proxy_base_url(8787) == "http://127.0.0.1:8787/v1"


def test_default_api_url_is_deepseek_public() -> None:
    assert DEFAULT_API_URL == "https://api.deepseek.com"


def test_build_launch_env_sets_deepseek_base_url_and_passes_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    env, display = build_launch_env(9000)
    assert env["DEEPSEEK_BASE_URL"] == "http://127.0.0.1:9000/v1"
    assert env["DEEPSEEK_API_KEY"] == "sk-test"
    assert display == ["DEEPSEEK_BASE_URL=http://127.0.0.1:9000/v1"]


def test_resolve_dsh_command_web_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "headroom.providers.dsh.runtime.shutil.which", lambda _name: "/usr/bin/dsh"
    )
    assert resolve_dsh_command() == ["/usr/bin/dsh", "web"]


def test_resolve_dsh_command_headless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "headroom.providers.dsh.runtime.shutil.which", lambda _name: "/usr/bin/dsh"
    )
    assert resolve_dsh_command(profile="headless", task_args=("explain foo",)) == [
        "/usr/bin/dsh",
        "--profile",
        "headless",
        "explain foo",
    ]


def test_resolve_dsh_command_explicit_command_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "headroom.providers.dsh.runtime.shutil.which", lambda _name: "/usr/bin/dsh"
    )
    assert resolve_dsh_command(command="pnpm dsh") == ["pnpm", "dsh", "web"]


def test_resolve_dsh_command_missing_binary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "headroom.providers.dsh.runtime.shutil.which", lambda _name: None
    )
    with pytest.raises(RuntimeError, match="not found on PATH"):
        resolve_dsh_command()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_dsh_runtime.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'headroom.providers.dsh'`

- [ ] **Step 3: Write the implementation**

Create `headroom/providers/dsh/runtime.py`:

```python
"""Runtime helpers for DeepSeek Harness (dsh) integrations."""

from __future__ import annotations

import os
import shlex
import shutil
from collections.abc import Mapping

DEFAULT_API_URL = "https://api.deepseek.com"


def proxy_base_url(port: int) -> str:
    """Return the local proxy base URL used by OpenAI-compatible integrations."""
    return f"http://127.0.0.1:{port}/v1"


def build_launch_env(
    port: int, environ: Mapping[str, str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """Build environment variables for dsh through the local proxy.

    Sets ``DEEPSEEK_BASE_URL`` to the proxy and leaves ``DEEPSEEK_API_KEY``
    (and every other variable) as-is so dsh resolves its own key and the proxy
    forwards the bearer token verbatim.
    """
    env = dict(environ or os.environ)
    base_url = proxy_base_url(port)
    env["DEEPSEEK_BASE_URL"] = base_url
    return env, [f"DEEPSEEK_BASE_URL={base_url}"]


def resolve_dsh_command(
    *,
    profile: str = "web",
    command: str | None = None,
    task_args: tuple[str, ...] = (),
) -> list[str]:
    """Resolve the dsh launch argv.

    Precedence for the launcher: explicit ``command``, then ``dsh`` on ``PATH``,
    then ``pnpm dsh``. ``profile`` is ``"web"`` (default) or ``"headless"``.
    """
    if command:
        argv = shlex.split(command)
    else:
        dsh_bin = shutil.which("dsh")
        if dsh_bin:
            argv = [dsh_bin]
        elif shutil.which("pnpm"):
            argv = ["pnpm", "dsh"]
        else:
            raise RuntimeError(
                "dsh was not found on PATH. Install it with `npm i -g @deepseek-ai/dsh` "
                "or pass `--command`."
            )
    if profile == "headless":
        argv.extend(["--profile", "headless", *task_args])
    else:
        argv.extend(["web", *task_args])
    return argv
```

Create `headroom/providers/dsh/__init__.py`:

```python
"""DeepSeek Harness (dsh) provider helpers."""

from .runtime import DEFAULT_API_URL, build_launch_env, proxy_base_url, resolve_dsh_command

__all__ = [
    "DEFAULT_API_URL",
    "build_launch_env",
    "proxy_base_url",
    "resolve_dsh_command",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_dsh_runtime.py -v`
Expected: PASS (all 7 tests)

- [ ] **Step 5: Commit**

```bash
git add headroom/providers/dsh/ tests/test_dsh_runtime.py
git commit -m "feat(dsh): add dsh provider runtime package"
```

---

### Task 2: dsh install env builder + registry entry

**Files:**
- Create: `headroom/providers/dsh/install.py`
- Modify: `headroom/providers/install_registry.py`
- Test: `tests/test_dsh_runtime.py` (append)

**Interfaces:**
- Produces: `headroom.providers.dsh.install.build_install_env(*, port: int, backend: str) -> dict[str, str]`.
- Consumes: `proxy_base_url` from Task 1.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dsh_runtime.py`:

```python
from headroom.providers.dsh.install import build_install_env
from headroom.providers.install_registry import build_install_target_envs


def test_dsh_build_install_env_sets_deepseek_base_url() -> None:
    assert build_install_env(port=9000, backend="anthropic") == {
        "DEEPSEEK_BASE_URL": "http://127.0.0.1:9000/v1"
    }


def test_install_registry_includes_dsh() -> None:
    envs = build_install_target_envs(port=9000, backend="anthropic", targets=["dsh"])
    assert envs["dsh"] == {"DEEPSEEK_BASE_URL": "http://127.0.0.1:9000/v1"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_dsh_runtime.py -v`
Expected: FAIL (`ModuleNotFoundError` for `headroom.providers.dsh.install`, or `KeyError: 'dsh'` if the install module is added before the registry entry)

- [ ] **Step 3: Write the implementation**

Create `headroom/providers/dsh/install.py`:

```python
"""DeepSeek Harness (dsh) install-time helpers."""

from __future__ import annotations

from .runtime import proxy_base_url


def build_install_env(*, port: int, backend: str) -> dict[str, str]:
    """Build the persistent install environment for DeepSeek Harness."""
    del backend
    return {"DEEPSEEK_BASE_URL": proxy_base_url(port)}
```

In `headroom/providers/install_registry.py`, add the import alongside the other provider install imports (after the `opencode` import block), and add a `"dsh"` entry to `_ENV_BUILDERS`:

```python
from headroom.providers.dsh.install import build_install_env as _build_dsh_install_env
```

and inside `_ENV_BUILDERS` (the dict at the top of the file, keyed by provider name):

```python
    "dsh": _build_dsh_install_env,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_dsh_runtime.py -v`
Expected: PASS (all 9 tests)

- [ ] **Step 5: Commit**

```bash
git add headroom/providers/dsh/install.py headroom/providers/install_registry.py tests/test_dsh_runtime.py
git commit -m "feat(dsh): register dsh install env builder"
```

---

### Task 3: deepseek upstream target in registry

**Files:**
- Modify: `headroom/providers/registry.py`
- Test: `tests/test_registry_deepseek.py`

**Interfaces:**
- Produces: `ProviderApiOverrides.deepseek: str | None`, `ProviderApiTargets.deepseek: str`, `resolve_api_overrides(..., deepseek_api_url: str | None = None, ...)`, `resolve_api_targets(...)`, `ProxyProviderRuntime.deepseek_base_url: str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_registry_deepseek.py`:

```python
"""Tests for the deepseek upstream target in the provider registry."""

from __future__ import annotations

from headroom.providers.registry import (
    DEFAULT_DEEPSEEK_API_URL,
    ProviderApiOverrides,
    resolve_api_overrides,
    resolve_api_targets,
)


def test_resolve_api_overrides_deepseek_target_api_url() -> None:
    overrides = resolve_api_overrides(
        anthropic_api_url=None,
        openai_api_url=None,
        gemini_api_url=None,
        cloudcode_api_url=None,
        deepseek_api_url=None,
        environ={"DEEPSEEK_TARGET_API_URL": "https://deepseek.internal"},
    )
    assert overrides.deepseek == "https://deepseek.internal"


def test_resolve_api_overrides_deepseek_explicit_beats_env() -> None:
    overrides = resolve_api_overrides(
        anthropic_api_url=None,
        openai_api_url=None,
        gemini_api_url=None,
        cloudcode_api_url=None,
        deepseek_api_url="https://explicit.internal",
        environ={"DEEPSEEK_TARGET_API_URL": "https://env.internal"},
    )
    assert overrides.deepseek == "https://explicit.internal"


def test_resolve_api_targets_deepseek_default() -> None:
    targets = resolve_api_targets(ProviderApiOverrides(deepseek=None))
    assert targets.deepseek == DEFAULT_DEEPSEEK_API_URL
    assert DEFAULT_DEEPSEEK_API_URL == "https://api.deepseek.com"


def test_resolve_api_targets_deepseek_strips_v1() -> None:
    targets = resolve_api_targets(
        ProviderApiOverrides(deepseek="http://127.0.0.1:4000/v1")
    )
    assert targets.deepseek == "http://127.0.0.1:4000"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_registry_deepseek.py -v`
Expected: FAIL (missing `DEFAULT_DEEPSEEK_API_URL`, missing `deepseek` params/fields)

- [ ] **Step 3: Write the implementation**

In `headroom/providers/registry.py`, make these edits:

1. Add the import near the top (alongside the existing `DEFAULT_GEMINI_API_URL` import):

```python
from headroom.providers.dsh.runtime import DEFAULT_API_URL as DEFAULT_DEEPSEEK_API_URL
```

2. Add `deepseek` to `ProviderApiOverrides` (after `vertex`):

```python
    deepseek: str | None = None
```

3. Add `deepseek` to `ProviderApiTargets` (after `vertex`):

```python
    deepseek: str = DEFAULT_DEEPSEEK_API_URL
```

4. Add `deepseek_api_url` to `resolve_api_overrides` signature (after `vertex_api_url`) and to the returned `ProviderApiOverrides(...)`:

```python
def resolve_api_overrides(
    *,
    anthropic_api_url: str | None,
    openai_api_url: str | None,
    gemini_api_url: str | None,
    cloudcode_api_url: str | None,
    vertex_api_url: str | None = None,
    deepseek_api_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProviderApiOverrides:
    env = environ or os.environ
    return ProviderApiOverrides(
        anthropic=anthropic_api_url
        or env.get("ANTHROPIC_TARGET_API_URL")
        or env.get("ANTHROPIC_FOUNDRY_BASE_URL"),
        openai=openai_api_url or env.get("OPENAI_TARGET_API_URL"),
        gemini=gemini_api_url or env.get("GEMINI_TARGET_API_URL"),
        cloudcode=cloudcode_api_url or env.get("CLOUDCODE_TARGET_API_URL"),
        vertex=vertex_api_url or env.get("VERTEX_TARGET_API_URL"),
        deepseek=deepseek_api_url or env.get("DEEPSEEK_TARGET_API_URL"),
    )
```

5. Add `deepseek` to `resolve_api_targets`:

```python
        deepseek=_normalize_api_url(overrides.deepseek, default=DEFAULT_DEEPSEEK_API_URL),
```

6. Add a `deepseek_base_url` property to `ProxyProviderRuntime` (mirroring the existing `openai_base_url` property):

```python
    @property
    def deepseek_base_url(self) -> str:
        return self.api_targets.deepseek
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_registry_deepseek.py -v`
Expected: PASS (all 4 tests)

- [ ] **Step 5: Commit**

```bash
git add headroom/providers/registry.py tests/test_registry_deepseek.py
git commit -m "feat(proxy): add deepseek upstream target to provider registry"
```

---

### Task 4: ProxyConfig deepseek field + overrides property

**Files:**
- Modify: `headroom/proxy/models.py`
- Test: `tests/test_registry_deepseek.py` (append)

**Interfaces:**
- Produces: `ProxyConfig.deepseek_api_url: str | None`; `ProxyConfig.provider_api_overrides` now carries `deepseek`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_registry_deepseek.py`:

```python
from headroom.proxy.models import ProxyConfig


def test_proxy_config_exposes_deepseek_api_url_in_overrides() -> None:
    config = ProxyConfig(deepseek_api_url="https://deepseek.internal")
    assert config.provider_api_overrides.deepseek == "https://deepseek.internal"


def test_proxy_config_deepseek_defaults_to_none() -> None:
    config = ProxyConfig()
    assert config.deepseek_api_url is None
    assert config.provider_api_overrides.deepseek is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_registry_deepseek.py -v`
Expected: FAIL (`ProxyConfig.__init__() got an unexpected keyword argument 'deepseek_api_url'`)

- [ ] **Step 3: Write the implementation**

In `headroom/proxy/models.py`:

1. Add the field after `openai_api_url` (line ~140):

```python
    deepseek_api_url: str | None = None  # Custom DeepSeek API URL override
```

2. Add `deepseek` to the `provider_api_overrides` property (line ~527-536):

```python
        return ProviderApiOverrides(
            anthropic=self.anthropic_api_url,
            openai=self.openai_api_url,
            gemini=self.gemini_api_url,
            cloudcode=self.cloudcode_api_url,
            vertex=self.vertex_api_url,
            deepseek=self.deepseek_api_url,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_registry_deepseek.py -v`
Expected: PASS (all 6 tests)

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/models.py tests/test_registry_deepseek.py
git commit -m "feat(proxy): add deepseek_api_url to ProxyConfig"
```

---

### Task 5: proxy server exposes DEEPSEEK_API_URL

**Files:**
- Modify: `headroom/proxy/server.py`
- Test: `tests/test_backend_bugs.py` (append a DeepSeek normalization test)

**Interfaces:**
- Produces: `HeadroomProxy.DEEPSEEK_API_URL` class attribute, populated from `api_targets.deepseek`; proxy reads `DEEPSEEK_TARGET_API_URL` env; banner shows DeepSeek.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backend_bugs.py` (inside `TestOpenAIURLNormalization`, following the existing `/v1` normalization tests):

```python
    def test_deepseek_v1_suffix_stripped(self):
        from headroom.proxy.server import HeadroomProxy, ProxyConfig

        original = HeadroomProxy.DEEPSEEK_API_URL
        try:
            config = ProxyConfig(
                deepseek_api_url="http://localhost:4000/v1",
                optimize=False,
                cache_enabled=False,
                rate_limit_enabled=False,
            )
            proxy = HeadroomProxy(config)
            assert proxy.DEEPSEEK_API_URL == "http://localhost:4000"
        finally:
            HeadroomProxy.DEEPSEEK_API_URL = original
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_backend_bugs.py::TestOpenAIURLNormalization::test_deepseek_v1_suffix_stripped -v`
Expected: FAIL (`AttributeError: type object 'HeadroomProxy' has no attribute 'DEEPSEEK_API_URL'`)

- [ ] **Step 3: Write the implementation**

In `headroom/proxy/server.py`:

1. Add `DEFAULT_DEEPSEEK_API_URL` to the existing import block (near `DEFAULT_OPENAI_API_URL`, ~line 105):

```python
    DEFAULT_DEEPSEEK_API_URL,
```

2. Add the class attribute after `OPENAI_API_URL` (line ~765):

```python
    DEEPSEEK_API_URL = DEFAULT_DEEPSEEK_API_URL
```

3. Populate it from resolved targets (after line ~799 `HeadroomProxy.VERTEX_API_URL = api_targets.vertex`):

```python
        HeadroomProxy.DEEPSEEK_API_URL = api_targets.deepseek
```

4. Read the env var when building `ProxyConfig` (after line ~5130 `openai_api_url=os.environ.get("OPENAI_TARGET_API_URL"),`):

```python
        deepseek_api_url=os.environ.get("DEEPSEEK_TARGET_API_URL"),
```

5. Add a DeepSeek line to the banner (after the `Vertex AI:` line ~5288):

```python
║    DeepSeek:   {api_targets.deepseek:<57}║
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_backend_bugs.py::TestOpenAIURLNormalization -v`
Expected: PASS (all 4 tests, including the new DeepSeek one)

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/server.py tests/test_backend_bugs.py
git commit -m "feat(proxy): expose DEEPSEEK_API_URL on the proxy server"
```

---

### Task 6: DeepSeek detection in the OpenAI chat handler

**Files:**
- Modify: `headroom/proxy/handlers/openai.py`
- Test: `tests/test_deepseek_routing.py`

**Interfaces:**
- Produces: `_is_deepseek_request(request_headers: dict[str, str], model: str | None) -> bool`; `OpenAIHandlerMixin._resolve_openai_upstream(self, request, model: str | None = None) -> str` (now selects `self.DEEPSEEK_API_URL` for DeepSeek traffic).

- [ ] **Step 1: Write the failing test**

Create `tests/test_deepseek_routing.py`:

```python
"""Tests for DeepSeek traffic detection and upstream routing."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from headroom.proxy.handlers.openai import OpenAIHandlerMixin, _is_deepseek_request


def _headers(*pairs: tuple[str, str]) -> dict[str, str]:
    return dict(pairs)


def test_is_deepseek_request_header() -> None:
    assert _is_deepseek_request(
        _headers(("x-deepseek-harness-user-id", "anon")), model=None
    )


def test_is_deepseek_request_model_prefix() -> None:
    assert _is_deepseek_request(_headers(), model="deepseek-v4-flash")


def test_is_deepseek_request_neither() -> None:
    assert not _is_deepseek_request(_headers(), model="gpt-4o")


def test_is_deepseek_request_none_model() -> None:
    assert not _is_deepseek_request(_headers(), model=None)


def _mix() -> Any:
    mix = OpenAIHandlerMixin.__new__(OpenAIHandlerMixin)
    mix.OPENAI_API_URL = "https://api.openai.com"
    mix.DEEPSEEK_API_URL = "https://api.deepseek.com"
    return mix


def test_resolve_openai_upstream_deepseek_header() -> None:
    mix = _mix()
    req = SimpleNamespace(headers={"x-deepseek-harness-user-id": "anon"})
    assert mix._resolve_openai_upstream(req, model=None) == "https://api.deepseek.com"


def test_resolve_openai_upstream_deepseek_model() -> None:
    mix = _mix()
    req = SimpleNamespace(headers={})
    assert (
        mix._resolve_openai_upstream(req, model="deepseek-v4-pro")
        == "https://api.deepseek.com"
    )


def test_resolve_openai_upstream_openai_default() -> None:
    mix = _mix()
    req = SimpleNamespace(headers={})
    assert (
        mix._resolve_openai_upstream(req, model="gpt-4o") == "https://api.openai.com"
    )


def test_resolve_openai_upstream_custom_base_url_wins_when_not_deepseek() -> None:
    mix = _mix()
    req = SimpleNamespace(headers={"x-headroom-base-url": "https://gateway.example/v1"})
    assert mix._resolve_openai_upstream(req, model="gpt-4o") == "https://gateway.example"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_deepseek_routing.py -v`
Expected: FAIL (`ImportError: cannot import name '_is_deepseek_request'`)

- [ ] **Step 3: Write the implementation**

In `headroom/proxy/handlers/openai.py`:

1. Add the helper after `_resolve_openai_upstream_base` (after line ~379):

```python
def _is_deepseek_request(request_headers: dict[str, str], model: str | None) -> bool:
    """Return True when a chat-completions request belongs to DeepSeek Harness.

    dsh sends ``x-deepseek-harness-user-id`` on every provider request after
    credential resolution; the ``deepseek-*`` model prefix is a fallback for
    non-harness clients targeting DeepSeek models.
    """
    if _header_get(request_headers, "x-deepseek-harness-user-id") is not None:
        return True
    return isinstance(model, str) and model.startswith("deepseek-")
```

2. Change `_resolve_openai_upstream` (line ~1710):

```python
    def _resolve_openai_upstream(self, request: Request, model: str | None = None) -> str:
        """Return the upstream base URL for ``request``.

        Honors the ``x-headroom-base-url`` request header so OpenAI-compatible
        gateways route through the dedicated handlers, and routes DeepSeek
        Harness traffic (``x-deepseek-harness-user-id`` header, or a
        ``deepseek-*`` model) to the configured DeepSeek upstream. Falls back to
        the configured ``OPENAI_API_URL``.
        """
        if _is_deepseek_request(request.headers, model):
            return self.DEEPSEEK_API_URL
        return _resolve_openai_upstream_base(request.headers) or self.OPENAI_API_URL
```

3. Update the caller in `handle_openai_chat` (line ~2973) AND the URL-build site (line ~4429):

```python
        upstream_base_url = self._resolve_openai_upstream(request, model=model)  # line ~2973
        ...
        url = build_copilot_upstream_url(
            self._resolve_openai_upstream(request, model=model),  # line ~4429 (the actual routing)
            handler_path,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_deepseek_routing.py -v`
Expected: PASS (all 9 tests)

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/handlers/openai.py tests/test_deepseek_routing.py
git commit -m "feat(proxy): route dsh DeepSeek traffic to the deepseek upstream"
```

---

### Task 7: wrap/unwrap dsh CLI + deepseek_api_url plumbing

**Files:**
- Modify: `headroom/cli/wrap.py`
- Test: `tests/test_cli/test_wrap_dsh.py`

**Interfaces:**
- Produces: `wrap dsh` / `unwrap dsh` Click commands. Threads `deepseek_api_url` through `_start_proxy`, `_ensure_proxy_unlocked`, and `_launch_tool` (mirroring `openai_api_url`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli/test_wrap_dsh.py`:

```python
"""Tests for the `headroom wrap dsh` command."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from headroom.cli.main import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _capture(captured: dict[str, object]):
    def fake_launch_tool(**kwargs: object) -> None:
        captured.update(kwargs)

    return fake_launch_tool


def test_wrap_dsh_launches_web_with_proxy_env(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr("headroom.cli.wrap._launch_tool", _capture(captured))
    monkeypatch.setattr(
        "headroom.providers.dsh.runtime.shutil.which",
        lambda _name: "/usr/bin/dsh",
    )

    result = runner.invoke(main, ["wrap", "dsh", "--port", "9000"])
    assert result.exit_code == 0, result.output

    env = captured["env"]
    assert env["DEEPSEEK_BASE_URL"] == "http://127.0.0.1:9000/v1"
    assert captured["binary"] == "/usr/bin/dsh"
    assert captured["args"] == ("web",)
    assert captured["agent_type"] == "dsh"


def test_wrap_dsh_headless_profile(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr("headroom.cli.wrap._launch_tool", _capture(captured))
    monkeypatch.setattr(
        "headroom.providers.dsh.runtime.shutil.which",
        lambda _name: "/usr/bin/dsh",
    )

    result = runner.invoke(
        main, ["wrap", "dsh", "--profile", "headless", "explain foo"]
    )
    assert result.exit_code == 0, result.output
    assert captured["args"] == ("--profile", "headless", "explain foo")


def test_wrap_dsh_forwards_deepseek_api_url(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr("headroom.cli.wrap._launch_tool", _capture(captured))
    monkeypatch.setattr(
        "headroom.providers.dsh.runtime.shutil.which",
        lambda _name: "/usr/bin/dsh",
    )

    result = runner.invoke(
        main, ["wrap", "dsh", "--deepseek-api-url", "https://deepseek.internal"]
    )
    assert result.exit_code == 0, result.output
    assert captured["deepseek_api_url"] == "https://deepseek.internal"


def test_wrap_dsh_missing_binary_fails(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "headroom.providers.dsh.runtime.shutil.which", lambda _name: None
    )

    result = runner.invoke(main, ["wrap", "dsh"])
    assert result.exit_code == 1
    assert "not found in PATH" in result.output


def test_unwrap_dsh_stops_proxy(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "headroom.cli.wrap._stop_local_proxy_for_unwrap",
        lambda _port: "stopped",
    )
    monkeypatch.setattr(
        "headroom.cli.wrap._echo_unwrap_proxy_stop_status",
        lambda _status, _port: None,
    )

    result = runner.invoke(main, ["unwrap", "dsh"])
    assert result.exit_code == 0, result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_cli/test_wrap_dsh.py -v`
Expected: FAIL (Click error `No such command 'dsh'` — the wrap command does not exist yet)

- [ ] **Step 3: Write the implementation**

In `headroom/cli/wrap.py`, make these edits:

1. Add `deepseek_api_url` to `_start_proxy` (signature ~line 600, after `openai_api_url`):

```python
    deepseek_api_url: str | None = None,
```

and in the `_start_proxy` body, after the `openai_api_url` flag block (`if openai_api_url: cmd.extend(["--openai-api-url", openai_api_url])`):

```python
    if deepseek_api_url:
        cmd.extend(["--deepseek-api-url", deepseek_api_url])
```

and after the `openai_api_url` env block (`if openai_api_url: proxy_env["OPENAI_TARGET_API_URL"] = openai_api_url`):

```python
    if deepseek_api_url:
        proxy_env["DEEPSEEK_TARGET_API_URL"] = deepseek_api_url
```

2. Add `deepseek_api_url` to `_ensure_proxy_unlocked` (signature ~line 3587, after `openai_api_url`):

```python
    deepseek_api_url: str | None = None,
```

and pass it through to `_start_proxy` (the call at ~line 3929, after `openai_api_url=openai_api_url,`):

```python
                    deepseek_api_url=deepseek_api_url,
```

Add the mismatch check mirroring `openai_api_url` (add after each of the three `if openai_api_url:` running-config checks, i.e. after the `missing.append("openai-api-url")` blocks at ~3678, ~3742, ~3837):

```python
                    if deepseek_api_url:
                        if _normalize_proxy_api_url(running_config.get("deepseek_api_url")) != _normalize_proxy_api_url(deepseek_api_url):
                            missing.append("deepseek-api-url")
```

3. Add `deepseek_api_url` to `_launch_tool` (signature ~line 4235, after `openai_api_url`):

```python
    deepseek_api_url: str | None = None,
```

and thread it to `_ensure_proxy` (the call at ~line 4270, after `openai_api_url=openai_api_url,`):

```python
            deepseek_api_url=deepseek_api_url,
```

4. Add the wrap command (mirroring the `kimi` command; place it after the `opencode` wrap command):

```python
# =============================================================================
# DeepSeek Harness (dsh)
# =============================================================================


@wrap.command(context_settings={"ignore_unknown_options": True})
@_retired_context_tool_option
@click.option(
    "--port", "-p", default=8787, type=click.IntRange(1, 65535), help="Proxy port (default: 8787)"
)
@click.option("--no-proxy", is_flag=True, help="Skip proxy startup (use existing proxy)")
@click.option("--learn", is_flag=True, help="Enable live traffic learning")
@click.option("--memory", is_flag=True, help="Enable persistent cross-session memory")
@click.option(
    "--profile",
    "profile",
    default="web",
    type=click.Choice(["web", "headless"]),
    help="dsh launch profile (default: web)",
)
@click.option("--command", "command", default=None, help="Explicit dsh command/launcher override")
@click.option(
    "--deepseek-api-url",
    default=None,
    help="DeepSeek upstream API URL (default: https://api.deepseek.com)",
)
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
@click.option("--prepare-only", is_flag=True, hidden=True)
@click.argument("dsh_args", nargs=-1, type=click.UNPROCESSED)
def dsh(
    port: int,
    no_proxy: bool,
    learn: bool,
    memory: bool,
    profile: str,
    command: str,
    deepseek_api_url: str,
    verbose: bool,
    prepare_only: bool,
    dsh_args: tuple,
) -> None:
    """Launch DeepSeek Harness (dsh) through Headroom proxy.

    \b
    Sets DEEPSEEK_BASE_URL to route dsh's OpenAI-compatible /chat/completions
    traffic through Headroom. The DeepSeek bearer (DEEPSEEK_API_KEY) is
    forwarded upstream, so no extra login is required.

    \b
    Examples:
        headroom wrap dsh                              # Start proxy + dsh web
        headroom wrap dsh --profile headless "task"    # One-shot task
        headroom wrap dsh --command "pnpm dsh"         # Custom launcher
        headroom wrap dsh --deepseek-api-url https://api.deepseek.com
    """
    if prepare_only:
        return

    try:
        argv = resolve_dsh_command(profile=profile, command=command, task_args=dsh_args)
    except RuntimeError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    env, env_vars_display = build_launch_env(port, os.environ)

    _launch_tool(
        binary=argv[0],
        args=tuple(argv[1:]),
        env=env,
        port=port,
        no_proxy=no_proxy,
        tool_label="DSH",
        env_vars_display=env_vars_display,
        learn=learn,
        memory=memory,
        agent_type="dsh",
        deepseek_api_url=deepseek_api_url,
    )
```

5. Add the unwrap command (mirroring `unwrap_omp`; place near the other unwrap commands):

```python
@unwrap.command("dsh")
@click.option(
    "--port", "-p", default=8787, type=click.IntRange(1, 65535), help="Proxy port (default: 8787)"
)
@click.option("--no-stop-proxy", is_flag=True, help="Do not stop the local Headroom proxy")
def unwrap_dsh(port: int, no_stop_proxy: bool) -> None:
    """Undo ``headroom wrap dsh`` (stop the local proxy)."""
    if not no_stop_proxy:
        _echo_unwrap_proxy_stop_status(_stop_local_proxy_for_unwrap(port), port)
```


Also add the `resolve_dsh_command` and `build_launch_env` imports near the top of `wrap.py` (alongside the other provider runtime imports):

```python
from headroom.providers.dsh.runtime import build_launch_env, resolve_dsh_command
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_cli/test_wrap_dsh.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
git add headroom/cli/wrap.py tests/test_cli/test_wrap_dsh.py
git commit -m "feat(dsh): add wrap/unwrap dsh CLI commands"
```

---

### Task 8: documentation

**Files:**
- Modify: `README.md` (agent-compatibility matrix)
- Modify: `llms.txt`
- Modify: `wiki/` (add a dsh page or extend the proxy/agent docs)

- [ ] **Step 1: Add the dsh row to the README compatibility matrix**

In `README.md`, the agent table (after the ZCode row), add:

```markdown
| DeepSeek Harness (dsh) | ✅ | `web` + `headless`; routes via `DEEPSEEK_BASE_URL` |
```

- [ ] **Step 2: Add dsh to llms.txt and wiki**

Add a `dsh` entry to `llms.txt` pointing at the new wiki page, and create/update a `wiki/` page documenting `headroom wrap dsh` usage (mirroring the structure of an existing agent page, e.g. the proxy or opencode page). Keep it to the wrap/unwrap usage, the `DEEPSEEK_BASE_URL` mechanism, the `--deepseek-api-url` override, and the baseURL-precedence caveat (a dsh settings/cordis.yml `baseURL` overrides `$DEEPSEEK_BASE_URL` and would bypass the proxy).

- [ ] **Step 3: Commit**

```bash
git add README.md llms.txt wiki/
git commit -m "docs(dsh): document dsh wrap in compatibility matrix and wiki"
```

---

### Task 9: full-suite regression + smoke test

**Files:** none (verification only)

- [ ] **Step 1: Run the full Python test suite**

Run: `uv run python -m pytest tests/ -q`
Expected: PASS, with no regressions from the new `deepseek` target threading.

- [ ] **Step 2: Smoke test against a mock DeepSeek upstream**

Start a mock OpenAI-compatible SSE server on a port (e.g. reuse the pattern from `tests/test_codex_live.py`, or a minimal `httpx`/`fastapi` app that echoes a chat-completion chunk). Then start the proxy pointed at it:

```bash
DEEPSEEK_TARGET_API_URL=http://127.0.0.1:<mock-port> python -m headroom.proxy proxy --port 8787
```

Send a DeepSeek chat-completion through the proxy and assert compression + upstream routing:

```bash
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer sk-test" \
  -H "x-deepseek-harness-user-id: anon" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"repeat a long JSON tool output"}]}'
```

Expected: the mock upstream receives the request (confirming DeepSeek routing), and the proxy applies compression to large tool-output content in the messages.

- [ ] **Step 3: Verify `headroom wrap dsh --prepare-only` exits cleanly**

Run: `headroom wrap dsh --prepare-only`
Expected: exit 0, no proxy started (prepare-only is a no-op).

- [ ] **Step 4: Commit any test fixtures added during smoke verification**

If a reusable smoke fixture was added under `tests/`, commit it:

```bash
git add tests/
git commit -m "test(dsh): add dsh proxy smoke fixture"
```

---

## Self-Review Notes

- **Spec coverage:** wrap/unwrap (Task 7), provider runtime + command resolution (Task 1), install env (Task 2), registry target (Task 3), config field (Task 4), server class attr/env/banner (Task 5), routing detection (Task 6), docs (Task 8), regression + smoke (Task 9). Output shaping rides the existing pipeline (verified in Task 9 smoke). No spec section is unaddressed.
- **Placeholder scan:** no placeholders remain. The unwrap command reuses the real `_stop_local_proxy_for_unwrap` / `_echo_unwrap_proxy_stop_status` helpers (verified against `unwrap_zcode`, which is the no-durable-config template). All code is fully specified.
- **Type consistency:** `deepseek_api_url`, `DEEPSEEK_API_URL`, `DEEPSEEK_TARGET_API_URL`, `deepseek_base_url`, `DEFAULT_DEEPSEEK_API_URL` are used consistently across Tasks 3–7 and match the design spec. `resolve_dsh_command` / `build_launch_env` signatures are defined in Task 1 and consumed identically in Task 7.
