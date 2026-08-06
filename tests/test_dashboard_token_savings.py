"""Regression tests for dashboard token savings copy."""

from __future__ import annotations

import re

from headroom.dashboard import get_dashboard_html


def _getter_body(html: str, name: str) -> str:
    match = re.search(rf"get {name}\(\) \{{(?P<body>.*?)\n\s*\}},", html, re.S)
    assert match is not None
    return match.group("body")


def test_token_savings_headline_uses_total_wire_denominator() -> None:
    html = get_dashboard_html()

    headline_body = _getter_body(html, "headlineSavingsPercent")
    assert "stats.tokens?.savings_percent" in headline_body
    assert "stats.tokens?.proxy_savings_percent" in headline_body
    assert "stats.tokens?.active_savings_percent" not in headline_body
    assert "stats.tokens?.proxy_attempted_tokens" not in headline_body

    # The subtitle copy moved into help_text.json (overview.compression_ratio.body)
    # and is wired through the getter. Assert the getter points at the help path and
    # that the real copy renders, rather than expecting the stale inline literal.
    title_body = _getter_body(html, "headlineSavingsTitle")
    assert "helpText?.overview?.compression_ratio?.body" in title_body
    assert "Of total wire input tokens" in html
    assert "Of compressible tokens attempted" not in html
