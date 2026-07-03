"""Regression tests for the error/importance detection triage helpers."""

from __future__ import annotations

from headroom.transforms.error_detection import content_has_strong_error_indicators


def test_real_error_output_is_detected() -> None:
    text = "Traceback (most recent call last):\n  ...\nValueError: fatal error during load"
    assert content_has_strong_error_indicators(text)


def test_single_keyword_mention_is_not_flagged() -> None:
    # Only one distinct indicator keyword ("error") — should not trip the
    # two-keyword threshold.
    text = 'Wrote error_handler.py with an "errors": [] field.'
    assert not content_has_strong_error_indicators(text)


def test_tsc_passing_summary_is_not_flagged() -> None:
    # Regression for issue #1696: a clean `tsc` run mentions both "error"
    # and (via "0 failures" in a paired test run) "fail" while reporting
    # success. Previously this tripped the two-keyword heuristic and got
    # the message permanently protected from compression.
    text = "Found 0 errors. Watching for file changes.\nTests: 0 failures, 42 passed"
    assert not content_has_strong_error_indicators(text)


def test_eslint_passing_summary_is_not_flagged() -> None:
    text = "0 problems (0 errors, 0 warnings)\nno failing tests"
    assert not content_has_strong_error_indicators(text)


def test_zero_result_phrase_does_not_mask_a_real_second_error() -> None:
    # "0 errors" is stripped, but a genuine second distinct indicator
    # elsewhere in the same blob must still trigger protection.
    text = "0 errors from linter, but the build crashed with a fatal signal"
    assert content_has_strong_error_indicators(text)
