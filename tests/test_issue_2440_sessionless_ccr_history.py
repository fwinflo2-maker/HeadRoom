from __future__ import annotations

import pytest

from headroom.ccr.tool_injection import CCR_TOOL_NAME, CCRToolInjector
from headroom.proxy.helpers import apply_session_sticky_ccr_tool

ISSUE_2440_ERROR = "API Error: 400 Tool reference 'headroom_retrieve' not found in available tools"


def _strict_upstream(body: dict) -> dict:
    history_has_ccr_tool = any(
        message.get("role") == "assistant"
        and isinstance(message.get("content"), list)
        and any(
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == CCR_TOOL_NAME
            for block in message["content"]
        )
        for message in body["messages"]
    )
    declared_names = {tool.get("name") for tool in body.get("tools", [])}
    if history_has_ccr_tool and CCR_TOOL_NAME not in declared_names:
        raise RuntimeError(ISSUE_2440_ERROR)
    return {"status": 200, "declared_tools": declared_names}


def _issue_body(content: list[dict], tools: list[dict] | None = None) -> dict:
    return {
        "messages": [{"role": "assistant", "content": content}],
        "tools": tools or [],
    }


def test_sessionless_history_redeclares_ccr_tool() -> None:
    body = _issue_body([{"type": "tool_use", "name": CCR_TOOL_NAME}])

    with pytest.raises(RuntimeError, match="400 Tool reference") as error:
        _strict_upstream(body)
    assert str(error.value) == ISSUE_2440_ERROR

    injector = CCRToolInjector(provider="anthropic")
    injector.scan_for_markers(body["messages"])
    body["tools"], was_injected = apply_session_sticky_ccr_tool(
        provider="anthropic",
        session_id=None,
        request_id="issue-2440",
        existing_tools=body["tools"],
        has_compressed_content_this_turn=False,
        has_ccr_tool_use_history=injector.has_anthropic_ccr_tool_use_history,
    )

    response = _strict_upstream(body)

    assert was_injected is True
    assert response["status"] == 200
    assert response["declared_tools"] == {CCR_TOOL_NAME}


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "text", "text": CCR_TOOL_NAME}],
        [{"type": "tool_use", "name": "other_tool"}],
        [{"name": CCR_TOOL_NAME}],
    ],
)
def test_sessionless_history_requires_structured_ccr_event(content: list[dict]) -> None:
    body = _issue_body(content)
    injector = CCRToolInjector(provider="anthropic")
    injector.scan_for_markers(body["messages"])

    body["tools"], was_injected = apply_session_sticky_ccr_tool(
        provider="anthropic",
        session_id=None,
        request_id="negative-space",
        existing_tools=body["tools"],
        has_compressed_content_this_turn=False,
        has_ccr_tool_use_history=injector.has_anthropic_ccr_tool_use_history,
    )

    assert was_injected is False
    assert body["tools"] == []
