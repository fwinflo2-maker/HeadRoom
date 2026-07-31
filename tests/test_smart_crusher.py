"""Regression tests for qualified CCR retrieval tool names."""

from __future__ import annotations

import json

import pytest

from headroom import OpenAIProvider, Tokenizer
from headroom.ccr.tool_injection import CCR_TOOL_NAME
from headroom.config import SmartCrusherConfig

try:
    from headroom._core import SmartCrusher as _RustSmartCrusher  # noqa: F401
except ImportError:
    pytest.skip("headroom._core not built", allow_module_level=True)

from headroom.transforms.smart_crusher import SmartCrusher


def _big_content() -> str:
    return json.dumps([{"id": i, "value": "x" * 20} for i in range(60)])


def _apply_for_tool(tool_name: str):
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": _big_content()},
    ]
    tokenizer = Tokenizer(OpenAIProvider().get_token_counter("gpt-4o"), "gpt-4o")
    result = SmartCrusher(config=SmartCrusherConfig(min_tokens_to_crush=0)).apply(
        messages, tokenizer
    )
    return messages[1]["content"], result


@pytest.mark.parametrize(
    "tool_name",
    ["mcp__Headroom__headroom_retrieve", "mcp_Headroom_headroom_retrieve"],
)
def test_qualified_ccr_retrieval_result_is_preserved(tool_name: str) -> None:
    original, result = _apply_for_tool(tool_name)

    assert result.messages[1]["content"] == original
    assert not any("smart_crush" in transform for transform in result.transforms_applied)


def test_near_match_ccr_tool_name_still_compresses() -> None:
    original, result = _apply_for_tool("mcp__Headroom__headroom_retrieve_extra")

    assert result.messages[1]["content"] != original or result.tokens_after < result.tokens_before


def test_bare_ccr_tool_name_remains_preserved() -> None:
    original, result = _apply_for_tool(CCR_TOOL_NAME)

    assert result.messages[1]["content"] == original
