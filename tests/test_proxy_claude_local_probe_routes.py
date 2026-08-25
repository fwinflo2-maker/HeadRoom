"""Regression tests for Claude-local probe routes on the proxy gateway."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.responses import Response
from fastapi.testclient import TestClient

from headroom.proxy.server import ProxyConfig, create_app


@pytest.fixture
def app_and_client():
    config = ProxyConfig(
        optimize=False,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
    )
    app = create_app(config)
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 12345),
    ) as test_client:
        yield app, test_client


@pytest.fixture
def passthrough_probe(monkeypatch, app_and_client):
    app, client = app_and_client
    proxy = app.state.proxy
    calls: list[tuple[str, str]] = []

    async def fake_handle_passthrough(request, *args, **kwargs):
        calls.append((request.method, request.url.path))
        return Response(status_code=599)

    monkeypatch.setattr(proxy, "handle_passthrough", fake_handle_passthrough)
    return client, calls


def test_claude_event_logging_batch_is_acknowledged_locally(passthrough_probe):
    client, calls = passthrough_probe

    response = client.post(
        "/api/event_logging/batch",
        headers={
            "User-Agent": "claude-code/2.0.77",
            "X-Service-Name": "claude-code",
            "Content-Type": "application/json",
        },
        json={"events": []},
    )

    assert response.status_code == 204
    assert calls == []


def test_non_claude_event_logging_batch_still_passthroughs(passthrough_probe):
    client, calls = passthrough_probe

    response = client.post(
        "/api/event_logging/batch",
        headers={"User-Agent": "curl/8.5.0", "Content-Type": "application/json"},
        json={"events": []},
    )

    assert response.status_code == 599
    assert calls == [("POST", "/api/event_logging/batch")]


def test_bun_api_hello_probe_is_acknowledged_locally(passthrough_probe):
    client, calls = passthrough_probe

    response = client.head(
        "/api/hello",
        headers={"User-Agent": "Bun/1.4.0", "Accept": "*/*"},
    )

    assert response.status_code == 200
    assert calls == []


def test_non_claude_api_hello_still_passthroughs(passthrough_probe):
    client, calls = passthrough_probe

    response = client.get(
        "/api/hello",
        headers={"User-Agent": "curl/8.5.0", "Accept": "*/*"},
    )

    assert response.status_code == 599
    assert calls == [("GET", "/api/hello")]
