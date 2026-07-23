"""Stale tool-search reference rescue + reference-forced CCR injection.

A replayed ``tool_search_tool_result`` block whose ``tool_reference`` names a
tool absent from the request's tools array makes the upstream API reject the
whole request with ``400 Tool reference '<name>' not found in available
tools`` — permanently, since the client replays the same history every turn.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from headroom.proxy.ccr_marker_policy import should_inject_ccr_tool
from headroom.proxy.server import ProxyConfig, create_app
from headroom.proxy.tool_reference_sanitizer import (
    collect_tool_reference_names,
    sanitize_stale_tool_references,
)


def _search_result_block(*names: str) -> dict:
    # Exact GA wire shape observed from the Claude API (content is a dict).
    return {
        "type": "tool_search_tool_result",
        "tool_use_id": "srvtoolu_01AC5nVzqY3JALcf2cNMXBPv",
        "content": {
            "type": "tool_search_tool_search_result",
            "tool_references": [{"type": "tool_reference", "tool_name": name} for name in names],
        },
    }


def _messages_with(block: dict) -> list[dict]:
    return [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "searching"}, block]},
    ]


class TestSanitizeStaleToolReferences:
    def test_drops_only_the_stale_reference(self) -> None:
        messages = _messages_with(
            _search_result_block("headroom_retrieve", "mcp__headroom__headroom_retrieve")
        )
        out, dropped = sanitize_stale_tool_references(
            messages, {"mcp__headroom__headroom_retrieve"}
        )
        assert dropped == ["headroom_retrieve"]
        refs = out[1]["content"][1]["content"]["tool_references"]
        assert [r["tool_name"] for r in refs] == ["mcp__headroom__headroom_retrieve"]

    def test_noop_returns_same_object(self) -> None:
        messages = _messages_with(_search_result_block("mcp__headroom__headroom_retrieve"))
        out, dropped = sanitize_stale_tool_references(
            messages, {"mcp__headroom__headroom_retrieve"}
        )
        assert dropped == []
        assert out is messages

    def test_copy_on_write_leaves_input_untouched(self) -> None:
        messages = _messages_with(_search_result_block("gone"))
        out, dropped = sanitize_stale_tool_references(messages, set())
        assert dropped == ["gone"]
        assert out is not messages
        # Original still holds the stale reference; untouched siblings share identity.
        assert messages[1]["content"][1]["content"]["tool_references"] != []
        assert out[0] is messages[0]

    def test_all_refs_stale_leaves_empty_list(self) -> None:
        # An empty tool_references list is accepted by the API (observed in a
        # live transcript), so the block itself stays.
        messages = _messages_with(_search_result_block("gone_a", "gone_b"))
        out, dropped = sanitize_stale_tool_references(messages, {"other"})
        assert sorted(dropped) == ["gone_a", "gone_b"]
        assert out[1]["content"][1]["content"]["tool_references"] == []

    def test_list_shaped_content_variant(self) -> None:
        block = {
            "type": "tool_search_tool_result",
            "tool_use_id": "srvtoolu_x",
            "content": [
                {"type": "tool_reference", "tool_name": "gone"},
                {"type": "tool_reference", "tool_name": "kept"},
            ],
        }
        out, dropped = sanitize_stale_tool_references(_messages_with(block), {"kept"})
        assert dropped == ["gone"]
        assert [r["tool_name"] for r in out[1]["content"][1]["content"]] == ["kept"]

    def test_malformed_content_is_left_alone(self) -> None:
        messages = [
            {"role": "user", "content": "plain string content"},
            {"role": "assistant", "content": [{"type": "tool_search_tool_result"}]},
            {"role": "assistant", "content": [{"type": "tool_search_tool_result", "content": 7}]},
            "not-a-dict-message",
        ]
        out, dropped = sanitize_stale_tool_references(messages, set())
        assert dropped == []
        assert out is messages

    def test_non_reference_entries_survive(self) -> None:
        block = _search_result_block("gone")
        block["content"]["tool_references"].append({"type": "something_else", "x": 1})
        out, dropped = sanitize_stale_tool_references(_messages_with(block), set())
        assert dropped == ["gone"]
        assert out[1]["content"][1]["content"]["tool_references"] == [
            {"type": "something_else", "x": 1}
        ]


class TestCollectToolReferenceNames:
    def test_collects_across_messages(self) -> None:
        messages = _messages_with(_search_result_block("a", "b"))
        assert collect_tool_reference_names(messages) == {"a", "b"}

    def test_empty_and_malformed(self) -> None:
        assert collect_tool_reference_names(None) == set()
        assert collect_tool_reference_names([{"role": "user", "content": "hi"}]) == set()


class TestShouldInjectCcrToolHistoryOverride:
    def test_history_reference_forces_injection_past_deferral(self) -> None:
        should, override = should_inject_ccr_tool(
            configured_inject_tool=True,
            frozen_message_count=3,
            has_compressed_content=False,
            history_references_tool=True,
        )
        assert should is True
        assert override is True

    def test_without_history_reference_deferral_still_wins(self) -> None:
        should, override = should_inject_ccr_tool(
            configured_inject_tool=True,
            frozen_message_count=3,
            has_compressed_content=False,
        )
        assert should is False
        assert override is False


# --- Integration through the real Anthropic handler -------------------------


class _FakePrefixTracker:
    def __init__(self, frozen_count: int):
        self._frozen_count = frozen_count
        self._cached_token_count = 0
        self._last_forwarded_messages: list[dict] = []

    def get_frozen_message_count(self) -> int:
        return self._frozen_count

    def get_last_original_messages(self):  # noqa: ANN201
        return []

    def get_last_forwarded_messages(self):  # noqa: ANN201
        return self._last_forwarded_messages.copy()

    def update_from_response(self, **kwargs):  # noqa: ANN003
        self._last_forwarded_messages = kwargs.get("messages", []).copy()
        return None


class _FakeCompressionCache:
    def __init__(self, frozen_count: int):
        self._frozen_count = frozen_count

    def apply_cached(self, messages):  # noqa: ANN201
        return messages

    def compute_frozen_count(self, messages) -> int:  # noqa: ARG002
        return self._frozen_count

    def mark_stable_from_messages(self, messages, frozen_count) -> None:  # noqa: ARG002
        return None

    def update_from_result(self, originals, compressed) -> None:  # noqa: ARG002
        return None


def _make_proxy_client() -> TestClient:
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
    app = create_app(config)
    return TestClient(app)


def _disable_pipeline_extensions(proxy) -> None:  # noqa: ANN001
    proxy.pipeline_extensions.emit = lambda *args, **kwargs: SimpleNamespace(
        messages=kwargs.get("messages"),
        tools=kwargs.get("tools"),
        headers=kwargs.get("headers"),
        metadata=kwargs.get("metadata"),
    )


def _capture_forwarded_body(proxy, captured: dict) -> None:  # noqa: ANN001
    async def _fake_retry(method, url, headers, body, stream=False, **kwargs):  # noqa: ANN001
        captured["body"] = body
        return httpx.Response(
            200,
            json={
                "id": "msg_stale_refs",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 20, "output_tokens": 3},
            },
        )

    proxy._retry_request = _fake_retry


def test_handler_drops_stale_reference_before_forwarding() -> None:
    captured: dict[str, object] = {}
    messages = _messages_with(
        _search_result_block("headroom_retrieve", "mcp__headroom__headroom_retrieve")
    )

    with _make_proxy_client() as client:
        proxy = client.app.state.proxy
        proxy.config.optimize = True
        _disable_pipeline_extensions(proxy)
        _capture_forwarded_body(proxy, captured)

        response = client.post(
            "/v1/messages",
            headers={"x-api-key": "test-key", "anthropic-version": "2023-06-01"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 64,
                "messages": messages,
                "tools": [
                    {
                        "name": "mcp__headroom__headroom_retrieve",
                        "description": "retrieve",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
            },
        )

        assert response.status_code == 200
        forwarded = captured["body"]
        refs = forwarded["messages"][1]["content"][1]["content"]["tool_references"]
        assert [r["tool_name"] for r in refs] == ["mcp__headroom__headroom_retrieve"]


def test_handler_forces_ccr_injection_when_history_references_tool() -> None:
    captured: dict[str, object] = {}
    # Frozen prefix would normally defer injection; the replayed reference to
    # headroom_retrieve must override it or upstream 400s the request.
    messages = _messages_with(_search_result_block("headroom_retrieve"))

    with _make_proxy_client() as client:
        proxy = client.app.state.proxy
        proxy.config.optimize = True
        proxy.config.ccr_inject_tool = True
        proxy.config.mode = "cache"
        _disable_pipeline_extensions(proxy)
        _capture_forwarded_body(proxy, captured)

        fake_tracker = _FakePrefixTracker(frozen_count=1)
        proxy.session_tracker_store.compute_session_id = lambda request, model, messages: (
            "stable-session"
        )
        proxy.session_tracker_store.get_or_create = lambda session_id, provider: fake_tracker
        proxy._get_compression_cache = lambda session_id: _FakeCompressionCache(frozen_count=1)

        response = client.post(
            "/v1/messages",
            headers={"x-api-key": "test-key", "anthropic-version": "2023-06-01"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 64,
                "messages": messages,
            },
        )

        assert response.status_code == 200
        forwarded = captured["body"]
        tool_names = [t.get("name") for t in forwarded.get("tools", [])]
        assert "headroom_retrieve" in tool_names
        # With the tool injected the replayed reference resolves — nothing dropped.
        refs = forwarded["messages"][1]["content"][1]["content"]["tool_references"]
        assert [r["tool_name"] for r in refs] == ["headroom_retrieve"]
