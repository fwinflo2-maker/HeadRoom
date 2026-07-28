"""Dangling ``tool_reference`` recovery for server-side Tool Search.

:func:`headroom.proxy.helpers.inject_tool_search_deferral` stamps
``defer_loading: true`` on every non-core tool and injects a ``tool_search_tool_*``
tool, so Anthropic keeps the deferred schemas out of the context window. The model
then discovers a tool by search and the transcript permanently gains a
``tool_reference`` naming it.

Anthropic validates those references on every turn: each name a ``tool_reference``
mentions MUST still have a definition in the request's ``tools`` array, or the
request 400s with ``Tool reference 'X' not found in available tools``. A tool can
leave ``tools`` between turns for reasons this proxy does not control — an MCP
server disconnects, the client trims its surface for a subagent turn — while the
transcript keeps replaying the reference. Every deferred tool is exposed this way,
not just the proxy-injected ``headroom_retrieve``.

Recovery is two-tier, per request:

1. **Re-inject** from the per-session definition cache. Whatever this proxy
   deferred it has already seen in full, so the definition is restored verbatim
   and still deferred — no context cost.
2. **Sanitize** when tier 1 cannot help (a client-owned reference whose definition
   we have never seen). The orphaned name is dropped from the transcript so the
   request is valid; the model loses the discovery record and may search again,
   which beats a hard 400.

The same per-session state also makes deferral *sticky*: see
:func:`session_has_deferred`.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

_TOOL_REFERENCE_TYPE = "tool_reference"

# A proxy process serves many sessions for a long time, so the cache is LRU-capped
# rather than unbounded. A session's deferred surface is the client's whole
# non-core tool list, hence the generous per-session cap.
_MAX_SESSIONS = 256
_MAX_TOOLS_PER_SESSION = 2048

_lock = threading.Lock()
_deferred_by_session: OrderedDict[str, dict[str, Any]] = OrderedDict()


# ---------------------------------------------------------------------------
# Per-session deferred-definition cache
# ---------------------------------------------------------------------------


def remember_deferred_tools(session_id: str | None, tools: Any) -> None:
    """Record the full definition of each tool this proxy deferred for a session.

    Accumulates across turns: a tool deferred on turn 3 stays recoverable on turn
    30 even if the client stopped sending it in between.
    """
    if not session_id or not isinstance(tools, list):
        return
    definitions = {
        tool["name"]: tool
        for tool in tools
        if isinstance(tool, dict)
        and tool.get("defer_loading")
        and isinstance(tool.get("name"), str)
        and tool["name"]
    }
    if not definitions:
        return
    with _lock:
        known = _deferred_by_session.pop(session_id, None) or {}
        known.update(definitions)
        if len(known) > _MAX_TOOLS_PER_SESSION:
            known = dict(list(known.items())[-_MAX_TOOLS_PER_SESSION:])
        _deferred_by_session[session_id] = known
        while len(_deferred_by_session) > _MAX_SESSIONS:
            _deferred_by_session.popitem(last=False)


def session_has_deferred(session_id: str | None) -> bool:
    """Whether this session has ever had tools deferred.

    Drives sticky deferral: without it the tool-count threshold can stop applying
    mid-session, dropping the search tool and the deferred definitions while the
    transcript still replays ``tool_reference`` blocks for them.
    """
    if not session_id:
        return False
    with _lock:
        return session_id in _deferred_by_session


def cached_definition(session_id: str | None, name: str) -> Any | None:
    """The remembered definition for *name*, or ``None`` if we never deferred it."""
    if not session_id:
        return None
    with _lock:
        return (_deferred_by_session.get(session_id) or {}).get(name)


def reset_state() -> None:
    """Drop all cached state (tests, and the wrap CLI between runs)."""
    with _lock:
        _deferred_by_session.clear()


# ---------------------------------------------------------------------------
# Transcript scanning
# ---------------------------------------------------------------------------


def reference_name(block: Any) -> str | None:
    """Name a ``tool_reference`` block points at, or ``None`` if it isn't one.

    Tool-search results key it as ``tool_name``; the mid-conversation
    ``tool_addition``/``tool_removal`` shape uses ``name``.
    """
    if not isinstance(block, dict) or block.get("type") != _TOOL_REFERENCE_TYPE:
        return None
    for key in ("tool_name", "name"):
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _nested_reference_blocks(block: Any) -> list[Any]:
    """The ``tool_reference`` carriers one level inside a result block.

    ``tool_result`` nests a plain list of blocks; ``tool_search_tool_result`` nests
    a ``tool_search_tool_search_result`` dict whose ``tool_references`` holds them.
    """
    if not isinstance(block, dict):
        return []
    nested = block.get("content")
    if isinstance(nested, dict):
        nested = nested.get("tool_references")
    return nested if isinstance(nested, list) else []


def _candidate_blocks(block: Any) -> list[Any]:
    """The block itself plus every place a ``tool_reference`` hides one level in."""
    candidates = [block]
    if isinstance(block, dict):
        # tool_addition / tool_removal wrap the reference under "tool".
        wrapped = block.get("tool")
        if isinstance(wrapped, dict):
            candidates.append(wrapped)
        candidates.extend(_nested_reference_blocks(block))
    return candidates


def collect_tool_reference_names(messages: Any) -> set[str]:
    """Every tool name the transcript references, at top level or one level deep."""
    names: set[str] = set()
    if not isinstance(messages, list):
        return names
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            for candidate in _candidate_blocks(block):
                name = reference_name(candidate)
                if name:
                    names.add(name)
    return names


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _sanitize_content_block(block: Any, orphans: set[str]) -> Any:
    """Drop orphaned ``tool_reference`` entries from a result block.

    Only the two tool-search carriers are rewritten. ``tool_addition`` /
    ``tool_removal`` are left alone: they are explicit client instructions this
    proxy never generates, and editing them would change request semantics.
    """
    if not isinstance(block, dict):
        return block
    nested = block.get("content")

    if isinstance(nested, dict) and isinstance(nested.get("tool_references"), list):
        references = nested["tool_references"]
        kept = [ref for ref in references if reference_name(ref) not in orphans]
        if len(kept) == len(references):
            return block
        # An empty tool_references array is the documented "no matches" shape, so
        # the block stays valid even when every reference was orphaned.
        return {**block, "content": {**nested, "tool_references": kept}}

    if isinstance(nested, list):
        kept = [ref for ref in nested if reference_name(ref) not in orphans]
        if len(kept) == len(nested):
            return block
        if not kept:
            # A tool_result needs content; say why the reference went away.
            kept = [{"type": "text", "text": "(referenced tool is no longer available)"}]
        return {**block, "content": kept}

    return block


@dataclass
class ReconcileResult:
    """Outcome of :func:`reconcile_tool_references`."""

    tools: Any
    messages: Any
    reinjected: list[str] = field(default_factory=list)
    sanitized: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.reinjected or self.sanitized)


def reconcile_tool_references(
    messages: Any,
    tools: Any,
    *,
    session_id: str | None,
) -> ReconcileResult:
    """Make every transcript ``tool_reference`` resolvable against ``tools``.

    Re-injects a cached definition when this proxy deferred the tool; otherwise
    drops the orphaned reference from the transcript. Neither input is mutated in
    place — a changed ``tools``/``messages`` is returned as a new object so the
    caller can tell whether the body needs re-serializing.
    """
    referenced = collect_tool_reference_names(messages)
    if not referenced:
        return ReconcileResult(tools, messages)

    declared = (
        {tool.get("name") for tool in tools if isinstance(tool, dict)}
        if isinstance(tools, list)
        else set()
    )
    missing = referenced - declared
    if not missing:
        return ReconcileResult(tools, messages)

    tools_out = tools
    reinjected: list[str] = []
    for name in sorted(missing):
        definition = cached_definition(session_id, name)
        if definition is None:
            continue
        if tools_out is tools:
            tools_out = list(tools) if isinstance(tools, list) else []
        tools_out.append(definition)
        reinjected.append(name)

    orphans = missing - set(reinjected)
    messages_out = messages
    sanitized: list[str] = []
    if orphans and isinstance(messages, list):
        rebuilt: list[Any] = []
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                rebuilt.append(message)
                continue
            new_content = [_sanitize_content_block(block, orphans) for block in content]
            if any(new is not old for new, old in zip(new_content, content)):
                rebuilt.append({**message, "content": new_content})
            else:
                rebuilt.append(message)
        if any(new is not old for new, old in zip(rebuilt, messages)):
            messages_out = rebuilt
            sanitized = sorted(orphans)

    return ReconcileResult(tools_out, messages_out, reinjected, sanitized)
