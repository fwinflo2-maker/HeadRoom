"""ASGI request-scope mutation helpers."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from fastapi import Request


def normalize_scope_path(scope: MutableMapping[str, Any], path: str) -> None:
    """Set an ASGI scope path and keep ``raw_path`` aligned when present."""
    scope["path"] = path
    if "raw_path" in scope:
        scope["raw_path"] = quote(path).encode("ascii")


def normalize_request_path(request: Request, path: str) -> None:
    """Set a FastAPI request path and clear its cached URL, if any."""
    normalize_scope_path(request.scope, path)
    if hasattr(request, "_url"):
        delattr(request, "_url")


def set_scope_header(request: Request, name: str, value: str) -> None:
    """Replace every existing occurrence of ``name`` in the ASGI scope with one
    ``value``.

    Drops all pairs whose (lower-cased) name matches, then appends the single
    injected value. That matters for internal routing headers: Starlette's
    ``Headers.get`` returns the *first* duplicate, so a client could otherwise
    send its own ``x-headroom-base-url`` and shadow an internally injected one,
    steering the request to a client-controlled upstream. Sanitizing first makes
    the injected value authoritative.
    """
    lowered = name.lower().encode("latin-1")
    headers = request.scope["headers"]
    headers[:] = [(key, val) for (key, val) in headers if key.lower() != lowered]
    headers.append((lowered, value.encode("latin-1")))
    if hasattr(request, "_headers"):
        delattr(request, "_headers")
