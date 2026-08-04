"""Template regression tests for Recent Requests token columns."""

from __future__ import annotations

import re

from headroom.dashboard import get_dashboard_html


def _method_body(html: str, name: str) -> str:
    match = re.search(rf"{name}\([^)]*\) \{{(?P<body>.*?)\n\s*\}},", html, re.S)
    assert match is not None, f"method {name} not found in dashboard template"
    return match.group("body")


def test_recent_requests_shows_five_token_columns() -> None:
    html = get_dashboard_html()

    assert "Question" in html
    assert "Input Token" in html
    assert "Headroom Compress" in html
    assert "Output Token" in html
    assert ">Saving<" in html or ">Saving</div>" in html
    assert "requestQuestionPreview(req)" in html
    assert "input_tokens_original" in html
    assert "input_tokens_optimized" in html
    assert "tokens_saved" in html
    assert 'data-testid="token-summary-line"' in html
    assert "tokenSummaryLine" in html


def test_recent_requests_shows_origin_and_compressed_labels() -> None:
    html = get_dashboard_html()

    assert "Origin Text" in html
    assert "Compressed Text" in html
    assert "sent to LLM" in html
    assert "requestOriginPreview(req)" in html
    assert "requestCompressedPreview(req)" in html
    assert "hasRequestTextContent(req)" in html


def test_recent_request_preview_helpers_use_message_snapshots() -> None:
    html = get_dashboard_html()

    origin_body = _method_body(html, "requestOriginPreview")
    compressed_body = _method_body(html, "requestCompressedPreview")
    question_body = _method_body(html, "requestQuestionPreview")

    assert "request_messages" in origin_body
    assert "compressed_messages" in compressed_body
    assert "flattenMessages" in origin_body
    assert "flattenMessages" in compressed_body
    assert "request_messages" in question_body
    assert "user" in question_body
