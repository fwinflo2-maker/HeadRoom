"""``recount_messages_offloaded`` keeps the consistency re-count off the loop (#2810)."""

from __future__ import annotations

import asyncio
import threading

import pytest

from headroom.proxy.token_counting import recount_messages_offloaded


class _SumTokenizer:
    """Deterministic tokenizer: token count == total content length."""

    def count_messages(self, messages) -> int:  # noqa: ANN001
        return sum(len(m.get("content", "")) for m in messages)


class _OwnerWithExecutor:
    """Owner whose executor runs the work off the event-loop thread."""

    async def _run_compression_in_executor(self, fn, timeout):  # noqa: ANN001, ANN201
        return await asyncio.to_thread(fn)


class _OwnerNoExecutor:
    pass


@pytest.mark.asyncio
async def test_recount_runs_off_the_event_loop_thread() -> None:
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    class _ThreadRecordingTokenizer:
        def count_messages(self, messages) -> int:  # noqa: ANN001
            seen["thread"] = threading.get_ident()
            return len(messages)

    result = await recount_messages_offloaded(
        _OwnerWithExecutor(), _ThreadRecordingTokenizer(), [{}, {}], [{}]
    )

    assert result == (2, 1)  # per-list counts, in order
    assert "thread" in seen, "the count never ran"
    assert seen["thread"] != loop_thread, "the count ran on the event-loop thread"


@pytest.mark.asyncio
async def test_recount_shares_one_tokenizer_across_lists() -> None:
    # Both endpoints must be counted with the SAME tokenizer so the delta is on
    # one scale; the helper counts every list with the tokenizer it is given.
    tok = _SumTokenizer()
    before, after = await recount_messages_offloaded(
        _OwnerWithExecutor(), tok, [{"content": "aaaa"}], [{"content": "bb"}]
    )
    assert (before, after) == (4, 2)


@pytest.mark.asyncio
async def test_recount_fails_open_without_executor() -> None:
    # No compression executor: count inline rather than raising.
    result = await recount_messages_offloaded(
        _OwnerNoExecutor(), _SumTokenizer(), [{"content": "ab"}], [{"content": "c"}]
    )
    assert result == (2, 1)


@pytest.mark.asyncio
async def test_recount_fails_open_on_executor_error() -> None:
    class _BoomOwner:
        async def _run_compression_in_executor(self, fn, timeout):  # noqa: ANN001, ANN201
            raise RuntimeError("executor down")

    # A failing/timed-out offload must fall back to an inline count, not surface
    # the error to the request handler.
    result = await recount_messages_offloaded(
        _BoomOwner(), _SumTokenizer(), [{"content": "xyz"}], []
    )
    assert result == (3, 0)
