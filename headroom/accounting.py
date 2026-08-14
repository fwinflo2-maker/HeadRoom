"""Model usage accounting — tracks per-model compression statistics.

Records every model invocation with input/output sizes and runtime,
then exposes aggregated statistics including percentiles, distributions,
and coefficient of variation. No null values in any output field.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

# ---------------------------------------------------------------------------
# Histogram helpers
# ---------------------------------------------------------------------------

_POWER_OF_TWO_BUCKETS = [
    (0, 0),
    (1, 128),
    (129, 256),
    (257, 512),
    (513, 1024),
    (1025, 2048),
    (2049, 4096),
    (4097, 8192),
    (8193, 16384),
    (16385, 32768),
    (32769, 65536),
    (65537, 131072),
    (131073, 262144),
    (262145, 524288),
    (524289, 1048576),
    (1048577, 2097152),
    (2097153, 4194304),
    (4194305, 8388608),
]

_RUNTIME_BUCKETS_MS = [
    (0, 0),
    (1, 1),
    (2, 5),
    (6, 10),
    (11, 25),
    (26, 50),
    (51, 100),
    (101, 250),
    (251, 500),
    (501, 1000),
    (1001, 2000),
    (2001, 5000),
    (5001, 10000),
    (10001, 30000),
    (30001, 60000),
]


def _bucket_label(low: int, high: int) -> str:
    if low == 0 and high == 0:
        return "0"
    if low == high:
        return str(low)
    return f"{low}-{high}"


def _histogram(values: list[float], buckets: list[tuple[int, int]]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for b in buckets:
        hist[_bucket_label(*b)] = 0
    for v in values:
        for low, high in buckets:
            if low <= v <= high:
                hist[_bucket_label(low, high)] = hist.get(_bucket_label(low, high), 0) + 1
                break
    return hist


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (p / 100.0) * (len(sorted_vals) - 1)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    return float(sorted_vals[f]) * (c - k) + float(sorted_vals[c]) * (k - f)


def _cv(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    if mean == 0:
        return 0.0
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(variance) / mean


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ModelUsageRecord:
    """A single usage record for a model invocation."""

    model_name: str
    input_tokens: int
    output_tokens: int
    runtime_ms: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class ModelCompressionStats:
    """Aggregated statistics for a single model.

    All fields are guaranteed non-null. Empty models return zero-value stats.
    """

    total_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_runtime_ms: float = 0.0
    avg_input_tokens: float = 0.0
    avg_output_tokens: float = 0.0
    avg_runtime_ms: float = 0.0
    min_input_tokens: int = 0
    max_input_tokens: int = 0
    min_output_tokens: int = 0
    max_output_tokens: int = 0
    min_runtime_ms: float = 0.0
    max_runtime_ms: float = 0.0
    p1_input_tokens: int = 0
    p5_input_tokens: int = 0
    p50_input_tokens: int = 0
    p95_input_tokens: int = 0
    p99_input_tokens: int = 0
    p1_output_tokens: int = 0
    p5_output_tokens: int = 0
    p50_output_tokens: int = 0
    p95_output_tokens: int = 0
    p99_output_tokens: int = 0
    p1_runtime_ms: float = 0.0
    p5_runtime_ms: float = 0.0
    p50_runtime_ms: float = 0.0
    p95_runtime_ms: float = 0.0
    p99_runtime_ms: float = 0.0
    cv_input_tokens: float = 0.0
    cv_output_tokens: float = 0.0
    cv_runtime_ms: float = 0.0
    input_token_distribution: dict[str, int] = field(default_factory=dict)
    output_token_distribution: dict[str, int] = field(default_factory=dict)
    runtime_ms_distribution: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_records(cls, records: list[ModelUsageRecord]) -> ModelCompressionStats:
        if not records:
            return cls()

        n = len(records)
        total_input = sum(r.input_tokens for r in records)
        total_output = sum(r.output_tokens for r in records)
        total_runtime = sum(r.runtime_ms for r in records)

        input_vals = [float(r.input_tokens) for r in records]
        output_vals = [float(r.output_tokens) for r in records]
        runtime_vals = [r.runtime_ms for r in records]

        sorted_input = sorted(input_vals)
        sorted_output = sorted(output_vals)
        sorted_runtime = sorted(runtime_vals)

        return cls(
            total_calls=n,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            total_runtime_ms=total_runtime,
            avg_input_tokens=total_input / n,
            avg_output_tokens=total_output / n,
            avg_runtime_ms=total_runtime / n,
            min_input_tokens=int(sorted_input[0]),
            max_input_tokens=int(sorted_input[-1]),
            min_output_tokens=int(sorted_output[0]),
            max_output_tokens=int(sorted_output[-1]),
            min_runtime_ms=sorted_runtime[0],
            max_runtime_ms=sorted_runtime[-1],
            p1_input_tokens=int(round(_percentile(sorted_input, 1))),
            p5_input_tokens=int(round(_percentile(sorted_input, 5))),
            p50_input_tokens=int(round(_percentile(sorted_input, 50))),
            p95_input_tokens=int(round(_percentile(sorted_input, 95))),
            p99_input_tokens=int(round(_percentile(sorted_input, 99))),
            p1_output_tokens=int(round(_percentile(sorted_output, 1))),
            p5_output_tokens=int(round(_percentile(sorted_output, 5))),
            p50_output_tokens=int(round(_percentile(sorted_output, 50))),
            p95_output_tokens=int(round(_percentile(sorted_output, 95))),
            p99_output_tokens=int(round(_percentile(sorted_output, 99))),
            p1_runtime_ms=_percentile(sorted_runtime, 1),
            p5_runtime_ms=_percentile(sorted_runtime, 5),
            p50_runtime_ms=_percentile(sorted_runtime, 50),
            p95_runtime_ms=_percentile(sorted_runtime, 95),
            p99_runtime_ms=_percentile(sorted_runtime, 99),
            cv_input_tokens=_cv(input_vals),
            cv_output_tokens=_cv(output_vals),
            cv_runtime_ms=_cv(runtime_vals),
            input_token_distribution=_histogram(input_vals, _POWER_OF_TWO_BUCKETS),
            output_token_distribution=_histogram(output_vals, _POWER_OF_TWO_BUCKETS),
            runtime_ms_distribution=_histogram(runtime_vals, _RUNTIME_BUCKETS_MS),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_calls": self.total_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_runtime_ms": round(self.total_runtime_ms, 2),
            "avg_input_tokens": round(self.avg_input_tokens, 2),
            "avg_output_tokens": round(self.avg_output_tokens, 2),
            "avg_runtime_ms": round(self.avg_runtime_ms, 2),
            "min_input_tokens": self.min_input_tokens,
            "max_input_tokens": self.max_input_tokens,
            "min_output_tokens": self.min_output_tokens,
            "max_output_tokens": self.max_output_tokens,
            "min_runtime_ms": round(self.min_runtime_ms, 2),
            "max_runtime_ms": round(self.max_runtime_ms, 2),
            "p1_input_tokens": self.p1_input_tokens,
            "p5_input_tokens": self.p5_input_tokens,
            "p50_input_tokens": self.p50_input_tokens,
            "p95_input_tokens": self.p95_input_tokens,
            "p99_input_tokens": self.p99_input_tokens,
            "p1_output_tokens": self.p1_output_tokens,
            "p5_output_tokens": self.p5_output_tokens,
            "p50_output_tokens": self.p50_output_tokens,
            "p95_output_tokens": self.p95_output_tokens,
            "p99_output_tokens": self.p99_output_tokens,
            "p1_runtime_ms": round(self.p1_runtime_ms, 2),
            "p5_runtime_ms": round(self.p5_runtime_ms, 2),
            "p50_runtime_ms": round(self.p50_runtime_ms, 2),
            "p95_runtime_ms": round(self.p95_runtime_ms, 2),
            "p99_runtime_ms": round(self.p99_runtime_ms, 2),
            "cv_input_tokens": round(self.cv_input_tokens, 6),
            "cv_output_tokens": round(self.cv_output_tokens, 6),
            "cv_runtime_ms": round(self.cv_runtime_ms, 6),
            "input_token_distribution": dict(self.input_token_distribution),
            "output_token_distribution": dict(self.output_token_distribution),
            "runtime_ms_distribution": dict(self.runtime_ms_distribution),
        }


# ---------------------------------------------------------------------------
# Global model accounting store
# ---------------------------------------------------------------------------


class ModelAccounting:
    """Thread-safe per-model usage tracker.

    Records every AI model invocation so callers can retrieve aggregated
    statistics including percentiles, distributions, and CV.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._records: dict[str, list[ModelUsageRecord]] = {}

    def record(
        self,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        runtime_ms: float,
    ) -> None:
        with self._lock:
            if model_name not in self._records:
                self._records[model_name] = []
            self._records[model_name].append(
                ModelUsageRecord(
                    model_name=model_name,
                    input_tokens=max(0, input_tokens),
                    output_tokens=max(0, output_tokens),
                    runtime_ms=max(0.0, runtime_ms),
                )
            )

    def get_stats(self, model_name: str | None = None) -> dict[str, ModelCompressionStats]:
        with self._lock:
            if model_name is not None:
                records = self._records.get(model_name, [])
                return {model_name: ModelCompressionStats.from_records(records)}
            result: dict[str, ModelCompressionStats] = {}
            for name, recs in self._records.items():
                result[name] = ModelCompressionStats.from_records(recs)
            return result

    def get_stats_dict(self, model_name: str | None = None) -> dict[str, Any]:
        raw = self.get_stats(model_name)
        if model_name is not None and model_name in raw:
            return raw[model_name].to_dict()
        return {name: stats.to_dict() for name, stats in raw.items()}

    def reset(self, model_name: str | None = None) -> None:
        with self._lock:
            if model_name is not None:
                self._records.pop(model_name, None)
            else:
                self._records.clear()

    @property
    def total_calls(self) -> int:
        with self._lock:
            return sum(len(recs) for recs in self._records.values())

    @property
    def models_seen(self) -> list[str]:
        with self._lock:
            return sorted(self._records.keys())


# Module-level singleton (matching headroom patterns)
_accounting_lock = Lock()
_global_accounting: ModelAccounting | None = None


def get_model_accounting() -> ModelAccounting:
    """Get or create the global ModelAccounting singleton."""
    global _global_accounting
    if _global_accounting is not None:
        return _global_accounting
    with _accounting_lock:
        if _global_accounting is not None:
            return _global_accounting
        _global_accounting = ModelAccounting()
        return _global_accounting


def reset_model_accounting() -> None:
    """Reset the global accounting store (for testing)."""
    global _global_accounting
    with _accounting_lock:
        _global_accounting = ModelAccounting()
