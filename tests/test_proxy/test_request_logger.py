"""Tests for the in-memory request logger.

Covers the `log_full_messages` gate, which controls whether the
pre-compression (`request_messages`) and post-compression
(`compressed_messages`) payloads persist past the in-memory entry onto disk.
Both sides are governed by the same flag so the two sides of the compression
stay in sync - it's pointless to store one without the other.
"""

from __future__ import annotations

from headroom.proxy.models import RequestLog
from headroom.proxy.request_logger import RequestLogger


def _entry(**overrides) -> RequestLog:
    base: dict = {
        "request_id": "r1",
        "timestamp": "2026-04-24T10:00:00Z",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "input_tokens_original": 100,
        "input_tokens_optimized": 40,
        "output_tokens": 10,
        "tokens_saved": 60,
        "savings_percent": 60.0,
        "optimization_latency_ms": 1.0,
        "total_latency_ms": 20.0,
        "tags": {},
        "cache_hit": False,
        "transforms_applied": ["kompress:user:0.4"],
    }
    base.update(overrides)
    return RequestLog(**base)


def test_get_recent_strips_compressed_messages_alongside_request_and_response():
    logger = RequestLogger(log_file=None, log_full_messages=True)
    logger.log(
        _entry(
            request_messages=[{"role": "user", "content": "pre"}],
            compressed_messages=[{"role": "user", "content": "post"}],
            response_content="ok",
        )
    )

    recent = logger.get_recent(10)
    assert len(recent) == 1
    assert "request_messages" not in recent[0]
    assert "compressed_messages" not in recent[0]
    assert "response_content" not in recent[0]


def test_get_recent_with_messages_returns_compressed_messages():
    logger = RequestLogger(log_file=None, log_full_messages=True)
    logger.log(
        _entry(
            request_messages=[{"role": "user", "content": "pre"}],
            compressed_messages=[{"role": "user", "content": "post"}],
        )
    )

    recent = logger.get_recent_with_messages(10)
    assert len(recent) == 1
    assert recent[0]["request_messages"] == [{"role": "user", "content": "pre"}]
    assert recent[0]["compressed_messages"] == [{"role": "user", "content": "post"}]


def test_jsonl_file_strips_both_sides_when_log_full_messages_disabled(tmp_path):
    log_file = tmp_path / "requests.jsonl"
    logger = RequestLogger(log_file=str(log_file), log_full_messages=False)
    logger.log(
        _entry(
            request_messages=[{"role": "user", "content": "pre"}],
            compressed_messages=[{"role": "user", "content": "post"}],
            response_content="ok",
        )
    )

    import json

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert "request_messages" not in obj
    assert "compressed_messages" not in obj
    assert "response_content" not in obj


def test_get_memory_stats_accounts_for_compressed_messages():
    logger = RequestLogger(log_file=None)
    logger.log(
        _entry(
            compressed_messages=[{"role": "user", "content": "post"}],
        )
    )

    stats = logger.get_memory_stats()
    assert stats.entry_count == 1
    assert stats.size_bytes > 0


def test_safe_mode_never_keeps_messages_or_response_content(tmp_path):
    log_file = tmp_path / "requests.jsonl"
    logger = RequestLogger(log_file=str(log_file), log_full_messages=True, safe_mode=True)
    logger.log(
        _entry(
            request_messages=[{"role": "user", "content": "sk-testsecret123456"}],
            compressed_messages=[{"role": "user", "content": "post"}],
            response_content="secret response",
            error="upstream failed with sk-testsecret123456",
            tags={"authorization": "Authorization: Bearer token123456789"},
        )
    )

    recent_with_messages = logger.get_recent_with_messages(10)
    assert len(recent_with_messages) == 1
    assert "request_messages" not in recent_with_messages[0]
    assert "compressed_messages" not in recent_with_messages[0]
    assert "response_content" not in recent_with_messages[0]
    assert recent_with_messages[0]["error"] == "[REDACTED_ERROR]"
    assert recent_with_messages[0]["tags"]["authorization"] == "Authorization: Bearer [REDACTED]"

    recent = logger.get_recent(10)
    assert len(recent) == 1
    assert "request_messages" not in recent[0]
    assert "compressed_messages" not in recent[0]
    assert "response_content" not in recent[0]

    import json

    obj = json.loads(log_file.read_text().strip())
    assert "request_messages" not in obj
    assert "compressed_messages" not in obj
    assert "response_content" not in obj
    assert "sk-testsecret123456" not in log_file.read_text()


def test_safe_mode_forces_log_full_messages_off():
    logger = RequestLogger(log_file=None, log_full_messages=True, safe_mode=True)

    assert logger.safe_mode is True
    assert logger.log_full_messages is False


def test_normal_mode_preserves_log_full_messages_true():
    logger = RequestLogger(log_file=None, log_full_messages=True, safe_mode=False)

    assert logger.safe_mode is False
    assert logger.log_full_messages is True
