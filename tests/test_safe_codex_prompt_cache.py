from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from headroom.cli._utils.safe_codex import (
    normalize_prompt_cache_retention,
    resolve_prompt_cache_key,
    resolve_prompt_cache_options,
)
from headroom.proxy.handlers.openai import _apply_prompt_cache_options
from headroom.proxy.server import ProxyConfig, create_app


def test_prompt_cache_key_auto_is_opaque(tmp_path: Path) -> None:
    key = resolve_prompt_cache_key("auto", cwd=tmp_path)

    assert key is not None
    assert key.startswith("codex:")
    assert str(tmp_path) not in key
    assert "/" not in key
    assert "\\" not in key


def test_prompt_cache_key_disabled_values() -> None:
    for value in (None, "", "default", "none", "off", "false", "0"):
        assert resolve_prompt_cache_key(value) is None


def test_prompt_cache_key_accepts_safe_custom_value() -> None:
    assert resolve_prompt_cache_key("codex:project_01") == "codex:project_01"


def test_prompt_cache_key_rejects_path_like_value(tmp_path: Path) -> None:
    with pytest.raises(click.UsageError):
        resolve_prompt_cache_key(str(tmp_path))


def test_prompt_cache_key_rejects_secret_like_value() -> None:
    with pytest.raises(click.UsageError):
        resolve_prompt_cache_key("sk-test123")


def test_prompt_cache_retention_aliases() -> None:
    assert normalize_prompt_cache_retention(None) is None
    assert normalize_prompt_cache_retention("default") is None
    assert normalize_prompt_cache_retention("in_memory") == "in-memory"
    assert normalize_prompt_cache_retention("in-memory") == "in-memory"
    assert normalize_prompt_cache_retention("24h") == "24h"


def test_prompt_cache_retention_rejects_unknown_value() -> None:
    with pytest.raises(click.UsageError):
        normalize_prompt_cache_retention("1h")


def test_resolve_prompt_cache_options() -> None:
    key, retention = resolve_prompt_cache_options(
        prompt_cache_key="codex:test",
        prompt_cache_retention="in_memory",
    )

    assert key == "codex:test"
    assert retention == "in-memory"


def test_apply_prompt_cache_options_requires_safe_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEADROOM_PROFILE", raising=False)
    monkeypatch.setenv("HEADROOM_PROMPT_CACHE_KEY", "codex:test")
    monkeypatch.setenv("HEADROOM_PROMPT_CACHE_RETENTION", "in-memory")

    body: dict[str, Any] = {}

    assert _apply_prompt_cache_options(body) is False
    assert body == {}


def test_apply_prompt_cache_options_injects_safe_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEADROOM_PROFILE", "safe-codex")
    monkeypatch.setenv("HEADROOM_PROMPT_CACHE_KEY", "codex:test")
    monkeypatch.setenv("HEADROOM_PROMPT_CACHE_RETENTION", "in-memory")

    body: dict[str, Any] = {}

    assert _apply_prompt_cache_options(body) is True
    assert body["prompt_cache_key"] == "codex:test"
    assert body["prompt_cache_retention"] == "in-memory"


def test_apply_prompt_cache_options_does_not_override_client_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEADROOM_PROFILE", "safe-codex")
    monkeypatch.setenv("HEADROOM_PROMPT_CACHE_KEY", "codex:env")
    monkeypatch.setenv("HEADROOM_PROMPT_CACHE_RETENTION", "in-memory")

    body: dict[str, Any] = {
        "prompt_cache_key": "codex:client",
        "prompt_cache_retention": "24h",
    }

    assert _apply_prompt_cache_options(body) is False
    assert body["prompt_cache_key"] == "codex:client"
    assert body["prompt_cache_retention"] == "24h"


def _make_proxy_client() -> TestClient:
    config = ProxyConfig(
        optimize=False,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        ccr_inject_tool=False,
        ccr_handle_responses=False,
        ccr_context_tracking=False,
        image_optimize=False,
    )
    return TestClient(create_app(config))


def test_openai_chat_injects_prompt_cache_options_for_safe_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEADROOM_PROFILE", "safe-codex")
    monkeypatch.setenv("HEADROOM_PROMPT_CACHE_KEY", "codex:test123")
    monkeypatch.setenv("HEADROOM_PROMPT_CACHE_RETENTION", "in-memory")

    captured: dict[str, Any] = {}

    with _make_proxy_client() as client:
        proxy = client.app.state.proxy

        async def _fake_retry(method, url, headers, body, stream=False, **kwargs):  # noqa: ANN001
            captured["body"] = dict(body)
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl_1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 5,
                        "total_tokens": 105,
                        "prompt_tokens_details": {"cached_tokens": 50},
                    },
                },
            )

        proxy._retry_request = _fake_retry

        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer test-key"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )

    assert response.status_code == 200, response.text
    assert captured["body"]["prompt_cache_key"] == "codex:test123"
    assert captured["body"]["prompt_cache_retention"] == "in-memory"


def test_openai_chat_does_not_inject_prompt_cache_options_without_safe_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HEADROOM_PROFILE", raising=False)
    monkeypatch.setenv("HEADROOM_PROMPT_CACHE_KEY", "codex:test123")
    monkeypatch.setenv("HEADROOM_PROMPT_CACHE_RETENTION", "in-memory")

    captured: dict[str, Any] = {}

    with _make_proxy_client() as client:
        proxy = client.app.state.proxy

        async def _fake_retry(method, url, headers, body, stream=False, **kwargs):  # noqa: ANN001
            captured["body"] = dict(body)
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl_1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
                },
            )

        proxy._retry_request = _fake_retry

        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer test-key"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )

    assert response.status_code == 200, response.text
    assert "prompt_cache_key" not in captured["body"]
    assert "prompt_cache_retention" not in captured["body"]
