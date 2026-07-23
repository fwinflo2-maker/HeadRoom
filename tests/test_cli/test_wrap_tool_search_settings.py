"""Persist ENABLE_TOOL_SEARCH into settings.local.json for daemon workers (#2492).

wrap --tool-search false / ENABLE_TOOL_SEARCH=false previously only patched the
launched process env. Claude Code's daemon-spawned conversation workers read
settings.local.json fresh (#951), so an init-baked true kept winning and
Foundry still 400'd on tool_search_server.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from headroom.cli import init as init_cli
from headroom.cli import wrap as wrap_cli
from headroom.providers.claude import TOOL_SEARCH_DEFAULT, TOOL_SEARCH_FOUNDRY_DEFAULT


def _settings(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "settings.local.json"


def test_should_persist_when_flag_set() -> None:
    assert wrap_cli._should_persist_tool_search_settings(
        flag_value="false", resolved_value="false"
    )
    assert wrap_cli._should_persist_tool_search_settings(
        flag_value="true", resolved_value="true"
    )
    assert wrap_cli._should_persist_tool_search_settings(
        flag_value="auto", resolved_value="auto"
    )


def test_should_persist_when_resolved_disables_without_flag() -> None:
    assert wrap_cli._should_persist_tool_search_settings(
        flag_value=None, resolved_value="false"
    )
    assert wrap_cli._should_persist_tool_search_settings(flag_value=None, resolved_value="0")
    assert wrap_cli._should_persist_tool_search_settings(flag_value=None, resolved_value="off")


def test_should_not_persist_generic_true_default() -> None:
    assert not wrap_cli._should_persist_tool_search_settings(
        flag_value=None, resolved_value=TOOL_SEARCH_DEFAULT
    )
    assert not wrap_cli._should_persist_tool_search_settings(
        flag_value=None, resolved_value="auto"
    )
    assert not wrap_cli._should_persist_tool_search_settings(flag_value=None, resolved_value=None)
    assert not wrap_cli._should_persist_tool_search_settings(flag_value=None, resolved_value="  ")


def test_write_tool_search_creates_key(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    prev = wrap_cli._write_claude_wrap_tool_search("false", settings_path=path)
    assert prev is None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["env"]["ENABLE_TOOL_SEARCH"] == "false"


def test_write_tool_search_preserves_sibling_keys(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
                    "ENABLE_TOOL_SEARCH": "true",
                    "KEEP": "1",
                }
            }
        ),
        encoding="utf-8",
    )
    prev = wrap_cli._write_claude_wrap_tool_search("false", settings_path=path)
    assert prev == "true"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["env"]["ENABLE_TOOL_SEARCH"] == "false"
    assert payload["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8787"
    assert payload["env"]["KEEP"] == "1"


def test_write_restore_roundtrip_leaves_init_true(tmp_path: Path) -> None:
    """Happy path from the issue: init baked true; wrap false; restore puts true back."""
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
                    "ENABLE_TOOL_SEARCH": "true",
                }
            }
        ),
        encoding="utf-8",
    )
    prev = wrap_cli._write_claude_wrap_tool_search("false", settings_path=path)
    assert prev == "true"
    assert json.loads(path.read_text(encoding="utf-8"))["env"]["ENABLE_TOOL_SEARCH"] == "false"

    wrap_cli._restore_claude_wrap_tool_search(prev, settings_path=path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["env"]["ENABLE_TOOL_SEARCH"] == "true"
    assert payload["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8787"


def test_restore_removes_key_when_previous_none(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
                    "ENABLE_TOOL_SEARCH": "false",
                }
            }
        ),
        encoding="utf-8",
    )
    wrap_cli._restore_claude_wrap_tool_search(None, settings_path=path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "ENABLE_TOOL_SEARCH" not in payload["env"]
    assert payload["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8787"


def test_worker_visible_path_matches_init_then_wrap_disable(tmp_path: Path) -> None:
    """Simulate init (true) then wrap --tool-search false write path workers read."""
    path = _settings(tmp_path)
    init_cli._ensure_claude_hooks(path, profile="init-user", port=8787)
    env_after_init = json.loads(path.read_text(encoding="utf-8"))["env"]
    assert env_after_init["ENABLE_TOOL_SEARCH"] == "true"

    process_env = {"ENABLE_TOOL_SEARCH": "true"}
    written = wrap_cli._configure_tool_search_env(process_env, "false")
    assert written == "false"
    assert process_env["ENABLE_TOOL_SEARCH"] == "false"
    assert wrap_cli._should_persist_tool_search_settings(
        flag_value="false", resolved_value=process_env["ENABLE_TOOL_SEARCH"]
    )

    prev = wrap_cli._write_claude_wrap_tool_search(
        process_env["ENABLE_TOOL_SEARCH"], settings_path=path
    )
    assert prev == "true"
    # Workers re-reading settings.local.json after wrap see false.
    assert json.loads(path.read_text(encoding="utf-8"))["env"]["ENABLE_TOOL_SEARCH"] == "false"


def test_env_false_without_flag_still_persists(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"env": {"ENABLE_TOOL_SEARCH": "true"}}),
        encoding="utf-8",
    )
    process_env = {"ENABLE_TOOL_SEARCH": "false"}
    assert wrap_cli._configure_tool_search_env(process_env, None) is None
    assert wrap_cli._should_persist_tool_search_settings(
        flag_value=None, resolved_value=process_env["ENABLE_TOOL_SEARCH"]
    )
    wrap_cli._write_claude_wrap_tool_search("false", settings_path=path)
    assert json.loads(path.read_text(encoding="utf-8"))["env"]["ENABLE_TOOL_SEARCH"] == "false"


def test_read_settings_env_value(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    assert wrap_cli._read_claude_settings_env_value("ENABLE_TOOL_SEARCH", settings_path=path) is None
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"env": {"ENABLE_TOOL_SEARCH": "true"}}), encoding="utf-8")
    assert (
        wrap_cli._read_claude_settings_env_value("ENABLE_TOOL_SEARCH", settings_path=path) == "true"
    )


def test_init_foundry_defaults_tool_search_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_USE_FOUNDRY", "1")
    settings = tmp_path / "settings.json"
    init_cli._ensure_claude_hooks(settings, profile="init-user", port=8787)
    env = json.loads(settings.read_text(encoding="utf-8"))["env"]
    assert env["ENABLE_TOOL_SEARCH"] == TOOL_SEARCH_FOUNDRY_DEFAULT


def test_init_foundry_respects_existing_user_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_USE_FOUNDRY", "1")
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"env": {"ENABLE_TOOL_SEARCH": "auto"}}) + "\n", encoding="utf-8"
    )
    init_cli._ensure_claude_hooks(settings, profile="init-user", port=8787)
    env = json.loads(settings.read_text(encoding="utf-8"))["env"]
    assert env["ENABLE_TOOL_SEARCH"] == "auto"


def test_init_non_foundry_still_defaults_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_USE_FOUNDRY", raising=False)
    settings = tmp_path / "settings.json"
    init_cli._ensure_claude_hooks(settings, profile="init-user", port=8787)
    env = json.loads(settings.read_text(encoding="utf-8"))["env"]
    assert env["ENABLE_TOOL_SEARCH"] == TOOL_SEARCH_DEFAULT
