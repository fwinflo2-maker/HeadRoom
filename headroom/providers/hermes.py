"""Hermes Studio scoped coding-agent proxy support.

Hermes owns authentication and protocol adaptation for its scoped proxy routes.
This module owns the small Headroom integration point: safely compress the chat
portion of those requests before the generic proxy forwards them upstream.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("headroom.providers.hermes")

_CHAT_ROLES = frozenset({"user", "assistant"})
_CODEX_RESPONSES_SUFFIX = "/v1/responses"
_CLAUDE_MESSAGES_SUFFIX = "/v1/messages"


def is_scoped_coding_agent_path(path: str) -> bool:
    """Return whether *path* is a Hermes scoped coding-agent endpoint."""
    return (path.startswith("/api/codex-proxy/") and path.endswith(_CODEX_RESPONSES_SUFFIX)) or (
        path.startswith("/api/claude-code-proxy/") and path.endswith(_CLAUDE_MESSAGES_SUFFIX)
    )


def compress_scoped_passthrough_body(
    path: str,
    body: bytes,
    *,
    optimize: bool,
    bypass: bool,
) -> bytes:
    """Compress supported Hermes request bodies, otherwise return *body* unchanged.

    The adapter deliberately understands only Hermes's two scoped routes. It
    leaves system, tool, reasoning, and non-dictionary input items untouched;
    only user/assistant messages are handed to Headroom's compressor.
    """
    if not optimize or bypass or not is_scoped_coding_agent_path(path):
        return body

    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return body
        model = str(payload.get("model") or "").strip()
        if not model:
            return body

        if path.startswith("/api/claude-code-proxy/"):
            field_name = "messages"
            route_name = "claude-code"
        else:
            field_name = "input"
            route_name = "codex"

        raw_items = payload.get(field_name)
        if isinstance(raw_items, str) and route_name == "codex":
            compressed = _compress_messages(
                [{"role": "user", "content": raw_items}], model=model, route_name=route_name
            )
            if compressed is None:
                return body
            payload[field_name] = compressed
            return _encode_payload(payload)

        if not isinstance(raw_items, list):
            return body

        chat_indices = [
            index for index, item in enumerate(raw_items) if _is_compressible_chat_message(item)
        ]
        if not chat_indices:
            return body

        chat_messages = [raw_items[index] for index in chat_indices]
        compressed = _compress_messages(chat_messages, model=model, route_name=route_name)
        if compressed is None:
            return body
        payload[field_name] = _splice_compressed_messages(raw_items, chat_indices, compressed)
        return _encode_payload(payload)
    except Exception as exc:  # Compression must never block Hermes passthrough.
        logger.info("Hermes passthrough compression skipped: %s", exc)
        return body


def _is_compressible_chat_message(item: Any) -> bool:
    """Keep structured tool/reasoning content outside the compressor."""
    if not isinstance(item, dict) or item.get("role") not in _CHAT_ROLES:
        return False
    content = item.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return all(
            isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
            for part in content
        )
    return False


def _compress_messages(
    messages: list[dict[str, Any]], *, model: str, route_name: str
) -> list[dict[str, Any]] | None:
    from headroom import compress as headroom_compress

    before_bytes = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
    result = headroom_compress(messages=messages, model=model, optimize=True)
    compressed = list(result.messages)
    after_bytes = len(json.dumps(compressed, ensure_ascii=False).encode("utf-8"))
    logger.info(
        "Hermes %s passthrough compression: %d -> %d bytes (saved %d)",
        route_name,
        before_bytes,
        after_bytes,
        max(0, before_bytes - after_bytes),
    )
    return compressed


def _splice_compressed_messages(
    original_items: list[Any], chat_indices: list[int], compressed_items: list[dict[str, Any]]
) -> list[Any]:
    """Restore compressed chat messages to their original slots.

    A defensive fallback retains an original item if a compressor unexpectedly
    returns fewer messages than it received.
    """
    compressed_by_index = dict(zip(chat_indices, compressed_items, strict=False))
    return [compressed_by_index.get(index, item) for index, item in enumerate(original_items)]


def _encode_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
