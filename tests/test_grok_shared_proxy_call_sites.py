"""Call-site regression tests for Grok CLI routing on a shared proxy.

``_resolve_openai_upstream`` returning ``api.x.ai`` is not enough: both
``handle_openai_chat`` and ``handle_openai_responses`` rebind a local
custom-header-only base mid-handler, and building the outbound URL from that
local instead of re-running the resolver is what sent Grok session tokens to
``api.openai.com``. These tests drive the handlers and assert on the URL that
actually goes on the wire, so reverting either call site fails the suite.
"""

from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest

from headroom.cache.prefix_tracker import SessionTrackerStore
from tests.test_openai_codex_routing import (
    _build_request,
    _DummyOpenAIHandler,
    _DummyTokenizer,
)


class _MinimalConfig(SimpleNamespace):
    """Unset options read as ``None`` so every optional chat feature stays off.

    These tests care about one thing — the outbound URL — so the handler should
    take the shortest path to it.
    """

    def __getattr__(self, name: str) -> None:
        return None


class _ChatHandler(_DummyOpenAIHandler):
    """``_DummyOpenAIHandler`` plus the attributes the chat path reads."""

    def __init__(self) -> None:
        super().__init__()
        self.config = _MinimalConfig(**vars(self.config))
        self.cache = None
        # Real store: the chat path drives the prefix tracker heavily, and a
        # hand-rolled mock would only prove the mock.
        self.session_tracker_store = SessionTrackerStore()


# Wire signals from Grok CLI 0.2.117; it cannot stamp ``x-headroom-base-url``.
_GROK_HEADERS = {
    "Authorization": "Bearer xai-redacted",
    "x-xai-token-auth": "xai-grok-cli",
    "User-Agent": "grok-pager/0.2.117 grok-shell/0.2.117 (macos; aarch64)",
}


@pytest.fixture(autouse=True)
def _stub_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("headroom.tokenizers.get_tokenizer", lambda model: _DummyTokenizer())


def test_chat_call_site_sends_grok_traffic_to_xai() -> None:
    request = _build_request(
        {"model": "grok-4", "messages": [{"role": "user", "content": "hello"}]},
        _GROK_HEADERS,
        path="/v1/chat/completions",
    )
    handler = _ChatHandler()
    assert handler.OPENAI_API_URL == "https://api.openai.com"  # shared Claude/Codex proxy

    anyio.run(handler.handle_openai_chat, request)

    assert handler.captured_request is not None
    _method, url, _headers, _body = handler.captured_request
    assert url == "https://api.x.ai/v1/chat/completions"


def test_responses_call_site_sends_grok_traffic_to_xai() -> None:
    request = _build_request({"model": "grok-4", "input": "hello"}, _GROK_HEADERS)
    handler = _DummyOpenAIHandler()

    anyio.run(handler.handle_openai_responses, request)

    assert handler.captured_request is not None
    _method, url, _headers, _body = handler.captured_request
    assert url == "https://api.x.ai/v1/responses"


def test_chat_call_site_keeps_non_grok_traffic_on_openai() -> None:
    request = _build_request(
        {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
        {"Authorization": "Bearer sk-test", "User-Agent": "codex-tui/0.146.0"},
        path="/v1/chat/completions",
    )
    handler = _ChatHandler()

    anyio.run(handler.handle_openai_chat, request)

    assert handler.captured_request is not None
    _method, url, _headers, _body = handler.captured_request
    assert url == "https://api.openai.com/v1/chat/completions"


def test_configured_gateway_is_not_bypassed_by_grok_signals() -> None:
    """An operator's ``--openai-api-url`` outranks Grok wire signals.

    Otherwise any Grok-branded client silently skips the configured gateway and
    carries its ``OPENAI_TARGET_API_HEADERS`` to xAI.
    """
    request = _build_request({"model": "grok-4", "input": "hello"}, _GROK_HEADERS)
    handler = _DummyOpenAIHandler()
    handler.OPENAI_API_URL = "https://gateway.internal"

    anyio.run(handler.handle_openai_responses, request)

    assert handler.captured_request is not None
    _method, url, _headers, _body = handler.captured_request
    assert url == "https://gateway.internal/v1/responses"
