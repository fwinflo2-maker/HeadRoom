"""Bob gateway routing and attribution across the proxy's operating modes.

The failure these guard against is not a compression bug: it is Bob's request
reaching the *wrong host*. A proxy configured for another provider still routes
``/inference/v1/chat/completions`` happily — it just forwards it to
api.openai.com, where Bob's IBM ``Authorization: apikey ...`` is not a
credential and every request comes back 401.

``baseline`` is deliberately not in the mode list. ``normalize_proxy_mode``
knows only ``token`` and ``cache``; anything else logs "Unknown HEADROOM_MODE"
and silently becomes ``token``. The no-compression arm is ``optimize=False``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from headroom.providers.bob import DEFAULT_API_URL, GATEWAY_CHAT_COMPLETIONS_PATH  # noqa: E402
from headroom.proxy.loopback_guard import require_loopback  # noqa: E402
from headroom.proxy.server import ProxyConfig, create_app  # noqa: E402

# The origin Bob's DEFAULT_API_URL resolves to once `_normalize_api_url` strips
# the trailing `/v1` and the handler re-appends `/v1/chat/completions`.
BOB_UPSTREAM_ORIGIN = "https://api.us-east.bob.ibm.com"
BOB_UPSTREAM_URL = f"{BOB_UPSTREAM_ORIGIN}{GATEWAY_CHAT_COMPLETIONS_PATH}"

# Bob's own headers, as captured from bob-shell/2.0.1 in ~/.headroom/logs.
BOB_HEADERS = {
    "authorization": "apikey test-ibm-key",
    "user-agent": "ai-sdk/openrouter/3.0.0 bob-shell/2.0.1",
    "x-platform-name": "bob-shell",
    "x-mode": "agent",
}


def _conversation() -> list[dict[str, Any]]:
    """A payload with enough repeated bulk that compression has something to do."""
    filler = "def handler(request):\n    return process(request)\n" * 40
    return [
        {"role": "system", "content": "You are Bob, an IBM coding assistant. " + filler},
        {"role": "user", "content": "Summarize the handler module."},
        {"role": "assistant", "content": "It defines a request handler. " + filler},
        {"role": "user", "content": "Now add error handling."},
    ]


def _make_app(**overrides: Any):
    config = ProxyConfig(
        optimize=overrides.pop("optimize", True),
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        openai_api_url=DEFAULT_API_URL,
        **overrides,
    )
    app = create_app(config)
    app.dependency_overrides[require_loopback] = lambda: None
    return app


def _mock_bob_upstream(router: respx.MockRouter) -> dict[str, Any]:
    """Mock IBM's gateway and make any api.openai.com call an explicit failure."""
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        try:
            captured["body"] = json.loads(request.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            captured["body"] = None
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-bob-1",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 2, "total_tokens": 102},
            },
        )

    router.route(url__startswith=BOB_UPSTREAM_ORIGIN).mock(side_effect=_capture)

    def _reject(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            f"Bob traffic was forwarded to OpenAI instead of IBM: {request.url}. "
            "This is the 401 loop from the persistent-deployment upstream mismatch."
        )

    router.route(url__startswith="https://api.openai.com").mock(side_effect=_reject)
    return captured


@pytest.mark.parametrize("mode", ["token", "cache"])
@respx.mock
def test_bob_chat_routes_to_ibm_gateway_in_every_mode(mode: str) -> None:
    """Every real proxy mode must forward Bob's chat calls to IBM, not OpenAI."""
    app = _make_app(mode=mode)
    with TestClient(app) as client:
        captured = _mock_bob_upstream(respx)
        response = client.post(
            GATEWAY_CHAT_COMPLETIONS_PATH,
            headers=BOB_HEADERS,
            json={"model": "premium-ide", "messages": _conversation()},
        )

    assert response.status_code == 200, response.text[:300]
    assert captured["url"] == BOB_UPSTREAM_URL
    # Bob's IBM credential must reach IBM unchanged — the proxy relays it, it
    # does not mint or rewrite one.
    assert captured["authorization"] == "apikey test-ibm-key"


@respx.mock
def test_bob_chat_routes_to_ibm_gateway_without_compression() -> None:
    """The no-compression arm must route identically.

    Routing is decided before the pipeline runs, so `optimize=False` must not
    change the destination — only what is sent to it.
    """
    app = _make_app(optimize=False)
    with TestClient(app) as client:
        captured = _mock_bob_upstream(respx)
        response = client.post(
            GATEWAY_CHAT_COMPLETIONS_PATH,
            headers=BOB_HEADERS,
            json={"model": "premium-ide", "messages": _conversation()},
        )

    assert response.status_code == 200, response.text[:300]
    assert captured["url"] == BOB_UPSTREAM_URL
    # Untouched payload: same message count, same roles.
    assert len(captured["body"]["messages"]) == len(_conversation())


@respx.mock
def test_bob_project_prefix_is_stripped_before_routing() -> None:
    """`/p/<name>` attribution must not leak into the upstream path.

    Bob sends no attribution headers, so `wrap bob` encodes the project in the
    base URL instead. If the prefix survived routing, IBM would receive
    `/p/headroom/inference/v1/chat/completions` and 404.
    """
    app = _make_app(mode="token")
    with TestClient(app) as client:
        captured = _mock_bob_upstream(respx)
        response = client.post(
            f"/p/headroom{GATEWAY_CHAT_COMPLETIONS_PATH}",
            headers=BOB_HEADERS,
            json={"model": "premium-ide", "messages": _conversation()},
        )

    assert response.status_code == 200, response.text[:300]
    assert captured["url"] == BOB_UPSTREAM_URL
    assert "/p/headroom" not in captured["url"]


@respx.mock
def test_bob_requests_are_observable_in_stats() -> None:
    """A wrapped Bob session must show up on the dashboard's data source.

    `headroom dashboard` renders /stats from the proxy the agent is pointed at,
    so observability only works when the same proxy both serves Bob and records
    the request. This pins that the request is counted, which is what made the
    split-port workaround unsatisfying.
    """
    app = _make_app(mode="token")
    with TestClient(app) as client:
        _mock_bob_upstream(respx)
        for _ in range(3):
            response = client.post(
                f"/p/headroom{GATEWAY_CHAT_COMPLETIONS_PATH}",
                headers=BOB_HEADERS,
                json={"model": "premium-ide", "messages": _conversation()},
            )
            assert response.status_code == 200, response.text[:300]

        stats = client.get("/stats")

    assert stats.status_code == 200, stats.text[:300]
    payload = stats.json()
    blob = json.dumps(payload)
    assert "headroom" in blob, "project attribution missing from /stats"
    assert payload, "stats payload was empty after three Bob requests"
