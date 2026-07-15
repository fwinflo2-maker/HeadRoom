"""Pure, bounded aggregate state for the durable Dashboard Lifetime view."""

from __future__ import annotations

import math
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 5
MAX_PROVIDER_VALUES = 32
MAX_STACK_VALUES = 64
MAX_TRACKED_MODELS = 200
MAX_EXPOSED_MODELS = 100
MAX_LABEL_LENGTH = 128
MAX_SCOPE_LABELS = 64

KNOWN_MISS_REASONS = frozenset({"ttl_expiry", "prefix_change", "unknown"})
KNOWN_WASTE_SIGNALS = frozenset(
    {
        "json_noise",
        "html_noise",
        "base64",
        "whitespace",
        "dynamic_date",
        "repetition",
        "reread",
        "reread_compressed",
    }
)


def utc_now() -> datetime:
    """Return the current UTC time without sub-second noise in persisted state."""

    return datetime.now(timezone.utc).replace(microsecond=0)


def _to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _coerce_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _coerce_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) and result >= 0 else 0.0


def _label(value: Any) -> str:
    if not isinstance(value, str):
        return "other"
    value = value.strip()
    return value[:MAX_LABEL_LENGTH] if value else "other"


def _model_entry(raw: Any = None) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "requests": _coerce_int(raw.get("requests")),
        "input_tokens": _coerce_int(raw.get("input_tokens")),
        "output_tokens": _coerce_int(raw.get("output_tokens")),
        "attempted_input_tokens": _coerce_int(raw.get("attempted_input_tokens")),
        "tokens_saved": _coerce_int(raw.get("tokens_saved")),
        "last_activity_at": raw.get("last_activity_at")
        if isinstance(raw.get("last_activity_at"), str)
        else None,
    }


def _empty_state() -> dict[str, Any]:
    return {
        "started_at": None,
        "last_activity_at": None,
        "full_fidelity_started_at": None,
        "requests": {
            "total": 0,
            "cached": 0,
            "failed": 0,
            "rate_limited": 0,
            "by_provider": {},
            "by_stack": {},
        },
        "tokens": {"input": 0, "output": 0, "attempted_input": 0, "saved": 0},
        "prefix_cache": {
            "requests": 0,
            "hit_requests": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
            "uncached_input_tokens": 0,
            "cache_write_5m_requests": 0,
            "cache_write_1h_requests": 0,
            "bust_count": 0,
            "bust_tokens": 0,
            "misses_by_reason": {},
            "by_provider": {},
        },
        "cost": {"input_usd": 0.0, "compression_savings_usd": 0.0, "cache_savings_usd": 0.0},
        "waste_signals": {},
        "models": {"tracked": {}, "other": _model_entry()},
        "compression": {
            "compressions_by_strategy": {},
            "tokens_saved_by_strategy": {},
        },
        "codex_ws": {
            "units_total": 0,
            "units_modified_total": 0,
            "units_to_kompress_total": 0,
            "units_kompress_attempted_total": 0,
            "units_by_strategy": {},
            "units_by_category": {},
            "units_by_content_type": {},
            "units_by_text_shape": {},
            "unit_elapsed_ms_sum": 0.0,
            "unit_elapsed_ms_max": 0.0,
            "unit_bytes_sum": 0,
            "unit_tokens_before_sum": 0,
            "unit_tokens_after_sum": 0,
            "unit_tokens_saved_sum": 0,
            "frames_attempted_total": 0,
            "frames_compressed_total": 0,
            "frames_failed_total": 0,
            "frames_to_kompress_total": 0,
            "frames_kompress_attempted_total": 0,
            "frame_elapsed_ms_sum": 0.0,
            "frame_elapsed_ms_max": 0.0,
            "frame_bytes_before_sum": 0,
            "frame_bytes_after_sum": 0,
            "frame_attempted_tokens_sum": 0,
            "frame_tokens_saved_sum": 0,
        },
        "persistence": {"last_saved_at": None},
    }


def _dict_or_empty(value: Any) -> dict[Any, Any]:
    return value if isinstance(value, dict) else {}


class PersistentMetricsState:
    """In-memory Lifetime aggregate with deterministic, bounded dimensions."""

    def __init__(
        self,
        raw: dict[str, Any] | None = None,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._now = now
        self._state = self._normalize(raw)
        self._compact_models()

    def _normalize(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        source = raw if isinstance(raw, dict) else {}
        result = _empty_state()
        for timestamp_key in ("started_at", "last_activity_at", "full_fidelity_started_at"):
            value = source.get(timestamp_key)
            if isinstance(value, str):
                result[timestamp_key] = value

        raw_requests = _dict_or_empty(source.get("requests"))
        for key in ("total", "cached", "failed", "rate_limited"):
            result["requests"][key] = _coerce_int(raw_requests.get(key))
        result["requests"]["by_provider"] = self._normalize_count_map(
            raw_requests.get("by_provider"), MAX_PROVIDER_VALUES
        )
        result["requests"]["by_stack"] = self._normalize_count_map(
            raw_requests.get("by_stack"), MAX_STACK_VALUES
        )

        raw_tokens = _dict_or_empty(source.get("tokens"))
        for key in ("input", "output", "attempted_input", "saved"):
            result["tokens"][key] = _coerce_int(raw_tokens.get(key))

        raw_cache = _dict_or_empty(source.get("prefix_cache"))
        for key in (
            "requests",
            "hit_requests",
            "cache_read_tokens",
            "cache_write_tokens",
            "cache_write_5m_tokens",
            "cache_write_1h_tokens",
            "uncached_input_tokens",
            "cache_write_5m_requests",
            "cache_write_1h_requests",
            "bust_count",
            "bust_tokens",
        ):
            result["prefix_cache"][key] = _coerce_int(raw_cache.get(key))
        result["prefix_cache"]["by_provider"] = self._normalize_count_map(
            raw_cache.get("by_provider"), MAX_PROVIDER_VALUES
        )
        result["prefix_cache"]["misses_by_reason"] = self._normalize_enum_map(
            raw_cache.get("misses_by_reason"), KNOWN_MISS_REASONS
        )

        raw_compression = _dict_or_empty(source.get("compression"))
        for key in ("compressions_by_strategy", "tokens_saved_by_strategy"):
            result["compression"][key] = self._normalize_count_map(
                raw_compression.get(key), MAX_SCOPE_LABELS
            )
        raw_ws = _dict_or_empty(source.get("codex_ws"))
        int_fields = [
            "units_total",
            "units_modified_total",
            "units_to_kompress_total",
            "units_kompress_attempted_total",
            "unit_bytes_sum",
            "unit_tokens_before_sum",
            "unit_tokens_after_sum",
            "unit_tokens_saved_sum",
            "frames_attempted_total",
            "frames_compressed_total",
            "frames_failed_total",
            "frames_to_kompress_total",
            "frames_kompress_attempted_total",
            "frame_bytes_before_sum",
            "frame_bytes_after_sum",
            "frame_attempted_tokens_sum",
            "frame_tokens_saved_sum",
        ]
        float_fields = [
            "unit_elapsed_ms_sum",
            "unit_elapsed_ms_max",
            "frame_elapsed_ms_sum",
            "frame_elapsed_ms_max",
        ]
        map_fields = [
            "units_by_strategy",
            "units_by_category",
            "units_by_content_type",
            "units_by_text_shape",
        ]
        for key in int_fields:
            result["codex_ws"][key] = _coerce_int(raw_ws.get(key))
        for key in float_fields:
            result["codex_ws"][key] = round(_coerce_float(raw_ws.get(key)), 6)
        for key in map_fields:
            result["codex_ws"][key] = self._normalize_count_map(raw_ws.get(key), MAX_SCOPE_LABELS)

        raw_cost = _dict_or_empty(source.get("cost"))
        for key in ("input_usd", "compression_savings_usd", "cache_savings_usd"):
            result["cost"][key] = round(_coerce_float(raw_cost.get(key)), 6)
        result["waste_signals"] = self._normalize_enum_map(
            source.get("waste_signals"), KNOWN_WASTE_SIGNALS
        )

        raw_models = _dict_or_empty(source.get("models"))
        raw_tracked = _dict_or_empty(raw_models.get("tracked"))
        for name, entry in raw_tracked.items():
            normalized_name = self._model_name(name)
            if normalized_name == "other":
                self._merge_model_entry(result["models"]["other"], _model_entry(entry))
                continue
            result["models"]["tracked"][normalized_name] = _model_entry(entry)
        self._merge_model_entry(result["models"]["other"], _model_entry(raw_models.get("other")))
        raw_persistence = _dict_or_empty(source.get("persistence"))
        if isinstance(raw_persistence.get("last_saved_at"), str):
            result["persistence"]["last_saved_at"] = raw_persistence["last_saved_at"]
        return result

    @staticmethod
    def _normalize_count_map(raw: Any, limit: int) -> dict[str, int]:
        result: dict[str, int] = {}
        if not isinstance(raw, dict):
            return result
        for key, value in raw.items():
            label = _label(key)
            result[label] = result.get(label, 0) + _coerce_int(value)
        PersistentMetricsState._compact_count_map(result, limit)
        return result

    @staticmethod
    def _normalize_enum_map(raw: Any, allowed: frozenset[str]) -> dict[str, int]:
        result: dict[str, int] = {}
        if not isinstance(raw, dict):
            return result
        for key, value in raw.items():
            label = key if isinstance(key, str) and key in allowed else "unknown"
            result[label] = result.get(label, 0) + _coerce_int(value)
        return result

    @staticmethod
    def _compact_count_map(values: dict[str, int], limit: int) -> None:
        named = [key for key in values if key != "other"]
        while len(named) > limit:
            evicted = min(named, key=lambda key: (values[key], key))
            values["other"] = values.get("other", 0) + values.pop(evicted)
            named.remove(evicted)

    def _record_activity(self) -> str:
        timestamp = _to_iso(self._now())
        if self._state["started_at"] is None:
            self._state["started_at"] = timestamp
        if self._state["full_fidelity_started_at"] is None:
            self._state["full_fidelity_started_at"] = timestamp
        self._state["last_activity_at"] = timestamp
        return timestamp or ""

    @staticmethod
    def _increment_count(values: dict[str, int], label: str, limit: int) -> None:
        values[label] = values.get(label, 0) + 1
        PersistentMetricsState._compact_count_map(values, limit)

    @staticmethod
    def _model_name(value: Any) -> str:
        """Never retain the legacy unknown model bucket as a named model."""

        label = _label(value)
        return "other" if label.lower() == "unknown" else label

    @staticmethod
    def _merge_model_entry(destination: dict[str, Any], source: dict[str, Any]) -> None:
        for key in (
            "requests",
            "input_tokens",
            "output_tokens",
            "attempted_input_tokens",
            "tokens_saved",
        ):
            destination[key] += _coerce_int(source.get(key))
        if destination["last_activity_at"] is None or (
            source["last_activity_at"] is not None
            and source["last_activity_at"] > destination["last_activity_at"]
        ):
            destination["last_activity_at"] = source["last_activity_at"]

    @staticmethod
    def _model_rank(item: tuple[str, dict[str, Any]]) -> tuple[int, str, str]:
        name, entry = item
        observed_tokens = entry["input_tokens"] + entry["output_tokens"]
        return (-observed_tokens, entry["last_activity_at"] or "", name)

    def _compact_models(self) -> None:
        tracked = self._state["models"]["tracked"]
        if len(tracked) <= MAX_TRACKED_MODELS:
            return
        ranked = sorted(tracked.items(), key=self._model_rank)
        kept = dict(ranked[:MAX_EXPOSED_MODELS])
        other = self._state["models"]["other"]
        for _, entry in ranked[MAX_EXPOSED_MODELS:]:
            self._merge_model_entry(other, entry)
        self._state["models"]["tracked"] = kept

    def _record_model(
        self,
        *,
        model: str | None,
        timestamp: str,
        input_tokens: int,
        output_tokens: int,
        attempted_input_tokens: int,
        tokens_saved: int,
    ) -> None:
        name = self._model_name(model)
        models = self._state["models"]
        entry = (
            models["other"]
            if name == "other"
            else models["tracked"].setdefault(name, _model_entry())
        )
        entry["requests"] += 1
        entry["input_tokens"] += input_tokens
        entry["output_tokens"] += output_tokens
        entry["attempted_input_tokens"] += attempted_input_tokens
        entry["tokens_saved"] += tokens_saved
        entry["last_activity_at"] = timestamp
        self._compact_models()

    def record_request(
        self,
        *,
        provider: str | None = None,
        stack: str | None = None,
        model: str | None,
        input_tokens: Any = 0,
        output_tokens: Any = 0,
        attempted_input_tokens: Any = 0,
        tokens_saved: Any = 0,
        cached: bool = False,
        record_stack: bool = True,
        cache_read_tokens: Any = 0,
        cache_write_tokens: Any = 0,
        cache_write_5m_tokens: Any = 0,
        cache_write_1h_tokens: Any = 0,
        cache_write_5m_requests: Any = 0,
        cache_write_1h_requests: Any = 0,
        uncached_input_tokens: Any = 0,
        input_usd: Any = 0.0,
        compression_savings_usd: Any = 0.0,
        cache_savings_usd: Any = 0.0,
        waste_signals: dict[str, Any] | None = None,
    ) -> None:
        """Accumulate one completed top-level request after coercing all deltas."""

        timestamp = self._record_activity()
        input_delta = _coerce_int(input_tokens)
        output_delta = _coerce_int(output_tokens)
        attempted_delta = _coerce_int(attempted_input_tokens)
        saved_delta = _coerce_int(tokens_saved)
        provider_label = _label(provider)
        stack_label = _label(stack)

        requests = self._state["requests"]
        requests["total"] += 1
        requests["cached"] += int(bool(cached))
        self._increment_count(requests["by_provider"], provider_label, MAX_PROVIDER_VALUES)
        if record_stack:
            self._increment_count(requests["by_stack"], stack_label, MAX_STACK_VALUES)

        tokens = self._state["tokens"]
        tokens["input"] += input_delta
        tokens["output"] += output_delta
        tokens["attempted_input"] += attempted_delta
        tokens["saved"] += saved_delta

        cache = self._state["prefix_cache"]
        cache_read_delta = _coerce_int(cache_read_tokens)
        cache_write_delta = _coerce_int(cache_write_tokens)
        if cache_read_delta > 0 or cache_write_delta > 0:
            cache["requests"] += 1
            cache["hit_requests"] += int(cache_read_delta > 0)
            cache["cache_read_tokens"] += cache_read_delta
            cache["cache_write_tokens"] += cache_write_delta
            cache["cache_write_5m_tokens"] += _coerce_int(cache_write_5m_tokens)
            cache["cache_write_1h_tokens"] += _coerce_int(cache_write_1h_tokens)
            cache["cache_write_5m_requests"] += _coerce_int(cache_write_5m_requests)
            cache["cache_write_1h_requests"] += _coerce_int(cache_write_1h_requests)
            cache["uncached_input_tokens"] += _coerce_int(uncached_input_tokens)
            self._increment_count(cache["by_provider"], provider_label, MAX_PROVIDER_VALUES)

        cost = self._state["cost"]
        cost["input_usd"] = round(cost["input_usd"] + _coerce_float(input_usd), 6)
        cost["compression_savings_usd"] = round(
            cost["compression_savings_usd"] + _coerce_float(compression_savings_usd), 6
        )
        cost["cache_savings_usd"] = round(
            cost["cache_savings_usd"] + _coerce_float(cache_savings_usd), 6
        )

        if isinstance(waste_signals, dict):
            for name, token_count in waste_signals.items():
                bucket = name if isinstance(name, str) and name in KNOWN_WASTE_SIGNALS else "other"
                self._state["waste_signals"][bucket] = self._state["waste_signals"].get(
                    bucket, 0
                ) + _coerce_int(token_count)

        self._record_model(
            model=model,
            timestamp=timestamp,
            input_tokens=input_delta,
            output_tokens=output_delta,
            attempted_input_tokens=attempted_delta,
            tokens_saved=saved_delta,
        )

    def record_stack(self, stack: str | None) -> None:
        """Accumulate the existing inbound stack label without adding a request."""

        if stack is None:
            return
        self._record_activity()
        self._increment_count(self._state["requests"]["by_stack"], _label(stack), MAX_STACK_VALUES)

    def record_failed(self, *, provider: str | None = None, model: str | None = None) -> None:
        """Record a failed request without changing the completed-request denominator."""

        self._record_activity()
        self._state["requests"]["failed"] += 1

    def record_rate_limited(self, *, provider: str | None = None, model: str | None = None) -> None:
        """Record a rate-limited request without redefining total request semantics."""

        self._record_activity()
        self._state["requests"]["rate_limited"] += 1

    def record_cache_bust(self, *, tokens_lost: Any = 0) -> None:
        self._record_activity()
        cache = self._state["prefix_cache"]
        cache["bust_count"] += 1
        cache["bust_tokens"] += _coerce_int(tokens_lost)

    def record_compression(
        self, *, strategy: str, original_tokens: Any, compressed_tokens: Any
    ) -> None:
        self._record_activity()
        key = _label(strategy)
        self._increment_count(
            self._state["compression"]["compressions_by_strategy"], key, MAX_SCOPE_LABELS
        )
        saved = max(_coerce_int(original_tokens) - _coerce_int(compressed_tokens), 0)
        if saved:
            values = self._state["compression"]["tokens_saved_by_strategy"]
            values[key] = values.get(key, 0) + saved
            self._compact_count_map(values, MAX_SCOPE_LABELS)

    def record_codex_ws_unit(
        self,
        *,
        strategy: str,
        reason_category: str,
        elapsed_ms: Any,
        text_bytes: Any,
        tokens_before: Any,
        tokens_after: Any,
        tokens_saved: Any,
        modified: bool,
        strategy_chain: list[str] | None = None,
        content_type: str = "unknown",
        text_shape: str = "unknown",
    ) -> None:
        self._record_activity()
        ws = self._state["codex_ws"]
        ws["units_total"] += 1
        for field, value in (
            ("units_by_strategy", strategy),
            ("units_by_category", reason_category),
            ("units_by_content_type", content_type),
            ("units_by_text_shape", text_shape),
        ):
            self._increment_count(ws[field], _label(value), MAX_SCOPE_LABELS)
        ws["units_modified_total"] += int(modified)
        chain = strategy_chain or []
        ws["units_to_kompress_total"] += int(strategy == "kompress")
        ws["units_kompress_attempted_total"] += int(strategy == "kompress" or "kompress" in chain)
        elapsed = _coerce_float(elapsed_ms)
        ws["unit_elapsed_ms_sum"] = round(ws["unit_elapsed_ms_sum"] + elapsed, 6)
        ws["unit_elapsed_ms_max"] = max(ws["unit_elapsed_ms_max"], elapsed)
        for field, value in (
            ("unit_bytes_sum", text_bytes),
            ("unit_tokens_before_sum", tokens_before),
            ("unit_tokens_after_sum", tokens_after),
            ("unit_tokens_saved_sum", tokens_saved),
        ):
            ws[field] += _coerce_int(value)

    def record_codex_ws_frame(
        self,
        *,
        elapsed_ms: Any,
        bytes_before: Any,
        bytes_after: Any = 0,
        attempted_tokens: Any = 0,
        tokens_saved: Any = 0,
        modified: bool = False,
        failed: bool = False,
        strategy_chain: list[str] | None = None,
        final_strategies: list[str] | None = None,
    ) -> None:
        self._record_activity()
        ws = self._state["codex_ws"]
        ws["frames_attempted_total"] += 1
        ws["frames_compressed_total"] += int(modified)
        ws["frames_failed_total"] += int(failed)
        chain = strategy_chain or []
        strategies = final_strategies or []
        ws["frames_to_kompress_total"] += int("kompress" in strategies)
        ws["frames_kompress_attempted_total"] += int(
            "kompress" in chain or "kompress" in strategies
        )
        elapsed = _coerce_float(elapsed_ms)
        ws["frame_elapsed_ms_sum"] = round(ws["frame_elapsed_ms_sum"] + elapsed, 6)
        ws["frame_elapsed_ms_max"] = max(ws["frame_elapsed_ms_max"], elapsed)
        for field, value in (
            ("frame_bytes_before_sum", bytes_before),
            ("frame_bytes_after_sum", bytes_after),
            ("frame_attempted_tokens_sum", attempted_tokens),
            ("frame_tokens_saved_sum", tokens_saved),
        ):
            ws[field] += _coerce_int(value)

    def record_cache_miss(self, *, provider: str | None, reason: str | None) -> None:
        self._record_activity()
        bucket = reason if isinstance(reason, str) and reason in KNOWN_MISS_REASONS else "unknown"
        misses = self._state["prefix_cache"]["misses_by_reason"]
        misses[bucket] = misses.get(bucket, 0) + 1

    def set_last_saved_at(self, value: str | None) -> None:
        self._state["persistence"]["last_saved_at"] = value

    def to_dict(self) -> dict[str, Any]:
        """Return the persisted form without derived values or I/O metadata."""

        return deepcopy(self._state)

    @staticmethod
    def _percent(numerator: int, denominator: int) -> float | None:
        if denominator <= 0:
            return None
        return round(numerator / denominator * 100, 6)

    def _by_model_snapshot(self) -> dict[str, dict[str, Any]]:
        ranked = sorted(self._state["models"]["tracked"].items(), key=self._model_rank)
        visible = ranked[:MAX_EXPOSED_MODELS]
        other = _model_entry(self._state["models"]["other"])
        for _, entry in ranked[MAX_EXPOSED_MODELS:]:
            self._merge_model_entry(other, entry)
        result = {name: deepcopy(entry) for name, entry in visible}
        result["other"] = other
        return result

    def snapshot(self, *, persistence: dict[str, Any]) -> dict[str, Any]:
        """Return an API-safe aggregate and derive all percentages at read time."""

        cache = self._state["prefix_cache"]
        cache_write_total = cache["cache_write_1h_tokens"] + cache["cache_write_5m_tokens"]
        tokens = self._state["tokens"]
        return {
            "scope": "lifetime",
            "schema_version": SCHEMA_VERSION,
            "generated_at": _to_iso(self._now()),
            "started_at": self._state["started_at"],
            "last_activity_at": self._state["last_activity_at"],
            "full_fidelity_started_at": self._state["full_fidelity_started_at"],
            "requests": deepcopy(self._state["requests"]),
            "tokens": {
                **deepcopy(tokens),
                "token_savings_percent": self._percent(tokens["saved"], tokens["attempted_input"]),
            },
            "prefix_cache": {
                **deepcopy(cache),
                "cache_hit_rate": self._percent(cache["hit_requests"], cache["requests"]),
                "ttl_1h_percent": self._percent(cache["cache_write_1h_tokens"], cache_write_total),
                "ttl_5m_percent": self._percent(cache["cache_write_5m_tokens"], cache_write_total),
            },
            "cost": deepcopy(self._state["cost"]),
            "compression": deepcopy(self._state["compression"]),
            "codex_ws": deepcopy(self._state["codex_ws"]),
            "waste_signals": deepcopy(self._state["waste_signals"]),
            "by_model": self._by_model_snapshot(),
            "persistence": {
                **deepcopy(persistence),
                "last_saved_at": self._state["persistence"]["last_saved_at"],
            },
        }
