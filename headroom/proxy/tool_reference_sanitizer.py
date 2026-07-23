"""Stale tool-search reference rescue.

Anthropic's server-side tool search records ``tool_reference`` entries inside
``tool_search_tool_result`` blocks in the assistant turn, and the client
replays them verbatim on every subsequent request. The API then validates each
replayed reference against the *current* request's tools array and rejects the
whole request with ``400 Tool reference '<name>' not found in available
tools`` when one is missing. Once that happens the session is permanently
bricked: the stale block is replayed forever.

A reference can go stale through no fault of the client: a plugin-provided
tool flapping out of the tools array mid-session, or headroom itself
withdrawing a proxy-injected tool (e.g. the CCR ``headroom_retrieve`` tool)
on a later turn. Dropping a reference whose tool is absent is exactly the
API's own validation predicate, so the rewrite can only ever convert a
guaranteed 400 into a working request — a reference that resolves is never
touched.
"""

from __future__ import annotations

from typing import Any


def _block_references(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract ``tool_reference`` entries from a ``tool_search_tool_result`` block.

    The GA shape nests them at ``content.tool_references`` (content is a
    dict); a list-shaped ``content`` holding reference blocks directly is
    handled defensively.
    """
    content = block.get("content")
    if isinstance(content, dict):
        refs = content.get("tool_references")
    elif isinstance(content, list):
        refs = content
    else:
        refs = None
    if not isinstance(refs, list):
        return []
    return [r for r in refs if isinstance(r, dict) and r.get("type") == "tool_reference"]


def collect_tool_reference_names(messages: list[dict[str, Any]] | None) -> set[str]:
    """Tool names referenced by replayed ``tool_search_tool_result`` blocks."""
    names: set[str] = set()
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_search_tool_result":
                for ref in _block_references(block):
                    name = ref.get("tool_name")
                    if isinstance(name, str):
                        names.add(name)
    return names


def _keep_resolvable(refs: list[Any], available: set[str], dropped: list[str]) -> list[Any]:
    """Filter out ``tool_reference`` entries whose tool is absent from ``available``.

    Anything that is not a well-formed tool_reference is kept untouched — the
    sanitizer only ever removes entries the API would reject by name.
    """
    kept: list[Any] = []
    for ref in refs:
        name = ref.get("tool_name") if isinstance(ref, dict) else None
        if (
            isinstance(ref, dict)
            and ref.get("type") == "tool_reference"
            and isinstance(name, str)
            and name not in available
        ):
            dropped.append(name)
        else:
            kept.append(ref)
    return kept


def _sanitize_block(
    block: dict[str, Any], available: set[str], dropped: list[str]
) -> dict[str, Any]:
    """Return ``block`` with stale references removed, or ``block`` itself unchanged."""
    content = block.get("content")
    if isinstance(content, dict):
        refs = content.get("tool_references")
        if not isinstance(refs, list):
            return block
        kept = _keep_resolvable(refs, available, dropped)
        if len(kept) == len(refs):
            return block
        return {**block, "content": {**content, "tool_references": kept}}
    if isinstance(content, list):
        kept = _keep_resolvable(content, available, dropped)
        if len(kept) == len(content):
            return block
        return {**block, "content": kept}
    return block


def sanitize_stale_tool_references(
    messages: list[dict[str, Any]],
    available_tool_names: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop replayed tool references to tools absent from ``available_tool_names``.

    Copy-on-write: returns ``(messages, [])`` with the *same* list object when
    nothing is stale, so callers can cheaply detect a no-op and byte-faithful
    forwarding stays untouched. When references are dropped, only the affected
    messages/blocks are copied; untouched siblings keep their identity.
    """
    dropped: list[str] = []
    out = messages
    for mi, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        new_content = content
        for bi, block in enumerate(content):
            if not (isinstance(block, dict) and block.get("type") == "tool_search_tool_result"):
                continue
            new_block = _sanitize_block(block, available_tool_names, dropped)
            if new_block is not block:
                if new_content is content:
                    new_content = list(content)
                new_content[bi] = new_block
        if new_content is not content:
            if out is messages:
                out = list(messages)
            out[mi] = {**message, "content": new_content}
    return out, dropped
