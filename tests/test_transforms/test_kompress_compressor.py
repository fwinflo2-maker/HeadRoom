"""Tests for Kompress compressor.

Covers:
- Lazy imports: module importable without torch installed
- is_kompress_available(): correct detection of [ml] extra
- KompressConfig / KompressResult: dataclass defaults
- KompressCompressor: passthrough for short content, fallback on error
- Transform interface: apply() method
"""

import logging
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import headroom.transforms.kompress_compressor as kc
from headroom.cache.kompress_cache import KompressCache

# ── Import safety (the whole point of the fix) ─────────────────────────


class TestLazyImports:
    """The module must be importable without torch/transformers."""

    def test_is_kompress_available_importable(self) -> None:
        """is_kompress_available can be imported even without torch."""
        from headroom.transforms.kompress_compressor import is_kompress_available

        # Should return bool (True or False depending on environment)
        result = is_kompress_available()
        assert isinstance(result, bool)

    def test_module_import_without_torch(self) -> None:
        """Importing the module with torch blocked should not raise."""
        import sys

        # Block torch AND onnxruntime imports
        with patch.dict(
            sys.modules,
            {"torch": None, "torch.nn": None, "onnxruntime": None},
        ):
            from headroom.transforms.kompress_compressor import (
                _is_pytorch_available,
            )

            # Without both torch and onnxruntime, should return False
            assert _is_pytorch_available() is False
            # Note: is_kompress_available() may still return True if onnxruntime
            # was already imported before patching. Test the individual checkers.

    def test_dataclasses_importable_without_torch(self) -> None:
        """KompressConfig, KompressResult, KompressCompressor are importable without torch."""
        from headroom.transforms.kompress_compressor import (
            KompressCompressor,  # noqa: F401
            KompressConfig,
            KompressResult,
        )

        # These don't need torch to instantiate
        config = KompressConfig()
        assert config.device == "auto"
        assert config.enable_ccr is True

        result = KompressResult(
            compressed="hello",
            original="hello world",
            original_tokens=2,
            compressed_tokens=1,
            compression_ratio=0.5,
        )
        assert result.tokens_saved == 1
        assert result.savings_percentage == 50.0


class TestKompressBackendSelection:
    def test_selected_backend_aliases(self, monkeypatch) -> None:
        import headroom.transforms.kompress_compressor as kmod

        monkeypatch.setenv("HEADROOM_KOMPRESS_BACKEND", "mps")
        assert kmod._selected_backend() == "pytorch_mps"

        monkeypatch.setenv("HEADROOM_KOMPRESS_BACKEND", "coreml")
        assert kmod._selected_backend() == "onnx_coreml"

        monkeypatch.setenv("HEADROOM_KOMPRESS_BACKEND", "cpu")
        assert kmod._selected_backend() == "onnx_cpu"

        monkeypatch.setenv("HEADROOM_KOMPRESS_BACKEND", "gpu")
        assert kmod._selected_backend() == "onnx_gpu"

        monkeypatch.setenv("HEADROOM_KOMPRESS_BACKEND", "cuda")
        assert kmod._selected_backend() == "onnx_gpu"

        monkeypatch.setenv("HEADROOM_KOMPRESS_BACKEND", "onnx_gpu")
        assert kmod._selected_backend() == "onnx_gpu"

        monkeypatch.setenv("HEADROOM_KOMPRESS_BACKEND", "onnx-gpu")
        assert kmod._selected_backend() == "onnx_gpu"

        monkeypatch.setenv("HEADROOM_KOMPRESS_BACKEND", "unknown")
        assert kmod._selected_backend() == "auto"

    def test_unrecognized_backend_warns_and_falls_back_to_auto(self, monkeypatch, caplog) -> None:
        import headroom.transforms.kompress_compressor as kmod

        monkeypatch.setenv("HEADROOM_KOMPRESS_BACKEND", "tpu")
        with caplog.at_level(logging.WARNING, logger=kmod.logger.name):
            assert kmod._selected_backend() == "auto"

        assert any(
            "unrecognized" in record.getMessage() and "tpu" in record.getMessage()
            for record in caplog.records
        )

    def test_valid_backend_values_do_not_warn(self, monkeypatch, caplog) -> None:
        import headroom.transforms.kompress_compressor as kmod

        with caplog.at_level(logging.WARNING, logger=kmod.logger.name):
            for value in (
                "auto",
                "onnx",
                "cpu",
                "coreml",
                "mps",
                "torch",
                "gpu",
                "cuda",
                "onnx-gpu",
                "ONNX-CPU",
            ):
                monkeypatch.setenv("HEADROOM_KOMPRESS_BACKEND", value)
                kmod._selected_backend()
            monkeypatch.delenv("HEADROOM_KOMPRESS_BACKEND", raising=False)
            kmod._selected_backend()

        assert not caplog.records

    def test_forced_pytorch_mps_backend_uses_mps_device(self, monkeypatch) -> None:
        import headroom.transforms.kompress_compressor as kmod

        calls: list[tuple[str, str]] = []
        monkeypatch.setenv("HEADROOM_KOMPRESS_BACKEND", "pytorch_mps")
        monkeypatch.setattr(kmod, "_kompress_cache", {})
        monkeypatch.setattr(
            kmod,
            "_load_kompress_pytorch",
            lambda model_id, device, *, allow_download=True: (
                calls.append((model_id, device)) or ("model", "tokenizer", "pytorch")
            ),
        )

        assert kmod._load_kompress("model-a", device="auto") == ("model", "tokenizer", "pytorch")
        assert calls == [("model-a", "mps")]

    def test_forced_coreml_backend_uses_onnx_coreml(self, monkeypatch) -> None:
        import headroom.transforms.kompress_compressor as kmod

        calls: list[tuple[str, bool]] = []
        monkeypatch.setenv("HEADROOM_KOMPRESS_BACKEND", "onnx_coreml")
        monkeypatch.setattr(kmod, "_kompress_cache", {})
        monkeypatch.setattr(
            kmod,
            "_load_kompress_onnx",
            lambda model_id, *, use_coreml=False, allow_download=True: (
                calls.append((model_id, use_coreml)) or ("model", "tokenizer", "onnx_coreml")
            ),
        )

        assert kmod._load_kompress("model-b") == ("model", "tokenizer", "onnx_coreml")
        assert calls == [("model-b", True)]

    def test_forced_onnx_gpu_backend_calls_with_use_gpu(self, monkeypatch) -> None:
        import headroom.transforms.kompress_compressor as kmod

        calls: list[tuple[str, bool]] = []
        monkeypatch.setenv("HEADROOM_KOMPRESS_BACKEND", "onnx_gpu")
        monkeypatch.setattr(kmod, "_kompress_cache", {})
        monkeypatch.setattr(
            kmod,
            "_load_kompress_onnx",
            lambda model_id, *, use_coreml=False, use_gpu=False, allow_download=True: (
                calls.append((model_id, use_gpu)) or ("model", "tokenizer", "onnx_gpu")
            ),
        )

        assert kmod._load_kompress("model-c") == ("model", "tokenizer", "onnx_gpu")
        assert calls == [("model-c", True)]

    def test_forced_onnx_gpu_backend_uses_cuda_provider(self, monkeypatch) -> None:
        import sys

        import headroom.transforms.kompress_compressor as kmod

        # transformers may not be installed; inject a minimal stub so the
        # `from transformers import AutoTokenizer` inside _load_kompress_onnx
        # does not raise.
        fake_transformers = MagicMock()
        fake_transformers.AutoTokenizer = MagicMock()
        monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

        captured_providers: list[Any] = []
        monkeypatch.setenv("HEADROOM_KOMPRESS_BACKEND", "onnx_gpu")
        monkeypatch.setattr(kmod, "_is_onnx_available", lambda: True)
        monkeypatch.setattr(kmod, "_available_gpu_providers", lambda: ["CUDAExecutionProvider"])
        monkeypatch.setattr(kmod, "_kompress_cache", {})
        monkeypatch.setattr(
            kmod,
            "_create_onnx_session",
            lambda model_id, providers, *, allow_download=True: (
                captured_providers.append(providers) or MagicMock()
            ),
        )
        monkeypatch.setattr(
            kmod,
            "_load_modernbert_tokenizer",
            lambda auto_tokenizer, *, allow_download: MagicMock(),
        )

        result = kmod._load_kompress_onnx("model-d", use_gpu=True)
        assert result[2] == "onnx_gpu"
        assert len(captured_providers) == 1
        providers = captured_providers[0]
        assert providers == ["CUDAExecutionProvider", "CPUExecutionProvider"]

    def test_auto_backend_preserves_onnx_first(self, monkeypatch) -> None:
        import headroom.transforms.kompress_compressor as kmod

        calls: list[str] = []
        monkeypatch.delenv("HEADROOM_KOMPRESS_BACKEND", raising=False)
        monkeypatch.setattr(kmod, "_kompress_cache", {})
        monkeypatch.setattr(kmod, "_is_onnx_available", lambda: True)
        monkeypatch.setattr(kmod, "_is_pytorch_available", lambda: True)
        monkeypatch.setattr(
            kmod,
            "_load_kompress_onnx",
            lambda model_id, *, use_coreml=False, allow_download=True: (
                calls.append("onnx") or ("model", "tokenizer", "onnx")
            ),
        )
        monkeypatch.setattr(
            kmod,
            "_load_kompress_pytorch",
            lambda model_id, device, *, allow_download=True: (
                calls.append("pytorch") or ("model", "tokenizer", "pytorch")
            ),
        )

        assert kmod._load_kompress("model-c") == ("model", "tokenizer", "onnx")
        assert calls == ["onnx"]

    def test_onnx_session_options_read_thread_caps(self, monkeypatch) -> None:
        import headroom.transforms.kompress_compressor as kmod

        created: list[SimpleNamespace] = []

        class FakeSessionOptions:
            def __init__(self) -> None:
                self.intra_op_num_threads = None
                self.inter_op_num_threads = None
                self.enable_cpu_mem_arena = True
                self.enable_mem_pattern = True

        fake_ort = SimpleNamespace(
            SessionOptions=lambda: created.append(FakeSessionOptions()) or created[-1]
        )
        monkeypatch.setenv("HEADROOM_KOMPRESS_ONNX_INTRA_THREADS", "2")
        monkeypatch.setenv("HEADROOM_KOMPRESS_ONNX_INTER_THREADS", "1")

        options = kmod._onnx_session_options(fake_ort)

        assert options.intra_op_num_threads == 2
        assert options.inter_op_num_threads == 1
        assert options.enable_cpu_mem_arena is False
        assert options.enable_mem_pattern is False


# ── KompressResult ──────────────────────────────────────────────────────


class TestKompressResult:
    def test_tokens_saved(self) -> None:
        from headroom.transforms.kompress_compressor import KompressResult

        r = KompressResult(
            compressed="a b",
            original="a b c d",
            original_tokens=4,
            compressed_tokens=2,
            compression_ratio=0.5,
        )
        assert r.tokens_saved == 2

    def test_tokens_saved_no_negative(self) -> None:
        from headroom.transforms.kompress_compressor import KompressResult

        r = KompressResult(
            compressed="a b c d e",
            original="a b c",
            original_tokens=3,
            compressed_tokens=5,
            compression_ratio=1.67,
        )
        assert r.tokens_saved == 0

    def test_savings_percentage_zero_tokens(self) -> None:
        from headroom.transforms.kompress_compressor import KompressResult

        r = KompressResult(
            compressed="",
            original="",
            original_tokens=0,
            compressed_tokens=0,
            compression_ratio=1.0,
        )
        assert r.savings_percentage == 0.0

    def test_default_model(self) -> None:
        from headroom.transforms.kompress_compressor import HF_MODEL_ID, KompressResult

        r = KompressResult(
            compressed="x",
            original="x y",
            original_tokens=2,
            compressed_tokens=1,
            compression_ratio=0.5,
        )
        assert r.model_used == HF_MODEL_ID


# ── KompressCompressor (without model) ──────────────────────────────────


class TestKompressCompressorPassthrough:
    """Test compressor behavior that doesn't require the actual model."""

    def test_short_content_passthrough(self) -> None:
        """Content under 10 words should pass through unchanged."""
        from headroom.transforms.kompress_compressor import KompressCompressor

        compressor = KompressCompressor()
        result = compressor.compress("hello world")
        assert result.compressed == "hello world"
        assert result.compression_ratio == 1.0
        assert result.original_tokens == 2
        assert result.compressed_tokens == 2

    def test_empty_content_passthrough(self) -> None:
        from headroom.transforms.kompress_compressor import KompressCompressor

        compressor = KompressCompressor()
        result = compressor.compress("")
        assert result.compressed == ""
        assert result.compression_ratio == 1.0

    def test_fallback_on_model_error(self) -> None:
        """If _load_kompress fails, compress should return passthrough."""
        from headroom.transforms.kompress_compressor import KompressCompressor

        compressor = KompressCompressor()
        long_text = " ".join(f"word{i}" for i in range(20))

        with patch(
            "headroom.transforms.kompress_compressor._load_kompress",
            side_effect=RuntimeError("no model"),
        ):
            result = compressor.compress(long_text)
            assert result.compressed == long_text
            assert result.compression_ratio == 1.0


class TestKompressResultCache:
    def test_identical_section_hits_without_second_inference(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        text = "one two three four five six seven eight nine ten eleven twelve"
        calls = 0

        def fake_inference(*args, **kwargs):
            nonlocal calls
            calls += 1
            return kc.KompressResult(
                compressed="one two three",
                original=text,
                original_tokens=12,
                compressed_tokens=3,
                compression_ratio=0.25,
            ), "success"

        monkeypatch.setattr(compressor, "_compress_uncached", fake_inference)

        first = compressor.compress(text)
        second = compressor.compress(text)

        assert first.compressed == second.compressed == "one two three"
        assert calls == 1

    def test_concurrent_same_payload_uses_single_flight(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        text = "one two three four five six seven eight nine ten eleven twelve"
        inference_started = threading.Event()
        release_inference = threading.Event()
        calls = 0

        def fake_inference(*args, **kwargs):
            nonlocal calls
            calls += 1
            inference_started.set()
            assert release_inference.wait(timeout=1)
            return kc.KompressResult(
                compressed="one two three",
                original=text,
                original_tokens=12,
                compressed_tokens=3,
                compression_ratio=0.25,
            ), "success"

        monkeypatch.setattr(compressor, "_compress_uncached", fake_inference)
        results: list[kc.KompressResult] = []

        first_thread = threading.Thread(target=lambda: results.append(compressor.compress(text)))
        second_thread = threading.Thread(target=lambda: results.append(compressor.compress(text)))
        first_thread.start()
        assert inference_started.wait(timeout=1)
        second_thread.start()
        second_thread.join(timeout=0.05)
        assert second_thread.is_alive()
        release_inference.set()
        first_thread.join(timeout=1)
        second_thread.join(timeout=1)

        assert not first_thread.is_alive()
        assert not second_thread.is_alive()
        assert [result.compressed for result in results] == ["one two three"] * 2
        assert calls == 1
        assert cache._inflight == {}

    def test_single_flight_waiter_times_out_without_cache_failure(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        monkeypatch.setattr(kc, "_request_deadline_seconds", lambda: 0.05)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        text = "one two three four five six seven eight nine ten eleven twelve"
        inference_started = threading.Event()
        release_inference = threading.Event()
        owner_result: list[kc.KompressResult] = []
        waiter_result: list[kc.KompressResult] = []

        def fake_inference(*args, **kwargs):
            inference_started.set()
            assert release_inference.wait(timeout=1)
            return kc.KompressResult(
                compressed="one two three",
                original=text,
                original_tokens=12,
                compressed_tokens=3,
                compression_ratio=0.25,
            ), "success"

        monkeypatch.setattr(compressor, "_compress_uncached", fake_inference)
        owner_thread = threading.Thread(
            target=lambda: owner_result.append(compressor.compress(text))
        )
        waiter_thread = threading.Thread(
            target=lambda: waiter_result.append(compressor.compress(text))
        )
        owner_thread.start()
        assert inference_started.wait(timeout=1)
        waiter_thread.start()
        waiter_thread.join(timeout=1)

        assert not waiter_thread.is_alive()
        assert waiter_result[0].compressed == text
        assert cache.lookup(text) is None
        assert cache.stats()["failures"] == 0

        release_inference.set()
        owner_thread.join(timeout=1)
        assert not owner_thread.is_alive()
        assert owner_result[0].compressed == "one two three"
        assert cache._inflight == {}

    def test_single_flight_retries_use_remaining_deadline(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        text = "one two three four five six seven eight nine ten eleven twelve"
        cache.record_failure(text)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        remaining = iter((0.05, 0.0))
        deadline_checks: list[float] = []
        waits: list[float] = []

        def remaining_timeout(_):
            timeout = next(remaining)
            deadline_checks.append(timeout)
            return timeout

        monkeypatch.setattr(kc, "_single_flight_wait_timeout_seconds", remaining_timeout)

        def acquire(content, namespace, *, timeout_seconds):
            waits.append(timeout_seconds)
            return False if len(waits) == 1 else None

        monkeypatch.setattr(cache, "acquire_inference", acquire)

        result = compressor.compress(text)

        assert result.compressed == text
        assert deadline_checks == [0.05, 0.0]
        assert waits == [0.05]
        assert cache.stats()["failures"] == 1

    def test_effective_configuration_namespaces_cached_results(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        text = "one two three four five six seven eight nine ten eleven twelve"
        first = kc.KompressCompressor(kc.KompressConfig(model_id="model-a", enable_ccr=False))
        second = kc.KompressCompressor(kc.KompressConfig(model_id="model-b", enable_ccr=False))
        calls: list[str] = []

        def fake_inference(compressor):
            def run(*args, **kwargs):
                calls.append(compressor.config.model_id)
                return kc.KompressResult(
                    compressed=compressor.config.model_id,
                    original=text,
                    original_tokens=12,
                    compressed_tokens=1,
                    compression_ratio=1 / 12,
                ), "success"

            return run

        monkeypatch.setattr(first, "_compress_uncached", fake_inference(first))
        monkeypatch.setattr(second, "_compress_uncached", fake_inference(second))

        assert first.compress(text).compressed == "model-a"
        assert second.compress(text).compressed == "model-b"
        assert calls == ["model-a", "model-b"]

    def test_onnx_path_is_part_of_effective_cache_namespace(self, monkeypatch) -> None:
        compressor = kc.KompressCompressor()
        default_namespace = compressor._cache_namespace()

        monkeypatch.setenv("HEADROOM_KOMPRESS_ONNX_PATH", "C:/models/first.onnx")
        first_namespace = compressor._cache_namespace()
        monkeypatch.setenv("HEADROOM_KOMPRESS_ONNX_PATH", "C:/models/second.onnx")
        second_namespace = compressor._cache_namespace()

        assert first_namespace != default_namespace
        assert second_namespace != first_namespace

    def test_exhausted_payload_failure_bypasses_inference(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        text = "one two three four five six seven eight nine ten eleven twelve"
        calls = 0

        def failing_inference(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise TimeoutError("payload too large")

        monkeypatch.setattr(compressor, "_compress_uncached", failing_inference)

        compressor.compress(text)
        compressor.compress(text)
        result = compressor.compress(text)

        assert result.compressed == text
        assert calls == 2

    def test_gpu_single_content_batch_failures_reach_exhaustion(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        text = "one two three four five six seven eight nine ten eleven twelve"
        inference_calls = 0

        class BatchEncoding(dict):
            def __init__(self, batch_words):
                input_ids = [[1] * len(words) for words in batch_words]
                super().__init__(input_ids=input_ids, attention_mask=input_ids)
                self.batch_words = batch_words

            def word_ids(self, batch_index=0):
                return list(range(len(self.batch_words[batch_index])))

        class BatchTokenizer:
            def __call__(self, batch_words, **kwargs):
                return BatchEncoding(batch_words)

        class FailingGpuModel:
            def get_scores(self, input_ids, attention_mask):
                nonlocal inference_calls
                inference_calls += 1
                raise TimeoutError("GPU batch inference failed")

        monkeypatch.setattr(
            kc,
            "_load_kompress",
            lambda *args, **kwargs: (FailingGpuModel(), BatchTokenizer(), "onnx_gpu"),
        )
        monkeypatch.setattr(
            compressor, "_should_batch_single_content", lambda *args, **kwargs: True
        )
        monkeypatch.setattr(compressor, "_should_use_sequential_fallback", lambda: False)

        first = compressor.compress(text)
        first_entry = cache.lookup(text)
        second = compressor.compress(text)
        second_entry = cache.lookup(text)
        exhausted = compressor.compress(text)

        assert first.compressed == second.compressed == exhausted.compressed == text
        assert first_entry is not None and first_entry.attempts == 1
        assert second_entry is not None and second_entry.attempts == 2
        assert second_entry.exhausted is True
        assert inference_calls == 2

    def test_generic_model_load_failure_is_not_cached_or_suppressed(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        text = "one two three four five six seven eight nine ten eleven twelve"
        loads = 0

        def failing_load(*args, **kwargs):
            nonlocal loads
            loads += 1
            raise RuntimeError("model load failed")

        monkeypatch.setattr(kc, "_load_kompress", failing_load)

        first = compressor.compress(text)
        second = compressor.compress(text)

        assert first.compressed == second.compressed == text
        assert loads == 2
        assert cache.lookup(text) is None

    def test_model_load_failure_preserves_existing_retryable_failure(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=3)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        text = "one two three four five six seven eight nine ten eleven twelve"
        cache.record_failure(text)
        monkeypatch.setattr(
            kc,
            "_load_kompress",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("model load failed")),
        )

        result = compressor.compress(text)

        assert result.compressed == text
        entry = cache.lookup(text)
        assert entry is not None
        assert entry.attempts == 1

    def test_delegated_batch_saturation_preserves_existing_retryable_failure(
        self, monkeypatch
    ) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=3)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        text = "one two three four five six seven eight nine ten eleven twelve"
        cache.record_failure(text)

        class BatchEncoding(dict):
            def __init__(self, batch_words):
                input_ids = [[1] * len(words) for words in batch_words]
                super().__init__(input_ids=input_ids, attention_mask=input_ids)
                self.batch_words = batch_words

            def word_ids(self, batch_index=0):
                return list(range(len(self.batch_words[batch_index])))

        class BatchTokenizer:
            def __call__(self, batch_words, **kwargs):
                return BatchEncoding(batch_words)

        class BatchModel:
            def get_scores(self, input_ids, attention_mask):
                return [[0.0] * len(row) for row in input_ids]

        monkeypatch.setattr(
            kc,
            "_load_kompress",
            lambda *args, **kwargs: (BatchModel(), BatchTokenizer(), "onnx_gpu"),
        )
        monkeypatch.setattr(
            compressor, "_should_batch_single_content", lambda *args, **kwargs: True
        )
        monkeypatch.setattr(compressor, "_should_use_sequential_fallback", lambda: False)
        monkeypatch.setenv("HEADROOM_KOMPRESS_EXECUTION_TIMEOUT_MS", "0")
        monkeypatch.setenv("HEADROOM_KOMPRESS_MAX_CONCURRENT", "1")
        monkeypatch.setattr(kc, "_execution_semaphores", {})
        semaphore = kc._execution_semaphore("onnx_gpu", "onnx_gpu")
        assert semaphore.acquire(blocking=False)
        try:
            result = compressor.compress(text)
        finally:
            semaphore.release()

        assert result.compressed == text
        entry = cache.lookup(text)
        assert entry is not None
        assert entry.attempts == 1

    def test_success_replaces_cached_failure(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=3)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        text = "one two three four five six seven eight nine ten eleven twelve"
        outcomes = iter(
            [
                (compressor._passthrough(text, 12), "failure"),
                (
                    kc.KompressResult(
                        compressed="one two three",
                        original=text,
                        original_tokens=12,
                        compressed_tokens=3,
                        compression_ratio=0.25,
                    ),
                    "success",
                ),
            ]
        )
        monkeypatch.setattr(
            compressor, "_compress_uncached", lambda *args, **kwargs: next(outcomes)
        )

        assert compressor.compress(text).compressed == text
        assert compressor.compress(text).compressed == "one two three"
        assert compressor.compress(text).compressed == "one two three"

    def test_no_compression_success_discards_retryable_failure(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=3)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        text = "one two three four five six seven eight nine ten eleven twelve"
        calls = 0

        def outcomes(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls in {1, 3}:
                raise TimeoutError("transient inference failure")
            return compressor._passthrough(text, 12), "success"

        monkeypatch.setattr(compressor, "_compress_uncached", outcomes)

        compressor.compress(text)
        assert cache.lookup(text) is not None

        no_compression = compressor.compress(text)
        assert no_compression.compressed == text
        assert cache.lookup(text) is None

        compressor.compress(text)
        entry = cache.lookup(text)
        assert calls == 3
        assert entry is not None
        assert entry.attempts == 1

    def test_execution_saturation_is_not_cached(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        text = "one two three four five six seven eight nine ten eleven twelve"
        monkeypatch.setattr(
            compressor,
            "_compress_uncached",
            lambda *args, **kwargs: (_ for _ in ()).throw(kc._KompressExecutionSaturated()),
        )

        result = compressor.compress(text)

        assert result.compressed == text
        assert cache.lookup(text) is None

    def test_cache_hit_regenerates_ccr_marker_for_current_original(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor()
        text = "one two three four five six seven eight nine ten eleven twelve"
        stored: list[tuple[str, str]] = []
        monkeypatch.setattr(
            compressor,
            "_store_in_ccr",
            lambda original, compressed, original_tokens: (
                stored.append((original, compressed)) or "fresh-key"
            ),
        )
        cache.record_success(text, "one two three", 12, 3)

        result = compressor.compress(text, ccr_original="current original")

        assert result.cache_key == "fresh-key"
        assert "hash=fresh-key" in result.compressed
        assert stored == [("current original", "one two three")]
        cached = cache.lookup(text)
        assert cached is not None
        assert cached.compressed == "one two three"
        assert cached.compressed is not None and "fresh-key" not in cached.compressed


# ── Transform interface ─────────────────────────────────────────────────


class TestKompressTransformInterface:
    def test_apply_short_messages_unchanged(self) -> None:
        """Messages with <10 words should pass through apply() unchanged."""
        from headroom.transforms.kompress_compressor import KompressCompressor

        compressor = KompressCompressor()
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "tool", "content": "short"},
        ]
        tokenizer = MagicMock()
        tokenizer.count_text = MagicMock(return_value=5)

        result = compressor.apply(messages, tokenizer)
        assert len(result.messages) == 2
        assert result.messages[0]["content"] == "hello"
        assert result.messages[1]["content"] == "short"

    def test_apply_preserves_user_messages(self) -> None:
        """User messages should never be compressed."""
        from headroom.transforms.kompress_compressor import KompressCompressor

        compressor = KompressCompressor()
        long_text = " ".join(f"word{i}" for i in range(50))
        messages = [{"role": "user", "content": long_text}]
        tokenizer = MagicMock()
        tokenizer.count_text = MagicMock(return_value=50)

        with patch(
            "headroom.transforms.kompress_compressor._load_kompress",
            side_effect=RuntimeError("should not be called"),
        ):
            result = compressor.apply(messages, tokenizer)
            assert result.messages[0]["content"] == long_text


# ── compress_batch ──────────────────────────────────────────────────────


class TestKompressCompressorBatch:
    """Tests for the batched compression API (compress_batch).

    These exercise the non-model paths -- passthrough handling, argument
    validation, order preservation, and fallback behavior on model-load
    failure. The actual batched inference path is covered by integration
    tests that require the model to be downloaded.
    """

    def test_batch_prepass_reuses_cache_preserves_order_and_regenerates_ccr(
        self, monkeypatch
    ) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor()
        cached = "cached " * 20
        fresh = "fresh " * 20
        cache.record_success(cached, "cached", 20, 1)
        stored: list[str] = []
        inference_calls = 0

        class FakeEncoding(dict):
            def __init__(self, batch_words):
                input_ids = [[1] * len(words) for words in batch_words]
                super().__init__(input_ids=input_ids, attention_mask=input_ids)
                self.batch_words = batch_words

            def word_ids(self, batch_index=0):
                return list(range(len(self.batch_words[batch_index])))

        class FakeTokenizer:
            def __call__(self, batch_words, **kwargs):
                return FakeEncoding(batch_words)

        class FakeModel:
            def get_scores(self, input_ids, attention_mask):
                nonlocal inference_calls
                inference_calls += 1
                return [[1.0] + [0.0] * (len(row) - 1) for row in input_ids]

        monkeypatch.setattr(compressor, "_should_use_sequential_fallback", lambda: False)
        monkeypatch.setattr(
            kc,
            "_load_kompress",
            lambda *args, **kwargs: (FakeModel(), FakeTokenizer(), "onnx"),
        )
        monkeypatch.setattr(
            compressor,
            "_store_in_ccr",
            lambda original, compressed, original_tokens: stored.append(original) or "key",
        )

        results = compressor.compress_batch([fresh, cached], batch_size=32)

        assert [result.original for result in results] == [fresh, cached]
        assert [
            result.compressed.startswith(prefix)
            for result, prefix in zip(results, ("fresh", "cached"), strict=True)
        ] == [True, True]
        assert [result.cache_key for result in results] == ["key", "key"]
        assert sorted(stored) == sorted([fresh, cached])
        assert inference_calls == 1
        assert cache.stats()["hits"] == 1

    def test_concurrent_gpu_batches_single_flight_identical_miss(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        text = "one two three four five six seven eight nine ten eleven twelve"
        inference_started = threading.Event()
        release_inference = threading.Event()
        calls = 0

        class BatchEncoding(dict):
            def __init__(self, batch_words):
                input_ids = [[1] * len(words) for words in batch_words]
                super().__init__(input_ids=input_ids, attention_mask=input_ids)
                self.batch_words = batch_words

            def word_ids(self, batch_index=0):
                return list(range(len(self.batch_words[batch_index])))

        class BatchTokenizer:
            def __call__(self, batch_words, **kwargs):
                return BatchEncoding(batch_words)

        class BatchModel:
            def get_scores(self, input_ids, attention_mask):
                nonlocal calls
                calls += 1
                inference_started.set()
                assert release_inference.wait(timeout=1)
                return [[1.0] + [0.0] * (len(row) - 1) for row in input_ids]

        monkeypatch.setattr(
            kc,
            "_load_kompress",
            lambda *args, **kwargs: (BatchModel(), BatchTokenizer(), "onnx_gpu"),
        )
        results: list[list[kc.KompressResult]] = []
        first = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        second = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        for compressor in (first, second):
            monkeypatch.setattr(compressor, "_should_use_sequential_fallback", lambda: False)

        first_thread = threading.Thread(target=lambda: results.append(first.compress_batch([text])))
        second_thread = threading.Thread(
            target=lambda: results.append(second.compress_batch([text]))
        )
        first_thread.start()
        assert inference_started.wait(timeout=1)
        second_thread.start()
        second_thread.join(timeout=0.05)
        assert second_thread.is_alive()
        release_inference.set()
        first_thread.join(timeout=1)
        second_thread.join(timeout=1)

        assert not first_thread.is_alive()
        assert not second_thread.is_alive()
        assert len(results) == 2
        assert all(batch[0].compressed.startswith("one") for batch in results)
        assert calls == 1
        assert cache._inflight == {}

    def test_batch_size_zero_releases_gpu_claims(self, monkeypatch) -> None:
        import pytest

        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        text = "one two three four five six seven eight nine ten eleven twelve"

        class BatchEncoding(dict):
            def __init__(self, batch_words):
                input_ids = [[1] * len(words) for words in batch_words]
                super().__init__(input_ids=input_ids, attention_mask=input_ids)
                self.batch_words = batch_words

            def word_ids(self, batch_index=0):
                return list(range(len(self.batch_words[batch_index])))

        class BatchTokenizer:
            def __call__(self, batch_words, **kwargs):
                return BatchEncoding(batch_words)

        class BatchModel:
            def get_scores(self, input_ids, attention_mask):
                return [[1.0] + [0.0] * (len(row) - 1) for row in input_ids]

        monkeypatch.setattr(
            kc,
            "_load_kompress",
            lambda *args, **kwargs: (BatchModel(), BatchTokenizer(), "onnx_gpu"),
        )
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        monkeypatch.setattr(compressor, "_should_use_sequential_fallback", lambda: False)

        with pytest.raises(ValueError, match="batch_size"):
            compressor.compress_batch([text], batch_size=0)

        assert cache._inflight == {}
        [result] = compressor.compress_batch([text], batch_size=32)
        assert result.compressed.startswith("one")
        assert cache._inflight == {}

    def test_gpu_batch_retries_use_remaining_deadline(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        text = "one two three four five six seven eight nine ten eleven twelve"
        cache.record_failure(text)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        monkeypatch.setattr(
            kc, "_load_kompress", lambda *args, **kwargs: (object(), object(), "onnx_gpu")
        )
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        monkeypatch.setattr(compressor, "_should_use_sequential_fallback", lambda: False)
        remaining = iter((0.05, 0.0))
        deadline_checks: list[float] = []
        waits: list[float] = []

        def remaining_timeout(_):
            timeout = next(remaining)
            deadline_checks.append(timeout)
            return timeout

        monkeypatch.setattr(kc, "_single_flight_wait_timeout_seconds", remaining_timeout)

        def acquire(content, namespace, *, timeout_seconds):
            waits.append(timeout_seconds)
            return False if len(waits) == 1 else None

        monkeypatch.setattr(cache, "acquire_inference", acquire)

        [result] = compressor.compress_batch([text])

        assert result.compressed == text
        assert deadline_checks == [0.05, 0.0]
        assert waits == [0.05]
        assert cache.stats()["failures"] == 1

    def test_batch_preserves_cached_long_result_when_other_input_is_short(
        self, monkeypatch
    ) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        monkeypatch.setattr(kc, "_kompress_cache", {})
        cached = "cached " * 20
        short = "short input"
        cache.record_success(cached, "compressed", 20, 1)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        loads = 0

        def unexpected_load(*args, **kwargs):
            nonlocal loads
            loads += 1
            raise AssertionError("model should not be loaded for short active inputs")

        monkeypatch.setattr(kc, "_load_kompress", unexpected_load)
        monkeypatch.setattr(
            compressor,
            "_should_use_sequential_fallback",
            lambda: (_ for _ in ()).throw(AssertionError("backend should not be detected")),
        )

        results = compressor.compress_batch([cached, short])

        assert [result.original for result in results] == [cached, short]
        assert [result.compressed for result in results] == ["compressed", short]
        assert loads == 0

    def test_batch_failure_records_payloads_but_saturation_does_not(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor()
        contents = ["failure " * 20, "saturated " * 20]
        monkeypatch.setattr(compressor, "_should_use_sequential_fallback", lambda: False)

        class BatchEncoding(dict):
            def __init__(self, batch_words):
                input_ids = [[1] * len(words) for words in batch_words]
                super().__init__(input_ids=input_ids, attention_mask=input_ids)
                self.batch_words = batch_words

            def word_ids(self, batch_index=0):
                return list(range(len(self.batch_words[batch_index])))

        class BatchTokenizer:
            def __call__(self, batch_words, **kwargs):
                return BatchEncoding(batch_words)

        class FailureModel:
            def get_scores(self, input_ids, attention_mask):
                raise TimeoutError("batch timeout")

        monkeypatch.setattr(
            kc,
            "_load_kompress",
            lambda *args, **kwargs: (FailureModel(), BatchTokenizer(), "onnx"),
        )

        results = compressor.compress_batch(contents)

        assert [result.compressed for result in results] == contents
        for content in contents:
            failure_entry = cache.lookup(content)
            assert failure_entry is not None
            assert failure_entry.attempts == 1

        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)

        class SaturationModel:
            def get_scores(self, input_ids, attention_mask):
                return [[1.0] + [0.0] * (len(row) - 1) for row in input_ids]

        monkeypatch.setattr(
            kc,
            "_load_kompress",
            lambda *args, **kwargs: (SaturationModel(), BatchTokenizer(), "onnx"),
        )

        monkeypatch.setenv("HEADROOM_KOMPRESS_EXECUTION_TIMEOUT_MS", "0")
        monkeypatch.setenv("HEADROOM_KOMPRESS_MAX_CONCURRENT", "1")
        monkeypatch.setattr(kc, "_execution_semaphores", {})
        semaphore = kc._execution_semaphore("onnx", "onnx")
        assert semaphore.acquire(blocking=False)
        try:
            compressor.compress_batch(contents)
        finally:
            semaphore.release()

        assert cache.lookup(contents[0]) is None
        assert cache.lookup(contents[1]) is None

    def test_batch_failure_records_duplicate_payload_once_per_inference(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        content = "duplicate " * 20
        contents = [content, content]
        monkeypatch.setattr(compressor, "_should_use_sequential_fallback", lambda: False)
        calls = 0

        class BatchEncoding(dict):
            def __init__(self, batch_words):
                input_ids = [[1] * len(words) for words in batch_words]
                super().__init__(input_ids=input_ids, attention_mask=input_ids)
                self.batch_words = batch_words

            def word_ids(self, batch_index=0):
                return list(range(len(self.batch_words[batch_index])))

        class BatchTokenizer:
            def __call__(self, batch_words, **kwargs):
                return BatchEncoding(batch_words)

        class FailureModel:
            def get_scores(self, input_ids, attention_mask):
                nonlocal calls
                calls += 1
                raise TimeoutError("batch timeout")

        monkeypatch.setattr(
            kc,
            "_load_kompress",
            lambda *args, **kwargs: (FailureModel(), BatchTokenizer(), "onnx"),
        )

        first_results = compressor.compress_batch(contents)
        first_entry = cache.lookup(content)

        assert [result.compressed for result in first_results] == contents
        assert first_entry is not None
        assert first_entry.attempts == 1
        assert not first_entry.exhausted
        assert calls == 1

        second_results = compressor.compress_batch(contents)
        second_entry = cache.lookup(content)

        assert [result.compressed for result in second_results] == contents
        assert second_entry is not None
        assert second_entry.attempts == 2
        assert second_entry.exhausted
        assert calls == 2

    def test_batch_no_compression_success_discards_retryable_failure(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=3)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        monkeypatch.setattr(compressor, "_should_use_sequential_fallback", lambda: False)
        content = "one two three four five six seven eight nine ten eleven twelve"
        calls = 0

        class BatchEncoding(dict):
            def __init__(self, batch_words):
                input_ids = [[1] * len(words) for words in batch_words]
                super().__init__(input_ids=input_ids, attention_mask=input_ids)
                self.batch_words = batch_words

            def word_ids(self, batch_index=0):
                return list(range(len(self.batch_words[batch_index])))

        class BatchTokenizer:
            def __call__(self, batch_words, **kwargs):
                return BatchEncoding(batch_words)

        class Model:
            def get_scores(self, input_ids, attention_mask):
                nonlocal calls
                calls += 1
                if calls in {1, 3}:
                    raise TimeoutError("transient batch failure")
                return [[0.0] * len(row) for row in input_ids]

        monkeypatch.setattr(
            kc,
            "_load_kompress",
            lambda *args, **kwargs: (Model(), BatchTokenizer(), "onnx"),
        )

        compressor.compress_batch([content])
        assert cache.lookup(content) is not None

        no_compression = compressor.compress_batch([content])[0]
        assert no_compression.compressed == content
        assert cache.lookup(content) is None

        compressor.compress_batch([content])
        entry = cache.lookup(content)
        assert calls == 3
        assert entry is not None
        assert entry.attempts == 1

    def test_batch_model_not_cached_does_not_record_payload_failure(self, monkeypatch) -> None:
        content = "model unavailable " * 20
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor()
        monkeypatch.setattr(compressor, "_should_use_sequential_fallback", lambda: False)
        monkeypatch.setattr(
            kc,
            "_load_kompress",
            lambda *args, **kwargs: (_ for _ in ()).throw(kc.KompressModelNotCached("not cached")),
        )

        [result] = compressor.compress_batch([content])

        assert result.compressed == content
        assert cache.lookup(content) is None

    def test_batch_generic_model_load_failure_retries_without_cache_entry(
        self, monkeypatch
    ) -> None:
        content = "batch model failed " * 20
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor(kc.KompressConfig(enable_ccr=False))
        monkeypatch.setattr(compressor, "_should_use_sequential_fallback", lambda: False)
        loads = 0

        def failing_load(*args, **kwargs):
            nonlocal loads
            loads += 1
            raise RuntimeError("batch model load failed")

        monkeypatch.setattr(kc, "_load_kompress", failing_load)

        first = compressor.compress_batch([content])
        second = compressor.compress_batch([content])

        assert [result.compressed for result in first] == [content]
        assert [result.compressed for result in second] == [content]
        assert loads == 2
        assert cache.lookup(content) is None

    def test_target_ratio_bypasses_cache_but_none_reuses_payload_result(self, monkeypatch) -> None:
        content = "ratio-sensitive " * 20
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        cache.record_success(content, "cached", 20, 1)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor()
        monkeypatch.setattr(compressor, "_store_in_ccr", lambda *args: "ratio-key")
        uncached_calls: list[float | None] = []

        def fake_uncached(content, **kwargs):
            uncached_calls.append(kwargs["target_ratio"])
            return (
                kc.KompressResult(
                    compressed="ratio-specific",
                    original=content,
                    original_tokens=20,
                    compressed_tokens=2,
                    compression_ratio=0.1,
                ),
                "success",
            )

        monkeypatch.setattr(compressor, "_compress_uncached", fake_uncached)

        ratio_result = compressor.compress(content, target_ratio=0.5)
        cached_result = compressor.compress(content)

        assert ratio_result.compressed.startswith("ratio-specific")
        assert ratio_result.cache_key == "ratio-key"
        assert cached_result.compressed.startswith("cached")
        assert uncached_calls == [0.5]
        assert cache.stats()["hits"] == 1

    def test_execution_stats_include_cache_counters_without_payload_data(self, monkeypatch) -> None:
        cache = KompressCache(max_entries=7, max_bytes=1234, max_attempts=2)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        cache.record_failure("secret payload")
        cache.lookup("secret payload")

        stats = kc.get_kompress_execution_stats()

        assert stats["cache_hits_total"] == 1
        assert stats["cache_failures_total"] == 1
        assert stats["cache_max_entries"] == 7
        assert stats["cache_max_bytes"] == 1234
        assert "secret payload" not in str(stats)
        assert "digest" not in stats

    def test_batch_degraded_fast_path_bypasses_result_cache(self, monkeypatch) -> None:
        content = "degraded " * 20
        cache = KompressCache(max_entries=10, max_bytes=100_000, max_attempts=2)
        cache.record_success(content, "compressed", 20, 1)
        monkeypatch.setattr(kc, "get_kompress_cache", lambda: cache)
        compressor = kc.KompressCompressor()
        compressor._degraded_reason = "model unavailable"

        [result] = compressor.compress_batch([content])

        assert result.compressed == content
        assert cache.stats()["hits"] == 0

    def test_empty_batch_returns_empty_list(self) -> None:
        from headroom.transforms.kompress_compressor import KompressCompressor

        compressor = KompressCompressor()
        result = compressor.compress_batch([])
        assert result == []

    def test_all_short_texts_passthrough_without_model(self, monkeypatch) -> None:
        """Texts under 10 words must passthrough; model never loaded."""
        from headroom.transforms.kompress_compressor import KompressCompressor

        monkeypatch.setattr(kc, "_kompress_cache", {})
        compressor = KompressCompressor()
        contents = ["hello", "world", "short text here"]
        loads = 0

        def unexpected_load(*args, **kwargs):
            nonlocal loads
            loads += 1
            raise AssertionError("model should not be loaded for short texts")

        with patch(
            "headroom.transforms.kompress_compressor._load_kompress",
            side_effect=unexpected_load,
        ):
            results = compressor.compress_batch(contents)

        assert len(results) == 3
        for i, r in enumerate(results):
            assert r.compressed == contents[i]
            assert r.compression_ratio == 1.0
        assert loads == 0

    def test_order_preserved(self) -> None:
        """Output order must match input order even when model load fails."""
        from headroom.transforms.kompress_compressor import KompressCompressor

        compressor = KompressCompressor()
        long_texts = [
            " ".join(f"alpha{i}" for i in range(20)),
            " ".join(f"beta{i}" for i in range(20)),
            " ".join(f"gamma{i}" for i in range(20)),
        ]

        with patch(
            "headroom.transforms.kompress_compressor._load_kompress",
            side_effect=RuntimeError("no model"),
        ):
            results = compressor.compress_batch(long_texts)

        assert len(results) == 3
        assert results[0].original.startswith("alpha0")
        assert results[1].original.startswith("beta0")
        assert results[2].original.startswith("gamma0")

    def test_mixed_short_and_long_passthrough_on_model_failure(self) -> None:
        """Short texts passthrough; long texts fall back to passthrough on model failure."""
        from headroom.transforms.kompress_compressor import KompressCompressor

        compressor = KompressCompressor()
        contents = [
            "short",
            " ".join(f"word{i}" for i in range(20)),  # triggers model path
            "also short",
        ]

        with patch(
            "headroom.transforms.kompress_compressor._load_kompress",
            side_effect=RuntimeError("no model"),
        ):
            results = compressor.compress_batch(contents)

        assert len(results) == 3
        assert results[0].compressed == "short"
        assert results[0].compression_ratio == 1.0
        assert results[1].compression_ratio == 1.0  # passthrough fallback
        assert results[2].compressed == "also short"

    def test_ratio_list_length_mismatch_raises(self) -> None:
        """If target_ratio is a list it must match contents length."""
        from headroom.transforms.kompress_compressor import KompressCompressor

        compressor = KompressCompressor()
        contents = ["a b c", "d e f"]

        # Too short
        try:
            compressor.compress_batch(contents, target_ratio=[0.5])
            raise AssertionError("expected ValueError for length mismatch")
        except ValueError as e:
            assert "length" in str(e).lower()

        # Too long
        try:
            compressor.compress_batch(contents, target_ratio=[0.5, 0.5, 0.5])
            raise AssertionError("expected ValueError for length mismatch")
        except ValueError as e:
            assert "length" in str(e).lower()

    def test_batch_of_one_equivalent_to_single_compress_on_short_text(self) -> None:
        """Batch-of-one with short text should produce identical passthrough."""
        from headroom.transforms.kompress_compressor import KompressCompressor

        compressor = KompressCompressor()
        text = "hello world"

        single = compressor.compress(text)
        batch = compressor.compress_batch([text])

        assert len(batch) == 1
        assert batch[0].compressed == single.compressed
        assert batch[0].compression_ratio == single.compression_ratio
        assert batch[0].original_tokens == single.original_tokens

    def test_uniform_ratio_scalar(self) -> None:
        """A scalar target_ratio must apply to every text in the batch."""
        from headroom.transforms.kompress_compressor import KompressCompressor

        compressor = KompressCompressor()
        # Short texts -- passthrough regardless of ratio
        contents = ["short a", "short b", "short c"]

        results = compressor.compress_batch(contents, target_ratio=0.3)

        assert len(results) == 3
        for r, original in zip(results, contents, strict=True):
            assert r.compressed == original  # short passthrough

    def test_per_item_ratio_list_with_nones(self) -> None:
        """A list of ratios with some None entries must be accepted."""
        from headroom.transforms.kompress_compressor import KompressCompressor

        compressor = KompressCompressor()
        contents = ["short a", "short b", "short c"]
        ratios: list[float | None] = [0.5, None, 0.25]

        # Short texts always passthrough; validating the list shape alone.
        results = compressor.compress_batch(contents, target_ratio=ratios)
        assert len(results) == 3


# ── unload_kompress_model ───────────────────────────────────────────────


class TestUnloadKompressModel:
    def test_unload_when_no_model(self) -> None:
        import headroom.transforms.kompress_compressor as kmod
        from headroom.transforms.kompress_compressor import unload_kompress_model

        # Ensure no model is loaded (previous tests may have set the cache)
        kmod._kompress_cache.clear()

        # Should return False when no model is loaded
        assert unload_kompress_model() is False


# ── onnx_coreml backend gating (issue #2442) ────────────────────────────


class TestOnnxBackendPrefixGating:
    """Non-CPU ONNX backends (onnx_coreml, onnx_cpu) must take the ONNX path.

    The bug: sites gated on the exact string ``backend == "onnx"`` misclassified
    ``onnx_coreml`` as PyTorch and called ``next(model.parameters())`` on the
    ``_OnnxModel`` wrapper, which has no ``.parameters()`` — crashing every call
    and silently disabling Kompress. The fix uses ``backend.startswith("onnx")``.
    """

    class _FakeOnnxModel:
        """Mimics the ONNX wrapper: has get_keep_mask but no .parameters()."""

        def get_keep_mask(self, input_ids, attention_mask):  # noqa: ANN001, ANN201
            return [[True]]

    @staticmethod
    def _fake_tokenizer(words, **kwargs):  # noqa: ANN001, ANN205
        # ONNX path must request numpy tensors, never torch.
        assert kwargs.get("return_tensors") == "np"
        return {"input_ids": [[1, 2]], "attention_mask": [[1, 1]]}

    def test_timed_canary_onnx_coreml_skips_pytorch_device_dispatch(self) -> None:
        from headroom.transforms.kompress_compressor import KompressCompressor

        compressor = KompressCompressor()
        model = self._FakeOnnxModel()  # no .parameters()

        # Must not raise AttributeError: '_OnnxModel' object has no attribute
        # 'parameters'; returns a float wall-clock duration.
        elapsed = compressor._timed_canary(model, self._fake_tokenizer, "onnx_coreml")
        assert isinstance(elapsed, float)

    def test_explicit_onnx_cpu_skips_gpu_provider_probe(self, monkeypatch) -> None:
        import pytest

        import headroom.transforms.kompress_compressor as kmod

        kmod._kompress_cache.clear()
        monkeypatch.setenv("HEADROOM_KOMPRESS_BACKEND", "onnx_cpu")
        monkeypatch.setattr(
            kmod,
            "_available_gpu_providers",
            lambda: pytest.fail("explicit onnx_cpu must not probe GPU providers"),
        )
        monkeypatch.setattr(kmod, "_create_onnx_session", lambda *args, **kwargs: object())
        monkeypatch.setattr(kmod, "_load_modernbert_tokenizer", lambda *args, **kwargs: object())

        _model, _tokenizer, backend = kmod._load_kompress("test-model", allow_download=False)

        assert backend == "onnx"

    def test_timed_canary_pytorch_still_dispatches_to_device(self) -> None:
        # Negative control: the PyTorch branch DOES touch .parameters(), so the
        # paramless fake model raises there — proving the test above is only
        # green because onnx_coreml correctly skips that branch.
        import pytest

        from headroom.transforms.kompress_compressor import KompressCompressor

        compressor = KompressCompressor()
        model = self._FakeOnnxModel()

        def pt_tokenizer(words, **kwargs):  # noqa: ANN001, ANN202
            assert kwargs.get("return_tensors") == "pt"
            return {"input_ids": [[1, 2]], "attention_mask": [[1, 1]]}

        with pytest.raises(AttributeError):
            compressor._timed_canary(model, pt_tokenizer, "pytorch")


class TestPytorchWeightLoading:
    """_load_pytorch_weights must load the merged v2 checkpoint format correctly,
    fall back to the plain format only when the repo genuinely has no merged.pt,
    and refuse to run on a state-dict mismatch instead of silently ignoring it.
    """

    @staticmethod
    def _make_model(torch):
        import torch.nn as nn

        model = nn.Module()
        model.encoder = nn.Linear(4, 4)
        model.token_head = nn.Linear(4, 2)
        model.span_conv = nn.Sequential(nn.Conv1d(4, 4, 1), nn.GELU())
        return model

    def test_merged_checkpoint_loads_into_matching_submodules(self, tmp_path, monkeypatch) -> None:
        import pytest

        torch = pytest.importorskip("torch")
        import headroom.transforms.kompress_compressor as kmod

        model = self._make_model(torch)
        ckpt_path = tmp_path / "merged.pt"
        torch.save(
            {
                "encoder_state_dict": model.encoder.state_dict(),
                "token_head_state_dict": model.token_head.state_dict(),
                "span_conv_state_dict": model.span_conv.state_dict(),
            },
            ckpt_path,
        )

        fresh_model = self._make_model(torch)
        monkeypatch.setattr(kmod, "hf_hub_download_local_first", lambda *a, **k: str(ckpt_path))

        kmod._load_pytorch_weights(fresh_model, "some/repo", allow_download=True)

        for name, param in model.encoder.state_dict().items():
            assert torch.equal(param, fresh_model.encoder.state_dict()[name])

    def test_merged_checkpoint_load_disables_weights_only_restriction(
        self, tmp_path, monkeypatch
    ) -> None:
        import pytest

        torch = pytest.importorskip("torch")
        import headroom.transforms.kompress_compressor as kmod

        model = self._make_model(torch)
        ckpt_path = tmp_path / "merged.pt"
        torch.save(
            {
                "encoder_state_dict": model.encoder.state_dict(),
                "token_head_state_dict": model.token_head.state_dict(),
                "span_conv_state_dict": model.span_conv.state_dict(),
            },
            ckpt_path,
        )

        fresh_model = self._make_model(torch)
        monkeypatch.setattr(kmod, "hf_hub_download_local_first", lambda *a, **k: str(ckpt_path))
        original_torch_load = torch.load
        load_kwargs: dict[str, object] = {}

        def recording_load(*args, **kwargs):  # noqa: ANN002, ANN003
            load_kwargs.update(kwargs)
            return original_torch_load(*args, **kwargs)

        monkeypatch.setattr(torch, "load", recording_load)

        kmod._load_pytorch_weights(fresh_model, "some/repo", allow_download=True)

        assert load_kwargs["weights_only"] is False

    def test_merged_checkpoint_missing_section_raises(self, tmp_path, monkeypatch) -> None:
        import pytest

        torch = pytest.importorskip("torch")
        import headroom.transforms.kompress_compressor as kmod

        ckpt_path = tmp_path / "merged.pt"
        torch.save({"encoder_state_dict": {}}, ckpt_path)

        fresh_model = self._make_model(torch)
        monkeypatch.setattr(kmod, "hf_hub_download_local_first", lambda *a, **k: str(ckpt_path))

        with pytest.raises(RuntimeError, match="missing"):
            kmod._load_pytorch_weights(fresh_model, "some/repo", allow_download=True)

    def test_merged_checkpoint_key_mismatch_raises_instead_of_silently_dropping(
        self, tmp_path, monkeypatch
    ) -> None:
        """Regression test for the bug this loader used to have: loading a
        state-dict that does not match the module tree (e.g. an unmerged PEFT
        checkpoint) must fail loudly, not silently skip the mismatched keys.
        """
        import pytest

        torch = pytest.importorskip("torch")
        import headroom.transforms.kompress_compressor as kmod

        ckpt_path = tmp_path / "merged.pt"
        torch.save(
            {
                # Wrong prefix, mimics the unmerged PEFT structure documented
                # in scripts/export_kompress_v2_onnx.py.
                "encoder_state_dict": {"base_model.model.weight": torch.zeros(4, 4)},
                "token_head_state_dict": {},
                "span_conv_state_dict": {},
            },
            ckpt_path,
        )

        fresh_model = self._make_model(torch)
        monkeypatch.setattr(kmod, "hf_hub_download_local_first", lambda *a, **k: str(ckpt_path))

        with pytest.raises(RuntimeError, match="state_dict mismatch"):
            kmod._load_pytorch_weights(fresh_model, "some/repo", allow_download=True)

    def test_missing_merged_pt_falls_back_to_plain_safetensors(self, tmp_path, monkeypatch) -> None:
        import pytest

        torch = pytest.importorskip("torch")
        safetensors_torch = pytest.importorskip("safetensors.torch")
        import headroom.transforms.kompress_compressor as kmod

        model = self._make_model(torch)
        weights_path = tmp_path / "model.safetensors"
        safetensors_torch.save_file(dict(model.state_dict()), str(weights_path))

        fresh_model = self._make_model(torch)

        def fake_download(model_id, filename, *, allow_network=True, **kwargs):  # noqa: ANN001
            if filename == "merged.pt":
                raise kmod.EntryNotFoundError("no merged.pt in this repo")
            assert filename == "model.safetensors"
            return str(weights_path)

        monkeypatch.setattr(kmod, "hf_hub_download_local_first", fake_download)

        kmod._load_pytorch_weights(fresh_model, "some/v1/repo", allow_download=True)

        for name, param in model.state_dict().items():
            assert torch.equal(param, fresh_model.state_dict()[name])

    def test_plain_safetensors_key_mismatch_raises(self, tmp_path, monkeypatch) -> None:
        import pytest

        torch = pytest.importorskip("torch")
        safetensors_torch = pytest.importorskip("safetensors.torch")
        import headroom.transforms.kompress_compressor as kmod

        weights_path = tmp_path / "model.safetensors"
        safetensors_torch.save_file({"totally.unrelated.key": torch.zeros(2)}, str(weights_path))

        fresh_model = self._make_model(torch)

        def fake_download(model_id, filename, *, allow_network=True, **kwargs):  # noqa: ANN001
            if filename == "merged.pt":
                raise kmod.EntryNotFoundError("no merged.pt in this repo")
            return str(weights_path)

        monkeypatch.setattr(kmod, "hf_hub_download_local_first", fake_download)

        with pytest.raises(RuntimeError, match="state_dict mismatch"):
            kmod._load_pytorch_weights(fresh_model, "some/v1/repo", allow_download=True)

    def test_cache_only_miss_raises_kompress_model_not_cached(self, monkeypatch) -> None:
        import pytest

        pytest.importorskip("torch")
        import headroom.transforms.kompress_compressor as kmod

        def fake_download(model_id, filename, *, allow_network=True, **kwargs):  # noqa: ANN001
            raise kmod.LocalEntryNotFoundError("not cached")

        monkeypatch.setattr(kmod, "hf_hub_download_local_first", fake_download)
        monkeypatch.setattr(kmod, "hf_entry_known_absent", lambda *a, **k: False)

        with pytest.raises(kmod.KompressModelNotCached):
            kmod._load_pytorch_weights(SimpleNamespace(), "some/repo", allow_download=False)

    def test_cache_only_defers_instead_of_using_stale_plain_checkpoint(self, monkeypatch) -> None:
        """Regression test: if merged.pt is not cached yet and we have no
        confirmation it is genuinely absent upstream, a stale model.safetensors
        left over from a previous (pre-fix) run must NOT be used as a silent
        fallback - that would reintroduce the original bug for exactly the
        upgrade scenario that motivated this fix. It must defer instead.
        """
        import pytest

        pytest.importorskip("torch")
        import headroom.transforms.kompress_compressor as kmod

        plain_download_calls: list[str] = []

        def fake_download(model_id, filename, *, allow_network=True, **kwargs):  # noqa: ANN001
            if filename == "merged.pt":
                raise kmod.LocalEntryNotFoundError("merged.pt not cached yet")
            plain_download_calls.append(filename)
            return "/fake/cached/model.safetensors"

        monkeypatch.setattr(kmod, "hf_hub_download_local_first", fake_download)
        # Nothing has ever confirmed merged.pt is absent from this repo -
        # simulates a v2-style repo mid-upgrade, not a v1-style repo.
        monkeypatch.setattr(kmod, "hf_entry_known_absent", lambda *a, **k: False)

        with pytest.raises(kmod.KompressModelNotCached):
            kmod._load_pytorch_weights(
                SimpleNamespace(), "chopratejas/kompress-v2-base", allow_download=False
            )

        assert plain_download_calls == [], (
            "must not fall back to model.safetensors without confirming merged.pt is absent"
        )

    def test_cache_only_uses_plain_checkpoint_when_merged_pt_confirmed_absent(
        self, tmp_path, monkeypatch
    ) -> None:
        import pytest

        torch = pytest.importorskip("torch")
        safetensors_torch = pytest.importorskip("safetensors.torch")
        import headroom.transforms.kompress_compressor as kmod

        model = self._make_model(torch)
        weights_path = tmp_path / "model.safetensors"
        safetensors_torch.save_file(dict(model.state_dict()), str(weights_path))

        fresh_model = self._make_model(torch)

        def fake_download(model_id, filename, *, allow_network=True, **kwargs):  # noqa: ANN001
            if filename == "merged.pt":
                raise kmod.LocalEntryNotFoundError("merged.pt not cached")
            return str(weights_path)

        monkeypatch.setattr(kmod, "hf_hub_download_local_first", fake_download)
        # A prior real network lookup already confirmed this repo has no
        # merged.pt at all (the v1-style case), so the plain fallback is safe.
        monkeypatch.setattr(kmod, "hf_entry_known_absent", lambda *a, **k: True)

        kmod._load_pytorch_weights(fresh_model, "chopratejas/kompress-base", allow_download=False)

        for name, param in model.state_dict().items():
            assert torch.equal(param, fresh_model.state_dict()[name])

    def test_cache_only_raises_when_confirmed_absent_but_plain_also_missing(
        self, monkeypatch
    ) -> None:
        """merged.pt confirmed absent, but the plain fallback isn't cached either:
        still nothing to load from, so this must defer rather than raise a
        confusing lower-level error.
        """
        import pytest

        pytest.importorskip("torch")
        import headroom.transforms.kompress_compressor as kmod

        def fake_download(model_id, filename, *, allow_network=True, **kwargs):  # noqa: ANN001
            raise kmod.LocalEntryNotFoundError(f"{filename} not cached")

        monkeypatch.setattr(kmod, "hf_hub_download_local_first", fake_download)
        monkeypatch.setattr(kmod, "hf_entry_known_absent", lambda *a, **k: True)

        with pytest.raises(kmod.KompressModelNotCached):
            kmod._load_pytorch_weights(
                SimpleNamespace(), "chopratejas/kompress-base", allow_download=False
            )

    def test_genuine_download_failure_propagates_instead_of_falling_back(self, monkeypatch) -> None:
        """A real network/download failure (not a 404, not a cache miss under
        allow_download=False) must propagate as-is, not be swallowed into a
        silent fallback to the plain format.
        """
        import pytest

        pytest.importorskip("torch")
        import headroom.transforms.kompress_compressor as kmod

        def fake_download(model_id, filename, *, allow_network=True, **kwargs):  # noqa: ANN001
            raise OSError("connection reset")

        monkeypatch.setattr(kmod, "hf_hub_download_local_first", fake_download)

        with pytest.raises(OSError, match="connection reset"):
            kmod._load_pytorch_weights(SimpleNamespace(), "some/repo", allow_download=True)


class TestLoadKompressPytorchCaching:
    def test_returns_cached_entry_without_reloading(self, monkeypatch) -> None:
        import pytest

        pytest.importorskip("torch")
        import headroom.transforms.kompress_compressor as kmod

        sentinel = ("cached-model", "cached-tokenizer", "pytorch")
        monkeypatch.setattr(kmod, "_kompress_cache", {"some/repo": sentinel})

        def boom(*a, **k):  # noqa: ANN001, ANN202
            raise AssertionError("should not attempt to reload an already-cached model")

        monkeypatch.setattr(kmod, "_load_pytorch_weights", boom)

        assert kmod._load_kompress_pytorch("some/repo") == sentinel
