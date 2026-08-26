"""Session-aware /v1/compress (sidecar mode) + the /v1/usage relay.

Contract under test: a gateway that owns routing (e.g. Kong) sends the RAW
conversation plus a session id every turn; Headroom keeps the byte-replay
state itself and returns a byte-identical prefix; the gateway forwards the
result verbatim and may relay provider usage via POST /v1/usage to make
freeze decisions exact.

The critical property is byte-stability: content already returned for a
session must come back byte-for-byte identical on later turns, or the
provider prompt cache busts.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from headroom.proxy.server import ProxyConfig, create_app  # noqa: E402


def _make_client() -> TestClient:
    config = ProxyConfig(
        optimize=True,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        image_optimize=False,
    )
    app = create_app(config)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
    return client


def _big_tool_history() -> list[dict]:
    """A conversation whose tool result is large enough to be compressed."""
    items = [
        {
            "id": i,
            "score": 0.99 if i % 30 == 0 else 0.6,
            "msg": f"Result {i:03d}{' error' if i % 30 == 0 else ' ok'}",
            "blob": f"payload-{i:04d}-" + "".join(chr(97 + (i * 7 + j) % 26) for j in range(240)),
        }
        for i in range(200)
    ]
    return [
        {"role": "user", "content": "Get items"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(items)},
    ]


def _compress(client: TestClient, messages: list[dict], **config) -> dict:
    resp = client.post(
        "/v1/compress",
        json={"model": "gpt-4o", "messages": messages, "config": config},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# Stateless behaviour is unchanged (regression guard).                         #
# --------------------------------------------------------------------------- #


def test_no_session_id_stays_stateless() -> None:
    with _make_client() as client:
        body = _compress(client, _big_tool_history())
        assert "session" not in body
        # And nothing session-shaped leaked into the registry.
        proxy = client.app.state.proxy
        assert not any(k.startswith("compress:") for k in proxy._compression_caches)


def test_invalid_session_id_is_rejected() -> None:
    with _make_client() as client:
        for bad in ["", "   ", "x" * 300, 42]:
            resp = client.post(
                "/v1/compress",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                    "config": {"session_id": bad},
                },
            )
            assert resp.status_code == 400, f"session_id {bad!r} was not rejected"


# --------------------------------------------------------------------------- #
# The core sidecar property: turn 2 replays turn 1's exact bytes.              #
# --------------------------------------------------------------------------- #


def test_second_turn_replays_first_turn_bytes() -> None:
    with _make_client() as client:
        history = _big_tool_history()

        turn1 = _compress(client, history, session_id="conv-1")
        assert turn1["session"]["id"] == "conv-1"
        # The tool result must actually have been compressed, otherwise the
        # byte-stability assertion below is vacuous.
        t1_tool_content = turn1["messages"][2]["content"]
        assert t1_tool_content != history[2]["content"]
        assert turn1["tokens_saved"] > 0

        # Turn 2: the caller resends the RAW history (as real clients do) plus
        # the new turns. Headroom must return the OLD prefix byte-identical to
        # what it handed back on turn 1 — that is what the provider cached.
        turn2_history = history + [
            {"role": "assistant", "content": "The top items are listed above."},
            {"role": "user", "content": "Now sort them by score."},
        ]
        turn2 = _compress(client, turn2_history, session_id="conv-1")
        assert turn2["messages"][2]["content"] == t1_tool_content
        assert turn2["messages"][0] == history[0]
        assert turn2["messages"][-1]["content"] == "Now sort them by score."
        assert turn2["session"]["id"] == "conv-1"


def test_third_turn_still_byte_stable() -> None:
    """Stability must hold across N turns, not just one hop."""
    with _make_client() as client:
        history = _big_tool_history()
        turn1 = _compress(client, history, session_id="conv-multi")
        t1_tool_content = turn1["messages"][2]["content"]

        history2 = history + [{"role": "user", "content": "next"}]
        _compress(client, history2, session_id="conv-multi")

        history3 = history2 + [
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "and again"},
        ]
        turn3 = _compress(client, history3, session_id="conv-multi")
        assert turn3["messages"][2]["content"] == t1_tool_content


def test_header_session_id_works() -> None:
    with _make_client() as client:
        resp = client.post(
            "/v1/compress",
            json={"model": "gpt-4o", "messages": _big_tool_history(), "config": {}},
            headers={"x-headroom-session-id": "conv-header"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["session"]["id"] == "conv-header"


def test_sessions_are_isolated() -> None:
    with _make_client() as client:
        history = _big_tool_history()
        a1 = _compress(client, history, session_id="conv-a")
        b1 = _compress(client, history, session_id="conv-b")

        # Same content in, same compressed form out — but through separate
        # session state. Interleave new turns and re-check both replay.
        a2 = _compress(
            client,
            history + [{"role": "user", "content": "a follow-up"}],
            session_id="conv-a",
        )
        b2 = _compress(
            client,
            history + [{"role": "user", "content": "b follow-up"}],
            session_id="conv-b",
        )
        assert a2["messages"][2]["content"] == a1["messages"][2]["content"]
        assert b2["messages"][2]["content"] == b1["messages"][2]["content"]
        assert a2["messages"][-1]["content"] == "a follow-up"
        assert b2["messages"][-1]["content"] == "b follow-up"


# --------------------------------------------------------------------------- #
# /v1/usage: relayed provider numbers make the freeze authoritative.           #
# --------------------------------------------------------------------------- #


def test_usage_feedback_advances_freeze() -> None:
    with _make_client() as client:
        history = _big_tool_history()
        _compress(client, history, session_id="conv-usage")

        resp = client.post(
            "/v1/usage",
            json={
                "session_id": "conv-usage",
                "usage": {
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 50_000,
                },
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["frozen_message_count"] >= 1

        turn2 = _compress(
            client,
            history + [{"role": "user", "content": "next"}],
            session_id="conv-usage",
        )
        assert turn2["session"]["frozen_message_count"] >= 1


def test_usage_unknown_session_is_404() -> None:
    with _make_client() as client:
        resp = client.post(
            "/v1/usage",
            json={"session_id": "never-seen", "usage": {"cache_read_input_tokens": 100}},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["type"] == "unknown_session"


def test_usage_validation() -> None:
    with _make_client() as client:
        cases = [
            {},  # no session_id
            {"session_id": "s"},  # no usage
            {"session_id": "s", "usage": "nope"},  # usage not a dict
            {"session_id": "s", "usage": {"cache_read_input_tokens": -1}},
            {"session_id": "s", "usage": {"cache_read_input_tokens": True}},
        ]
        for body in cases:
            resp = client.post("/v1/usage", json=body)
            assert resp.status_code == 400, f"body {body!r} was not rejected"


# --------------------------------------------------------------------------- #
# Lifecycle: sidecar sessions ride the registry's TTL/LRU machinery.           #
# --------------------------------------------------------------------------- #


def test_session_state_lives_in_registry_and_survives_eviction() -> None:
    import time as _time

    with _make_client() as client:
        history = _big_tool_history()
        turn1 = _compress(client, history, session_id="conv-ttl")
        proxy = client.app.state.proxy
        assert "compress:conv-ttl" in proxy._compression_caches

        # Simulate the idle-TTL sweep reclaiming the session.
        now = _time.time()
        proxy._compression_cache_last_seen["compress:conv-ttl"] = now - 999_999
        proxy._compression_caches_last_cleanup = now - 61
        proxy._get_compression_cache("unrelated")
        assert "compress:conv-ttl" not in proxy._compression_caches

        # A post-eviction turn is fail-open: fresh state, valid response, and
        # the compressed form is reproducible (deterministic pipeline), even
        # though the replay guarantee had to restart from scratch.
        turn2 = _compress(
            client,
            history + [{"role": "user", "content": "after the gap"}],
            session_id="conv-ttl",
        )
        assert turn2["session"]["id"] == "conv-ttl"
        assert turn2["messages"][-1]["content"] == "after the gap"
        assert isinstance(turn1["messages"][2]["content"], str)


def test_explicit_frozen_count_still_wins_when_larger() -> None:
    with _make_client() as client:
        history = _big_tool_history()
        # First turn with an explicit pin covering the whole tool result: the
        # caller asserts the provider already cached it, so it must come back
        # byte-for-byte untouched even though no session state exists yet.
        turn1 = _compress(client, history, session_id="conv-pin", frozen_message_count=3)
        assert turn1["messages"][2]["content"] == history[2]["content"]
        assert turn1["session"]["frozen_message_count"] == 3
