"""Embedding server sidecar: in-process watchdog + Unix-socket client.

The watchdog starts a background asyncio server inside this process (before
uvicorn forks workers) that loads the ONNX embedder once.  Worker processes
connect via a ``EmbeddingServerClient`` that speaks a simple line-delimited
JSON protocol over the Unix socket, sharing a single model instance.

Motivation
----------
With N uvicorn workers, each worker loads its own ONNX runtime session
(~86 MB model + ~200-500 MB session overhead).  With the sidecar enabled
the proxy starts *one* embedder process shared by all workers, cutting
embedding-related RSS from O(N) to O(1).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

import numpy as np

from headroom.memory.adapters.embedders import OnnxLocalEmbedder

logger = logging.getLogger(__name__)

# ---- protocol helpers -------------------------------------------------------


def _serialize_array(arr: np.ndarray) -> dict[str, Any]:
    buf = arr.tobytes()
    return {
        "data": base64.b64encode(buf).decode("ascii"),
        "shape": list(arr.shape),
        "dtype": str(arr.dtype),
    }


def _deserialize_array(obj: dict[str, Any]) -> np.ndarray:
    buf = base64.b64decode(obj["data"])
    arr = np.frombuffer(buf, dtype=np.dtype(obj["dtype"]))
    return arr.reshape(obj["shape"])


# ---- server -----------------------------------------------------------------


class _EmbedProtocol(asyncio.Protocol):
    """Line-delimited JSON request/response protocol over Unix socket."""

    _MAX_LINE = 4 * 1024 * 1024  # 4 MiB — batch embedding payloads

    def __init__(self, embedder: OnnxLocalEmbedder) -> None:
        self._embedder = embedder
        self._buffer = b""
        self._transport: Any = None
        self._busy = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport

    def connection_lost(self, exc: Exception | None) -> None:
        self._transport = None

    def data_received(self, data: bytes) -> None:
        self._buffer += data
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if len(line) > self._MAX_LINE:
                self._respond({"error": "request too large"})
                continue
            try:
                request = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                self._respond({"error": "invalid json"})
                continue
            # Schedule async work without blocking the protocol callback
            asyncio.ensure_future(self._handle(request))

    async def _handle(self, request: dict[str, Any]) -> None:
        method = request.get("method", "")
        try:
            if method == "embed":
                result = await self._embedder.embed(request["text"])
                self._respond({"embedding": _serialize_array(result)})
            elif method == "embed_batch":
                results = await self._embedder.embed_batch(request["texts"])
                self._respond({"embeddings": [_serialize_array(r) for r in results]})
            elif method == "ping":
                self._respond({"pong": True})
            else:
                self._respond({"error": f"unknown method: {method}"})
        except Exception as exc:
            logger.debug("Embed server handler error: %s", exc)
            self._respond({"error": str(exc)})

    def _respond(self, payload: dict[str, Any]) -> None:
        if self._transport is None:
            return
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        self._transport.write(line.encode("utf-8"))


async def _run_embedding_server(socket_path: str) -> None:
    """Load the ONNX model and start serving on *socket_path*.

    Blocks forever (or until cancelled).  Intended to be driven by
    :class:`EmbeddingServerWatchdog` via ``asyncio.create_task``.
    """
    import os as _os

    # Clean up stale socket from a previous crashed run
    try:
        _os.unlink(socket_path)
    except OSError:
        pass

    embedder = OnnxLocalEmbedder()
    # Force eager model load so the first request is fast
    await embedder.embed("warmup")

    loop = asyncio.get_running_loop()
    server = await loop.create_unix_server(
        lambda: _EmbedProtocol(embedder),
        path=socket_path,
    )
    logger.info("Embedding server listening on %s", socket_path)

    try:
        async with server:
            await server.serve_forever()
    finally:
        try:
            _os.unlink(socket_path)
        except OSError:
            pass


# ---- client -----------------------------------------------------------------


class EmbeddingServerClient:
    """Socket-based embedder client — satisfies the ``Embedder`` protocol.

    Opens a persistent Unix-socket connection (reconnected on-demand).
    All methods are async and thread-safe via an internal lock.
    """

    DEFAULT_DIMENSION = 384
    DEFAULT_MAX_TOKENS = 256
    MODEL_NAME = "all-MiniLM-L6-v2 (sidecar)"

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def dimension(self) -> int:
        return self.DEFAULT_DIMENSION

    @property
    def max_tokens(self) -> int:
        return self.DEFAULT_MAX_TOKENS

    @property
    def model_name(self) -> str:
        return self.MODEL_NAME

    async def _ensure_connected(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is not None and self._writer is not None:
            return self._reader, self._writer
        reader, writer = await asyncio.open_unix_connection(self._socket_path)  # type: ignore[attr-defined]
        self._reader = reader
        self._writer = writer
        return reader, writer

    async def _close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    async def _call(self, request: dict[str, Any]) -> dict[str, Any]:
        """Send a request and return the parsed JSON response."""
        async with self._lock:
            reader, writer = await self._ensure_connected()
            line = json.dumps(request, separators=(",", ":")) + "\n"
            writer.write(line.encode("utf-8"))
            await writer.drain()
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=30.0)
            except asyncio.TimeoutError as err:
                await self._close()
                raise RuntimeError("Embedding server timed out") from err
            if not raw:
                await self._close()
                raise RuntimeError("Embedding server closed connection")
            try:
                result: dict[str, Any] = json.loads(raw.decode("utf-8"))  # type: ignore[no-any-return]
                return result
            except json.JSONDecodeError as err:
                raise RuntimeError(f"Bad response from embedding server: {raw!r}") from err

    async def embed(self, text: str) -> np.ndarray:
        resp = await self._call({"method": "embed", "text": text})
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return _deserialize_array(resp["embedding"])

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        if not texts:
            return []
        resp = await self._call({"method": "embed_batch", "texts": texts})
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return [_deserialize_array(e) for e in resp["embeddings"]]

    async def ping(self) -> bool:
        try:
            resp = await self._call({"method": "ping"})
            return bool(resp.get("pong", False))
        except Exception:
            return False


# ---- watchdog ---------------------------------------------------------------


class EmbeddingServerWatchdog:
    """Manages the embedding server task running in the *same* event loop.

    Since the proxy spawns workers via uvicorn *after* this runs, the
    server task lives on the main event loop (the one that called
    ``start()``).  Workers then connect as clients via the Unix socket.

    Thread model
    ------------
    The ONNX runtime releases the GIL during inference, so the server
    task is effectively CPU-concurrent with worker coroutines even
    though both share the same OS thread.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._task: asyncio.Task[None] | None = None
        self._healthy = False

    async def start(self) -> None:
        self._task = asyncio.ensure_future(_run_embedding_server(self._socket_path))
        # Give the server a moment to bind
        await asyncio.sleep(0.1)

    async def wait_until_healthy(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        client = EmbeddingServerClient(self._socket_path)
        while time.monotonic() < deadline:
            try:
                if await client.ping():
                    self._healthy = True
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.2)
        return False

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
