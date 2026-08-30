"""Pure key policy for proxy semantic response cache."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _is_cache_annotation(value: Any) -> bool:
    """Return whether ``value`` is an Anthropic prompt-cache annotation."""
    return (
        isinstance(value, dict)
        and value.get("type") == "ephemeral"
        and set(value) <= {"type", "ttl"}
    )


def strip_cache_control(obj: Any) -> Any:
    """Recursively drop prompt-cache annotations before hashing.

    ``cache_control`` is also a valid user-defined JSON Schema property name.
    Only remove values with Anthropic's annotation shape; stripping every key
    with that spelling collapses semantically different tool contracts.
    """
    if isinstance(obj, dict):
        return {
            k: strip_cache_control(v)
            for k, v in obj.items()
            if k != "cache_control" or not _is_cache_annotation(v)
        }
    if isinstance(obj, list):
        return [strip_cache_control(item) for item in obj]
    return obj


def compute_semantic_cache_key(
    messages: list[dict],
    model: str,
    **key_fields: Any,
) -> str:
    """Compute a stable cache key from request content and shaping fields.

    ``cache_control`` is stripped from ``messages`` as well as the shaping
    fields: it is a prompt-caching directive for the upstream provider that
    never changes the generated completion, so a moved breakpoint must not
    fragment the key. Messages are the primary key component and, on the
    Anthropic path, the most common place a client (e.g. Claude Code) moves a
    breakpoint between turns, so leaving them un-stripped defeated the strip for
    the field that matters most.
    """
    normalized = json.dumps(
        {
            "model": model,
            "messages": strip_cache_control(messages),
            **{k: strip_cache_control(v) for k, v in key_fields.items()},
        },
        sort_keys=True,
    )
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]
