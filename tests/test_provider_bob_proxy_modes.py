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


# Deliberately not "headroom": that is a real project in this repo's own
# savings history, so a test using it would read another session's counters.
STATS_PROJECT = "bob-stats-attribution-test"


def _recorded_requests(client: TestClient, project: str) -> int:
    """Requests /stats attributes to ``project`` right now."""
    stats = client.get("/stats")
    assert stats.status_code == 200, stats.text[:300]
    row = (stats.json().get("savings", {}).get("per_project", {}) or {}).get(project, {})
    return int(row.get("requests", 0))


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


def _assert_reached_ibm(captured: dict[str, Any]) -> None:
    """Assert the request reached IBM and nothing reached OpenAI.

    Checked by recording rather than by raising inside the mock: the proxy
    catches upstream exceptions and forwards them as an error response
    ("Forwarding upstream streaming error" in the real logs), so an
    ``AssertionError`` raised from a side_effect is swallowed on the streaming
    path and never reaches the test.
    """
    assert not captured["openai_hits"], (
        f"Bob traffic was forwarded to OpenAI instead of IBM: {captured['openai_hits']}. "
        "This is the 401 loop from the persistent-deployment upstream mismatch."
    )
    assert captured.get("url") == BOB_UPSTREAM_URL, (
        f"expected upstream {BOB_UPSTREAM_URL}, got {captured.get('url')!r}"
    )


def _record_openai_hits(router: respx.MockRouter, captured: dict[str, Any]) -> None:
    """Record any api.openai.com call so `_assert_reached_ibm` can report it."""
    captured["openai_hits"] = []

    def _record(request: httpx.Request) -> httpx.Response:
        captured["openai_hits"].append(str(request.url))
        return httpx.Response(401, json={"error": {"message": "no api key"}})

    router.route(url__startswith="https://api.openai.com").mock(side_effect=_record)


def _mock_bob_upstream(router: respx.MockRouter) -> dict[str, Any]:
    """Mock IBM's gateway and record any api.openai.com call as a failure."""
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
    _record_openai_hits(router, captured)
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
    _assert_reached_ibm(captured)
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
    _assert_reached_ibm(captured)
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
    _assert_reached_ibm(captured)
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
        before = _recorded_requests(client, STATS_PROJECT)
        for _ in range(3):
            response = client.post(
                f"/p/{STATS_PROJECT}{GATEWAY_CHAT_COMPLETIONS_PATH}",
                headers=BOB_HEADERS,
                json={"model": "premium-ide", "messages": _conversation()},
            )
            assert response.status_code == 200, response.text[:300]

        # Assert a *delta on the project key*, not a substring of the whole
        # blob. `savings.per_project` is persisted to
        # ~/.headroom/proxy_savings.json and survives across runs, so an
        # absolute count can pass on stale data from an earlier run. And a
        # substring check is vacuous: "headroom" appears in service_name,
        # without_headroom_usd, and the savings file path whether or not
        # attribution works.
        after = _recorded_requests(client, STATS_PROJECT)

    assert after - before == 3, f"expected 3 attributed requests, got {after - before}"


# ── Streaming ────────────────────────────────────────────────────────────────
#
# Bob streams in production: the 401s that motivated these tests were logged as
# "Forwarding upstream streaming error status=401". Every test above uses a
# non-streaming upstream, so the path Bob actually takes was untested.

SSE_CHUNKS = (
    'data: {"id":"c1","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"re"}}]}\n\n',
    'data: {"id":"c1","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{"content":"ady"},"finish_reason":"stop"}]}\n\n',
    'data: {"id":"c1","object":"chat.completion.chunk","choices":[],'
    '"usage":{"prompt_tokens":900,"completion_tokens":2,"total_tokens":902}}\n\n',
    "data: [DONE]\n\n",
)


def _mock_bob_sse_upstream(router: respx.MockRouter) -> dict[str, Any]:
    """Mock IBM's gateway with an SSE response; any OpenAI call fails the test."""
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(SSE_CHUNKS).encode(),
        )

    router.route(url__startswith=BOB_UPSTREAM_ORIGIN).mock(side_effect=_capture)
    _record_openai_hits(router, captured)
    return captured


@pytest.mark.parametrize("mode", ["token", "cache"])
@respx.mock
def test_bob_streaming_routes_to_ibm_gateway(mode: str) -> None:
    """A streamed Bob request must reach IBM, in every mode.

    Routing is decided per request, so a proxy correct for non-streaming is not
    automatically correct here — and this is the shape Bob actually sends.
    """
    app = _make_app(mode=mode)
    with TestClient(app) as client:
        captured = _mock_bob_sse_upstream(respx)
        response = client.post(
            GATEWAY_CHAT_COMPLETIONS_PATH,
            headers=BOB_HEADERS,
            json={"model": "premium-ide", "stream": True, "messages": _conversation()},
        )

    assert response.status_code == 200, response.text[:300]
    _assert_reached_ibm(captured)
    assert captured["authorization"] == "apikey test-ibm-key"
    # The proxy must not quietly downgrade the request to non-streaming: Bob
    # renders incrementally and would hang waiting for chunks that never come.
    assert captured["body"]["stream"] is True


@respx.mock
def test_bob_streaming_relays_sse_response_intact() -> None:
    """Every upstream SSE chunk must reach Bob unaltered, terminator included.

    A dropped or reordered chunk shows up as truncated output rather than an
    error, and a missing `[DONE]` leaves the client waiting on a finished
    stream.
    """
    app = _make_app(mode="token")
    with TestClient(app) as client:
        _mock_bob_sse_upstream(respx)
        response = client.post(
            GATEWAY_CHAT_COMPLETIONS_PATH,
            headers=BOB_HEADERS,
            json={"model": "premium-ide", "stream": True, "messages": _conversation()},
        )

    assert response.status_code == 200, response.text[:300]
    assert response.headers["content-type"].startswith("text/event-stream")

    body = response.text
    for chunk in SSE_CHUNKS:
        assert chunk in body, f"missing SSE chunk: {chunk[:60]}"
    assert body.rstrip().endswith("data: [DONE]"), body[-80:]

    # Reassembled deltas must spell the upstream content, in order.
    deltas = [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ") and not line.endswith("[DONE]")
    ]
    text = "".join(
        choice["delta"].get("content", "")
        for chunk in deltas
        for choice in chunk.get("choices", [])
    )
    assert text == "ready", text


@respx.mock
def test_bob_streaming_strips_project_prefix() -> None:
    """`/p/<name>` must be stripped on the streaming path too.

    The prefix is removed in ASGI scope before routing, which is shared with
    the non-streaming path — this pins that streaming does not bypass it.
    """
    app = _make_app(mode="token")
    with TestClient(app) as client:
        captured = _mock_bob_sse_upstream(respx)
        response = client.post(
            f"/p/{STATS_PROJECT}{GATEWAY_CHAT_COMPLETIONS_PATH}",
            headers=BOB_HEADERS,
            json={"model": "premium-ide", "stream": True, "messages": _conversation()},
        )

    assert response.status_code == 200, response.text[:300]
    _assert_reached_ibm(captured)
    assert "/p/" not in captured["url"]
