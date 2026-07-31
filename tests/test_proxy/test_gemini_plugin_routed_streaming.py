"""Production-route regressions for plugin-routed native Gemini requests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from headroom.compress import CompressResult  # noqa: E402
from headroom.proxy.server import ProxyConfig, create_app  # noqa: E402

# The OpenCode transport sets x-headroom-base-url to the upstream *origin* and
# forwards the upstream pathname verbatim, so a custom Google endpoint arrives
# as origin + /v1beta/models/{model}:... on the proxy.
PLUGIN_BASE_URL = "https://gateway.example"


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


def _streaming_optimize_config() -> ProxyConfig:
    return ProxyConfig(
        optimize=True,
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
                "x-headroom-base-url": PLUGIN_BASE_URL,
                "x-headroom-original-path": "/v1beta/models/gemini-2.5-flash:generateContent",
            },
            json=body,
        )

    assert response.status_code == 200, response.text
    assert response.json() == upstream
    assert captured["method"] == "POST"
    assert captured["url"] == (
        "https://gateway.example/v1beta/models/gemini-2.5-flash:generateContent"
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
                "x-headroom-base-url": PLUGIN_BASE_URL,
                "x-headroom-original-path": "/v1beta/models/gemini-2.5-flash:streamGenerateContent",
            },
            json=body,
        )

    assert response.status_code == 200, response.text
    assert response.content == sse_bytes
    assert captured["url"] == (
        "https://gateway.example/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse"
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
            headers={"x-headroom-base-url": PLUGIN_BASE_URL},
            content=b"adjacent",
        )

    assert response.status_code == 200, response.text
    assert captured == {
        "path": "/v1/adjacent-provider-path",
        "base_url": PLUGIN_BASE_URL,
        "sub_path": "",
        "provider_name": "",
    }


def test_plugin_routed_streaming_reaches_the_transform_pipeline() -> None:
    """The reported symptom is `transforms=none` on plugin-routed Gemini traffic.

    Base forwards these requests through the passthrough branch, so the pipeline
    never runs and `_stream_response` receives an empty transforms list.
    """
    body = {
        "contents": [
            {"role": "user", "parts": [{"text": "compress me " * 200}]},
        ]
    }
    captured: dict[str, object] = {}

    def fake_apply(**kwargs: object) -> CompressResult:
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        captured["pipeline_messages"] = messages
        return CompressResult(
            messages=[{"role": "user", "content": "compressed"}],
            tokens_before=1000,
            tokens_after=10,
            tokens_saved=990,
            compression_ratio=0.99,
            transforms_applied=["smart_crusher"],
        )

    async def fake_stream_response(*args: object, **__: object) -> StreamingResponse:
        captured["url"] = args[0]
        captured["body"] = args[2]
        captured["transforms_applied"] = args[9]
        return StreamingResponse(iter([b"data: {}\n\n"]), media_type="text/event-stream")

    app = create_app(_streaming_optimize_config())
    with TestClient(app) as client:
        proxy = client.app.state.proxy
        proxy.openai_pipeline.apply = fake_apply
        proxy._stream_response = fake_stream_response
        response = client.post(
            "/v1beta/models/gemini-2.5-flash:streamGenerateContent",
            headers={
                "x-headroom-base-url": PLUGIN_BASE_URL,
                "x-headroom-original-path": "/v1beta/models/gemini-2.5-flash:streamGenerateContent",
            },
            json=body,
        )

    assert response.status_code == 200, response.text
    assert captured["transforms_applied"] == ["smart_crusher"]
    assert captured["url"] == (
        "https://gateway.example/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse"
    )
    outbound = captured["body"]
    assert isinstance(outbound, dict)
    assert outbound["contents"] == [{"role": "user", "parts": [{"text": "compressed"}]}]
    assert captured["pipeline_messages"]


def test_direct_gemini_stream_path_still_bypasses_the_pipeline() -> None:
    """Preservation: the non-plugin native path is untouched by this change."""
    captured: dict[str, object] = {}

    def unexpected_apply(**_: object) -> CompressResult:
        raise AssertionError("direct Gemini streaming must not enter the pipeline")

    async def fake_stream_response(*args: object, **__: object) -> StreamingResponse:
        captured["transforms_applied"] = args[9]
        return StreamingResponse(iter([b"data: {}\n\n"]), media_type="text/event-stream")

    app = create_app(_streaming_optimize_config())
    with TestClient(app) as client:
        proxy = client.app.state.proxy
        proxy.openai_pipeline.apply = unexpected_apply
        proxy._stream_response = fake_stream_response
        response = client.post(
            "/v1beta/models/gemini-2.5-flash:streamGenerateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "direct"}]}]},
        )

    assert response.status_code == 200, response.text
    assert captured["transforms_applied"] == []


def test_gemini_count_tokens_does_not_opt_into_plugin_adapter() -> None:
    app = create_app(_config())
    with TestClient(app) as client:
        proxy = client.app.state.proxy
        proxy.handle_gemini_count_tokens = AsyncMock(return_value=JSONResponse({"ok": True}))
        response = client.post(
            "/v1beta/models/gemini-2.5-flash:countTokens",
            headers={"x-headroom-base-url": PLUGIN_BASE_URL},
            json={"contents": []},
        )

    assert response.status_code == 200, response.text
    proxy.handle_gemini_count_tokens.assert_awaited_once()
    call = proxy.handle_gemini_count_tokens.await_args
    assert call is not None
    assert call.args[1] == "gemini-2.5-flash"
    assert call.kwargs == {}
