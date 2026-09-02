"""Per-language circuit breaker for AST compression that keeps failing.

The compressor validates its own output and discards invalid results, so a
misbehaving grammar burns the full compression latency on every request and
throws the work away (field measurement: 270 discarded TypeScript compressions
in one install's retained logs at ~1.2s each). After enough failures in the
recent window the breaker pauses that language for a cooldown; other languages
and the original content are unaffected.
"""

from __future__ import annotations

import pytest

from headroom.transforms import code_compressor as cc


@pytest.fixture(autouse=True)
def reset_breaker_state():
    cc._syntax_breaker_outcomes.clear()
    cc._syntax_breaker_open_until.clear()
    yield
    cc._syntax_breaker_outcomes.clear()
    cc._syntax_breaker_open_until.clear()


def test_breaker_trips_after_min_failures_in_window():
    for _ in range(cc._SYNTAX_BREAKER_MIN_FAILURES - 1):
        cc._record_syntax_outcome("typescript", False)
    assert not cc._syntax_breaker_open("typescript")
    cc._record_syntax_outcome("typescript", False)
    assert cc._syntax_breaker_open("typescript")
    # Per-language isolation: python keeps compressing.
    assert not cc._syntax_breaker_open("python")


def test_successes_keep_the_breaker_closed():
    for _ in range(cc._SYNTAX_BREAKER_WINDOW * 2):
        cc._record_syntax_outcome("typescript", True)
    for _ in range(cc._SYNTAX_BREAKER_MIN_FAILURES - 1):
        cc._record_syntax_outcome("typescript", False)
    assert not cc._syntax_breaker_open("typescript")


def test_breaker_reopens_after_cooldown(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cc.time, "monotonic", lambda: now[0])
    for _ in range(cc._SYNTAX_BREAKER_MIN_FAILURES):
        cc._record_syntax_outcome("typescript", False)
    assert cc._syntax_breaker_open("typescript")
    now[0] += cc._SYNTAX_BREAKER_COOLDOWN_S + 1
    assert not cc._syntax_breaker_open("typescript")
    # The cleared window means one more failure does not instantly re-trip.
    cc._record_syntax_outcome("typescript", False)
    assert not cc._syntax_breaker_open("typescript")


def test_env_kill_switch(monkeypatch):
    monkeypatch.setenv("HEADROOM_CODE_SYNTAX_BREAKER", "0")
    for _ in range(cc._SYNTAX_BREAKER_WINDOW):
        cc._record_syntax_outcome("typescript", False)
    assert not cc._syntax_breaker_open("typescript")


def test_compress_short_circuits_while_open(monkeypatch):
    monkeypatch.setattr(cc, "_check_tree_sitter_available", lambda: True)

    def _boom(self, *a, **kw):  # pragma: no cover - must not run
        raise AssertionError("AST compression attempted while breaker open")

    monkeypatch.setattr(cc.CodeAwareCompressor, "_compress_with_ast", _boom)
    cc._syntax_breaker_open_until["python"] = cc.time.monotonic() + 60.0

    compressor = cc.CodeAwareCompressor(cc.CodeCompressorConfig(min_tokens_for_compression=1))
    code = "def f():\n    return 1\n" * 20
    result = compressor.compress(code, language="python")
    assert result.compressed == code
    assert result.compression_ratio == 1.0
    assert result.syntax_valid is True
