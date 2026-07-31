"""Production-route regressions for plugin-routed native Gemini requests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from headroom.proxy.server import ProxyConfig, create_app  # noqa: E402


def _config() -> ProxyConfig:
    return ProxyConfig(
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


def test_plugin_generate_content_uses_native_gemini_route_and_preserves_body() -> None:
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": "Keep this prompt."},
                    {"inlineData": {"mimeType": "image/png", "data": "aW1hZ2U="}},
                    {"functionCall": {"name": "lookup", "args": {"city": "Paris"}}},
                ],
            }
        ],
        "generationConfig": {"temperature": 0.2},
    }
    upstream = {
        "candidates": [{"content": {"parts": [{"text": "answer"}]}}],
        "usageMetadata": {"promptTokenCount": 42, "candidatesTokenCount": 7},
    }
    captured: dict[str, object] = {}

    async def fake_retry(
        method: str,
        url: str,
        headers: dict[str, str],
        request_body: dict,
        **_: object,
    ) -> httpx.Response:
        captured.update(method=method, url=url, headers=headers, body=request_body)
        return httpx.Response(200, json=upstream)

    app = create_app(_config())
    with TestClient(app) as client:
        proxy = client.app.state.proxy
        proxy._retry_request = fake_retry
        response = client.post(
            "/v1beta/models/gemini-2.5-flash:generateContent",
            headers={
                "x-headroom-base-url": "https://gateway.example/api/",
                "x-headroom-original-path": "/v1beta/models/gemini-2.5-flash:generateContent",
            },
            json=body,
        )

    assert response.status_code == 200, response.text
    assert response.json() == upstream
    assert captured["method"] == "POST"
    assert captured["url"] == (
        "https://gateway.example/api/v1beta/models/gemini-2.5-flash:generateContent"
    )
    assert captured["body"] == body
    outbound_headers = captured["headers"]
    assert isinstance(outbound_headers, dict)
    assert not any(key.lower().startswith("x-headroom-") for key in outbound_headers)


def test_plugin_stream_generate_content_preserves_sse_and_native_parts() -> None:
    body = {
        "contents": [
            {"role": "user", "parts": [{"text": "Keep this prompt."}]},
            {
                "role": "model",
                "parts": [
                    {"functionCall": {"name": "lookup", "args": {"city": "Paris"}}},
                    {"inlineData": {"mimeType": "image/png", "data": "aW1hZ2U="}},
                ],
            },
        ]
    }
    sse_bytes = (
        b'data: {"candidates":[{"content":{"parts":[{"text":"answer"}]}}],'
        b'"usageMetadata":{"promptTokenCount":42,"candidatesTokenCount":7}}\n\n'
    )
    captured: dict[str, object] = {}

    async def fake_stream_response(
        url: str,
        headers: dict[str, str],
        request_body: dict,
        *_: object,
        **__: object,
    ) -> StreamingResponse:
        captured.update(url=url, headers=headers, body=request_body)
        return StreamingResponse(iter([sse_bytes]), media_type="text/event-stream")

    app = create_app(_config())
    with TestClient(app) as client:
        proxy = client.app.state.proxy
        proxy._stream_response = fake_stream_response
        response = client.post(
            "/v1beta/models/gemini-2.5-flash:streamGenerateContent",
            headers={
                "x-headroom-base-url": "https://gateway.example/api",
                "x-headroom-original-path": "/v1beta/models/gemini-2.5-flash:streamGenerateContent",
            },
            json=body,
        )

    assert response.status_code == 200, response.text
    assert response.content == sse_bytes
    assert captured["url"] == (
        "https://gateway.example/api/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse"
    )
    assert captured["body"] == body
    outbound_headers = captured["headers"]
    assert isinstance(outbound_headers, dict)
    assert not any(key.lower().startswith("x-headroom-") for key in outbound_headers)


def test_direct_gemini_stream_path_keeps_existing_handler() -> None:
    captured: dict[str, object] = {}

    async def fake_stream_response(
        url: str,
        headers: dict[str, str],
        request_body: dict,
        *_: object,
        **__: object,
    ) -> StreamingResponse:
        captured.update(url=url, headers=headers, body=request_body)
        return StreamingResponse(iter([b"data: {}\n\n"]), media_type="text/event-stream")

    app = create_app(_config())
    with TestClient(app) as client:
        proxy = client.app.state.proxy
        proxy._stream_response = fake_stream_response
        response = client.post(
            "/v1beta/models/gemini-2.5-flash:streamGenerateContent",
            json={"contents": [{"parts": [{"text": "direct"}]}]},
        )

    assert response.status_code == 200, response.text
    assert response.content == b"data: {}\n\n"
    assert str(captured["url"]).startswith("https://generativelanguage.googleapis.com/")


def test_adjacent_custom_base_path_stays_passthrough() -> None:
    captured: dict[str, object] = {}

    async def fake_passthrough(
        request: Request,
        base_url: str,
        sub_path: str = "",
        provider_name: str = "",
    ) -> dict[str, object]:
        captured.update(
            path=request.url.path,
            base_url=base_url,
            sub_path=sub_path,
            provider_name=provider_name,
        )
        return {"handler": "passthrough"}

    app = create_app(_config())
    with TestClient(app) as client:
        proxy = client.app.state.proxy
        proxy.handle_passthrough = fake_passthrough
        response = client.post(
            "/v1/adjacent-provider-path",
            headers={"x-headroom-base-url": "https://gateway.example/api"},
            content=b"adjacent",
        )

    assert response.status_code == 200, response.text
    assert captured == {
        "path": "/v1/adjacent-provider-path",
        "base_url": "https://gateway.example/api",
        "sub_path": "",
        "provider_name": "",
    }


def test_gemini_count_tokens_does_not_opt_into_plugin_adapter() -> None:
    app = create_app(_config())
    with TestClient(app) as client:
        proxy = client.app.state.proxy
        proxy.handle_gemini_count_tokens = AsyncMock(return_value=JSONResponse({"ok": True}))
        response = client.post(
            "/v1beta/models/gemini-2.5-flash:countTokens",
            headers={"x-headroom-base-url": "https://gateway.example/api"},
            json={"contents": []},
        )

    assert response.status_code == 200, response.text
    proxy.handle_gemini_count_tokens.assert_awaited_once()
    call = proxy.handle_gemini_count_tokens.await_args
    assert call is not None
    assert call.args[1] == "gemini-2.5-flash"
    assert call.kwargs == {}
