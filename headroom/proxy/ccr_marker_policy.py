"""CCR marker freshness policy.

Retrieval-tool injection is decided by ``apply_session_sticky_ccr_tool`` in
``headroom.proxy.helpers``, from what the session has actually forwarded.
"""

from __future__ import annotations

from typing import Any, Literal


def has_new_ccr_markers(
    *,
    current_detected_hashes: list[str],
    previous_forwarded_messages: list[dict[str, Any]] | None,
    provider: Literal["anthropic", "openai", "google"],
) -> bool:
    """Return whether current CCR hashes contain hashes not previously forwarded."""

    current = set(current_detected_hashes)
    if not current:
        return False
    if not previous_forwarded_messages:
        return True

    from headroom.ccr.tool_injection import CCRToolInjector

    previous = CCRToolInjector(
        provider=provider,
        inject_tool=False,
        inject_system_instructions=False,
    )
    previous.scan_for_markers(previous_forwarded_messages)
    return bool(current - set(previous.detected_hashes))


def transcript_references_ccr_tool(
    messages: list[dict[str, Any]] | None,
    *,
    tool_name: str | None = None,
    provider: Literal["anthropic", "openai", "google"] = "anthropic",
) -> bool:
    """Whether the about-to-forward transcript already names the CCR retrieve tool.

    Once an Anthropic ``tool_reference``/``tool_use`` block, or an assistant
    ``tool_calls`` entry (OpenAI chat), names ``headroom_retrieve``, the request's
    ``tools`` array MUST still carry that tool or Anthropic 400s ("Tool reference
    'headroom_retrieve' not found in available tools"). The sticky guarantee is
    model-scoped and in-memory, so a ``/model`` switch or proxy restart loses it
    while the client transcript keeps re-sending the reference. This scan lets
    injection self-heal from the transcript, per request, independent of tracker
    state.

    ``provider="google"`` is accepted for signature parity with the sibling
    policy fns but currently falls through the Anthropic matcher — Gemini's
    ``functionCall`` parts are not matched (no google CCR handler calls this yet).

    Only the exact bare ``headroom_retrieve`` name matches — a client-owned
    ``mcp__headroom__headroom_retrieve`` (registered via MCP, lifecycle owned by
    the client) must not trigger proxy injection. Tolerates string content and
    malformed blocks.
    """
    if not messages:
        return False
    if tool_name is None:
        from headroom.ccr.tool_injection import CCR_TOOL_NAME

        tool_name = CCR_TOOL_NAME

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if provider == "openai":
            if _openai_message_references_tool(msg, tool_name):
                return True
        elif _anthropic_content_references_tool(msg.get("content"), tool_name):
            return True
    return False


def _anthropic_content_references_tool(content: Any, tool_name: str) -> bool:
    """Match a bare ``tool_name`` in tool_reference/tool_use blocks (one level deep).

    The two block types key the tool differently, so the match is split by type:
    ``tool_use`` carries ``name``, while a ``tool_reference`` carries ``tool_name``
    (tool-search results and the custom client-side tool-search shape) or ``name``
    (the mid-conversation ``tool_addition``/``tool_removal`` shape).
    """
    if not isinstance(content, list):
        return False

    def matches(block: Any) -> bool:
        if not isinstance(block, dict):
            return False
        block_type = block.get("type")
        if block_type == "tool_reference":
            return tool_name in (block.get("tool_name"), block.get("name"))
        return block_type == "tool_use" and block.get("name") == tool_name

    for block in content:
        if not isinstance(block, dict):
            continue
        if matches(block):
            return True
        # ``tool_result`` nests a plain list of blocks; ``tool_search_tool_result``
        # nests a ``tool_search_tool_search_result`` dict whose ``tool_references``
        # holds them.
        nested = block.get("content")
        if isinstance(nested, dict):
            nested = nested.get("tool_references")
        if isinstance(nested, list) and any(matches(inner) for inner in nested):
            return True
    return False


def _openai_message_references_tool(msg: dict[str, Any], tool_name: str) -> bool:
    """Match a bare ``tool_name`` in an assistant message's ``tool_calls`` (chat shape)."""
    tool_calls = msg.get("tool_calls")
    if not isinstance(tool_calls, list):
        return False
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        name = fn.get("name") if isinstance(fn, dict) else call.get("name")
        if name == tool_name:
            return True
    return False
