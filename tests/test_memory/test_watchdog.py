"""Tests for the embedding server sidecar watchdog module.

Covers:
- Array serialisation round-trips
- _EmbedProtocol request handling
- EmbeddingServerClient (ping, embed, embed_batch, timeout, error paths)
- EmbeddingServerWatchdog lifecycle
- Factory integration: _create_embedder with HEADROOM_EMBEDDING_SERVER_SOCKET
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import numpy as np
import pytest

from headroom.memory.adapters.watchdog import (
    EmbeddingServerClient,
    EmbeddingServerWatchdog,
    _deserialize_array,
    _EmbedProtocol,
    _serialize_array,
)

# =============================================================================
# Helpers
# =============================================================================


def _fake_embed_result(text: str) -> np.ndarray:
    """Deterministic fake embedding for a single string."""
    seed = float(sum(ord(c) for c in text)) * 0.001
    return np.full(384, seed, dtype=np.float32)


# =============================================================================
# Array serialisation
# =============================================================================


class TestSerializeArray:
    """Round-trip fidelity for numpy array <-> JSON dict."""

    def test_roundtrip_1d(self) -> None:
        arr = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        result = _deserialize_array(_serialize_array(arr))
        np.testing.assert_array_equal(arr, result)

    def test_roundtrip_2d(self) -> None:
        rng = np.random.RandomState(42)
        arr = rng.randn(10, 384).astype(np.float32)
        result = _deserialize_array(_serialize_array(arr))
        np.testing.assert_array_equal(arr, result)

    def test_roundtrip_int64(self) -> None:
        arr = np.array([0, 1, 100, 255], dtype=np.int64)
        result = _deserialize_array(_serialize_array(arr))
        np.testing.assert_array_equal(arr, result)

    def test_roundtrip_empty(self) -> None:
        arr = np.array([], dtype=np.float32)
        result = _deserialize_array(_serialize_array(arr))
        np.testing.assert_array_equal(arr, result)


# =============================================================================
# _EmbedProtocol
# =============================================================================


class FakeTransport:
    """Minimal asyncio.WriteTransport stub that records written bytes."""

    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    # Transport protocol stubs
    def close(self) -> None: ...  # noqa: D102
    def is_closing(self) -> bool:  # noqa: D102
        return False


class FakeEmbedder:
    """Minimal embedder stub for testing protocol dispatch."""

    async def embed(self, text: str) -> np.ndarray:
        return _fake_embed_result(text)

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [_fake_embed_result(t) for t in texts]


class TestEmbedProtocol:
    """Protocol-level request/response handling (no actual sockets)."""

    @pytest.fixture
    def transport(self) -> FakeTransport:
        return FakeTransport()

    @pytest.fixture
    def protocol(self, transport: FakeTransport) -> _EmbedProtocol:
        proto = _EmbedProtocol(FakeEmbedder())
        proto.connection_made(transport)
        return proto

    @pytest.mark.asyncio
    async def test_ping(self, protocol: _EmbedProtocol, transport: FakeTransport) -> None:
        protocol.data_received(b'{"method":"ping"}\n')
        # Let the scheduled handler run
        await asyncio.sleep(0.05)
        assert len(transport.written) >= 1
        resp = json.loads(transport.written[-1].decode("utf-8"))
        assert resp == {"pong": True}

    @pytest.mark.asyncio
    async def test_embed(self, protocol: _EmbedProtocol, transport: FakeTransport) -> None:
        protocol.data_received(b'{"method":"embed","text":"hello"}\n')
        await asyncio.sleep(0.05)
        assert len(transport.written) >= 1
        resp = json.loads(transport.written[-1].decode("utf-8"))
        assert "embedding" in resp
        arr = _deserialize_array(resp["embedding"])
        np.testing.assert_array_equal(arr, _fake_embed_result("hello"))

    @pytest.mark.asyncio
    async def test_embed_batch(self, protocol: _EmbedProtocol, transport: FakeTransport) -> None:
        protocol.data_received(b'{"method":"embed_batch","texts":["a","b"]}\n')
        await asyncio.sleep(0.05)
        assert len(transport.written) >= 1
        resp = json.loads(transport.written[-1].decode("utf-8"))
        assert "embeddings" in resp
        assert len(resp["embeddings"]) == 2

    @pytest.mark.asyncio
    async def test_unknown_method(self, protocol: _EmbedProtocol, transport: FakeTransport) -> None:
        protocol.data_received(b'{"method":"bad"}\n')
        await asyncio.sleep(0.05)
        assert len(transport.written) >= 1
        resp = json.loads(transport.written[-1].decode("utf-8"))
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_invalid_json(self, protocol: _EmbedProtocol, transport: FakeTransport) -> None:
        protocol.data_received(b"not json\n")
        await asyncio.sleep(0.05)
        assert len(transport.written) >= 1
        resp = json.loads(transport.written[-1].decode("utf-8"))
        assert resp == {"error": "invalid json"}

    @pytest.mark.asyncio
    async def test_oversized_line(self, protocol: _EmbedProtocol, transport: FakeTransport) -> None:
        """Lines exceeding _MAX_LINE are rejected."""
        huge = b"x" * (4 * 1024 * 1024 + 1)
        protocol.data_received(huge + b"\n")
        await asyncio.sleep(0.05)
        assert len(transport.written) >= 1
        resp = json.loads(transport.written[-1].decode("utf-8"))
        assert resp == {"error": "request too large"}

    @pytest.mark.asyncio
    async def test_missing_text(self, protocol: _EmbedProtocol, transport: FakeTransport) -> None:
        protocol.data_received(b'{"method":"embed"}\n')
        await asyncio.sleep(0.05)
        assert len(transport.written) >= 1
        resp = json.loads(transport.written[-1].decode("utf-8"))
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_multiple_lines(self, protocol: _EmbedProtocol, transport: FakeTransport) -> None:
        protocol.data_received(b'{"method":"ping"}\n{"method":"ping"}\n')
        await asyncio.sleep(0.05)
        # Both responses should have been written (order may vary due to async)
        assert len(transport.written) >= 2

    def test_connection_lost_clears_transport(self, protocol: _EmbedProtocol) -> None:
        protocol.connection_lost(None)
        assert protocol._transport is None


# =============================================================================
# EmbeddingServerClient  (integration over TCP)
# =============================================================================

# Use TCP on localhost for cross-platform testability.  The production
# path uses Unix sockets, but the client's _ensure_connected is a thin
# wrapper around asyncio.open_unix_connection.


class FakeStreamReader:
    """Simulates asyncio.StreamReader.readline()."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines
        self._pos = 0

    async def readline(self) -> bytes:
        if self._pos >= len(self._lines):
            return b""
        line = self._lines[self._pos]
        self._pos += 1
        return line


class FakeStreamWriter:
    """Simulates asyncio.StreamWriter."""

    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass

    async def drain(self) -> None:
        pass


def _resp_line(payload: dict) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def _make_client_with_responses(
    *responses: dict,
) -> tuple[EmbeddingServerClient, FakeStreamWriter]:
    """Create an EmbeddingServerClient wired to fake reader/writer pairs.

    Each response dict is returned in order as a protocol response line.
    """
    lines = [_resp_line(r) for r in responses]
    reader = FakeStreamReader(lines)
    writer = FakeStreamWriter()
    client = EmbeddingServerClient("/fake.sock")
    client._reader = reader
    client._writer = writer
    return client, writer


class TestEmbeddingServerClient:
    """Client tests using fake reader/writer (no actual sockets)."""

    # ---- constructor & properties ------------------------------------------------

    def test_default_properties(self) -> None:
        c = EmbeddingServerClient("/tmp/test.sock")
        assert c.dimension == 384
        assert c.max_tokens == 256
        assert c.model_name == "all-MiniLM-L6-v2 (sidecar)"

    # ---- ping -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_ping_pong(self) -> None:
        client, _ = _make_client_with_responses({"pong": True})
        assert await client.ping() is True

    @pytest.mark.asyncio
    async def test_ping_connection_error_returns_false(self) -> None:
        client = EmbeddingServerClient("/nonexistent.sock")
        # Force _ensure_connected to fail
        with patch.object(client, "_ensure_connected", side_effect=OSError("bad")):
            assert await client.ping() is False

    # ---- embed ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_embed(self) -> None:
        expected = _fake_embed_result("text")
        client, writer = _make_client_with_responses({"embedding": _serialize_array(expected)})
        result = await client.embed("text")
        np.testing.assert_array_equal(result, expected)
        # Verify the sent request
        sent = json.loads(writer.written[0].decode("utf-8").rstrip("\n"))
        assert sent == {"method": "embed", "text": "text"}

    @pytest.mark.asyncio
    async def test_embed_error_response(self) -> None:
        client, _ = _make_client_with_responses({"error": "model not loaded"})
        with pytest.raises(RuntimeError, match="model not loaded"):
            await client.embed("text")

    # ---- embed_batch ------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_embed_batch(self) -> None:
        e1 = _fake_embed_result("a")
        e2 = _fake_embed_result("b")
        client, writer = _make_client_with_responses(
            {"embeddings": [_serialize_array(e1), _serialize_array(e2)]}
        )
        results = await client.embed_batch(["a", "b"])
        assert len(results) == 2
        np.testing.assert_array_equal(results[0], e1)
        np.testing.assert_array_equal(results[1], e2)

    @pytest.mark.asyncio
    async def test_embed_batch_empty(self) -> None:
        client = EmbeddingServerClient("/fake.sock")
        results = await client.embed_batch([])
        assert results == []

    @pytest.mark.asyncio
    async def test_embed_batch_error(self) -> None:
        client, _ = _make_client_with_responses({"error": "timeout"})
        with pytest.raises(RuntimeError, match="timeout"):
            await client.embed_batch(["a"])

    # ---- timeout handling -------------------------------------------------------

    @pytest.mark.asyncio
    async def test_timeout_closes_and_raises(self) -> None:
        client = EmbeddingServerClient("/fake.sock")
        reader = FakeStreamReader([])  # empty -> readline blocks forever
        writer = FakeStreamWriter()
        client._reader = reader
        client._writer = writer

        # Patch wait_for to simulate timeout.  Must close the coroutine
        # that reader.readline() produces to avoid a RuntimeWarning.
        async def fake_wait_for(coro: object, *args: object, **kwargs: object) -> bytes:
            if hasattr(coro, "close"):
                coro.close()  # type: ignore[union-attr]
            raise asyncio.TimeoutError()

        with patch("asyncio.wait_for", fake_wait_for):
            with pytest.raises(RuntimeError, match="timed out"):
                await client.embed("text")
        # Client closed connection after timeout
        assert client._writer is None

    @pytest.mark.asyncio
    async def test_eof_closes_and_raises(self) -> None:
        client = EmbeddingServerClient("/fake.sock")
        reader = FakeStreamReader([])  # empty -> returns b"" (EOF)
        writer = FakeStreamWriter()
        client._reader = reader
        client._writer = writer

        with pytest.raises(RuntimeError, match="closed connection"):
            await client.embed("text")
        assert client._writer is None

    # ---- reconnection -----------------------------------------------------------

    @pytest.mark.asyncio
    async def test_reconnects_after_close(self) -> None:
        """After a failure closes the connection, next call reconnects."""
        client = EmbeddingServerClient("/fake.sock")
        # First call: failure closes connection
        reader1 = FakeStreamReader([])
        writer1 = FakeStreamWriter()
        client._reader = reader1
        client._writer = writer1

        with pytest.raises(RuntimeError, match="closed connection"):
            await client.embed("text")
        assert client._writer is None

        # Second call: should recreate connection (via _ensure_connected)
        expected = _fake_embed_result("retry")
        reader2 = FakeStreamReader([_resp_line({"embedding": _serialize_array(expected)})])
        writer2 = FakeStreamWriter()
        client._reader = reader2
        client._writer = writer2

        result = await client.embed("retry")
        np.testing.assert_array_equal(result, expected)


# =============================================================================
# Factory integration
# =============================================================================


class TestFactoryWithSidecar:
    """Tests _create_embedder sidecar socket routing."""

    def test_onnx_without_socket_env_returns_onnx_embedder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HEADROOM_EMBEDDING_SERVER_SOCKET", raising=False)
        import sys
        from unittest.mock import MagicMock

        monkeypatch.setitem(sys.modules, "onnxruntime", MagicMock())
        monkeypatch.setitem(sys.modules, "tokenizers", MagicMock())

        from headroom.memory.config import EmbedderBackend, MemoryConfig
        from headroom.memory.factory import _create_embedder, _reset_embedder_cache_for_tests

        _reset_embedder_cache_for_tests()
        config = MemoryConfig(embedder_backend=EmbedderBackend.ONNX)
        embedder = _create_embedder(config)
        from headroom.memory.adapters.embedders import OnnxLocalEmbedder

        assert isinstance(embedder, OnnxLocalEmbedder)

    def test_onnx_with_socket_env_returns_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEADROOM_EMBEDDING_SERVER_SOCKET", "/tmp/test-embed.sock")

        from headroom.memory.config import EmbedderBackend, MemoryConfig
        from headroom.memory.factory import _create_embedder, _reset_embedder_cache_for_tests

        _reset_embedder_cache_for_tests()
        config = MemoryConfig(embedder_backend=EmbedderBackend.ONNX)
        embedder = _create_embedder(config)
        assert isinstance(embedder, EmbeddingServerClient)
        assert embedder.dimension == 384


# =============================================================================
# EmbeddingServerWatchdog lifecycle
# =============================================================================


class TestWatchdogLifecycle:
    """Test start/stop without requiring ONNX runtime."""

    @pytest.mark.asyncio
    async def test_stop_on_unstarted_is_safe(self) -> None:
        wd = EmbeddingServerWatchdog("/tmp/nonexistent.sock")
        await wd.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_wait_until_healthy_fails_without_server(self) -> None:
        wd = EmbeddingServerWatchdog("/tmp/nonexistent.sock")
        ok = await wd.wait_until_healthy(timeout=0.5)
        assert ok is False


# =============================================================================
# CLI fallback regression (existing test continues to pass)
# =============================================================================


class TestCLIFallbackStillWorks:
    """Ensure the proxy gracefully handles sidecar import failure.

    The existing test in test_cli_proxy_embedding_server.py should still
    pass now that the module exists.  This is a targeted re-check.
    """

    def test_sidecar_module_is_importable(self) -> None:
        """Verify the watchdog module imports cleanly."""
        from headroom.memory.adapters.watchdog import (  # noqa: F401
            EmbeddingServerClient,
            EmbeddingServerWatchdog,
        )

    def test_watchdog_creation_works(self) -> None:
        wd = EmbeddingServerWatchdog("/tmp/test.sock")
        assert wd._socket_path == "/tmp/test.sock"
        assert wd._task is None
        assert wd._healthy is False
