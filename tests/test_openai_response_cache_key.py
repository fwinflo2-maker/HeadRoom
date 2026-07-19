"""Response-cache key drift on the OpenAI chat path (parity with #2124 / #327).

`handle_openai_chat` looks the response cache up with `messages` at `cache.get`,
then the `pre_compress` hook reassigns `messages` before `cache.set`. Caching the
response under the live (hooked) `messages` stores it under a key no future
lookup can produce, so the response cache never hits and fills with unreachable
entries. This is the OpenAI twin of the anthropic fix pinned by
``tests/test_anthropic_pre_upstream_backpressure.py`` (#2124).

The drift only fires when a message-rewriting ``pre_compress`` hook is configured
(a non-default deployment extension point); OSS default hooks are no-ops.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from headroom.hooks import CompressionHooks  # noqa: E402
from headroom.proxy import semantic_cache as semantic_cache_mod  # noqa: E402
from headroom.proxy import server as server_mod  # noqa: E402
from headroom.proxy.server import ProxyConfig, create_app  # noqa: E402


class _MutatingHooks(CompressionHooks):
    """A deployment-provided ``pre_compress`` hook that rewrites history (the
    kind of cross-turn dedup / memory injection / redaction the hook exists for).

    It returns a NEW list — the documented contract (``hooks.py`` "Modify and
    return") and what the handler relies on (``messages = pre_compress(...)``) —
    reproducing the get -> mutate -> set key drift.
    """

    def pre_compress(self, messages, ctx):
        return [dict(m, content="MUTATED") for m in messages]


class _ResponseStub:
    """Minimal stand-in for the upstream httpx response on the direct
    (non-backend) non-streaming OpenAI chat path."""

    status_code = 200
    headers = {"content-type": "application/json"}
    content = (
        b'{"id":"chatcmpl-1","object":"chat.completion",'
        b'"choices":[{"index":0,"message":{"role":"assistant","content":"hi"},'
        b'"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":5,"completion_tokens":1,"total_tokens":6}}'
    )

    def json(self) -> dict[str, Any]:
        return json.loads(self.content)


def test_openai_chat_response_cache_keys_on_lookup_messages_not_mutated(monkeypatch):
    """The OpenAI response must be cached under the same messages it was looked
    up by. ``messages`` is reassigned by the ``pre_compress`` hook after the
    ``cache.get``; caching under the live value stores the entry under a
    different key than it was read by, so the cache never hits.
    """
    captured: dict[str, Any] = {"get": None, "set": None}

    async def _recording_get(self, messages, model, *args, **kwargs):
        captured["get"] = copy.deepcopy(messages)
        return None  # force a miss so the upstream response gets cached

    async def _recording_set(self, messages, model, *args, **kwargs):
        captured["set"] = copy.deepcopy(messages)

    async def _fake_retry_request(self, method, url, headers, body, *args, **kwargs):
        return _ResponseStub()

    monkeypatch.setattr(semantic_cache_mod.SemanticCache, "get", _recording_get)
    monkeypatch.setattr(semantic_cache_mod.SemanticCache, "set", _recording_set)
    monkeypatch.setattr(server_mod.HeadroomProxy, "_retry_request", _fake_retry_request)

    config = ProxyConfig(
        optimize=False,
        cache_enabled=True,
        rate_limit_enabled=False,
        hooks=_MutatingHooks(),
    )

    app = create_app(config)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]},
            headers={"Authorization": "Bearer sk-test"},
        )
        assert resp.status_code == 200, resp.text[:300]

    assert captured["get"] is not None, "cache.get was not called"
    assert captured["set"] is not None, "cache.set was not called"
    # Same messages at get and set -> same key -> the cache can actually hit.
    assert captured["set"] == captured["get"]
    # And specifically the raw lookup messages, not the hook's rewrite.
    assert captured["set"][0]["content"] == "hello"
