"""Regression tests for opt-in stdout logging (#3087).

``_setup_file_logging`` disables propagation on the ``headroom`` logger, so in
container deployments (which collect stdout, not the in-container log file)
application logs are silently lost. ``HEADROOM_LOG_TO_STDOUT`` restores them via
a dedicated stdout handler without re-enabling propagation.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator

import pytest

from headroom.proxy.helpers import _setup_file_logging


@pytest.fixture(autouse=True)
def _restore_headroom_logger() -> Iterator[None]:
    """Snapshot and restore the shared ``headroom`` logger around each test."""
    logger = logging.getLogger("headroom")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    try:
        yield
    finally:
        logger.handlers = saved_handlers
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


def _stdout_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if getattr(h, "stream", None) is sys.stdout]


def test_no_stdout_handler_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEADROOM_LOG_TO_STDOUT", raising=False)
    logger = logging.getLogger("headroom")
    logger.handlers = []

    _setup_file_logging()

    assert _stdout_handlers(logger) == []
    # Propagation stays disabled — the historical de-dup behavior is unchanged.
    assert logger.propagate is False


def test_opt_in_adds_stdout_handler_and_emits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HEADROOM_LOG_TO_STDOUT", "1")
    logger = logging.getLogger("headroom")
    logger.handlers = []

    _setup_file_logging()

    assert len(_stdout_handlers(logger)) == 1
    # Propagation is still off — the stdout handler, not the root logger, is
    # what carries records to stdout, so the rotating file is never doubled.
    assert logger.propagate is False

    logging.getLogger("headroom.proxy").info("container-visible-line")
    assert "container-visible-line" in capsys.readouterr().out


def test_opt_in_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEADROOM_LOG_TO_STDOUT", "yes")
    logger = logging.getLogger("headroom")
    logger.handlers = []

    _setup_file_logging()
    _setup_file_logging()

    assert len(_stdout_handlers(logger)) == 1
