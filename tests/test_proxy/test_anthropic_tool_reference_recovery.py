"""End-to-end: dangling ``tool_reference`` recovery through the Anthropic handler.

Unit coverage of the policy lives in
``tests/test_tool_search_reference_recovery.py``. These tests assert the wiring —
that the outbound body the proxy actually forwards is repaired — so a regression
in the handler hook is caught even when the policy itself stays correct.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from headroom.proxy.server import ProxyConfig, create_app
from headroom.proxy.tool_search_recovery import reset_state

CORE = ["bash", "read", "write", "edit", "glob", "grep", "task", "todowrite"]


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    reset_state()
    monkeypatch.setenv("HEADROOM_TOOL_SEARCH", "1")
    yield
    reset_state()


def _tool(name):
    return {"name": name, "description": "d", "input_schema": {"type": "object"}}


def _search_result_msg(*names):
    return {
        "role": "assistant",
        "content": [
            {
                "type": "server_tool_use",
                "id": "srvtoolu_1",
                "name": "tool_search_tool_regex",
                "input": {"pattern": "op"},
            },
            {
                "type": "tool_search_tool_result",
                "tool_use_id": "srvtoolu_1",
                "content": {
                    "type": "tool_search_tool_search_result",
                    "tool_references": [{"type": "tool_reference", "tool_name": n} for n in names],
                },
            },
        ],
    }


def _make_client() -> TestClient:
    config = ProxyConfig(
        optimize=False,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        ccr_inject_tool=False,
        ccr_handle_responses=False,
        ccr_context_tracking=False,
        image_optimize=False,
    )
    return TestClient(create_app(config))


def _run_turns(turns):
    """POST each ``(messages, tools)`` turn; return the forwarded body per turn."""
    captured: list[dict] = []

    with _make_client() as client:
        proxy = client.app.state.proxy
        proxy.session_tracker_store.compute_session_id = lambda request, model, messages: (
            "stable-session"
        )
        proxy.pipeline_extensions.emit = lambda *args, **kwargs: SimpleNamespace(
            messages=kwargs.get("messages"),
            tools=kwargs.get("tools"),
            headers=kwargs.get("headers"),
            metadata=kwargs.get("metadata"),
        )

        async def _fake_retry(method, url, headers, body, stream=False, **kwargs):  # noqa: ANN001
            captured.append(body)
            return httpx.Response(
                200,
                json={
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                },
            )

        proxy._retry_request = _fake_retry

        for messages, tools in turns:
            response = client.post(
                "/v1/messages",
                headers={"x-api-key": "test-key", "anthropic-version": "2023-06-01"},
                json={
                    "model": "claude-opus-4-6",
                    "max_tokens": 64,
                    "messages": messages,
                    "tools": tools,
                },
            )
            assert response.status_code == 200, response.text

    return captured


def _tool_names(body):
    return [t.get("name") for t in body.get("tools", []) if isinstance(t, dict)]


def test_deferred_tool_is_reinjected_when_the_client_stops_sending_it():
    wide = [_tool(n) for n in CORE] + [_tool(f"mcp__linear__op{i}") for i in range(6)]
    # Turn 1 defers the MCP tools; turn 2 arrives after the MCP server dropped,
    # but the transcript still references one of them.
    shrunk = [_tool(n) for n in CORE]
    bodies = _run_turns(
        [
            ([{"role": "user", "content": "hi"}], wide),
            ([_search_result_msg("mcp__linear__op4")], shrunk),
        ]
    )

    turn1, turn2 = bodies
    assert "mcp__linear__op4" in _tool_names(turn1)
    # The recovery: op4 is back in the forwarded tools even though the client
    # omitted it, so Anthropic can expand the transcript's tool_reference.
    assert "mcp__linear__op4" in _tool_names(turn2)
    restored = next(t for t in turn2["tools"] if t.get("name") == "mcp__linear__op4")
    assert restored["defer_loading"] is True
    # Transcript left intact — nothing had to be sanitized.
    refs = turn2["messages"][-1]["content"][1]["content"]["tool_references"]
    assert [r["tool_name"] for r in refs] == ["mcp__linear__op4"]


def test_unrecoverable_reference_is_sanitized_out_of_the_forwarded_transcript():
    tools = [_tool(n) for n in CORE] + [_tool(f"mcp__linear__op{i}") for i in range(6)]
    # ghost_tool was never deferred by this proxy, so there is no definition to
    # restore; forwarding the reference as-is would 400.
    bodies = _run_turns([([_search_result_msg("ghost_tool")], tools)])

    refs = bodies[0]["messages"][-1]["content"][1]["content"]["tool_references"]
    assert refs == []
    assert "ghost_tool" not in _tool_names(bodies[0])
    # The paired server_tool_use survives so the assistant turn stays well-formed.
    assert bodies[0]["messages"][-1]["content"][0]["type"] == "server_tool_use"


def test_search_tool_stays_declared_after_the_client_surface_shrinks():
    wide = [_tool(n) for n in CORE] + [_tool(f"mcp__linear__op{i}") for i in range(6)]
    core_only = [_tool(n) for n in CORE]
    bodies = _run_turns(
        [
            ([{"role": "user", "content": "hi"}], wide),
            ([_search_result_msg("mcp__linear__op1")], core_only),
        ]
    )

    def has_search_tool(body):
        return any(
            str(t.get("type", "")).startswith("tool_search_tool_")
            for t in body.get("tools", [])
            if isinstance(t, dict)
        )

    assert has_search_tool(bodies[0])
    # Sticky: without it the search tool vanishes here (core-only turn is below the
    # deferral threshold and defers nothing), orphaning the transcript's
    # server_tool_use block that names it.
    assert has_search_tool(bodies[1])
