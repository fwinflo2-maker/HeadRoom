"""``HEADROOM_COMPRESSION_MAX_WORKERS`` wiring tests (AQ-0610).

``ProxyConfig.compression_max_workers`` has documented both a
``HEADROOM_COMPRESSION_MAX_WORKERS`` env var and a ``--compression-max-workers``
CLI flag since the field was added, but neither was ever implemented — the
string appeared exactly once in the tree, inside that comment. The pool size was
therefore pinned to the auto-size with no way to change it short of editing
source.

That is not cosmetic. The auto-size is ``min(32, cpu_count * 4)`` = 32 on this
class of machine, and it sits in front of Kompress's execution semaphore, which
defaults to 1 concurrent inference. Workers 2..32 block on an *untimed*
``BoundedSemaphore.acquire()`` while still holding their pool slot, so the pool
reports 32/32 in-flight while at most one inference is running. Being able to
size the pool to the semaphore is the documented mitigation.
"""

from __future__ import annotations

import pytest

from headroom.proxy import server as srv


class TestEnvIntOrNone:
    """The parser must never take proxy startup down over a bad knob."""

    def test_reads_a_positive_integer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEADROOM_COMPRESSION_MAX_WORKERS", "3")

        assert srv._env_int_or_none("HEADROOM_COMPRESSION_MAX_WORKERS") == 3

    def test_unset_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HEADROOM_COMPRESSION_MAX_WORKERS", raising=False)

        assert srv._env_int_or_none("HEADROOM_COMPRESSION_MAX_WORKERS") is None

    def test_whitespace_is_tolerated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEADROOM_COMPRESSION_MAX_WORKERS", "  8  ")

        assert srv._env_int_or_none("HEADROOM_COMPRESSION_MAX_WORKERS") == 8

    @pytest.mark.parametrize("junk", ["", "   ", "abc", "3.5", "1e3"])
    def test_malformed_values_fall_back_rather_than_raise(
        self, monkeypatch: pytest.MonkeyPatch, junk: str
    ) -> None:
        monkeypatch.setenv("HEADROOM_COMPRESSION_MAX_WORKERS", junk)

        assert srv._env_int_or_none("HEADROOM_COMPRESSION_MAX_WORKERS") is None

    @pytest.mark.parametrize("bad", ["0", "-1", "-32"])
    def test_non_positive_values_are_rejected(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        """A zero-worker pool would deadlock every compression, so refuse it."""
        monkeypatch.setenv("HEADROOM_COMPRESSION_MAX_WORKERS", bad)

        assert srv._env_int_or_none("HEADROOM_COMPRESSION_MAX_WORKERS") is None
