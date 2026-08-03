"""Decompression of compressed request bodies must be bounded.

``_read_request_body_bytes`` (headroom/proxy/helpers.py) expands
zstd/gzip/deflate/br request bodies before forwarding. The expansion used
to be unbounded (``gzip.decompress`` / ``zlib.decompress`` /
``brotli.decompress`` / ``stream_reader().read()``), so a tiny compressed
body could balloon into an unbounded in-memory buffer — a decompression-bomb
DoS. Every format is now fed incrementally against
``MAX_DECOMPRESSED_BODY_BYTES`` and raises ``RequestBodyTooLarge`` (a
``ValueError``) when the cap is exceeded.
"""

import asyncio
import gzip
import json
import sys
import zlib

import pytest

from headroom.proxy.helpers import (
    MAX_DECOMPRESSED_BODY_BYTES,
    RequestBodyTooLarge,
    _read_request_body_bytes,
)

_PAYLOAD = b"the quick brown fox jumps over the lazy dog " * 5000  # ~225 KB


class _FakeHeaders:
    def __init__(self, d=None):
        self._d = {k.lower(): v for k, v in (d or {}).items()}

    def get(self, k, default=None):
        return self._d.get(k.lower(), default)


class _FakeRequest:
    def __init__(self, raw, headers=None):
        self._raw = raw
        self.headers = _FakeHeaders(headers)

    async def body(self):
        return self._raw


def _read(raw, encoding):
    return asyncio.run(_read_request_body_bytes(_FakeRequest(raw, {"content-encoding": encoding})))


# ---------------------------------------------------------------------------
# Round-trips: compressed bodies still decompress byte-for-byte
# ---------------------------------------------------------------------------


def test_gzip_round_trip():
    raw = gzip.compress(_PAYLOAD)
    assert _read(raw, "gzip") == _PAYLOAD


def test_deflate_round_trip():
    raw = zlib.compress(_PAYLOAD)
    assert _read(raw, "deflate") == _PAYLOAD


def test_zstd_round_trip():
    zstandard = pytest.importorskip("zstandard")
    raw = zstandard.ZstdCompressor().compress(_PAYLOAD)
    assert _read(raw, "zstd") == _PAYLOAD


def test_brotli_round_trip():
    brotli = pytest.importorskip("brotli")
    raw = brotli.compress(_PAYLOAD)
    assert _read(raw, "br") == _PAYLOAD


# ---------------------------------------------------------------------------
# Decompression bombs: small compressed input expanding past the cap is
# rejected with RequestBodyTooLarge (a ValueError) instead of OOMing
# ---------------------------------------------------------------------------


def _bomb_under_cap(monkeypatch, compress):
    # Shrink the cap so the test stays fast and memory-cheap: the bomb below
    # expands 1 MB -> ~1 MB of output from ~1 KB of compressed input, which
    # dwarfs the 4 KB test cap.
    monkeypatch.setattr("headroom.proxy.helpers.MAX_DECOMPRESSED_BODY_BYTES", 4096)
    bomb = compress(b"A" * (1024 * 1024))
    assert len(bomb) < 4096  # sanity: the input itself is tiny
    return bomb


def test_gzip_bomb_rejected(monkeypatch):
    with pytest.raises(RequestBodyTooLarge):
        _read(_bomb_under_cap(monkeypatch, gzip.compress), "gzip")


def test_deflate_bomb_rejected(monkeypatch):
    with pytest.raises(RequestBodyTooLarge):
        _read(_bomb_under_cap(monkeypatch, zlib.compress), "deflate")


def test_zstd_bomb_rejected(monkeypatch):
    zstandard = pytest.importorskip("zstandard")
    with pytest.raises(RequestBodyTooLarge):
        _read(
            _bomb_under_cap(monkeypatch, zstandard.ZstdCompressor().compress),
            "zstd",
        )


def test_brotli_bomb_rejected(monkeypatch):
    brotli = pytest.importorskip("brotli")
    with pytest.raises(RequestBodyTooLarge):
        _read(_bomb_under_cap(monkeypatch, brotli.compress), "br")


def test_payload_at_cap_is_accepted(monkeypatch):
    monkeypatch.setattr("headroom.proxy.helpers.MAX_DECOMPRESSED_BODY_BYTES", 1024)
    payload = b"x" * 1024
    assert _read(gzip.compress(payload), "gzip") == payload


# ---------------------------------------------------------------------------
# Error behavior preserved
# ---------------------------------------------------------------------------


def test_corrupt_gzip_still_raises_value_error():
    with pytest.raises(ValueError):
        _read(b"this is not a gzip stream at all", "gzip")


def test_corrupt_deflate_still_raises_value_error():
    with pytest.raises(ValueError):
        _read(b"this is not a deflate stream at all", "deflate")


def test_corrupt_zstd_still_raises_value_error():
    zstandard = pytest.importorskip("zstandard")
    del zstandard  # bytes only; the ImportError path is covered separately
    with pytest.raises(ValueError):
        _read(b"this is not a zstd frame at all", "zstd")


def test_zstd_not_installed_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "zstandard", None)
    with pytest.raises(ValueError, match="not installed"):
        _read(b"ignored", "zstd")


def test_brotli_not_installed_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "brotli", None)
    with pytest.raises(ValueError, match="not installed"):
        _read(b"ignored", "br")


def test_gzip_multi_member_rejected():
    # zlib stops at the first gzip member; anything after it must not be
    # silently dropped (gzip.decompress() used to decompress all members).
    m1 = gzip.compress(b"first member " * 100)
    m2 = gzip.compress(b"second member " * 100)
    with pytest.raises(ValueError, match="trailing data"):
        _read(m1 + m2, "gzip")


def test_gzip_trailing_garbage_rejected():
    with pytest.raises(ValueError, match="trailing data"):
        _read(gzip.compress(b"ok") + b"NOTGZIP", "gzip")


def test_truncated_gzip_still_raises_value_error():
    # Header + partial body, no end marker: the old one-shot call raised
    # BadGzipFile; the incremental path must raise too.
    truncated = gzip.compress(_PAYLOAD)[:32]
    with pytest.raises(ValueError):
        _read(truncated, "gzip")


def test_gzip_payload_across_slice_boundary_round_trips():
    # Exactly 64 KiB + a few bytes: exercises the multi-iteration path where
    # the final output is emitted after the cap-sized slice.
    payload = b"x" * (64 * 1024 + 7)
    assert _read(gzip.compress(payload), "gzip") == payload


def test_truncated_brotli_still_raises_value_error():
    brotli = pytest.importorskip("brotli")
    truncated = brotli.compress(_PAYLOAD)[:16]
    with pytest.raises(ValueError):
        _read(truncated, "br")


def test_unsupported_encoding_raises():
    with pytest.raises(ValueError):
        _read(b"data", "lzma")


def test_identity_and_missing_encoding_passthrough():
    payload = b'{"model": "x"}'
    assert _read(payload, "identity") == payload
    assert _read(payload, "") == payload


# ---------------------------------------------------------------------------
# Full path: read_request_json_with_bytes still decodes compressed JSON
# ---------------------------------------------------------------------------


def test_read_request_json_with_bytes_decompresses_gzip():
    from headroom.proxy.helpers import read_request_json_with_bytes

    body = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
    raw = gzip.compress(json.dumps(body).encode("utf-8"))
    result, out_raw = asyncio.run(
        read_request_json_with_bytes(_FakeRequest(raw, {"content-encoding": "gzip"}))
    )
    assert result == body
    assert json.loads(out_raw) == body


def test_cap_is_sane_default():
    # The default cap mirrors the uncompressed request budget: a compressed
    # body must never expand beyond what the handler would accept anyway.
    assert MAX_DECOMPRESSED_BODY_BYTES > 0
