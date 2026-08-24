"""Regression tests: file reads over the OpenAI Responses API path must stay verbatim.

Copilot CLI (and other Responses-native harnesses) read files two ways:

1. A first-class ``view`` tool (the Copilot equivalent of Claude Code's ``Read``)
   whose output is raw file content the model will byte-patch against.
2. Shell reads through ``bash`` (``cat``/``nl``/``sed -n`` …), which the
   chat/Anthropic path protects via ``HEADROOM_PROTECT_READS`` read-command
   detection in ``ContentRouter``.

The Responses compression-units path historically protected neither: only
``DEFAULT_EXCLUDE_TOOLS`` names were honored, and ``HEADROOM_PROTECT_READS``
was never consulted. Lossy (Kompress) compression of a fresh file read garbles
exactly the bytes the model needs for line-precise edits, forcing re-reads
(turn inflation) — the harm read protection exists to prevent.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace

from headroom.proxy.handlers.openai import OpenAIHandlerMixin
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


def _lossy_router() -> ContentRouter:
    """Router whose compress() always 'lossy-compresses' any candidate it sees."""

    router = ContentRouter()

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="kept words",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    return router


def _run(handler: OpenAIHandlerMixin, payload: dict):
    return handler._compress_openai_responses_live_text_units_with_router(
        payload,
        model="gpt-5",
        request_id="req_read_protection",
    )


_FILE_CONTENT = "\n".join(
    f"## Section {i}\nSome roadmap prose line {i} with enough words to matter"
    for i in range(90)
)

_NL_OUTPUT = "\n".join(
    f"{i}\tline {i} of the roadmap file with a handful of words in it"
    for i in range(1, 110)
)


def test_responses_view_tool_read_stays_verbatim():
    """Copilot's `view` tool returns raw file bytes: never lossy-compress them."""
    handler = _handler_with_router(_lossy_router())
    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "function_call",
                "call_id": "call_view",
                "name": "view",
                "arguments": '{"path": "/repo/ROADMAP.md"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_view",
                "output": _FILE_CONTENT,
            },
        ],
    }

    new_payload, _modified, _saved, _t, _u, _s, _a = _run(handler, payload)

    assert new_payload["input"][1]["output"] == _FILE_CONTENT


def test_responses_bash_read_command_stays_verbatim_when_protect_reads(monkeypatch):
    """HEADROOM_PROTECT_READS=1 must cover bash file reads on the Responses path too."""
    monkeypatch.setenv("HEADROOM_PROTECT_READS", "1")
    handler = _handler_with_router(_lossy_router())
    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "function_call",
                "call_id": "call_bash",
                "name": "bash",
                "arguments": (
                    '{"command": "nl -ba .overlay/ROADMAP.md | sed -n \'1,75p\'"}'
                ),
            },
            {
                "type": "function_call_output",
                "call_id": "call_bash",
                "output": _NL_OUTPUT,
            },
        ],
    }

    new_payload, _modified, _saved, _t, _u, _s, _a = _run(handler, payload)

    assert new_payload["input"][1]["output"] == _NL_OUTPUT


def test_responses_excluded_read_tool_stays_verbatim_control():
    """Control: Claude-style `Read` outputs are already protected today."""
    handler = _handler_with_router(_lossy_router())
    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "function_call",
                "call_id": "call_read",
                "name": "Read",
                "arguments": '{"file_path": "/repo/ROADMAP.md"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_read",
                "output": _FILE_CONTENT,
            },
        ],
    }

    new_payload, _modified, _saved, _t, _u, _s, _a = _run(handler, payload)

    assert new_payload["input"][1]["output"] == _FILE_CONTENT
