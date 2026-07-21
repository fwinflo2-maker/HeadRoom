"""Tests for the /v1/compress endpoint in the proxy server.

These tests verify that the compression-only endpoint works correctly
for the TypeScript SDK and other HTTP clients.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

# Skip if fastapi not available
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from headroom.proxy.server import ProxyConfig, create_app


@pytest.fixture
def client():
    """Create test client with optimization enabled."""
    config = ProxyConfig(
        optimize=True,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
    )
    app = create_app(config)
    # /v1/compress is loopback-gated (#1227).
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as c:
        yield c


@pytest.fixture
def client_no_optimize():
    """Create test client with optimization disabled."""
    config = ProxyConfig(
        optimize=False,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
    )
    app = create_app(config)
    # /v1/compress is loopback-gated (#1227).
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as c:
        yield c


class TestCompressEndpointValidation:
    """Test request validation for /v1/compress."""

    def test_missing_messages_returns_400(self, client):
        """Request without messages field should return 400."""
        response = client.post("/v1/compress", json={"model": "gpt-4"})
        assert response.status_code == 400
        data = response.json()
        assert "error" in data
        assert data["error"]["type"] == "invalid_request"
        assert "messages" in data["error"]["message"]

    def test_missing_model_returns_400(self, client):
        """Request without model field should return 400."""
        response = client.post(
            "/v1/compress",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
        assert response.status_code == 400
        data = response.json()
        assert "error" in data
        assert data["error"]["type"] == "invalid_request"
        assert "model" in data["error"]["message"]

    def test_invalid_json_returns_400(self, client):
        """Request with invalid JSON should return 400."""
        response = client.post(
            "/v1/compress",
            content=b"not valid json",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400
        data = response.json()
        assert data["error"]["type"] == "invalid_request"


class TestCompressEndpointBasic:
    """Test basic compress endpoint behavior."""

    def test_empty_messages_returns_empty(self, client):
        """Empty messages list should return as-is with zero metrics."""
        response = client.post(
            "/v1/compress",
            json={"messages": [], "model": "gpt-4"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["messages"] == []
        assert data["tokens_before"] == 0
        assert data["tokens_after"] == 0
        assert data["tokens_saved"] == 0
        assert data["compression_ratio"] == 1.0
        assert data["transforms_applied"] == []
        assert data["ccr_hashes"] == []

    def test_basic_compression_response_shape(self, client):
        """Verify the response contains all expected fields."""
        response = client.post(
            "/v1/compress",
            json={
                "messages": [{"role": "user", "content": "Hello, world!"}],
                "model": "gpt-4",
            },
        )
        assert response.status_code == 200
        data = response.json()

        # Check all expected fields are present
        assert "messages" in data
        assert "tokens_before" in data
        assert "tokens_after" in data
        assert "tokens_saved" in data
        assert "compression_ratio" in data
        assert "transforms_applied" in data
        assert "ccr_hashes" in data

        # Messages should be a list
        assert isinstance(data["messages"], list)
        assert len(data["messages"]) >= 1

        # Numeric fields should be non-negative
        assert data["tokens_before"] >= 0
        assert data["tokens_after"] >= 0
        assert data["tokens_saved"] >= 0
        assert data["compression_ratio"] > 0

    def test_bypass_header_returns_uncompressed(self, client):
        """X-Headroom-Bypass header should skip compression."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"},
        ]
        response = client.post(
            "/v1/compress",
            json={"messages": messages, "model": "gpt-4"},
            headers={"x-headroom-bypass": "true"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["messages"] == messages
        assert data["tokens_before"] == 0
        assert data["tokens_after"] == 0
        assert data["tokens_saved"] == 0
        assert data["compression_ratio"] == 1.0
        assert data["transforms_applied"] == []
        assert data["ccr_hashes"] == []

    def test_bypass_header_case_insensitive(self, client):
        """Bypass header should be case-insensitive."""
        messages = [{"role": "user", "content": "Hello"}]
        response = client.post(
            "/v1/compress",
            json={"messages": messages, "model": "gpt-4"},
            headers={"x-headroom-bypass": "TRUE"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["messages"] == messages


class TestCompressEndpointCompression:
    """Test that actual compression happens for large content."""

    def test_large_tool_output_gets_compressed(self, client):
        """Large tool output content should result in tokens_saved > 0."""
        # Create a large repetitive tool output that should be compressible
        large_data = json.dumps(
            [
                {
                    "id": i,
                    "name": f"Item {i}",
                    "description": f"This is a detailed description for item number {i}. "
                    f"It contains various attributes and metadata that are typical "
                    f"of API responses. The item has a status of active and was "
                    f"created on 2024-01-{(i % 28) + 1:02d}. Additional fields "
                    f"include category=electronics, price={i * 10.99:.2f}, "
                    f"rating={4.0 + (i % 10) / 10:.1f}, stock={i * 5}.",
                    "tags": ["electronics", "sale", "featured", "new-arrival"],
                    "metadata": {
                        "created_by": "system",
                        "updated_at": "2024-01-15T00:00:00Z",
                        "version": i,
                        "source": "api",
                    },
                }
                for i in range(200)
            ]
        )

        messages = [
            {"role": "user", "content": "What items are available?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "list_items",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_123",
                "content": large_data,
            },
            {"role": "user", "content": "Summarize the first 5 items."},
        ]

        response = client.post(
            "/v1/compress",
            json={"messages": messages, "model": "gpt-4"},
        )
        assert response.status_code == 200
        data = response.json()

        # With a large tool output, the pipeline should process successfully
        assert data["tokens_before"] > 0
        assert data["tokens_after"] > 0
        assert data["tokens_after"] <= data["tokens_before"]
        assert data["tokens_saved"] == data["tokens_before"] - data["tokens_after"]
        assert 0 < data["compression_ratio"] <= 1.0
        assert isinstance(data["transforms_applied"], list)

    def test_small_content_may_not_compress(self, client):
        """Small messages may not get compressed but should still work."""
        response = client.post(
            "/v1/compress",
            json={
                "messages": [{"role": "user", "content": "Hi"}],
                "model": "gpt-4",
            },
        )
        assert response.status_code == 200
        data = response.json()
        # Should still return valid response regardless of compression
        assert data["tokens_before"] >= 0
        assert data["tokens_after"] >= 0
        assert isinstance(data["transforms_applied"], list)

    def test_success_records_request_outcome(self, client, monkeypatch):
        """A completed compression should update request metrics."""
        proxy = client.app.state.proxy
        result = SimpleNamespace(
            messages=[{"role": "user", "content": "compressed"}],
            tokens_before=12,
            tokens_after=7,
            transforms_applied=["test_transform"],
            transforms_summary={"test_transform": 1},
            markers_inserted=[],
        )
        run_compression = AsyncMock(return_value=result)
        record_outcome = AsyncMock()
        monkeypatch.setattr(proxy, "_run_compression_in_executor", run_compression)
        monkeypatch.setattr(proxy, "_record_request_outcome", record_outcome)

        response = client.post(
            "/v1/compress",
            json={
                "messages": [{"role": "user", "content": "compress me"}],
                "model": "gpt-4",
            },
        )

        assert response.status_code == 200
        record_outcome.assert_awaited_once()
        outcome = record_outcome.await_args.args[0]
        assert outcome.provider == "compress"
        assert outcome.model == "gpt-4"
        assert outcome.original_tokens == 12
        assert outcome.optimized_tokens == 7
        assert outcome.tokens_saved == 5
        assert outcome.attempted_input_tokens == 12
        assert outcome.num_messages == 1
        assert outcome.transforms_applied == ("test_transform",)
        assert outcome.total_latency_ms >= 0

    def test_compression_error_records_failed_request(self, client, monkeypatch):
        """A hard compression failure should increment failed metrics."""
        proxy = client.app.state.proxy
        run_compression = AsyncMock(side_effect=RuntimeError("compression broke"))
        record_failed = AsyncMock()
        monkeypatch.setattr(proxy, "_run_compression_in_executor", run_compression)
        monkeypatch.setattr(proxy.metrics, "record_failed", record_failed)

        response = client.post(
            "/v1/compress",
            json={
                "messages": [{"role": "user", "content": "compress me"}],
                "model": "gpt-4",
            },
        )

        assert response.status_code == 503
        assert response.json() == {
            "error": {
                "type": "compression_error",
                "message": "compression broke",
            }
        }
        record_failed.assert_awaited_once_with(provider="compress")

    def test_compression_timeout_records_fail_open_outcome(self, client, monkeypatch):
        """A timeout should count as a zero-savings completed request."""
        proxy = client.app.state.proxy
        run_compression = AsyncMock(side_effect=TimeoutError)
        record_outcome = AsyncMock()
        record_compression_failed = Mock()
        monkeypatch.setattr(proxy, "_run_compression_in_executor", run_compression)
        monkeypatch.setattr(proxy, "_record_request_outcome", record_outcome)
        monkeypatch.setattr(
            proxy.metrics,
            "record_compression_failed",
            record_compression_failed,
        )
        messages = [{"role": "user", "content": "compress me"}]

        response = client.post(
            "/v1/compress",
            json={"messages": messages, "model": "gpt-4"},
        )

        assert response.status_code == 200
        assert response.json()["messages"] == messages
        assert response.json()["skip_reason"] == "compression_timeout"
        record_compression_failed.assert_called_once_with("timeout")
        record_outcome.assert_awaited_once()
        outcome = record_outcome.await_args.args[0]
        assert outcome.provider == "compress"
        assert outcome.model == "gpt-4"
        assert outcome.original_tokens == 0
        assert outcome.optimized_tokens == 0
        assert outcome.tokens_saved == 0


class TestCompressEndpointConfig:
    """Per-request CompressConfig on /v1/compress.

    The ``config`` body object exposes the user-facing CompressConfig
    knobs over HTTP. Unknown keys and mistyped values are a 400 — never
    a silent no-op — so callers can't believe an option took effect
    when it didn't.
    """

    @staticmethod
    def _recording_apply(proxy, monkeypatch):
        """Patch the pipeline to record the kwargs apply() receives."""
        captured: dict = {}

        def fake_apply(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                messages=kwargs["messages"],
                tokens_before=10,
                tokens_after=10,
                transforms_applied=[],
                transforms_summary={},
                markers_inserted=[],
            )

        monkeypatch.setattr(proxy.openai_pipeline, "apply", fake_apply)
        return captured

    def test_all_config_knobs_reach_pipeline(self, client, monkeypatch):
        """Every documented config field must be forwarded to apply()."""
        captured = self._recording_apply(client.app.state.proxy, monkeypatch)

        response = client.post(
            "/v1/compress",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "model": "gpt-4",
                "config": {
                    "compress_user_messages": True,
                    "compress_system_messages": False,
                    "protect_recent": 2,
                    "protect_analysis_context": False,
                    "frozen_message_count": 3,
                    "min_tokens_to_compress": 100,
                    "target_ratio": 0.5,
                },
            },
        )

        assert response.status_code == 200
        assert captured["compress_user_messages"] is True
        assert captured["compress_system_messages"] is False
        assert captured["protect_recent"] == 2
        assert captured["protect_analysis_context"] is False
        assert captured["frozen_message_count"] == 3
        assert captured["min_tokens_to_compress"] == 100
        assert captured["target_ratio"] == 0.5

    def test_omitted_config_uses_server_defaults(self, client, monkeypatch):
        """No config object → apply() sees only server-level kwargs."""
        captured = self._recording_apply(client.app.state.proxy, monkeypatch)

        response = client.post(
            "/v1/compress",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "model": "gpt-4",
            },
        )

        assert response.status_code == 200
        # None of the request-level keys were sent, so none may appear
        # unless the server config/profile set them.
        assert "frozen_message_count" not in captured

    def test_partial_config_only_sends_given_keys(self, client, monkeypatch):
        """Keys the caller didn't send must not be forced onto apply()."""
        captured = self._recording_apply(client.app.state.proxy, monkeypatch)

        response = client.post(
            "/v1/compress",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "model": "gpt-4",
                "config": {"protect_recent": 0},
            },
        )

        assert response.status_code == 200
        assert captured["protect_recent"] == 0
        assert "min_tokens_to_compress" not in captured or captured["min_tokens_to_compress"] != 0

    def test_unknown_config_key_returns_400(self, client):
        response = client.post(
            "/v1/compress",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "model": "gpt-4",
                "config": {"tarrget_ratio": 0.5},
            },
        )
        assert response.status_code == 400
        data = response.json()
        assert data["error"]["type"] == "invalid_request"
        assert "tarrget_ratio" in data["error"]["message"]
        # The error is actionable: it lists the allowed keys.
        assert "target_ratio" in data["error"]["message"]

    def test_config_must_be_object(self, client):
        response = client.post(
            "/v1/compress",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "model": "gpt-4",
                "config": "aggressive",
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request"

    @pytest.mark.parametrize(
        "key,value",
        [
            ("compress_user_messages", "yes"),
            ("compress_system_messages", 1),
            ("protect_analysis_context", "true"),
        ],
    )
    def test_bool_fields_reject_non_bool(self, client, key, value):
        response = client.post(
            "/v1/compress",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "model": "gpt-4",
                "config": {key: value},
            },
        )
        assert response.status_code == 400
        assert key in response.json()["error"]["message"]

    @pytest.mark.parametrize(
        "key,value",
        [
            ("protect_recent", -1),
            ("protect_recent", "4"),
            ("frozen_message_count", 1.5),
            ("frozen_message_count", True),
            ("min_tokens_to_compress", -250),
        ],
    )
    def test_int_fields_reject_invalid(self, client, key, value):
        response = client.post(
            "/v1/compress",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "model": "gpt-4",
                "config": {key: value},
            },
        )
        assert response.status_code == 400
        assert key in response.json()["error"]["message"]

    @pytest.mark.parametrize("value", [0, 0.0, -0.5, 1.5, "0.5", True])
    def test_target_ratio_rejects_out_of_range(self, client, value):
        response = client.post(
            "/v1/compress",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "model": "gpt-4",
                "config": {"target_ratio": value},
            },
        )
        assert response.status_code == 400
        assert "target_ratio" in response.json()["error"]["message"]

    def test_target_ratio_accepts_boundary_one(self, client, monkeypatch):
        """1.0 (keep everything) is a legal keep-ratio."""
        captured = self._recording_apply(client.app.state.proxy, monkeypatch)

        response = client.post(
            "/v1/compress",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "model": "gpt-4",
                "config": {"target_ratio": 1.0},
            },
        )
        assert response.status_code == 200
        assert captured["target_ratio"] == 1.0

    def test_request_config_overrides_server_profile(self, monkeypatch):
        """Request-level config must win over HEADROOM_SAVINGS_PROFILE knobs."""
        config = ProxyConfig(
            optimize=True,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
            savings_profile="agent-90",
        )
        app = create_app(config)
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
            captured = self._recording_apply(app.state.proxy, monkeypatch)

            response = client.post(
                "/v1/compress",
                json={
                    "messages": [{"role": "user", "content": "hello"}],
                    "model": "gpt-4",
                    "config": {"protect_recent": 9, "compress_user_messages": False},
                },
            )

            assert response.status_code == 200
            assert captured["protect_recent"] == 9
            assert captured["compress_user_messages"] is False


class TestCompressEndpointLossyInlineMode:
    """config.mode="lossy_inline" must compress losslessly-then-lossily but emit
    NO CCR marker / retrieval round-trip, so the output is safe to forward
    straight to a provider (Kong-sidecar use case)."""

    def _big_tool_message(self):
        large_data = json.dumps(
            [
                {
                    "id": i,
                    "name": f"Item {i}",
                    "description": f"Detailed description for item {i}. "
                    f"Status active, created 2024-01-{(i % 28) + 1:02d}, "
                    f"category=electronics, price={i * 10.99:.2f}, stock={i * 5}.",
                    "tags": ["electronics", "sale", "featured", "new-arrival"],
                }
                for i in range(200)
            ]
        )
        return [
            {"role": "user", "content": "What items are available?"},
            {"role": "tool", "tool_call_id": "call_1", "content": large_data},
            {"role": "user", "content": "Summarize them."},
        ]

    @pytest.fixture
    def client(self):
        # disable_kompress keeps the real ONNX model out of the test: marker
        # suppression is exercised by SmartCrusher (pure-Python) regardless, and
        # the mode inherits enable_kompress from config so this stays fast.
        config = ProxyConfig(
            optimize=True,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
            disable_kompress=True,
        )
        app = create_app(config)
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as c:
            yield c

    def test_lossy_inline_emits_no_ccr_markers(self, client):
        messages = self._big_tool_message()
        response = client.post(
            "/v1/compress",
            json={"messages": messages, "model": "gpt-4", "config": {"mode": "lossy_inline"}},
        )
        assert response.status_code == 200
        data = response.json()

        # Core guarantee: no CCR markers anywhere — no store/retrieval needed.
        assert data["ccr_hashes"] == []
        blob = json.dumps(data["messages"])
        assert "<<ccr:" not in blob
        assert "Retrieve more: hash=" not in blob
        assert "Retrieve original: hash=" not in blob

        # Real compression happened. These guard against a fail-open-to-zero
        # (e.g. a content-detector hang tripping the executor timeout) passing
        # vacuously as 0 <= 0.
        assert data["tokens_before"] > 0
        assert data["tokens_saved"] > 0
        assert data["tokens_after"] < data["tokens_before"]
        assert data["tokens_saved"] == data["tokens_before"] - data["tokens_after"]

    def test_lossless_then_lossy_alias(self, client):
        """The spelled-out alias selects the same mode."""
        messages = self._big_tool_message()
        response = client.post(
            "/v1/compress",
            json={
                "messages": messages,
                "model": "gpt-4",
                "config": {"mode": "lossless_then_lossy"},
            },
        )
        assert response.status_code == 200
        assert response.json()["ccr_hashes"] == []


class TestCompressEndpointDoesNotBlockLoop:
    """/v1/compress must offload to the compression executor so a slow/large
    payload cannot freeze the single event loop (#718)."""

    async def test_compress_does_not_block_liveness(self, monkeypatch):
        import asyncio
        import threading
        from types import SimpleNamespace

        import httpx

        config = ProxyConfig(
            optimize=True,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
        )
        app = create_app(config)
        proxy = app.state.proxy

        entered = threading.Event()
        release = threading.Event()

        def blocking_apply(**kwargs):
            # Stand in for a large CPU-bound compression: blocks its worker until
            # released. If this ran inline on the loop (the bug), the loop would
            # be frozen and /livez below could not be served.
            entered.set()
            release.wait(timeout=10)
            return SimpleNamespace(
                messages=kwargs["messages"],
                tokens_before=10,
                tokens_after=5,
                transforms_applied=[],
                transforms_summary={},
                markers_inserted=[],
            )

        monkeypatch.setattr(proxy.openai_pipeline, "apply", blocking_apply)

        # /v1/compress is loopback-gated (#1227) — present as 127.0.0.1.
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            compress = asyncio.create_task(
                client.post(
                    "/v1/compress",
                    json={
                        "messages": [{"role": "user", "content": "hello world"}],
                        "model": "gpt-4",
                    },
                )
            )
            # Wait until the compression is actually in flight (running in the
            # executor thread), then prove the loop is still responsive.
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set(), "compression never started"

            livez = await asyncio.wait_for(client.get("/livez"), timeout=5)
            assert livez.status_code == 200
            assert livez.json()["alive"] is True
            # The compression is still blocked — /livez was served concurrently.
            assert not compress.done()

            release.set()
            resp = await asyncio.wait_for(compress, timeout=5)
            assert resp.status_code == 200
            assert resp.json()["tokens_saved"] == 5
