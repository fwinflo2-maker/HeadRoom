"""Tests for headroom.proxy.interceptors.base."""

#  Copyright (c) 2026 Noel Kuntze

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from headroom.config import TransformResult
from headroom.proxy.interceptors.base import (
    INTERCEPTORS,
    InterceptionResult,
    ToolResultInterceptor,
    ToolResultInterceptorTransform,
    TransformSpan,
    _build_tool_use_index,
    _record_failure,
    _tool_use_id_for_message,
    apply_to_messages,
    interceptor_failure_counts,
    register,
    reset_interceptor_failure_counts,
)
from headroom.tokenizer import Tokenizer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_globals() -> None:
    reset_interceptor_failure_counts()
    INTERCEPTORS.clear()
    yield
    reset_interceptor_failure_counts()
    INTERCEPTORS.clear()
    # This module intentionally clears the shared registry for isolation. Put
    # the package's built-in interceptor back so later modules see normal
    # default startup state rather than an order-dependent empty registry.
    from headroom.proxy.interceptors.astgrep import AstGrepReadOutline

    register(AstGrepReadOutline())


@pytest.fixture
def tokenizer() -> Tokenizer:
    t = MagicMock(spec=Tokenizer)
    t.count_text.side_effect = lambda s: max(len(s) // 2, 1)
    t.count_messages.return_value = 100
    return t


# ---------------------------------------------------------------------------
# interceptor_failure_counts / reset_interceptor_failure_counts
# ---------------------------------------------------------------------------


class TestFailureCounts:
    def test_initial_state(self) -> None:
        assert interceptor_failure_counts() == {}

    def test_after_single_failure(self) -> None:
        _record_failure("test-interceptor")
        counts = interceptor_failure_counts()
        assert counts["test-interceptor"] == 1

    def test_after_multiple_failures(self) -> None:
        _record_failure("a")
        _record_failure("a")
        _record_failure("b")
        counts = interceptor_failure_counts()
        assert counts == {"a": 2, "b": 1}

    def test_reset_clears_counts(self) -> None:
        _record_failure("test")
        reset_interceptor_failure_counts()
        assert interceptor_failure_counts() == {}

    def test_reset_is_idempotent(self) -> None:
        reset_interceptor_failure_counts()
        assert interceptor_failure_counts() == {}

    def test_snapshot_isolation(self) -> None:
        _record_failure("x")
        snapshot = interceptor_failure_counts()
        _record_failure("x")
        assert snapshot == {"x": 1}

    def test_concurrent_safety(self) -> None:
        import threading

        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(100):
                    _record_failure("conc")
                    interceptor_failure_counts()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert interceptor_failure_counts()["conc"] == 400


# ---------------------------------------------------------------------------
# ToolResultInterceptor Protocol
# ---------------------------------------------------------------------------


class TestToolResultInterceptor:
    def test_protocol_is_runtime_checkable(self) -> None:
        assert hasattr(ToolResultInterceptor, "__instancecheck__")

    def test_valid_implementation_is_detected(self) -> None:
        class Impl:
            name = "test"

            def matches(self, tool_name, tool_input, tool_output):
                return True

            def transform(self, tool_name, tool_input, tool_output):
                return None

            def progressive_disclosure_key(self, tool_name, tool_input):
                return None

        assert isinstance(Impl(), ToolResultInterceptor)

    def test_missing_name_is_not_interceptor(self) -> None:
        class Impl:
            def matches(self, tool_name, tool_input, tool_output):
                return True

            def transform(self, tool_name, tool_input, tool_output):
                return None

        assert not isinstance(Impl(), ToolResultInterceptor)

    def test_missing_matches_is_not_interceptor(self) -> None:
        class Impl:
            name = "test"

            def transform(self, tool_name, tool_input, tool_output):
                return None

        assert not isinstance(Impl(), ToolResultInterceptor)

    def test_missing_transform_is_not_interceptor(self) -> None:
        class Impl:
            name = "test"

            def matches(self, tool_name, tool_input, tool_output):
                return True

        assert not isinstance(Impl(), ToolResultInterceptor)

    def test_progressive_disclosure_key_can_return_none(self) -> None:
        class Impl:
            name = "test"

            def matches(self, tool_name, tool_input, tool_output):
                return True

            def transform(self, tool_name, tool_input, tool_output):
                return None

            def progressive_disclosure_key(self, tool_name, tool_input):
                return None

        assert isinstance(Impl(), ToolResultInterceptor)

    def test_method_signatures_accept_none_tool_name(self) -> None:
        class Impl:
            name = "test"

            def matches(self, tool_name, tool_input, tool_output):
                return tool_name is None

            def transform(self, tool_name, tool_input, tool_output):
                return None

        impl = Impl()
        assert impl.matches(None, {}, "")
        assert impl.transform(None, {}, "") is None

    def test_progressive_disclosure_returns_none_by_default(self) -> None:
        class Impl:
            name = "test"

            def matches(self, tool_name, tool_input, tool_output):
                return False

            def transform(self, tool_name, tool_input, tool_output):
                return None

            def progressive_disclosure_key(self, tool_name, tool_input):
                return None

        assert Impl().progressive_disclosure_key("Read", {}) is None


# ---------------------------------------------------------------------------
# TransformSpan
# ---------------------------------------------------------------------------


class TestTransformSpan:
    def test_construction(self) -> None:
        span = TransformSpan(tool="test", tokens_before=100, tokens_after=60)
        assert span.tool == "test"
        assert span.tokens_before == 100
        assert span.tokens_after == 60

    def test_tokens_saved_positive(self) -> None:
        span = TransformSpan(tool="t", tokens_before=100, tokens_after=60)
        assert span.tokens_saved == 40

    def test_tokens_saved_zero_when_same(self) -> None:
        span = TransformSpan(tool="t", tokens_before=50, tokens_after=50)
        assert span.tokens_saved == 0

    def test_tokens_saved_clamps_to_zero(self) -> None:
        span = TransformSpan(tool="t", tokens_before=50, tokens_after=80)
        assert span.tokens_saved == 0

    def test_immutable_fields(self) -> None:
        span = TransformSpan(tool="t", tokens_before=10, tokens_after=5)
        with pytest.raises(AttributeError):
            span.tool = "other"  # type: ignore[misc]

    def test_tokens_saved_property(self) -> None:
        span = TransformSpan(tool="t", tokens_before=200, tokens_after=150)
        assert span.tokens_saved == 50

    def test_zero_before_and_after(self) -> None:
        span = TransformSpan(tool="t", tokens_before=0, tokens_after=0)
        assert span.tokens_saved == 0


# ---------------------------------------------------------------------------
# InterceptionResult
# ---------------------------------------------------------------------------


class TestInterceptionResult:
    def test_construction(self) -> None:
        result = InterceptionResult(messages=[], spans=[])
        assert result.messages == []
        assert result.spans == []

    def test_with_messages(self) -> None:
        msgs = [{"role": "user", "content": "hi"}]
        spans = [TransformSpan(tool="t", tokens_before=10, tokens_after=5)]
        result = InterceptionResult(messages=msgs, spans=spans)
        assert result.messages is msgs
        assert result.spans is spans

    def test_immutable_fields(self) -> None:
        result = InterceptionResult(messages=[], spans=[])
        with pytest.raises(AttributeError):
            result.messages = [{"x": "y"}]  # type: ignore[misc]
        with pytest.raises(AttributeError):
            result.spans = []  # type: ignore[misc]


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_adds_interceptor(self) -> None:
        interceptor = _make_interceptor("a")
        register(interceptor)
        assert interceptor in INTERCEPTORS

    def test_register_multiple(self) -> None:
        a = _make_interceptor("a")
        b = _make_interceptor("b")
        register(a)
        register(b)
        assert len(INTERCEPTORS) == 2

    def test_register_is_idempotent_by_name(self) -> None:
        a1 = _make_interceptor("dup")
        a2 = _make_interceptor("dup")
        register(a1)
        register(a2)
        assert len(INTERCEPTORS) == 1

    def test_different_names_both_registered(self) -> None:
        i1 = _make_interceptor("x")
        i2 = _make_interceptor("y")
        register(i1)
        register(i2)
        assert {i.name for i in INTERCEPTORS} == {"x", "y"}


# ---------------------------------------------------------------------------
# _build_tool_use_index
# ---------------------------------------------------------------------------


class TestBuildToolUseIndex:
    def test_empty_messages(self) -> None:
        assert _build_tool_use_index([]) == {}

    def test_anthropic_tool_use(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "Read",
                        "input": {"file_path": "foo.py"},
                    }
                ],
            }
        ]
        idx = _build_tool_use_index(msgs)
        assert idx["tu_1"] == ("Read", {"file_path": "foo.py"})

    def test_openai_tool_call(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "read_file", "arguments": '{"path":"x.py"}'},
                    }
                ],
            }
        ]
        idx = _build_tool_use_index(msgs)
        assert idx["call_1"] == ("read_file", {"path": "x.py"})

    def test_openai_tool_call_with_dict_args(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_2",
                        "function": {"name": "search", "arguments": {"q": "hello"}},
                    }
                ],
            }
        ]
        idx = _build_tool_use_index(msgs)
        assert idx["call_2"] == ("search", {"q": "hello"})

    def test_skips_non_dict_content_blocks(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": ["string_block", {"type": "tool_use", "id": "tu_2", "name": "cat"}],
            }
        ]
        idx = _build_tool_use_index(msgs)
        assert "tu_2" in idx

    def test_skips_tool_use_without_id(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Read", "input": {}}],
            }
        ]
        assert _build_tool_use_index(msgs) == {}

    def test_skips_tool_use_with_non_string_id(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": 123, "name": "Read"}],
            }
        ]
        assert _build_tool_use_index(msgs) == {}

    def test_openai_bad_json_args_falls_back_to_empty_dict(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {"name": "f", "arguments": "not-json"},
                    }
                ],
            }
        ]
        idx = _build_tool_use_index(msgs)
        assert idx["c1"] == ("f", {})

    def test_openai_without_function_returns_none_name(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "c1"}],
            }
        ]
        idx = _build_tool_use_index(msgs)
        assert idx["c1"] == (None, {})


# ---------------------------------------------------------------------------
# _tool_use_id_for_message
# ---------------------------------------------------------------------------


class TestToolUseIdForMessage:
    def test_anthropic_tool_result(self) -> None:
        msg = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "out"}],
        }
        assert _tool_use_id_for_message(msg) == "tu_1"

    def test_openai_tool_result(self) -> None:
        msg = {"role": "tool", "tool_call_id": "call_1", "content": "out"}
        assert _tool_use_id_for_message(msg) == "call_1"

    def test_no_tool_result_returns_none(self) -> None:
        msg = {"role": "user", "content": "plain text"}
        assert _tool_use_id_for_message(msg) is None

    def test_anthropic_without_tool_use_id(self) -> None:
        msg = {"role": "user", "content": [{"type": "tool_result", "content": "out"}]}
        assert _tool_use_id_for_message(msg) is None

    def test_openai_without_tool_call_id(self) -> None:
        msg = {"role": "tool", "content": "out"}
        assert _tool_use_id_for_message(msg) is None


# ---------------------------------------------------------------------------
# apply_to_messages
# ---------------------------------------------------------------------------


class TestApplyToMessages:
    def test_no_interceptors_returns_unchanged(self, tokenizer: Tokenizer) -> None:
        msgs = [{"role": "user", "content": "hi"}]
        result = apply_to_messages(msgs, tokenizer)
        assert result.messages is msgs
        assert result.spans == []

    def test_non_tool_result_messages_pass_through(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor("test", matches=True, transform="shorter")
        register(interceptor)
        msgs = [{"role": "user", "content": "hello"}]
        result = apply_to_messages(msgs, tokenizer)
        assert result.messages == msgs

    def test_matching_interceptor_transforms_tool_result(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor("test", matches=True, transform="shorter")
        register(interceptor)
        msgs = _anthropic_tool_result_msg("tu_1", "long content here")
        tu = _anthropic_tool_use("tu_1", "Read", {"file_path": "x.py"})
        all_msgs = [tu, msgs]
        result = apply_to_messages(all_msgs, tokenizer)
        assert len(result.spans) == 1
        assert result.spans[0].tool == "test"
        assert result.messages[1] is not msgs

    def test_non_matching_interceptor_passes_through(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor("test", matches=False)
        register(interceptor)
        msgs = _anthropic_tool_result_msg("tu_1", "long content here")
        tu = _anthropic_tool_use("tu_1", "Read", {"file_path": "x.py"})
        all_msgs = [tu, msgs]
        result = apply_to_messages(all_msgs, tokenizer)
        assert result.spans == []
        assert result.messages[1] is msgs

    def test_transform_returning_none_skips(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor("test", matches=True, transform=None)
        register(interceptor)
        msgs = _anthropic_tool_result_msg("tu_1", "content")
        result = apply_to_messages([_anthropic_tool_use("tu_1", "Read", {}), msgs], tokenizer)
        assert result.spans == []

    def test_transform_returning_same_content_skips(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor("test", matches=True, transform="same")
        register(interceptor)
        msgs = _anthropic_tool_result_msg("tu_1", "same")
        result = apply_to_messages([_anthropic_tool_use("tu_1", "Read", {}), msgs], tokenizer)
        assert result.spans == []

    def test_refuses_to_enlarge(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor("test", matches=True, transform="much much longer content")
        register(interceptor)
        msgs = _anthropic_tool_result_msg("tu_1", "short")
        result = apply_to_messages([_anthropic_tool_use("tu_1", "Read", {}), msgs], tokenizer)
        assert result.spans == []

    def test_multiple_interceptors_chain(self, tokenizer: Tokenizer) -> None:
        a = _make_interceptor("a", matches=True, transform="from aaaa")
        b = _make_interceptor("b", matches=True, transform="from bb")
        register(a)
        register(b)
        msgs = _anthropic_tool_result_msg("tu_1", "original long content here")
        result = apply_to_messages([_anthropic_tool_use("tu_1", "Read", {}), msgs], tokenizer)
        assert len(result.spans) == 2
        assert result.spans[0].tool == "a"
        assert result.spans[1].tool == "b"

    def test_frozen_messages_pass_through(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor("test", matches=True, transform="short")
        register(interceptor)
        msgs = _anthropic_tool_result_msg("tu_1", "long content here")
        tu = _anthropic_tool_use("tu_1", "Read", {"file_path": "x.py"})
        all_msgs = [tu, msgs]
        result = apply_to_messages(all_msgs, tokenizer, frozen_count=2)
        assert result.spans == []
        assert result.messages == all_msgs

    def test_frozen_prefix_seeds_progressive_disclosure(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor(
            "test", matches=True, transform="short", disclosure_key="/x.py"
        )
        register(interceptor)
        frozen_tr = _anthropic_tool_result_msg("tu_frozen", "long content here")
        frozen_tu = _anthropic_tool_use("tu_frozen", "Read", {"file_path": "/x.py"})
        mutable_tr = _anthropic_tool_result_msg("tu_mut", "long content here again")
        mutable_tu = _anthropic_tool_use("tu_mut", "Read", {"file_path": "/x.py"})
        all_msgs = [frozen_tu, frozen_tr, mutable_tu, mutable_tr]
        result = apply_to_messages(all_msgs, tokenizer, frozen_count=2)
        assert result.spans == []

    def test_progressive_disclosure_skips_second_read(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor(
            "test", matches=True, transform="short", disclosure_key="/x.py"
        )
        register(interceptor)
        tr1 = _anthropic_tool_result_msg("tu_1", "long content here")
        tu1 = _anthropic_tool_use("tu_1", "Read", {"file_path": "/x.py"})
        tr2 = _anthropic_tool_result_msg("tu_2", "long content here again")
        tu2 = _anthropic_tool_use("tu_2", "Read", {"file_path": "/x.py"})
        all_msgs = [tu1, tr1, tu2, tr2]
        result = apply_to_messages(all_msgs, tokenizer)
        assert len(result.spans) == 1
        assert result.spans[0].tool == "test"

    def test_openai_format_tool_result(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor("test", matches=True, transform="short")
        register(interceptor)
        msgs = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "c1", "function": {"name": "Read", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "this is a long output"},
        ]
        result = apply_to_messages(msgs, tokenizer)
        assert len(result.spans) == 1

    def test_interceptor_exception_is_caught(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor("test", matches=True, transform=_RaisesException())
        register(interceptor)
        msgs = _anthropic_tool_result_msg("tu_1", "content")
        result = apply_to_messages([_anthropic_tool_use("tu_1", "Read", {}), msgs], tokenizer)
        assert result.messages[1] is msgs
        assert interceptor_failure_counts().get("test", 0) == 1

    def test_matches_exception_is_caught(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor("test", matches=_RaisesException())
        register(interceptor)
        msgs = _anthropic_tool_result_msg("tu_1", "content")
        result = apply_to_messages([_anthropic_tool_use("tu_1", "Read", {}), msgs], tokenizer)
        assert result.messages[1] is msgs

    def test_orphaned_tool_result(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor("test", matches=True, transform="short")
        register(interceptor)
        msgs = _anthropic_tool_result_msg("orphan", "long content here")
        result = apply_to_messages([msgs], tokenizer)
        assert len(result.spans) == 1

    def test_empty_tool_result_content_skipped(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor("test", matches=True, transform="short")
        register(interceptor)
        msgs = _anthropic_tool_result_msg("tu_1", "")
        result = apply_to_messages([_anthropic_tool_use("tu_1", "Read", {}), msgs], tokenizer)
        assert result.spans == []


# ---------------------------------------------------------------------------
# ToolResultInterceptorTransform
# ---------------------------------------------------------------------------


class TestToolResultInterceptorTransform:
    def test_name(self) -> None:
        assert ToolResultInterceptorTransform.name == "tool_result_interceptors"

    def test_apply_no_interceptors(self, tokenizer: Tokenizer) -> None:
        transform = ToolResultInterceptorTransform()
        msgs = [{"role": "user", "content": "hi"}]
        result = transform.apply(msgs, tokenizer)
        assert isinstance(result, TransformResult)
        assert result.messages is msgs
        assert result.transforms_applied == []

    def test_apply_with_interceptor(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor("x", matches=True, transform="short")
        register(interceptor)
        transform = ToolResultInterceptorTransform()
        tr = _anthropic_tool_result_msg("tu_1", "long content here")
        msgs = [_anthropic_tool_use("tu_1", "Read", {}), tr]
        result = transform.apply(msgs, tokenizer)
        assert "interceptor:x" in result.transforms_applied
        assert result.tokens_before == 100  # from mock
        assert result.tokens_after == 100  # from mock

    def test_apply_passes_frozen_count(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor("x", matches=True, transform="short")
        register(interceptor)
        transform = ToolResultInterceptorTransform()
        tr = _anthropic_tool_result_msg("tu_1", "long content here")
        msgs = [_anthropic_tool_use("tu_1", "Read", {}), tr]
        result = transform.apply(msgs, tokenizer, frozen_message_count=2)
        assert result.messages == msgs

    def test_apply_no_spans_when_no_match(self, tokenizer: Tokenizer) -> None:
        interceptor = _make_interceptor("x", matches=False)
        register(interceptor)
        transform = ToolResultInterceptorTransform()
        tr = _anthropic_tool_result_msg("tu_1", "long content")
        msgs = [_anthropic_tool_use("tu_1", "Read", {}), tr]
        result = transform.apply(msgs, tokenizer)
        assert result.transforms_applied == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_interceptor(
    _name: str,
    matches: bool | _RaisesException = True,
    transform: str | None | _RaisesException = "short",
    disclosure_key: str | None = None,
) -> ToolResultInterceptor:
    class _Impl:
        name = _name

        def matches(self, tool_name, tool_input, tool_output):
            if isinstance(matches, _RaisesException):
                raise RuntimeError("matches failed")
            return matches if not callable(matches) else matches(tool_name, tool_input, tool_output)

        def transform(self, tool_name, tool_input, tool_output):
            if isinstance(transform, _RaisesException):
                raise RuntimeError("transform failed")
            return transform

        def progressive_disclosure_key(self, tool_name, tool_input):
            return disclosure_key

    return _Impl()


class _RaisesException:
    """Sentinel: raises on call."""


def _anthropic_tool_use(tuid: str, name: str, inp: dict) -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": tuid, "name": name, "input": inp}],
    }


def _anthropic_tool_result_msg(tuid: str, content: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tuid, "content": content}],
    }
