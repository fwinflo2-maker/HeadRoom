from __future__ import annotations

import threading
from types import MethodType, SimpleNamespace

import pytest

from headroom.proxy.handlers import openai as openai_handler
from headroom.proxy.handlers.openai import OpenAIHandlerMixin
from headroom.transforms.compression_units import UnitCompressionResult
from headroom.transforms.content_router import (
    CompressionStrategy,
    ContentRouter,
    RouterCompressionResult,
)


class TokenCounter:
    def count_text(self, text: str) -> int:
        return len(text.split())


def _handler_with_router(router: ContentRouter) -> OpenAIHandlerMixin:
    handler = OpenAIHandlerMixin()
    handler.openai_pipeline = SimpleNamespace(transforms=[router])
    handler.openai_provider = SimpleNamespace(
        get_token_counter=lambda _model: TokenCounter(),
    )
    return handler


def test_openai_responses_unit_parallelism_env_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("HEADROOM_TOOL_OUTPUT_COMPRESSION_PARALLELISM", raising=False)
    assert openai_handler._openai_responses_unit_parallelism() == 4

    monkeypatch.setenv("HEADROOM_TOOL_OUTPUT_COMPRESSION_PARALLELISM", "bad")
    assert openai_handler._openai_responses_unit_parallelism() == 4

    monkeypatch.setenv("HEADROOM_TOOL_OUTPUT_COMPRESSION_PARALLELISM", "0")
    assert openai_handler._openai_responses_unit_parallelism() == 1

    monkeypatch.setenv("HEADROOM_TOOL_OUTPUT_COMPRESSION_PARALLELISM", "999")
    assert openai_handler._openai_responses_unit_parallelism() == 16


def test_openai_responses_cached_unit_handles_results_without_router_result():
    result = UnitCompressionResult(
        original="original",
        compressed="compressed",
        modified=True,
        tokens_before=2,
        tokens_after=1,
        tokens_saved=1,
        transforms_applied=[],
        strategy="none",
        router_result=None,
    )

    assert openai_handler._openai_responses_result_with_cache_hit(result) is result


def test_openai_responses_unit_cache_evicts_oldest_entry(monkeypatch):
    monkeypatch.setattr(openai_handler, "_OPENAI_RESPONSES_UNIT_CACHE_MAX_ENTRIES", 1)
    handler = OpenAIHandlerMixin()
    first = UnitCompressionResult(
        original="first",
        compressed="first compressed",
        modified=True,
        tokens_before=2,
        tokens_after=1,
        tokens_saved=1,
        transforms_applied=[],
        strategy="none",
        router_result=None,
    )
    second = UnitCompressionResult(
        original="second",
        compressed="second compressed",
        modified=True,
        tokens_before=2,
        tokens_after=1,
        tokens_saved=1,
        transforms_applied=[],
        strategy="none",
        router_result=None,
    )

    handler._store_openai_responses_cached_unit("first", first)
    handler._store_openai_responses_cached_unit("second", second)

    assert handler._get_openai_responses_cached_unit("first") is None
    assert handler._get_openai_responses_cached_unit("second") is second


def test_openai_responses_adapter_compresses_only_live_text_slots():
    router = ContentRouter()

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="kept words",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)
    long_text = " ".join(f"word{i}" for i in range(180))
    payload = {
        "model": "gpt-5",
        "input": [
            {"type": "reasoning", "encrypted_content": long_text},
            {"type": "function_call", "arguments": long_text},
            {"type": "local_shell_call_output", "call_id": "c1", "output": long_text},
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": long_text}],
            },
        ],
    }

    new_payload, modified, saved, transforms, units_by_category, strategy_chain, _attempted = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_test",
        )
    )

    assert modified is True
    assert saved > 0
    assert new_payload["input"][0]["encrypted_content"] == long_text
    assert new_payload["input"][1]["arguments"] == long_text
    assert new_payload["input"][2]["output"] == "kept words"
    assert new_payload["input"][3]["content"][0]["text"] == long_text
    assert any(t.startswith("router:openai:responses:") for t in transforms)
    assert units_by_category == {"applied": 1}
    assert strategy_chain == []


def test_openai_responses_adapter_compresses_custom_tool_call_output():
    router = ContentRouter()

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="custom output summary",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)
    long_text = " ".join(f"word{i}" for i in range(180))
    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "custom_tool_call_output",
                "call_id": "c1",
                "output": long_text,
            }
        ],
    }

    new_payload, modified, saved, transforms, units_by_category, strategy_chain, _attempted = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_test",
        )
    )

    assert modified is True
    assert saved > 0
    assert new_payload["input"][0]["output"] == "custom output summary"
    assert "router:openai:responses:custom_tool_call_output:kompress" in transforms
    assert units_by_category == {"applied": 1}
    assert strategy_chain == []


def test_openai_responses_adapter_compresses_output_content_parts():
    router = ContentRouter()

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="content part output summary",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)
    long_text = " ".join(f"part{i}" for i in range(180))
    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [{"type": "output_text", "text": long_text}],
            }
        ],
    }

    new_payload, modified, saved, transforms, units_by_category, strategy_chain, _attempted = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_content_parts",
        )
    )

    assert modified is True
    assert saved > 0
    output_val = new_payload["input"][0]["output"]
    assert isinstance(output_val, list), "content-part output must remain a list"
    assert len(output_val) == 1
    assert isinstance(output_val[0], dict)
    assert output_val[0].get("type") == "output_text"
    assert output_val[0].get("text") == "content part output summary"
    assert "router:openai:responses:function_call_output:kompress" in transforms
    assert units_by_category == {"applied": 1}
    assert strategy_chain == []


def test_openai_responses_adapter_reuses_exact_tool_output_cache():
    router = ContentRouter()
    calls = {"count": 0}

    def compress(self, content: str, **_kwargs):
        calls["count"] += 1
        return RouterCompressionResult(
            compressed="cached output summary",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)
    long_text = " ".join(f"word{i}" for i in range(180))

    payload_one = {
        "model": "gpt-5",
        "input": [
            {"type": "local_shell_call_output", "call_id": "c1", "output": long_text},
        ],
    }
    payload_two = {
        "model": "gpt-5",
        "input": [
            {"type": "message", "role": "user", "content": "changed envelope"},
            {"type": "local_shell_call_output", "call_id": "c2", "output": long_text},
        ],
    }

    new_payload_one, modified_one, saved_one, *_ = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload_one,
            model="gpt-5",
            request_id="req_cache_one",
        )
    )
    new_payload_two, modified_two, saved_two, *_ = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload_two,
            model="gpt-5",
            request_id="req_cache_two",
        )
    )

    assert calls["count"] == 1
    assert modified_one is True
    assert modified_two is True
    assert saved_one > 0
    assert saved_two == saved_one
    assert new_payload_one["input"][0]["output"] == "cached output summary"
    assert new_payload_two["input"][1]["output"] == "cached output summary"


def test_openai_responses_adapter_reuses_identical_tool_output_in_same_request():
    router = ContentRouter()
    calls = {"count": 0}

    def compress(self, content: str, **_kwargs):
        calls["count"] += 1
        return RouterCompressionResult(
            compressed="same request cached summary",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)
    long_text = " ".join(f"word{i}" for i in range(180))
    payload = {
        "model": "gpt-5",
        "input": [
            {"type": "function_call_output", "call_id": "c1", "output": long_text},
            {"type": "function_call_output", "call_id": "c2", "output": long_text},
        ],
    }

    new_payload, modified, saved, *_ = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_same_request_cache",
        )
    )

    assert calls["count"] == 1
    assert modified is True
    assert saved > 0
    assert [item["output"] for item in new_payload["input"]] == [
        "same request cached summary",
        "same request cached summary",
    ]


def test_openai_responses_adapter_parallelizes_cache_misses_preserving_order(monkeypatch):
    monkeypatch.setenv("HEADROOM_TOOL_OUTPUT_COMPRESSION_PARALLELISM", "4")
    router = ContentRouter()
    lock = threading.Lock()
    release = threading.Event()
    active = {"count": 0, "max": 0}

    def compress(self, content: str, **_kwargs):
        with lock:
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
            if active["count"] >= 2:
                release.set()
        release.wait(0.05)
        try:
            marker = content.rsplit(" marker", 1)[1]
            return RouterCompressionResult(
                compressed=f"summary marker{marker}",
                original=content,
                strategy_used=CompressionStrategy.KOMPRESS,
            )
        finally:
            with lock:
                active["count"] -= 1

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)

    def long_text(index: int) -> str:
        return " ".join(f"word{index}_{j}" for j in range(180)) + f" marker{index}"

    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "local_shell_call_output",
                "call_id": f"c{i}",
                "output": long_text(i),
            }
            for i in range(4)
        ],
    }

    new_payload, modified, saved, *_ = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_parallel",
        )
    )

    assert active["max"] >= 2
    assert modified is True
    assert saved > 0
    assert [item["output"] for item in new_payload["input"]] == [
        "summary marker0",
        "summary marker1",
        "summary marker2",
        "summary marker3",
    ]


def test_openai_responses_adapter_accepts_empty_input_list():
    router = ContentRouter()
    handler = _handler_with_router(router)
    payload = {"model": "gpt-5", "input": [], "tools": []}

    new_payload, modified, saved, transforms, units_by_category, strategy_chain, _attempted = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_test",
        )
    )

    assert new_payload == payload
    assert modified is False
    assert saved == 0
    assert transforms == []
    assert units_by_category == {}
    assert strategy_chain == []


def test_openai_responses_adapter_preserves_headroom_retrieve_outputs():
    router = ContentRouter()

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="compressed retrieve output",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)
    retrieved = " ".join(f"retrieved{i}" for i in range(180))
    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "function_call",
                "call_id": "call_retrieve",
                "name": "mcp__headroom__headroom_retrieve",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_retrieve",
                "output": retrieved,
            },
        ],
    }

    new_payload, modified, saved, transforms, units_by_category, strategy_chain, _attempted = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_test",
        )
    )

    assert modified is False
    assert saved == 0
    assert transforms == []
    assert new_payload == payload
    assert units_by_category == {}
    assert strategy_chain == []


def test_openai_responses_adapter_preserves_excluded_tool_outputs():
    """Regression for #940: outputs for HEADROOM_EXCLUDE_TOOLS tools stay raw.

    The Responses path carries the tool name on the ``function_call`` item and
    the originating ``call_id`` on the matching ``function_call_output``; the
    adapter must correlate them and skip compression for excluded tools.
    """
    router = ContentRouter()
    router.config.exclude_tools = {"serena.find_symbol", "find_symbol"}

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="should not be used",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)
    output = " ".join(f"sym{i}" for i in range(180))
    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "serena.find_symbol",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": output,
            },
        ],
    }

    new_payload, modified, saved, transforms, units_by_category, strategy_chain, _attempted = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_test",
        )
    )

    assert modified is False
    assert saved == 0
    assert transforms == []
    assert new_payload == payload
    assert units_by_category == {}
    assert strategy_chain == []


def test_openai_responses_adapter_losslessly_folds_excluded_grep_output():
    """Excluded tools skip *lossy* compression, but grep/log/json output is still
    byte/data-losslessly compacted on the Responses path (matches chat/Anthropic).
    """
    from headroom.transforms.lossless_compaction import search_unheading

    router = ContentRouter()
    router.config.exclude_tools = {"grep"}
    handler = _handler_with_router(router)
    grep_out = "".join(
        f"src/mod_{f}.py:{ln}:some matching content on this line here\n"
        for f in range(8)
        for ln in range(6)
    )
    payload = {
        "model": "gpt-5",
        "input": [
            {"type": "function_call", "call_id": "call_1", "name": "grep", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": grep_out},
        ],
    }

    new_payload, modified, saved, transforms, _units, _chain, _attempted = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_test",
        )
    )

    assert modified is True
    assert saved >= 0  # token accounting never goes negative
    assert "router:excluded:lossless" in transforms
    folded = new_payload["input"][1]["output"]
    assert len(folded) < len(grep_out)  # byte-smaller (real guarantee)
    assert search_unheading(folded) == grep_out  # byte-exact recovery


def test_openai_responses_adapter_losslessly_folds_excluded_output_content_parts():
    from headroom.transforms.lossless_compaction import search_unheading

    router = ContentRouter()
    router.config.exclude_tools = {"grep"}
    handler = _handler_with_router(router)
    grep_out = "".join(
        f"src/part_{f}.py:{ln}:matching content in a content part\n"
        for f in range(8)
        for ln in range(6)
    )
    payload = {
        "model": "gpt-5",
        "input": [
            {"type": "function_call", "call_id": "call_1", "name": "grep", "arguments": "{}"},
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [{"type": "output_text", "text": grep_out}],
            },
        ],
    }

    new_payload, modified, saved, transforms, _units, _chain, _attempted = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_content_part_fold",
        )
    )

    assert modified is True
    assert saved >= 0
    assert "router:excluded:lossless" in transforms
    folded = new_payload["input"][1]["output"]
    # Content-part array is preserved after compression
    assert isinstance(folded, list), "output must remain a list for content-part arrays"
    assert len(folded) == 1
    assert isinstance(folded[0], dict)
    assert folded[0].get("type") == "output_text"
    folded_text = folded[0].get("text", "")
    assert len(folded_text) < len(grep_out)
    assert search_unheading(folded_text) == grep_out


def test_openai_responses_adapter_excludes_tool_case_insensitively_with_debug(monkeypatch):
    """Excluded match is case-insensitive, and the debug path stays exercised.

    The configured name is lowercase only; the call advertises a mixed-case
    name, so the protection must hit via the lowercased fallback. Debug logging
    is enabled so the protected-extraction debug record is also covered.
    """
    monkeypatch.setattr(openai_handler, "_log_codex_compression_debug", lambda *_a, **_k: None)
    router = ContentRouter()
    router.config.exclude_tools = {"serena.find_symbol"}

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="should not be used",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)
    output = " ".join(f"sym{i}" for i in range(180))
    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "Serena.Find_Symbol",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": output,
            },
        ],
    }

    new_payload, modified, saved, *_ = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_test",
        )
    )

    assert modified is False
    assert saved == 0
    assert new_payload == payload


def test_openai_responses_adapter_keeps_websearch_output_verbatim():
    """Default-excluded web tools must bypass both lossy and lossless rewrites."""
    router = ContentRouter()

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="should not be used",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)
    output = (
        "{\n"
        '  "results": [\n'
        '    {"title": "Headroom", "snippet": "structured web payload with spacing that must remain verbatim"}\n'
        "  ]\n"
        "}"
    )
    payload = {
        "model": "gpt-5",
        "input": [
            {"type": "function_call", "call_id": "call_1", "name": "WebSearch", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": output},
        ],
    }

    new_payload, modified, saved, *_ = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_test",
        )
    )

    assert modified is False
    assert saved == 0
    assert new_payload == payload


def test_openai_responses_adapter_compresses_non_excluded_tool_outputs():
    """Only excluded tools are protected; other tool outputs still compress."""
    router = ContentRouter()
    router.config.exclude_tools = {"serena.find_symbol"}

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="compressed tool output",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)
    output = " ".join(f"word{i}" for i in range(180))
    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "some.other_tool",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": output,
            },
        ],
    }

    new_payload, modified, saved, transforms, units_by_category, strategy_chain, _attempted = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_test",
        )
    )

    assert modified is True
    assert saved > 0
    assert new_payload["input"][1]["output"] == "compressed tool output"
    assert "router:openai:responses:function_call_output:kompress" in transforms
    assert units_by_category == {"applied": 1}


def test_openai_responses_adapter_keeps_small_and_opaque_items():
    router = ContentRouter()

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="short",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)
    payload = {
        "model": "gpt-5",
        "input": [
            {"type": "local_shell_call_output", "call_id": "c1", "output": "too small"},
            {"type": "compaction", "encrypted_content": " ".join(["secret"] * 200)},
        ],
    }

    new_payload, modified, saved, transforms, units_by_category, strategy_chain, _attempted = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_test",
        )
    )

    assert modified is False
    assert saved == 0
    assert transforms == []
    assert new_payload == payload
    assert units_by_category == {"size_floor": 1}
    assert strategy_chain == []


def test_openai_responses_payload_routes_through_content_router_without_rust(
    monkeypatch,
):
    router = ContentRouter()

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="compressed fallback",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)

    import headroom._core as core

    def rust_must_not_run(*_args, **_kwargs):
        raise AssertionError("Responses payload compression should route through ContentRouter")

    monkeypatch.setattr(core, "compress_openai_responses_live_zone", rust_must_not_run)

    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "local_shell_call_output",
                "call_id": "c1",
                "output": " ".join(f"word{i}" for i in range(180)),
            }
        ],
    }

    new_payload, modified, saved, transforms, reason, _, _, _ = (
        handler._compress_openai_responses_payload(
            payload,
            model="gpt-5",
            request_id="req_router",
        )
    )

    assert modified is True
    assert saved > 0
    assert reason is None
    assert new_payload["input"][0]["output"] == "compressed fallback"
    assert any(t.startswith("router:openai:responses:") for t in transforms)


def test_openai_responses_adapter_aggregates_small_tool_outputs_before_floor():
    """Regression for #2050: many individually-small tool outputs whose combined
    size clears the floor must still reach the router.

    The Responses path extracts each ``function_call_output`` as its own unit.
    A per-item size floor would reject every unit in a session made of many
    small tool outputs (the Codex shape), yielding 0% savings even though the
    aggregate compressible text is large. The floor must be evaluated against
    the aggregate of the extracted group, matching the batch (Anthropic) path.
    """
    router = ContentRouter()

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="tiny summary",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)

    # Each output is individually below OPENAI_RESPONSES_ROUTER_MIN_BYTES (512),
    # but the four combined exceed it — exactly the case that used to floor to
    # zero savings. Guard the premise so the test stays honest if the byte
    # shapes drift.
    floor = OpenAIHandlerMixin.OPENAI_RESPONSES_ROUTER_MIN_BYTES
    outputs = [" ".join(f"tok{i}_{j}" for j in range(30)) for i in range(4)]
    assert all(len(o.encode("utf-8")) < floor for o in outputs)
    assert sum(len(o.encode("utf-8")) for o in outputs) >= floor

    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "function_call_output",
                "call_id": f"c{i}",
                "output": output,
            }
            for i, output in enumerate(outputs)
        ],
    }

    new_payload, modified, saved, transforms, units_by_category, _strategy_chain, _attempted = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_aggregate_floor",
        )
    )

    assert modified is True
    assert saved > 0
    # No unit should be size-floored; every extracted unit is compressed.
    assert "size_floor" not in units_by_category
    assert units_by_category == {"applied": len(outputs)}
    assert all(item["output"] == "tiny summary" for item in new_payload["input"])


def test_openai_responses_adapter_floors_when_aggregate_below_threshold():
    """Complement to #2050: when the *whole* group is below the floor the units
    are still skipped, so trivially small payloads don't churn the router.
    """
    router = ContentRouter()

    def compress(self, content: str, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("aggregate below floor should skip compression")

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)

    floor = OpenAIHandlerMixin.OPENAI_RESPONSES_ROUTER_MIN_BYTES
    outputs = ["ok", "done"]
    assert sum(len(o.encode("utf-8")) for o in outputs) < floor

    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "function_call_output",
                "call_id": f"c{i}",
                "output": output,
            }
            for i, output in enumerate(outputs)
        ],
    }

    new_payload, modified, saved, _transforms, units_by_category, _strategy_chain, _attempted = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_aggregate_below",
        )
    )

    assert modified is False
    assert saved == 0
    assert units_by_category == {"size_floor": len(outputs)}
    assert new_payload == payload


def test_openai_responses_adapter_preserves_non_text_parts_in_content_parts():
    """Regression for #2235: when output is a content-part array with non-text
    parts (input_image), compression updates only the output_text part's text
    field and preserves all other parts unchanged.
    """
    router = ContentRouter()

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="compressed summary",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)
    long_text = " ".join(f"word{i}" for i in range(180))
    original_image_part = {
        "type": "input_image",
        "image_url": "data:image/png;base64,iVBORw0KGgo=",
    }
    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [
                    {"type": "output_text", "text": long_text},
                    original_image_part,
                ],
            }
        ],
    }

    new_payload, modified, saved, transforms, *_ = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_non_text_preserved",
        )
    )

    assert modified is True
    assert saved > 0
    output = new_payload["input"][0]["output"]
    assert isinstance(output, list), "output must remain a list"
    assert len(output) == 2, "both parts must be preserved"

    # First part: output_text with compressed text
    assert output[0]["type"] == "output_text"
    assert output[0]["text"] == "compressed summary"

    # Second part: input_image preserved byte-for-byte
    assert output[1] == original_image_part


def test_openai_responses_adapter_losslessly_folds_excluded_content_part_with_image():
    """Excluded tool (grep) with content-part output containing both text and
    an image: the text part should be losslessly folded, and the image part
    must remain byte-identical.
    """
    from headroom.transforms.lossless_compaction import search_unheading

    router = ContentRouter()
    router.config.exclude_tools = {"grep"}
    handler = _handler_with_router(router)
    grep_out = "\n".join(
        f"src/file_{f}.py:{ln}:some grep output here"
        for f in range(8)
        for ln in range(6)
    )
    original_image_part = {
        "type": "input_image",
        "image_url": "data:image/png;base64,iVBORw0KGgo=",
    }
    payload = {
        "model": "gpt-5",
        "input": [
            {"type": "function_call", "call_id": "call_1", "name": "grep", "arguments": "{}"},
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [
                    {"type": "output_text", "text": grep_out},
                    original_image_part,
                ],
            },
        ],
    }

    new_payload, modified, saved, transforms, *_ = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_excluded_content_img",
        )
    )

    assert modified is True
    assert "router:excluded:lossless" in transforms
    output = new_payload["input"][1]["output"]
    # Content-part array preserved
    assert isinstance(output, list), "output must remain a list"
    assert len(output) == 2, "both parts must be preserved"

    # Text part: losslessly folded
    assert output[0]["type"] == "output_text"
    folded_text = output[0]["text"]
    assert isinstance(folded_text, str)
    assert len(folded_text) < len(grep_out)
    assert search_unheading(folded_text) == grep_out

    # Image part: preserved byte-for-byte
    assert output[1] == original_image_part


@pytest.mark.parametrize(
    "desc,payload_output,expect_modified,expect_saved,checks",
    [
        (
            "multiple output_text parts: unchanged",
            [
                {"type": "output_text", "text": " ".join(f"word{i}" for i in range(180))},
                {"type": "output_text", "text": "second block"},
            ],
            True,   # compression ran but write-back skipped
            181,    # Router tokens saved, even though write-back is skipped
            [("output is unchanged", lambda o: o == [
                {"type": "output_text", "text": " ".join(f"word{i}" for i in range(180))},
                {"type": "output_text", "text": "second block"},
            ])],
        ),
        (
            "empty output array: unchanged",
            [],
            False,  # no compression attempted
            0,
            [("output is empty list", lambda o: o == [])],
        ),
    ],
)
def test_openai_responses_adapter_content_part_safety_guards(
    desc: str,
    payload_output: list,
    expect_modified: bool,
    expect_saved: int,
    checks: list[tuple[str, object]],
):
    """Content-part arrays that cannot be safely compressed in-place must
    be left unchanged."""
    router = ContentRouter()

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="compressed",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)
    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": payload_output,
            }
        ],
    }

    new_payload, modified, saved, *_ = (
        handler._compress_openai_responses_live_text_units_with_router(
            payload,
            model="gpt-5",
            request_id="req_" + desc.replace(" ", "_"),
        )
    )

    assert modified is expect_modified
    assert saved == expect_saved
    output = new_payload["input"][0]["output"]
    for label, check in checks:
        assert check(output), f"Check failed: {label}"

# ──────────────────────────────────────────────
# 对抗性测试 — content-part array 写回
# 验证 _set_slot_text 在各种异常/边缘输入下
# 不崩溃，不破坏数据
# ──────────────────────────────────────────────

_ADVERSARIAL_LONG_TEXT = "long " * 120  # 600 chars, enough to trigger compression


@pytest.mark.parametrize(
    "desc,payload_output,expect_output",
    [
        # ── 异常 part 结构 ──
        (
            "output_text at position 1 (not 0)",
            [
                {"type": "input_image", "image_url": "data:img/png;base64,abc"},
                {"type": "output_text", "text": _ADVERSARIAL_LONG_TEXT},
            ],
            # text at idx 1 should be compressed, image at idx 0 preserved
            lambda out: (
                out[0]["image_url"] == "data:img/png;base64,abc"
                and out[1]["text"] == "compressed_adversarial"
            ),
        ),
        (
            "output_text with extra keys preserved",
            [
                {"type": "output_text", "text": _ADVERSARIAL_LONG_TEXT, "annotations": [1, 2], "extra": "keep"},
                {"type": "input_image", "image_url": "data:img/png;base64,def"},
            ],
            lambda out: (
                out[0].get("annotations") == [1, 2]
                and out[0].get("extra") == "keep"
                and out[0]["text"] == "compressed_adversarial"
            ),
        ),
        (
            "non-dict element in list preserved",
            [
                {"type": "output_text", "text": _ADVERSARIAL_LONG_TEXT},
                "bare-string-survivor",
            ],
            lambda out: (
                out[0]["text"] == "compressed_adversarial"
                and out[1] == "bare-string-survivor"
            ),
        ),
        (
            "unknown part type preserved",
            [
                {"type": "output_text", "text": _ADVERSARIAL_LONG_TEXT},
                {"type": "weird_new_type", "data": "unknown-data"},
            ],
            lambda out: (
                out[0]["text"] == "compressed_adversarial"
                and out[1]["type"] == "weird_new_type"
                and out[1]["data"] == "unknown-data"
            ),
        ),
        (
            "output_text sandwiched between non-text parts",
            [
                {"type": "input_image", "image_url": "data:img/png;base64,img1"},
                {"type": "output_text", "text": _ADVERSARIAL_LONG_TEXT},
                {"type": "input_image", "image_url": "data:img/png;base64,img2"},
            ],
            lambda out: (
                out[0]["image_url"] == "data:img/png;base64,img1"
                and out[1]["text"] == "compressed_adversarial"
                and out[2]["image_url"] == "data:img/png;base64,img2"
            ),
        ),
        # ── 2+ output_text variants ──
        (
            "3 output_text parts interspersed with non-text (unchanged, data safety)",
            [
                {"type": "output_text", "text": _ADVERSARIAL_LONG_TEXT},
                {"type": "input_image", "image_url": "data:img/png;base64,img1"},
                {"type": "output_text", "text": "middle text"},
                {"type": "input_image", "image_url": "data:img/png;base64,img2"},
                {"type": "output_text", "text": "last text"},
            ],
            lambda out: out == [
                {"type": "output_text", "text": _ADVERSARIAL_LONG_TEXT},
                {"type": "input_image", "image_url": "data:img/png;base64,img1"},
                {"type": "output_text", "text": "middle text"},
                {"type": "input_image", "image_url": "data:img/png;base64,img2"},
                {"type": "output_text", "text": "last text"},
            ],
        ),
        # ── 大数组压力 ──
        (
            "1000 parts (1 text + 999 images) - stress test",
            # Start with output_text, then 999 image parts
            [{"type": "output_text", "text": _ADVERSARIAL_LONG_TEXT}]
            + [{"type": "input_image", "image_url": f"data:img/png;base64,{i}"} for i in range(999)],
            lambda out: (
                out[0]["text"] == "compressed_adversarial"
                and len(out) == 1000
            ),
        ),
        # ── output_text with null/edge text ──
        (
            "output_text with text=None — should not be compressed",
            [
                {"type": "output_text", "text": None},
                {"type": "input_image", "image_url": "data:img/png;base64,abc"},
            ],
            lambda out: (
                out[0]["type"] == "output_text"
                and out[0].get("text") is None
                and out[1]["image_url"] == "data:img/png;base64,abc"
            ),
        ),
        (
            "output_text with text=0 (int) — should not be compressed",
            [
                {"type": "output_text", "text": 0},
            ],
            lambda out: (
                out[0]["type"] == "output_text"
                and out[0]["text"] == 0
            ),
        ),
    ],
)
def test_openai_responses_adversarial_content_part_writeback(
    desc: str,
    payload_output: list,
    expect_output,
):
    """Adversarial / fuzz-style tests for _set_slot_text content-part
    writeback. Ensures no crash and no data corruption regardless
    of output part structure."""
    router = ContentRouter()

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="compressed_adversarial",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = _handler_with_router(router)
    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": payload_output,
            }
        ],
    }

    new_payload, *_ = handler._compress_openai_responses_live_text_units_with_router(
        payload,
        model="gpt-5",
        request_id="req_adv_" + desc.replace(" ", "_"),
    )

    result = new_payload["input"][0]["output"]
    assert expect_output(result), f"[{desc}] Adversarial check failed. Got: {result}"
