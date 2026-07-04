"""Runtime helpers for OpenCode integrations."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from headroom.mcp_registry.install import DEFAULT_PROXY_URL

from .config import HEADROOM_OPENCODE_PLUGIN, headroom_provider_entry


def proxy_base_url(port: int) -> str:
    """Return the local proxy base URL used by OpenCode integrations."""
    return f"http://127.0.0.1:{port}/v1"


def headroom_opencode_plugin_path() -> str | None:
    """Return the absolute path to the built OpenCode transport plugin, or None.

    OpenCode loads a plugin from an absolute file path (verified against
    opencode 1.17). The plugin's loader entry exports ONLY the plugin function
    (``plugins/opencode/dist/entry.opencode.js``) — the library barrel cannot
    be loaded directly ("Plugin export is not a function"). Returns ``None``
    when the plugin has not been built (e.g. a pip-only install that does not
    ship ``plugins/``), in which case wrap falls back to the native-provider
    baseURL override, which already covers Anthropic/OpenAI.

    ``HEADROOM_OPENCODE_PLUGIN_PATH`` overrides the resolved path.
    """
    override = os.environ.get("HEADROOM_OPENCODE_PLUGIN_PATH", "").strip()
    if override:
        return override if Path(override).is_file() else None
    # runtime.py → opencode → providers → headroom → <repo root>
    candidate = (
        Path(__file__).resolve().parents[3] / "plugins" / "opencode" / "dist" / "entry.opencode.js"
    )
    return str(candidate) if candidate.is_file() else None


def build_opencode_config_content(
    *,
    port: int,
    include_mcp: bool = True,
    include_plugin: bool = True,
) -> dict[str, object]:
    """Build JSON payload for ``OPENCODE_CONFIG_CONTENT``.

    Two complementary routing layers (both verified against opencode 1.17):

    1. **Native-provider baseURL override** — points OpenCode's built-in
       ``anthropic`` / ``openai`` providers at the proxy. Keeps native provider
       identity (model metadata, output-token limits) and reuses the user's own
       API keys (env / ``opencode auth``); the proxy forwards upstream by path
       (``/v1/messages`` → Anthropic, ``/v1/chat/completions`` → OpenAI). This
       is the reliable always-on layer and the only one shipped pip-only
       installs need.

    2. **Transparent transport plugin** — when the local plugin is built, it is
       loaded by absolute path and patches ``fetch``/``http`` to reroute *every*
       provider's traffic through the proxy, tagging the real upstream via
       ``x-headroom-base-url``. This covers providers we don't name (Gemini,
       Copilot, custom gateways) and providers added mid-session. The plugin
       self-configures from ``HEADROOM_PROXY_URL`` (set in :func:`build_launch_env`).
       Loopback URLs are not double-routed, so it coexists with layer 1.

    ponytail: config-level ``options.baseURL`` is reliable where the env-var
    override (``ANTHROPIC_BASE_URL``) is not — verified against opencode 1.17.
    """
    base_url = proxy_base_url(port)
    config: dict[str, object] = {
        "provider": {
            "anthropic": {"options": {"baseURL": base_url}},
            "openai": {"options": {"baseURL": base_url}},
            "headroom": headroom_provider_entry(port),
        }
    }
    if include_mcp:
        proxy_url = f"http://127.0.0.1:{port}"
        mcp_entry: dict[str, object] = {
            "type": "local",
            "command": ["headroom", "mcp", "serve"],
            "enabled": True,
        }
        if proxy_url != DEFAULT_PROXY_URL:
            mcp_entry["environment"] = {"HEADROOM_PROXY_URL": proxy_url}
        config["mcp"] = {
            "headroom": mcp_entry,
        }
    if include_plugin:
        plugin_path = headroom_opencode_plugin_path()
        if plugin_path:
            # Plain absolute-path string; the plugin reads HEADROOM_PROXY_URL
            # from the launch env (build_launch_env sets it).
            config["plugin"] = [plugin_path]
    return config


def build_launch_env(
    port: int,
    environ: Mapping[str, str] | None = None,
    project: str | None = None,
    *,
    include_mcp: bool = True,
    include_plugin: bool = True,
) -> tuple[dict[str, str], list[str]]:
    """Build environment variables for launching OpenCode through Headroom.

    ``OPENCODE_CONFIG_CONTENT`` carries Headroom provider/MCP/plugin config.
    Existing provider/base URL environment variables are preserved. When the
    transport plugin is loaded, ``HEADROOM_PROXY_URL`` tells it which proxy to
    route to.
    """
    env = dict(environ or os.environ)

    config_content = build_opencode_config_content(
        port=port,
        include_mcp=include_mcp,
        include_plugin=include_plugin,
    )
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config_content, separators=(",", ":"))

    display = ["OPENCODE_CONFIG_CONTENT={provider: headroom}"]
    if "plugin" in config_content:
        env["HEADROOM_PROXY_URL"] = f"http://127.0.0.1:{port}"
        display.append(f"plugin={HEADROOM_OPENCODE_PLUGIN}")

    if project and "HEADROOM_PROJECT" not in env:
        env["HEADROOM_PROJECT"] = project

    return env, display


_OPENCODE_VERSION_TIMEOUT = 2

# Go CLI builds (e.g. opencode v1.16.2) may print a bare SemVer ("v1.16.2")
# or a cobra-style line ("opencode version v1.16.2"). The Node SDK (oclif)
# surfaces Node/npm metadata ("node-v20.12.2", "@opencode-ai/cli"). We search
# for a SemVer token anywhere in the first line, but only after excluding
# Node/npm markers — the exclusion-first ordering prevents false-positives
# when a Node-SDK output also contains a SemVer (e.g.
# "@opencode-ai/cli/1.16.2 ... node-v20.12.2" has both, but the "node" marker
# routes it to "node-sdk" before the SemVer check runs).
_RE_VERSION_TOKEN = re.compile(
    r"v?\d+\.\d+\.\d+(?:[-+.]?[A-Za-z0-9-]+)*",
    re.IGNORECASE,
)


def _probe_version_output(binary: str) -> str:
    """Run ``binary --version`` and combine stdout+stderr, or return empty string on any failure."""
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=_OPENCODE_VERSION_TIMEOUT,
            check=False,
        )
    except Exception:  # defensive: TimeoutExpired, FileNotFoundError, PermissionError
        return ""
    return f"{result.stdout or ''}{result.stderr or ''}".strip()


def _classify_version_output(output: str) -> str:
    """Classify a ``--version`` probe result as ``go-cli``, ``node-sdk`` or ``unknown``."""
    if not output:
        return "unknown"

    output_lower = output.lower()
    node_markers = ("node", "npm", "npx", "@opencode-ai", "package.json")
    if any(marker in output_lower for marker in node_markers):
        return "node-sdk"

    first_line = output.splitlines()[0].strip()
    if _RE_VERSION_TOKEN.search(first_line):
        return "go-cli"

    return "unknown"


def detect_opencode_kind(binary: str | None) -> str:
    """Detect whether ``binary`` is the OpenCode Go CLI or the Node SDK.

    Returns one of:

    * ``"go-cli"`` — the binary looks like the Go CLI (simple SemVer output,
      no Node/npm markers).
    * ``"node-sdk"`` — the binary output contains Node/npm markers.
    * ``"unknown"`` — the binary could not be probed or the output did not
      match either signature.
    * ``"missing"`` — ``binary`` is empty/None.

    This function never raises; errors during probing are folded into
    ``"unknown"``.
    """
    if not binary:
        return "missing"
    output = _probe_version_output(binary)
    return _classify_version_output(output)
