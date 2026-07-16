"""Runtime helpers for Factory Droid integration.

``headroom wrap droid`` routes Droid through Headroom by pointing Droid's
Factory gateway at the local proxy via the ``FACTORY_API_BASE_URL`` environment
variable. The proxy compresses the Anthropic-shaped ``/api/llm/a/v1/messages``
inference route and forwards every other Factory REST path verbatim to the real
upstream resolved here, so all Droid models (including Droid Core) are
compressed on the user's Factory subscription with no ``customModels`` edits.
"""

from __future__ import annotations

import os

DEFAULT_FACTORY_API_URL = "https://api.factory.ai"


def proxy_base_url(port: int) -> str:
    """Return the local Headroom base URL Droid targets via FACTORY_API_BASE_URL."""
    return f"http://127.0.0.1:{port}"


def resolve_factory_upstream(explicit: str | None = None) -> str:
    """Resolve the real Factory upstream the proxy forwards to.

    Precedence: an explicit ``--factory-api-url``, then the caller's existing
    ``FACTORY_API_BASE_URL`` (an enterprise or EU gateway they already use),
    then the public default.
    """
    candidate = explicit or os.environ.get("FACTORY_API_BASE_URL") or DEFAULT_FACTORY_API_URL
    return candidate.rstrip("/")
