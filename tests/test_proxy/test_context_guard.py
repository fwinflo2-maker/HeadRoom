"""Tests for the context-limit guard (headroom/proxy/context_guard.py)."""

from __future__ import annotations

import json

import pytest

from headroom.proxy.context_guard import (
    REPORT_FRACTION,
    MessageStartGuard,
    believed_context_limit,
    context_guard_enabled,
    effective_context_limit,
    has_context_1m_beta,
    note_prompt_too_long,
    reset_learned_limits,
)


@pytest.fixture(autouse=True)
def _clean_learned_limits():
    reset_learned_limits()
    yield
    reset_learned_limits()


def _message_start_event(
    input_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> bytes:
    payload = {
        "type": "message_start",
        "message": {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": [],
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
                "output_tokens": 1,
            },
        },
    }
    return b"event: message_start\ndata: " + json.dumps(payload).encode() + b"\n\n"


def _parse_usage(event_bytes: bytes) -> dict:
    for line in event_bytes.split(b"\n"):
        if line.startswith(b"data:"):
            return json.loads(line[5:].strip())["message"]["usage"]
    raise AssertionError("no data line in event")


class TestBetaAndLimits:
    def test_has_context_1m_beta_detects_token(self):
        assert has_context_1m_beta("context-1m-2025-08-07")
        assert has_context_1m_beta("oauth-2025-04-20, context-1m-2025-08-07")
        assert not has_context_1m_beta("oauth-2025-04-20")
        assert not has_context_1m_beta(None)
        assert not has_context_1m_beta("")

    def test_believed_limit_raised_by_1m_beta(self):
        assert believed_context_limit(200_000, "context-1m-2025-08-07") == 1_000_000
        assert believed_context_limit(200_000, "oauth-2025-04-20") == 200_000

    def test_believed_limit_never_lowered(self):
        assert believed_context_limit(2_000_000, "context-1m-2025-08-07") == 2_000_000

    def test_effective_limit_optimistic_until_learned(self):
        beta = "context-1m-2025-08-07"
        assert effective_context_limit("claude-sonnet-4-5", 200_000, beta) == 1_000_000

    def test_learned_limit_caps_effective(self):
        beta = "context-1m-2025-08-07"
        learned = note_prompt_too_long(
            "claude-sonnet-4-5",
            beta,
            '{"error": {"message": "prompt is too long: 213021 tokens > 200000 maximum"}}',
        )
        assert learned == 200_000
        assert effective_context_limit("claude-sonnet-4-5", 200_000, beta) == 200_000

    def test_learned_limit_keyed_by_beta_presence(self):
        # A limit learned WITHOUT the 1m beta must not clamp sessions that
        # send it (their account may have real 1M access).
        note_prompt_too_long(
            "claude-sonnet-4-5",
            None,
            "prompt is too long: 201000 tokens > 200000 maximum",
        )
        assert (
            effective_context_limit("claude-sonnet-4-5", 200_000, "context-1m-2025-08-07")
            == 1_000_000
        )

    def test_note_prompt_too_long_ignores_other_errors(self):
        assert note_prompt_too_long("m", None, "rate limit exceeded") is None
        assert note_prompt_too_long("m", None, b"") is None

    def test_note_prompt_too_long_accepts_bytes(self):
        assert (
            note_prompt_too_long("m", None, b"prompt is too long: 250000 tokens > 200000 maximum")
            == 200_000
        )

    def test_enabled_by_default_and_kill_switch(self, monkeypatch):
        monkeypatch.delenv("HEADROOM_CONTEXT_GUARD", raising=False)
        assert context_guard_enabled()
        monkeypatch.setenv("HEADROOM_CONTEXT_GUARD", "0")
        assert not context_guard_enabled()
        monkeypatch.setenv("HEADROOM_CONTEXT_GUARD", "false")
        assert not context_guard_enabled()


class TestMessageStartGuard:
    def test_below_trigger_passes_through_byte_identical(self):
        guard = MessageStartGuard(believed_limit=200_000, effective_limit=200_000)
        event = _message_start_event(50_000, cache_read=100_000)
        assert guard.feed(event) == event
        # Inert afterwards: later chunks untouched even if they look like events.
        tail = b"event: content_block_delta\ndata: {}\n\n"
        assert guard.feed(tail) == tail

    def test_above_trigger_inflates_to_report_fraction_of_believed(self):
        guard = MessageStartGuard(believed_limit=1_000_000, effective_limit=200_000)
        # 185k total forwarded = 92.5% of the real 200k window.
        event = _message_start_event(5_000, cache_read=170_000, cache_creation=10_000)
        out = guard.feed(event)
        usage = _parse_usage(out)
        target_total = int(1_000_000 * REPORT_FRACTION)
        assert (
            usage["input_tokens"]
            + usage["cache_read_input_tokens"]
            + usage["cache_creation_input_tokens"]
            == target_total
        )
        # Cache components are never touched — only input_tokens absorbs the nudge.
        assert usage["cache_read_input_tokens"] == 170_000
        assert usage["cache_creation_input_tokens"] == 10_000
        assert usage["output_tokens"] == 1

    def test_same_limits_nudges_gauge_over_compact_threshold(self):
        guard = MessageStartGuard(believed_limit=200_000, effective_limit=200_000)
        event = _message_start_event(185_000)
        usage = _parse_usage(guard.feed(event))
        assert usage["input_tokens"] == int(200_000 * REPORT_FRACTION)

    def test_never_deflates(self):
        guard = MessageStartGuard(believed_limit=200_000, effective_limit=200_000)
        event = _message_start_event(199_000)  # above the 95% report target
        assert guard.feed(event) == event

    def test_split_chunks_across_event_boundary(self):
        guard = MessageStartGuard(believed_limit=1_000_000, effective_limit=200_000)
        event = _message_start_event(190_000)
        first, second = event[:40], event[40:]
        assert guard.feed(first) == b""  # held back: no complete event yet
        out = guard.feed(second)
        assert _parse_usage(out)["input_tokens"] == int(1_000_000 * REPORT_FRACTION)

    def test_ping_events_pass_through_before_message_start(self):
        guard = MessageStartGuard(believed_limit=1_000_000, effective_limit=200_000)
        ping = b'event: ping\ndata: {"type": "ping"}\n\n'
        assert guard.feed(ping) == ping
        out = guard.feed(_message_start_event(190_000))
        assert _parse_usage(out)["input_tokens"] == int(1_000_000 * REPORT_FRACTION)

    def test_non_message_start_first_event_disarms(self):
        guard = MessageStartGuard(believed_limit=1_000_000, effective_limit=200_000)
        error_event = b'event: error\ndata: {"type": "error"}\n\n'
        assert guard.feed(error_event) == error_event
        # Even a later message_start is untouched (protocol says it is first).
        event = _message_start_event(190_000)
        assert guard.feed(event) == event

    def test_malformed_json_passes_through(self):
        guard = MessageStartGuard(believed_limit=1_000_000, effective_limit=200_000)
        event = b"event: message_start\ndata: {not json\n\n"
        assert guard.feed(event) == event

    def test_buffer_cap_flushes_verbatim(self):
        guard = MessageStartGuard(believed_limit=1_000_000, effective_limit=200_000)
        blob = b"x" * (300 * 1024)  # no event boundary anywhere
        out = guard.feed(blob)
        assert out == blob
        assert guard.feed(b"more") == b"more"  # inert afterwards

    def test_flush_returns_held_bytes(self):
        guard = MessageStartGuard(believed_limit=1_000_000, effective_limit=200_000)
        partial = b"event: message_start\ndata: {"
        assert guard.feed(partial) == b""
        assert guard.flush() == partial

    def test_unusable_limits_make_guard_inert(self):
        guard = MessageStartGuard(believed_limit=0, effective_limit=200_000)
        event = _message_start_event(190_000)
        assert guard.feed(event) == event

    def test_rewritten_event_is_valid_sse(self):
        guard = MessageStartGuard(believed_limit=200_000, effective_limit=200_000)
        out = guard.feed(_message_start_event(185_000))
        assert out.startswith(b"event: message_start\n")
        assert out.endswith(b"\n\n")
        payload = _parse_usage(out)  # parses => data line is intact JSON
        assert payload["input_tokens"] > 185_000


class TestStreamResponseIntegration:
    """The guard wired through _stream_response with a mocked upstream."""

    def _create_mock_proxy(self):
        from unittest.mock import AsyncMock, MagicMock

        import httpx

        from headroom.proxy.server import HeadroomProxy

        proxy = object.__new__(HeadroomProxy)
        proxy.http_client = MagicMock(spec=httpx.AsyncClient)
        proxy.metrics = MagicMock()
        proxy.metrics.record_request = AsyncMock(return_value=None)
        proxy.metrics.record_failed = AsyncMock(return_value=None)
        proxy.cost_tracker = MagicMock()
        proxy.cost_tracker.estimate_cost.return_value = 0.001
        proxy.cost_tracker.record_request.return_value = None
        proxy.stats = {
            "requests_total": 0,
            "requests_optimized": 0,
            "tokens": {"original": 0, "optimized": 0, "saved": 0},
            "cost": {"total_usd": 0, "savings_usd": 0},
            "errors": 0,
            "active_requests": 0,
            "requests_per_model": {},
        }
        proxy.memory_manager = None
        proxy._config = MagicMock()
        proxy._config.memory_enabled = False
        proxy._config.ccr_inject_tool = False
        proxy._config.retry_max_attempts = 3
        proxy._config.retry_base_delay_ms = 0
        proxy._config.retry_max_delay_ms = 0
        proxy.config = proxy._config
        proxy._parse_sse_usage_from_buffer = MagicMock(return_value=None)
        proxy.memory_handler = None
        proxy.anthropic_provider = MagicMock()
        proxy.anthropic_provider.get_context_limit.return_value = 200_000
        return proxy

    def _mock_upstream(self, sse_bytes: bytes, status_code: int = 200):
        from unittest.mock import AsyncMock

        import httpx

        mock_response = AsyncMock()
        mock_response.headers = httpx.Headers({"content-type": "text/event-stream"})
        mock_response.status_code = status_code

        async def aiter_bytes():
            yield sse_bytes

        mock_response.aiter_bytes = aiter_bytes
        mock_response.aclose = AsyncMock()
        mock_response.aread = AsyncMock(return_value=sse_bytes)
        return mock_response

    async def _run(self, proxy, mock_response, headers):
        from unittest.mock import AsyncMock, MagicMock

        proxy.http_client.build_request = MagicMock(return_value=MagicMock())
        proxy.http_client.send = AsyncMock(return_value=mock_response)
        return await proxy._stream_response(
            url="https://api.anthropic.com/v1/messages",
            headers=headers,
            body={
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
            provider="anthropic",
            model="claude-sonnet-4-5",
            request_id="test-guard",
            original_tokens=10,
            optimized_tokens=10,
            tokens_saved=0,
            transforms_applied=[],
            tags={},
            optimization_latency=0.0,
        )

    @pytest.mark.asyncio
    async def test_near_limit_message_start_is_nudged_on_the_wire(self):
        proxy = self._create_mock_proxy()
        # 185k = above the 90% trigger, below the 95% report target — the
        # assertion below can only pass if the rewrite actually happened.
        upstream = self._mock_upstream(
            _message_start_event(185_000)
            + b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )
        result = await self._run(proxy, upstream, {"x-api-key": "sk-test"})
        client_bytes = b"".join([chunk async for chunk in result.body_iterator])
        usage = _parse_usage(client_bytes.split(b"\n\n")[0] + b"\n\n")
        assert usage["input_tokens"] == int(200_000 * REPORT_FRACTION)
        # The rest of the stream is untouched.
        assert b'event: message_stop\ndata: {"type":"message_stop"}\n\n' in client_bytes

    @pytest.mark.asyncio
    async def test_far_from_limit_stream_is_byte_identical(self):
        proxy = self._create_mock_proxy()
        sse = (
            _message_start_event(50_000) + b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )
        result = await self._run(proxy, self._mock_upstream(sse), {"x-api-key": "sk-test"})
        client_bytes = b"".join([chunk async for chunk in result.body_iterator])
        assert client_bytes == sse

    @pytest.mark.asyncio
    async def test_kill_switch_disables_nudge(self, monkeypatch):
        monkeypatch.setenv("HEADROOM_CONTEXT_GUARD", "0")
        proxy = self._create_mock_proxy()
        sse = _message_start_event(190_000)
        result = await self._run(proxy, self._mock_upstream(sse), {"x-api-key": "sk-test"})
        client_bytes = b"".join([chunk async for chunk in result.body_iterator])
        assert client_bytes == sse

    @pytest.mark.asyncio
    async def test_streaming_400_learns_real_limit(self):
        proxy = self._create_mock_proxy()
        error_body = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "prompt is too long: 213021 tokens > 200000 maximum",
                },
            }
        ).encode()
        upstream = self._mock_upstream(error_body, status_code=400)
        upstream.headers = __import__("httpx").Headers({"content-type": "application/json"})
        result = await self._run(
            proxy, upstream, {"x-api-key": "sk-test", "anthropic-beta": "context-1m-2025-08-07"}
        )
        assert result.status_code == 400
        assert (
            effective_context_limit("claude-sonnet-4-5", 200_000, "context-1m-2025-08-07")
            == 200_000
        )
