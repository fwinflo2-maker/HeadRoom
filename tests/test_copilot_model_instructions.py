"""The available-models block must inform the agent without harming user content.

Background: `wrap copilot` uses the CLI's BYOK transport, which is single-model.
Native Copilot knows the full model set and picks correctly; under BYOK the agent
has nothing authoritative to consult, so asked to "use a different model" it
invents names or scrapes them from repo files. Observed live from a wrapped
session: it reported `claude-fable-5`, `claude-opus-4.8-fast` and `grok-4.5` as
available -- none are Copilot models -- mixed with real ones.

These tests pin the two properties that make the fix safe to ship: the list is
accurate/refreshable, and the user's own instructions are never damaged.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from headroom.cli.wrap import (
    _copilot_models_instructions_block,
    _inject_copilot_models_instructions,
    _live_copilot_model_ids,
    _remove_copilot_models_instructions,
)

USER_CONTENT = "# My project rules\n\nAlways use tabs. Never edit generated files.\n"


@pytest.fixture
def instructions(tmp_path: Path) -> Path:
    path = tmp_path / ".github" / "copilot-instructions.md"
    path.parent.mkdir(parents=True)
    path.write_text(USER_CONTENT, encoding="utf-8")
    return path


def test_block_lists_ids_and_points_at_the_live_command() -> None:
    block = _copilot_models_instructions_block(["gpt-5.4", "claude-opus-5"])
    assert "`gpt-5.4`" in block
    assert "`claude-opus-5`" in block
    # The pointer matters as much as the list: the block is written at launch,
    # so this is what keeps the answer current mid-session.
    assert "headroom models" in block
    # It must actively discourage the two observed failure modes.
    assert "do not scrape" in block.lower()
    assert "invent" in block.lower()


def test_injection_preserves_user_content(instructions: Path) -> None:
    _inject_copilot_models_instructions(instructions, ["gpt-5.4"])
    text = instructions.read_text(encoding="utf-8")
    assert USER_CONTENT.strip() in text
    assert "gpt-5.4" in text


def test_relaunch_refreshes_the_list_instead_of_duplicating(instructions: Path) -> None:
    """A stale list is the bug being fixed, so the block must update in place."""
    _inject_copilot_models_instructions(instructions, ["gpt-5.4", "gemini-2.5-pro"])
    _inject_copilot_models_instructions(instructions, ["gpt-5.5", "claude-opus-5"])
    text = instructions.read_text(encoding="utf-8")
    assert text.count("<!-- headroom:available-models -->") == 1
    assert "gemini-2.5-pro" not in text  # retired entry gone on refresh
    assert "gpt-5.4" not in text
    assert "gpt-5.5" in text
    assert USER_CONTENT.strip() in text


def test_removal_leaves_only_user_content(instructions: Path) -> None:
    _inject_copilot_models_instructions(instructions, ["gpt-5.4"])
    assert _remove_copilot_models_instructions(instructions) is True
    text = instructions.read_text(encoding="utf-8")
    assert "headroom:available-models" not in text
    assert "gpt-5.4" not in text
    assert USER_CONTENT.strip() in text


def test_removal_is_a_noop_when_absent(instructions: Path) -> None:
    assert _remove_copilot_models_instructions(instructions) is False
    assert instructions.read_text(encoding="utf-8") == USER_CONTENT


def test_creates_the_file_when_missing(tmp_path: Path) -> None:
    path = tmp_path / ".github" / "copilot-instructions.md"
    _inject_copilot_models_instructions(path, ["gpt-5.4"])
    assert path.exists()
    assert "gpt-5.4" in path.read_text(encoding="utf-8")


def test_empty_model_list_writes_nothing(instructions: Path) -> None:
    """Discovery failed => say nothing rather than assert an empty catalogue."""
    assert _inject_copilot_models_instructions(instructions, []) is False
    assert instructions.read_text(encoding="utf-8") == USER_CONTENT


def test_live_ids_only_include_selectable_chat_models(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    fixture = (
        Path(__file__).parent / "fixtures" / "copilot_models" / "models_list.json"
    ).read_text(encoding="utf-8")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=json.loads(fixture)))
    ids = _live_copilot_model_ids("https://api.githubcopilot.com", "tok")
    assert "claude-opus-4.8" in ids
    assert "mai-code-1-flash-picker" in ids
    # Not selectable / not chat models must never be advertised to the agent.
    assert "text-embedding-3-small" not in ids
    assert "trajectory-compaction" not in ids
    assert "gpt-4o" not in ids


def test_live_ids_fail_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **k: object) -> None:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", _boom)
    assert _live_copilot_model_ids("https://api.githubcopilot.com", "tok") == []
    assert _live_copilot_model_ids(None, None) == []
