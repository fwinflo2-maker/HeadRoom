"""Startup loads Kompress before accepting requests.

The model download and native initialization run off the event loop with a
startup timeout, so the first request does not trigger a cold model load.
"""

from __future__ import annotations

import pytest

from headroom import onnx_runtime
from headroom.transforms import kompress_compressor as kc
from headroom.transforms.content_router import ContentRouter, ContentRouterConfig
from headroom.transforms.kompress_compressor import KompressModelNotCached


def test_local_first_no_network_when_disallowed(monkeypatch):
    """allow_network=False must never fall back to a network download."""
    import huggingface_hub
    from huggingface_hub.errors import LocalEntryNotFoundError

    calls: list[bool] = []

    def fake_download(repo_id, filename, **kwargs):
        local_only = kwargs.get("local_files_only", False)
        calls.append(local_only)
        if local_only:
            raise LocalEntryNotFoundError("not cached")
        return "/cache/networked"

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)

    with pytest.raises(LocalEntryNotFoundError):
        onnx_runtime.hf_hub_download_local_first("org/model", "f.onnx", allow_network=False)

    # Only the local-only lookup ran; the network branch was never taken.
    assert calls == [True]


def test_local_first_falls_back_to_network_by_default(monkeypatch):
    """allow_network=True (default) keeps the historic cold-start behavior."""
    import huggingface_hub
    from huggingface_hub.errors import LocalEntryNotFoundError

    calls: list[bool] = []

    def fake_download(repo_id, filename, **kwargs):
        local_only = kwargs.get("local_files_only", False)
        calls.append(local_only)
        if local_only:
            raise LocalEntryNotFoundError("not cached")
        return "/cache/networked"

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)

    path = onnx_runtime.hf_hub_download_local_first("org/model", "f.onnx")
    assert path == "/cache/networked"
    assert calls == [True, False]  # local-only miss, then network download


def test_load_kompress_onnx_cache_miss_raises_not_cached(monkeypatch):
    """A cache-only ONNX load surfaces KompressModelNotCached, not a network call."""
    from huggingface_hub.errors import LocalEntryNotFoundError

    monkeypatch.setattr(kc, "_kompress_cache", {})

    def fake_local_first(repo_id, filename, *, allow_network=True):
        assert allow_network is False  # eager preload must request cache-only
        raise LocalEntryNotFoundError("not cached")

    monkeypatch.setattr(kc, "hf_hub_download_local_first", fake_local_first)

    with pytest.raises(KompressModelNotCached):
        kc._load_kompress_onnx("org/model", allow_download=False)


def test_load_kompress_auto_does_not_pytorch_download_on_cache_miss(monkeypatch):
    """Auto mode must propagate the cache miss, not fall back to a PyTorch fetch."""
    monkeypatch.setattr(kc, "_kompress_cache", {})
    monkeypatch.setattr(kc, "_selected_backend", lambda: "auto")
    monkeypatch.setattr(kc, "_is_onnx_available", lambda: True)
    monkeypatch.setattr(kc, "_is_pytorch_available", lambda: True)

    def onnx_not_cached(model_id, *, use_coreml=False, allow_download=True):
        raise KompressModelNotCached(model_id)

    def pytorch_should_not_run(*args, **kwargs):
        raise AssertionError("PyTorch fallback must not download on a cache-only miss")

    monkeypatch.setattr(kc, "_load_kompress_onnx", onnx_not_cached)
    monkeypatch.setattr(kc, "_load_kompress_pytorch", pytorch_should_not_run)

    with pytest.raises(KompressModelNotCached):
        kc._load_kompress("org/model", allow_download=False)


class _StubCompressor:
    def __init__(self, *, cached: bool):
        self._cached = cached
        self.preload_calls: list[bool] = []

    def preload(self, *, allow_download: bool = True) -> str:
        self.preload_calls.append(allow_download)
        if self._cached or allow_download:
            return "onnx"
        raise KompressModelNotCached("org/model")


def _router_kompress_only() -> ContentRouter:
    return ContentRouter(
        ContentRouterConfig(
            enable_kompress=True,
            enable_code_aware=False,
            enable_smart_crusher=False,
        )
    )


@pytest.mark.parametrize("cache_state", ["cached", "uncached"])
def test_eager_load_preloads_kompress_regardless_of_cache_state(monkeypatch, cache_state):
    router = _router_kompress_only()
    stub = _StubCompressor(cached=cache_state == "cached")
    monkeypatch.setattr(router, "_get_kompress", lambda: stub)
    monkeypatch.setattr(router, "_prefetch_kompress_artifacts_async", lambda _cfg: False)

    status = router.eager_load_compressors()

    assert status["kompress"] == "enabled"
    assert status["kompress_backend"] == "onnx"
    assert stub.preload_calls == [True]


def test_eager_load_keeps_disabled_kompress_disabled(monkeypatch):
    router = ContentRouter(
        ContentRouterConfig(
            enable_kompress=False,
            enable_code_aware=False,
            enable_smart_crusher=False,
        )
    )
    stub = _StubCompressor(cached=True)
    monkeypatch.setattr(router, "_get_kompress", lambda: stub)

    status = router.eager_load_compressors()

    assert "kompress" not in status
    assert stub.preload_calls == []


def test_eager_load_reports_unavailable_kompress(monkeypatch):
    router = _router_kompress_only()
    monkeypatch.setattr(router, "_get_kompress", lambda: None)

    status = router.eager_load_compressors()

    assert status["kompress"] == "unavailable"


def test_non_kompress_warmups_continue_when_kompress_is_preloaded(monkeypatch):
    router = _router_kompress_only()
    stub = _StubCompressor(cached=True)
    monkeypatch.setattr(router, "_get_kompress", lambda: stub)
    monkeypatch.setattr(router, "_prefetch_kompress_artifacts_async", lambda _cfg: False)
    monkeypatch.setattr("headroom.compression.detector._magika_available", lambda: True)
    monkeypatch.setattr("headroom.compression.detector._get_magika", lambda: object())

    status = router.eager_load_compressors()

    assert status["kompress"] == "enabled"
    assert status["magika"] == "enabled"
    assert stub.preload_calls == [True]


# ── Startup model preload ──────────────────────────────────────────────────────


def test_eager_load_preloads_native_kompress_model(monkeypatch):
    router = _router_kompress_only()
    stub = _StubCompressor(cached=False)
    monkeypatch.setattr(router, "_get_kompress", lambda: stub)

    status = router.eager_load_compressors()

    assert status["kompress"] == "enabled"
    assert status["kompress_backend"] == "onnx"
    assert stub.preload_calls == [True]


def test_prefetch_never_constructs_a_session_or_imports_transformers(monkeypatch):
    """The safety property that makes startup prefetch legal at all."""
    monkeypatch.setattr(kc, "_kompress_cache", {})
    requested: list[str] = []

    def fake_local_first(repo_id, filename, *, allow_network=True):
        requested.append(filename)
        return f"/cache/{filename}"

    monkeypatch.setattr(kc, "hf_hub_download_local_first", fake_local_first)

    def explode(*args, **kwargs):
        raise AssertionError("prefetch must not build the model")

    monkeypatch.setattr(kc, "_load_kompress", explode)
    monkeypatch.setattr(kc, "_load_kompress_onnx", explode)

    assert kc.prefetch_kompress_artifacts("org/model") is True
    # Stops at the first candidate that resolves — the loader tries the same order.
    assert requested == [kc._onnx_filename_candidates()[0]]


def test_prefetch_reports_false_when_no_artifact_resolves(monkeypatch):
    monkeypatch.setattr(kc, "_kompress_cache", {})

    def always_missing(repo_id, filename, *, allow_network=True):
        raise OSError("not found")

    monkeypatch.setattr(kc, "hf_hub_download_local_first", always_missing)

    assert kc.prefetch_kompress_artifacts("org/model") is False


def test_background_prefetch_is_noop_when_model_already_cached(monkeypatch):
    monkeypatch.setattr(kc, "_kompress_cache", {"org/model": object()})
    monkeypatch.setattr(kc, "is_kompress_available", lambda: True)

    assert kc.ensure_background_prefetch("org/model") is False


@pytest.mark.asyncio
async def test_proxy_startup_preloads_cached_kompress_model(monkeypatch):
    pytest.importorskip("httpx")
    from headroom.proxy.server import HeadroomProxy, ProxyConfig

    proxy = HeadroomProxy(
        ProxyConfig(
            optimize=True,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
            code_aware_enabled=False,
        )
    )
    router = _router_kompress_only()
    stub = _StubCompressor(cached=True)
    monkeypatch.setattr(router, "_get_kompress", lambda: stub)
    proxy.anthropic_pipeline.transforms = [router]
    proxy.openai_pipeline.transforms = [router]

    await proxy.startup()
    try:
        assert stub.preload_calls == [True]
        assert proxy._kompress_status == "enabled"
    finally:
        await proxy.shutdown()


@pytest.mark.asyncio
async def test_proxy_startup_preloads_kompress_when_eager_preload_is_disabled(monkeypatch):
    pytest.importorskip("httpx")
    from headroom.proxy.server import HeadroomProxy, ProxyConfig

    proxy = HeadroomProxy(
        ProxyConfig(
            optimize=False,
            kompress_enabled=True,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
            code_aware_enabled=False,
        )
    )
    router = _router_kompress_only()
    preload_calls: list[int] = []
    monkeypatch.setattr(
        router,
        "preload_kompress",
        lambda: (preload_calls.append(1), "onnx")[1],
        raising=False,
    )
    proxy.anthropic_pipeline.transforms = [router]
    proxy.openai_pipeline.transforms = [router]

    await proxy.startup()
    try:
        assert preload_calls == [1]
        assert proxy._kompress_status == "enabled"
    finally:
        await proxy.shutdown()
