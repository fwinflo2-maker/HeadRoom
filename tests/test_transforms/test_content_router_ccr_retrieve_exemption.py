"""Regression tests: ContentRouter must not re-compress headroom_retrieve results.

Companion to ``test_smart_crusher_ccr_retrieve_exemption.py`` (issue #1077).
SmartCrusher.apply() already guards this exact failure mode, but ContentRouter
— the transform actually registered in the default/proxy pipeline (see
``transforms/pipeline.py``) — calls ``SmartCrusher.crush()`` directly, bypassing
that guard entirely. Without this fix, a ``headroom_retrieve`` tool result sent
back to the model on the next turn gets swept up by ContentRouter's own
compression routing like any other large tool output, producing a fresh
``<<ccr:hash>>`` marker the agent can never redeem (an unresolvable retrieval
loop — the original reported bug).

Tests use the fully-qualified MCP tool name (``mcp__headroom__headroom_retrieve``)
that Claude Code actually sends when connected to `headroom mcp serve` — not the
bare name — since routing the guard through ``is_tool_excluded()`` (alias-aware)
rather than a bare string comparison is the whole point of the fix.
"""

from __future__ import annotations

import json

from headroom.transforms.content_router import ContentRouter, ContentRouterConfig


def _get_tokenizer():
    from headroom.providers import OpenAIProvider
    from headroom.tokenizer import Tokenizer

    provider = OpenAIProvider()
    token_counter = provider.get_token_counter("gpt-4o")
    return Tokenizer(token_counter, "gpt-4o")


def _big_json() -> str:
    """A JSON array large enough to clear the compression threshold."""
    return json.dumps([{"id": i, "value": "x" * 20, "active": i % 2 == 0} for i in range(60)])


def _anthropic_messages(tool_name: str, content: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_ccr_1",
                    "name": tool_name,
                    "input": {"hash": "abc123def456"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_ccr_1",
                    "content": content,
                }
            ],
        },
    ]


def _openai_messages(tool_name: str, content: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_ccr_1",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": '{"hash":"abc123def456"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_ccr_1", "content": content},
    ]


class TestContentRouterCcrRetrieveExemption:
    def test_anthropic_qualified_mcp_name_not_recompressed(self):
        """The MCP-qualified name Claude Code actually sends must be exempted."""
        content = _big_json()
        router = ContentRouter(ContentRouterConfig(min_section_tokens=10))
        tokenizer = _get_tokenizer()

        messages = _anthropic_messages("mcp__headroom__headroom_retrieve", content)
        result = router.apply(messages, tokenizer)

        tool_result_block = result.messages[1]["content"][0]
        assert tool_result_block["content"] == content, (
            "headroom_retrieve result was recompressed via ContentRouter "
            "(unresolvable retrieval loop)"
        )
        assert "<<ccr:" not in tool_result_block["content"]
        assert "router:excluded:ccr_retrieve" in result.transforms_applied

    def test_openai_qualified_mcp_name_not_recompressed(self):
        """Same guarantee for the OpenAI/litellm tool-message shape."""
        content = _big_json()
        router = ContentRouter(ContentRouterConfig(min_section_tokens=10))
        tokenizer = _get_tokenizer()

        messages = _openai_messages("mcp__headroom__headroom_retrieve", content)
        result = router.apply(messages, tokenizer)

        tool_msg = next(m for m in result.messages if m.get("tool_call_id") == "call_ccr_1")
        assert tool_msg["content"] == content
        assert "<<ccr:" not in tool_msg["content"]
        assert "router:excluded:ccr_retrieve" in result.transforms_applied

    def test_bare_tool_name_also_protected(self):
        """The proxy's own internally-injected retrieval tool uses the bare
        name (not MCP-qualified) — must be protected too, matching the
        existing SmartCrusher.apply() coverage for this call shape."""
        content = _big_json()
        router = ContentRouter(ContentRouterConfig(min_section_tokens=10))
        tokenizer = _get_tokenizer()

        messages = _anthropic_messages("headroom_retrieve", content)
        result = router.apply(messages, tokenizer)

        tool_result_block = result.messages[1]["content"][0]
        assert tool_result_block["content"] == content
        assert "router:excluded:ccr_retrieve" in result.transforms_applied

    def test_unconditional_even_with_empty_exclude_tools(self):
        """The guard is config-independent: even a caller that explicitly
        empties exclude_tools (disabling every default exclusion) must not
        be able to recompress a headroom_retrieve result. This is the
        defense-in-depth half of the fix — config.py's DEFAULT_EXCLUDE_TOOLS/
        DEFAULT_VERBATIM_EXCLUDE_TOOLS additions alone would not survive this
        override, since ContentRouter replaces (not merges) exclude_tools
        when the caller sets it explicitly."""
        content = _big_json()
        router = ContentRouter(
            ContentRouterConfig(min_section_tokens=10, exclude_tools=frozenset())
        )
        tokenizer = _get_tokenizer()

        messages = _anthropic_messages("mcp__headroom__headroom_retrieve", content)
        result = router.apply(messages, tokenizer)

        tool_result_block = result.messages[1]["content"][0]
        assert tool_result_block["content"] == content, (
            "headroom_retrieve must stay protected even when exclude_tools is "
            "explicitly emptied — the guard must not depend on config"
        )
        assert "router:excluded:ccr_retrieve" in result.transforms_applied

    def test_non_retrieve_tool_still_compressed(self):
        """Exemption is narrow: an ordinary tool with the same large JSON
        content is still compressed — this isn't a blanket JSON passthrough."""
        content = _big_json()
        router = ContentRouter(ContentRouterConfig(min_section_tokens=10))
        tokenizer = _get_tokenizer()

        messages = _anthropic_messages("Bash", content)
        result = router.apply(messages, tokenizer)

        tool_result_block = result.messages[1]["content"][0]
        assert tool_result_block["content"] != content or result.tokens_after < result.tokens_before
