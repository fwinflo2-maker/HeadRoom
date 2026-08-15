"""Regression coverage for Anthropic no-optimize history forwarding."""

from __future__ import annotations

import copy
import json
from unittest.mock import AsyncMock

import httpx
from fastapi.testclient import TestClient

from headroom.cache.prefix_tracker import PrefixCacheTracker, PrefixFreezeConfig
from headroom.proxy.server import ProxyConfig, create_app

MODEL = "claude-sonnet-4-6"
MARKER = "large-tool-result-marker " * 80


def _config(**overrides) -> ProxyConfig:
    values = {
        "optimize": False,
        "cache_enabled": False,
        "rate_limit_enabled": False,
        "cost_tracking_enabled": False,
        "log_requests": False,
        "prefix_freeze_enabled": True,
    }
    values.update(overrides)
    return ProxyConfig(**values)


def _response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": MODEL,
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
    )


def test_no_optimize_second_turn_forwards_client_history_verbatim():
    """The no-optimize path must not replay a stale forwarded prefix."""
    app = create_app(_config())
    captured: list[dict] = []

    with TestClient(app) as client:
        proxy = client.app.state.proxy
        tracker = PrefixCacheTracker("anthropic", PrefixFreezeConfig(enabled=True))
        first = [{"role": "user", "content": MARKER}]
        proxy.session_tracker_store.resolve_tracker = lambda *args, **kwargs: tracker

        async def capture(method, url, headers, body, stream=False, **kwargs):
            captured.append(copy.deepcopy(body))
            return _response()

        proxy._retry_request = AsyncMock(side_effect=capture)
        first_response = client.post(
            "/v1/messages",
            json={"model": MODEL, "max_tokens": 16, "messages": first},
        )
        assert first_response.status_code == 200, first_response.text

        assistant = {"role": "assistant", "content": "ok"}
        previous = first + [assistant]
        tracker._last_original_messages = copy.deepcopy(previous)
        tracker._last_forwarded_messages = [
            {"role": "user", "content": MARKER + MARKER},
            assistant,
        ]
        current = previous + [{"role": "user", "content": "next turn"}]
        response = client.post(
            "/v1/messages",
            json={"model": MODEL, "max_tokens": 16, "messages": current},
        )

    assert response.status_code == 200, response.text
    outbound = captured[-1]["messages"]
    client_bytes = len(json.dumps(current, separators=(",", ":"), ensure_ascii=False).encode())
    outbound_bytes = len(json.dumps(outbound, separators=(",", ":"), ensure_ascii=False).encode())
    print(
        f"manual turn2 outbound_message_count={len(outbound)} "
        f"marker_count={sum(MARKER in json.dumps(message) for message in outbound)} "
        f"outbound_compact_utf8_bytes={outbound_bytes} client_compact_utf8_bytes={client_bytes}"
    )
    assert sum(MARKER in json.dumps(message) for message in outbound) == 1
    assert outbound_bytes <= client_bytes
