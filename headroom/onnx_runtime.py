"""ONNX Runtime helpers for long-running Headroom processes."""

#  Copyright (c) 2026 Noel Kuntze

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import sys
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

ONNX_CPU_ARENA_ENV = "HEADROOM_ONNX_CPU_ARENA"
ONNX_ALLOW_SPINNING_ENV = "HEADROOM_ONNX_ALLOW_SPINNING"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _env_flag(name: str) -> bool | None:
    raw = os.environ.get(name, "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    if raw and raw != "auto":
        logger.warning("%s must be a boolean or 'auto', got %r; using auto", name, raw)
    return None


def cpu_arena_enabled() -> bool:
    """Return whether ONNX Runtime's CPU memory arena should be enabled."""
    override = _env_flag(ONNX_CPU_ARENA_ENV)
    if override is not None:
        return override
    return sys.platform == "win32"


def onnx_thread_spinning_enabled() -> bool:
    """Whether ONNX Runtime intra/inter-op thread pools may spin-wait when idle.

    ORT's thread pools spin-wait on every core between inferences by default, so
    a long-lived proxy that keeps compression/embedding models loaded pegs all
    cores even while completely idle — the machine slows to a crawl after a
    while (#2495). Default to blocking idle threads (spinning off). Set
    ``HEADROOM_ONNX_ALLOW_SPINNING=1`` to restore ORT's spinning for peak
    throughput on a dedicated/batch box.
    """
    override = _env_flag(ONNX_ALLOW_SPINNING_ENV)
    if override is not None:
        return override
    return False


# ── HuggingFace model revision pinning ───────────────────────────────────
#
# Model artifacts are pinned to immutable commit SHAs for supply-chain
# integrity. A changed or compromised upstream repo cannot be pulled
# silently. Pinning is centralized here so every call site (kompress,
# memory embedder, image router) inherits it.
#
# HEADROOM_HF_PIN=off to bypass pinning (e.g. when intentionally evaluating a
# newer model revision). To upgrade a model, bump its SHA here deliberately.
_PINNED_REVISIONS: dict[str, str] = {
    # chopratejas/kompress-v2-base @ 2026-06-10
    "chopratejas/kompress-v2-base": "b1563631b35bfdcee37587ad530147497d820d4c",
    "chopratejas/technique-router-onnx": "27b0b4bfa510a1cff66d888072c0b807082721a8",
    "chopratejas/siglip-image-encoder-onnx": "d0a9fbd66d4bd8c761bff592d44831f7c2ae184e",
    # Third-party repo — pinning matters most here.
    "Qdrant/all-MiniLM-L6-v2-onnx": "5f1b8cd78bc4fb444dd171e59b18f3a3af89a079",
}


def _resolve_revision(repo_id: str, revision: str | None) -> str | None:
    """Resolve the HF revision to download: explicit arg wins, else the pinned
    SHA for a known repo, else ``None`` (floating ref)."""
    if revision is not None:
        return revision
    if os.environ.get("HEADROOM_HF_PIN", "").strip().lower() in ("off", "0", "false", "no"):
        return None
    return _PINNED_REVISIONS.get(repo_id)


# ── Shared onnxruntime availability / provider detection ──────────────
#
# onnxruntime is a heavy import: on a GPU build the first ``import
# onnxruntime`` dlopens ~1 GB of CUDA libraries and can take several
# seconds. Multiple subsystems probe it independently at startup (the
# proxy GPU banner, the memory embedder backend selector, the Kompress
# compressor). Probing concurrently — or before that slow first import
# finishes — produced inconsistent results: one caller would report
# "onnxruntime not available" / "No GPU detected" while another, a moment
# later, successfully loaded CUDA.
#
# These helpers make detection deterministic and race-free:
#   * the answer is computed once and memoized, so every caller agrees;
#   * only a *successful* probe is cached — a transient early failure is
#     never frozen in, so a later call still gets the real answer;
#   * :func:`warm_onnxruntime` lets startup perform the slow import once,
#     serially, before concurrent consumers probe it.
_ort_lock = threading.Lock()
_ort_available: bool | None = None
_gpu_providers: tuple[str, ...] | None = None

_GPU_PROVIDER_KEYWORDS = ("cuda", "tensorrt", "dml", "directml", "coreml", "rocm")

# Shared-library names for TensorRT's runtime across onnxruntime versions and
# platforms. onnxruntime-gpu ships ``TensorrtExecutionProvider`` compiled in,
# but creating a session with it aborts with a loud ``EP Error`` banner when
# this library is absent (e.g. our Docker image bundles CUDA/cuDNN but not
# TensorRT). Probing lets us drop the provider before it is ever requested.
_TENSORRT_RUNTIME_LIBS = (
    "libnvinfer.so",
    "libnvinfer.so.10",
    "libnvinfer.so.9",
    "libnvinfer.so.8",
    "nvinfer.dll",
)

# Provider names that require a real, driver-backed NVIDIA CUDA device.
# ``onnxruntime.get_available_providers()`` reports providers compiled into the
# build, not providers backed by present hardware; onnxruntime-gpu always lists
# ``CUDAExecutionProvider`` (and ``TensorrtExecutionProvider``) even on machines
# with no GPU. These are gated on an actual device probe below.
_CUDA_DEPENDENT_KEYWORDS = ("cuda", "tensorrt")

# The CUDA *driver* library is installed by the NVIDIA driver (or injected into
# containers by nvidia-container-toolkit). Its absence — or a device count of
# zero — definitively means there is no usable CUDA GPU, regardless of which
# providers onnxruntime was compiled with.
_CUDA_DRIVER_LIBS = (
    "libcuda.so.1",
    "libcuda.so",
    "nvcuda.dll",
)


def _tensorrt_runtime_available() -> bool:
    """Return whether TensorRT's runtime library can be dynamically loaded."""
    for name in _TENSORRT_RUNTIME_LIBS:
        try:
            ctypes.CDLL(name)
            return True
        except OSError:
            continue
    return False


def _cuda_device_available() -> bool:
    """Return whether at least one CUDA device is visible via the driver API.

    A provider being compiled into onnxruntime does not mean usable hardware
    exists. We query the CUDA driver (``libcuda``) directly: load it, call
    ``cuInit(0)`` and ``cuDeviceGetCount``. If the library is absent, the driver
    fails to initialise, or no devices are reported, there is no usable CUDA GPU
    and any CUDA-dependent execution provider must be treated as unavailable.
    """
    lib = None
    for name in _CUDA_DRIVER_LIBS:
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            continue
    if lib is None:
        return False
    try:
        lib.cuInit.restype = ctypes.c_int
        lib.cuInit.argtypes = [ctypes.c_uint]
        lib.cuDeviceGetCount.restype = ctypes.c_int
        lib.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
        # CUDA_SUCCESS == 0
        if lib.cuInit(0) != 0:
            return False
        count = ctypes.c_int(0)
        if lib.cuDeviceGetCount(ctypes.byref(count)) != 0:
            return False
        return count.value > 0
    except (OSError, AttributeError, ValueError):
        return False


def onnxruntime_available() -> bool:
    """Return whether ``onnxruntime`` can be imported in this process.

    The result is memoized once the import succeeds. A failed import is not
    cached, so a probe issued before the (slow) first import completes does
    not permanently poison the answer for later callers.
    """
    global _ort_available
    if _ort_available:
        return True
    with _ort_lock:
        if _ort_available:
            return True
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return False
        _ort_available = True
        return True


def available_gpu_providers() -> list[str]:
    """Return ONNX Runtime's available GPU execution providers, CUDA first.

    Uses case-insensitive matching so provider names like
    ``CUDAExecutionProvider``, ``TensorrtExecutionProvider``,
    ``DmlExecutionProvider``, etc. are identified regardless of case
    variance across onnxruntime versions. A provider being *available*
    (compiled into the build) does not guarantee its runtime libraries are
    present; that is resolved gracefully at session-creation time.

    The detected list is memoized after the first successful probe so every
    subsystem reports the same providers for the life of the process.
    """
    global _gpu_providers
    if _gpu_providers is not None:
        return list(_gpu_providers)
    if not onnxruntime_available():
        return []
    with _ort_lock:
        if _gpu_providers is not None:
            return list(_gpu_providers)
        import onnxruntime

        all_providers = onnxruntime.get_available_providers()
        gpu_providers = [
            p for p in all_providers if any(kw in p.lower() for kw in _GPU_PROVIDER_KEYWORDS)
        ]
        # Drop TensorRT when its runtime library is missing: it is compiled into
        # onnxruntime-gpu but fails session creation without libnvinfer, emitting
        # a noisy EP Error before onnxruntime falls back. Filtering it out here
        # avoids requesting a provider that is known to be unusable.
        if (
            any("tensorrt" in p.lower() for p in gpu_providers)
            and not _tensorrt_runtime_available()
        ):
            gpu_providers = [p for p in gpu_providers if "tensorrt" not in p.lower()]
        # Drop CUDA-dependent providers when no real CUDA device is present.
        # onnxruntime-gpu lists CUDAExecutionProvider purely because it was
        # compiled in; without an actual driver-backed GPU it cannot be used,
        # and reporting it produces a false "GPU detected" banner before
        # onnxruntime silently falls back to CPU at session creation.
        if (
            any(kw in p.lower() for p in gpu_providers for kw in _CUDA_DEPENDENT_KEYWORDS)
            and not _cuda_device_available()
        ):
            gpu_providers = [
                p
                for p in gpu_providers
                if not any(kw in p.lower() for kw in _CUDA_DEPENDENT_KEYWORDS)
            ]
        if "CUDAExecutionProvider" in gpu_providers:
            gpu_providers.insert(0, gpu_providers.pop(gpu_providers.index("CUDAExecutionProvider")))
        _gpu_providers = tuple(gpu_providers)
        return list(gpu_providers)


def warm_onnxruntime() -> None:
    """Eagerly import onnxruntime once, serially, warming the shared caches.

    Blocks until onnxruntime is importable (retrying up to 30s with 2s
    pauses) so the memoized provider list is definitive before concurrent
    subsystems probe it. Subsequent callers always see the warm result,
    eliminating the race between the slow first import and concurrent probes.

    A genuine absence of onnxruntime is never cached, so a later caller
    whose import succeeds independently still gets the real answer.
    """
    for _ in range(15):
        if onnxruntime_available():
            available_gpu_providers()
            return
        time.sleep(2)


def hf_hub_download_local_first(
    repo_id: str,
    filename: str,
    *,
    allow_network: bool = True,
    revision: str | None = None,
    force_download: bool = False,
) -> str:
    """Download a file from HuggingFace Hub, preferring the local cache.

    Tries ``local_files_only=True`` first to avoid a network HEAD request when
    the model is already cached.  Falls back to a normal (network-allowed)
    download on the first cold start.

    Args:
        repo_id: HuggingFace Hub repository identifier (e.g. ``"org/model"``).
        filename: Filename within the repository.
        allow_network: When ``False``, never fall back to a network download —
            a cache miss re-raises the local-lookup error. Used by startup
            preload so a cold cache cannot block (or, via native crashes in the
            download stack, kill) the process before it binds its port.
        revision: Explicit git revision (commit SHA / tag / branch). When
            ``None``, a pinned SHA is applied for known repos (see
            ``_PINNED_REVISIONS``) for supply-chain integrity; unknown repos use
            the floating default ref.
        force_download: When ``True``, skip the local cache and force a fresh
            network download. This is used to replace a corrupt local artifact.

    Returns:
        Absolute path to the local cached file.

    Raises:
        Any exception raised by ``hf_hub_download`` on a genuine download failure,
        or the local-lookup error when ``allow_network`` is ``False`` and the
        file is not cached.
    """
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import EntryNotFoundError, LocalEntryNotFoundError

    revision = _resolve_revision(repo_id, revision)

    if force_download:
        return str(
            hf_hub_download(
                repo_id,
                filename,
                revision=revision,
                force_download=True,
            )
        )

    try:
        return str(hf_hub_download(repo_id, filename, revision=revision, local_files_only=True))
    except (LocalEntryNotFoundError, EntryNotFoundError, OSError):
        if not allow_network:
            raise
        return str(hf_hub_download(repo_id, filename, revision=revision))


def hf_entry_known_absent(repo_id: str, filename: str, *, revision: str | None = None) -> bool:
    """True only if a prior network lookup already confirmed ``filename`` does
    not exist in ``repo_id`` at the resolved revision.

    Backed by ``huggingface_hub``'s own cache of negative lookups (the
    ``.no_exist`` marker written after a real 404), so this never makes a
    network call itself. Returns ``False`` both when the file is cached and
    when nothing is known yet about it, on purpose: callers in a cache-only
    (``allow_network=False``) code path can use this to tell "confirmed
    missing upstream, safe to use a fallback file" apart from "just never
    checked yet, do not guess."
    """
    from huggingface_hub import _CACHED_NO_EXIST, try_to_load_from_cache

    revision = _resolve_revision(repo_id, revision)
    result = try_to_load_from_cache(repo_id, filename, revision=revision)
    return result is _CACHED_NO_EXIST


def create_cpu_session_options(
    ort: Any,
    *,
    intra_op_num_threads: int | None = None,
    inter_op_num_threads: int | None = None,
) -> Any:
    """Create CPU-oriented ONNX Runtime session options.

    Headroom runs as a long-lived proxy process, so we bias toward predictable
    memory usage over peak ONNX throughput. Disabling ORT's CPU arena and memory
    pattern caches reduces retained anonymous RSS after variable-size inference
    workloads, which is especially important on small VMs.
    """
    sess_options = ort.SessionOptions()

    if intra_op_num_threads is not None:
        sess_options.intra_op_num_threads = intra_op_num_threads
    if inter_op_num_threads is not None:
        sess_options.inter_op_num_threads = inter_op_num_threads

    if hasattr(sess_options, "enable_cpu_mem_arena"):
        sess_options.enable_cpu_mem_arena = cpu_arena_enabled()
    if hasattr(sess_options, "enable_mem_pattern"):
        sess_options.enable_mem_pattern = cpu_arena_enabled()

    if not onnx_thread_spinning_enabled():
        # ORT's thread pools spin-wait on all cores between inferences by
        # default, so idle-but-loaded models peg every core in a long-lived
        # proxy (#2495). Make idle threads block instead. Best-effort: older ORT
        # builds may not recognize a key.
        for spin_key in (
            "session.intra_op.allow_spinning",
            "session.inter_op.allow_spinning",
        ):
            try:
                sess_options.add_session_config_entry(spin_key, "0")
            except Exception:
                pass

    return sess_options


def trim_process_heap() -> bool:
    """Ask glibc to return unused heap pages to the OS when available."""
    if not sys.platform.startswith("linux"):
        return False

    try:
        libc = ctypes.CDLL("libc.so.6")
    except OSError:
        return False

    try:
        return bool(libc.malloc_trim(0))
    except Exception:
        return False


class OnnxRuntimeWarmup:
    """Coordinates onnxruntime background warmup with ready signaling.

    Lets concurrent background tasks wait for onnxruntime to be probed
    before making decisions that depend on its availability, without
    forcing sequential initialization.

    Usage in an async context (e.g. proxy startup)::

        warmup = OnnxRuntimeWarmup()

        async def bg_onnx():
            await warmup.warmup()
            # GPU probe ...

        async def bg_memory():
            await warmup.wait_ready()
            # onnxruntime_available() now returns the correct answer
            ...
    """

    def __init__(self) -> None:
        self._ready = asyncio.Event()

    async def warmup(self) -> None:
        """Run :func:`warm_onnxruntime` in a thread and signal readiness.

        Returns once onnxruntime has been successfully imported and GPU
        providers probed (or the 30s retry loop exhausted).
        """
        await asyncio.to_thread(warm_onnxruntime)
        self._ready.set()

    async def wait_ready(self) -> None:
        """Wait indefinitely for the warmup to complete.

        Returns once :func:`warmup` has finished (onnxruntime has been
        successfully imported and GPU providers probed).
        """
        await self._ready.wait()
