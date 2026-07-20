from __future__ import annotations

from types import SimpleNamespace

import pytest

from headroom.ccr.tool_injection import CCR_TOOL_NAME, CCRToolInjector
from headroom.pipeline import PipelineStage
from headroom.proxy.helpers import (
    _reset_session_ccr_tracker_for_test,
    apply_session_sticky_ccr_tool,
    get_session_ccr_tracker,
)
from tests.test_anthropic_stage_timings import (
    _build_request as _build_anthropic_request,
)
from tests.test_anthropic_stage_timings import (
    _DummyAnthropicHandler,
    _ResponseStub,
)

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


@pytest.fixture(autouse=True)
def _reset_tracker():
    _reset_session_ccr_tracker_for_test()
    yield
    _reset_session_ccr_tracker_for_test()


@pytest.mark.parametrize("session_id", [None, "anthropic-session-without-tracker"])
def test_sessionless_history_redeclares_ccr_tool(session_id: str | None) -> None:
    body = _issue_body([{"type": "tool_use", "name": CCR_TOOL_NAME}])

    with pytest.raises(RuntimeError, match="400 Tool reference") as error:
        _strict_upstream(body)
    assert str(error.value) == ISSUE_2440_ERROR

    injector = CCRToolInjector(provider="anthropic")
    injector.scan_for_markers(body["messages"])
    body["tools"], was_injected = apply_session_sticky_ccr_tool(
        provider="anthropic",
        session_id=session_id,
        request_id="issue-2440",
        existing_tools=body["tools"],
        has_compressed_content_this_turn=False,
        has_ccr_tool_use_history=injector.has_anthropic_ccr_tool_use_history,
    )

    response = _strict_upstream(body)

    assert was_injected is True
    assert response["status"] == 200
    assert response["declared_tools"] == {CCR_TOOL_NAME}


def test_anthropic_handler_redeclares_history_added_by_pre_send() -> None:
    handler = _DummyAnthropicHandler()
    handler.config.ccr_inject_tool = True
    handler.session_tracker_store.compute_session_id = lambda *args, **kwargs: (
        "handler-session-empty"
    )
    handler.session_tracker_store.resolve_tracker = lambda *args, **kwargs: type(
        "FrozenTracker",
        (),
        {
            "get_frozen_message_count": lambda self: 3,
            "get_last_original_messages": lambda self: [],
            "get_last_forwarded_messages": lambda self: [],
            "record_request": lambda self, *args, **kwargs: None,
        },
    )()

    def emit(stage, **kwargs):
        if stage is PipelineStage.PRE_SEND:
            kwargs["messages"] = [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": CCR_TOOL_NAME}],
                }
            ]
        return SimpleNamespace(**kwargs)

    handler.pipeline_extensions = SimpleNamespace(emit=emit)

    async def strict_retry(method, url, headers, body, **kwargs):
        assert body.get("tools", [])
        assert [tool["name"] for tool in body["tools"]] == [CCR_TOOL_NAME]
        handler.captured = (method, url, headers, body)
        return _ResponseStub()

    handler._retry_request = strict_retry
    request = _build_anthropic_request(
        {
            "model": "claude-3-5-sonnet-latest",
            "messages": [{"role": "user", "content": "hello"}],
        },
        {"authorization": "Bearer sk-ant-api-test"},
    )

    import anyio

    anyio.run(handler.handle_anthropic_messages, request)

    assert get_session_ccr_tracker().has_done_ccr("anthropic", "handler-session-empty")


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
