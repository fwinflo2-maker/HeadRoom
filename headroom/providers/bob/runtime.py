"""Runtime helpers for IBM Bob CLI integrations."""

from __future__ import annotations

import os
from collections.abc import Mapping

from headroom.proxy.project_context import with_project_prefix

# IBM Bob's default gateway. Bob appends its own ``/inference/v1`` prefix to
# whatever gateway URL it is given, so this is a bare origin -- unlike most
# providers here, where the configured value already ends in ``/v1``.
DEFAULT_GATEWAY_URL = "https://api.us-east.bob.ibm.com"

# The upstream target handed to the proxy. ``_normalize_api_url`` strips the
# trailing ``/v1``, and ``handle_openai_chat`` re-appends ``/v1/chat/completions``,
# so the request lands on ``…/inference/v1/chat/completions`` -- the path Bob
# itself calls.
DEFAULT_API_URL = f"{DEFAULT_GATEWAY_URL}/inference/v1"

# Bob resolves its gateway as ``config.gatewayUrl ?? BOB_GATEWAY_URL ?? default``,
# so setting this env var reroutes inference without touching ~/.bob/settings.
PROXY_ENV_KEY = "BOB_GATEWAY_URL"

# Bob posts OpenAI-shaped chat completions under its own ``/inference/v1``
# prefix rather than the conventional ``/v1``. The proxy routes this path to
# ``handle_openai_chat`` so the traffic is compressed instead of falling
# through to the uncompressed catch-all passthrough.
GATEWAY_CHAT_COMPLETIONS_PATH = "/inference/v1/chat/completions"


def proxy_base_url(port: int) -> str:
    """Return the local proxy gateway URL used by IBM Bob integrations.

    No ``/v1`` suffix: Bob builds ``{gateway}/inference/v1/chat/completions``
    itself, so handing it a ``/v1`` base would produce a doubled prefix.
    """
    return f"http://127.0.0.1:{port}"


def build_launch_env(
    port: int,
    environ: Mapping[str, str] | None = None,
    project: str | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Build environment variables for IBM Bob CLI through the local proxy.

    Bob routes inference traffic through ``BOB_GATEWAY_URL`` when set. The
    proxy forwards OpenAI-compatible chat requests upstream to IBM while Bob
    keeps its own ``Authorization: apikey …`` credential and its non-inference
    routes (``/admin/v1/profile``, ``/metrics-forwarder/…``) pass through
    untouched.

    ``project`` (the wrap launch directory) is encoded as a ``/p/<name>``
    base-URL prefix because Bob sends no custom attribution headers; the proxy
    strips it and attributes savings per project.
    """
    env = dict(environ or os.environ)
    gateway_url = with_project_prefix(proxy_base_url(port), project)
    env[PROXY_ENV_KEY] = gateway_url
    return env, [f"{PROXY_ENV_KEY}={gateway_url}"]
