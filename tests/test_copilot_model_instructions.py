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


# ---------------------------------------------------------------------------
# Malformed markers and line endings — the cases that let real data loss through
# ---------------------------------------------------------------------------


SECURITY = "## IMPORTANT SECURITY RULES\nNever commit secrets. Never disable the audit log.\n"


def test_user_typed_start_marker_does_not_destroy_their_content(tmp_path: Path) -> None:
    """The data-loss bug: `index(END)` matched the FIRST end marker in the file.

    A user documenting this feature has the start marker in their own prose.
    Launch 1 appended a block; launch 2 then spliced from *their* marker to *our*
    block's end marker and deleted everything between — silently, while printing
    "Refreshed available-models list". Reproduced before the fix:
    `"Never commit secrets" in text` was False.
    """
    p = tmp_path / ".github" / "copilot-instructions.md"
    p.parent.mkdir(parents=True)
    p.write_text(
        f"# Team rules\n\n<!-- headroom:available-models -->\n(documented on purpose)\n\n{SECURITY}",
        encoding="utf-8",
    )
    _inject_copilot_models_instructions(p, ["gpt-5.4"])
    _inject_copilot_models_instructions(p, ["gpt-5.5"])
    text = p.read_text(encoding="utf-8")
    assert "Never commit secrets" in text
    assert "IMPORTANT SECURITY RULES" in text
    assert "# Team rules" in text

    _remove_copilot_models_instructions(p)
    assert "Never commit secrets" in p.read_text(encoding="utf-8")


def test_reversed_markers_do_not_duplicate_user_content(tmp_path: Path) -> None:
    """END before START gave `end < start`, which duplicated user text."""
    p = tmp_path / ".github" / "copilot-instructions.md"
    p.parent.mkdir(parents=True)
    p.write_text(
        "<!-- /headroom:available-models -->\nUSER-KEEP\n<!-- headroom:available-models -->\n",
        encoding="utf-8",
    )
    for _ in range(3):
        _inject_copilot_models_instructions(p, ["gpt-5.4"])
    assert p.read_text(encoding="utf-8").count("USER-KEEP") == 1


def test_crlf_line_endings_are_preserved(tmp_path: Path) -> None:
    """Rewriting one marked region must not convert the whole file's endings.

    `_read_text` normalizes CRLF to LF, so a naive rewrite turned every line of a
    source-controlled file into LF and showed up as a whole-file diff on Windows.
    """
    p = tmp_path / ".github" / "copilot-instructions.md"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"# Rules\r\n\r\nUse tabs.\r\n")
    before = p.read_bytes().count(b"\r\n")
    _inject_copilot_models_instructions(p, ["gpt-5.4"])
    _inject_copilot_models_instructions(p, ["gpt-5.5"])  # refresh path does the rewrite
    after = p.read_bytes()
    assert b"Use tabs." in after
    assert after.count(b"\r\n") >= before, "original CRLF endings were converted to LF"


def test_launch_survives_github_existing_as_a_file(tmp_path: Path) -> None:
    """`.github` as a FILE raised FileExistsError and killed the wrap pre-launch."""
    (tmp_path / ".github").write_text("not a directory", encoding="utf-8")
    target = tmp_path / ".github" / "copilot-instructions.md"
    with pytest.raises(OSError):
        _inject_copilot_models_instructions(target, ["gpt-5.4"])
    # The launch path must swallow exactly this, so assert it is an OSError
    # subclass (what the call site catches) rather than something broader.


# ---------------------------------------------------------------------------
# Ownership must be established by CONTENT, not position
# ---------------------------------------------------------------------------

_START = "<!-- headroom:available-models -->"
_END = "<!-- /headroom:available-models -->"
_KEEP = "DO NOT DELETE MY RULE"
_USER_PAIR = f"{_START}\n{_KEEP}\n{_END}\n"


def _real_block() -> str:
    from headroom.cli.wrap import _copilot_models_instructions_block

    return _copilot_models_instructions_block(["old-model"])


def _refresh_then_unwrap(path: Path) -> tuple[str, str]:
    """Two refreshes then a removal — the full lifecycle a user actually hits."""
    _inject_copilot_models_instructions(path, ["gpt-5.4"])
    _inject_copilot_models_instructions(path, ["gpt-5.5"])
    after = path.read_text(encoding="utf-8")
    _remove_copilot_models_instructions(path)
    return after, path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("label", "content"),
    [
        # The reviewer's repro: a COMPLETE user pair above the generated block.
        # Position-based ownership (first END + nearest preceding START) selected
        # the *user's* pair and deleted it while reporting a successful refresh.
        ("complete user pair before", f"# Rules\n\n{_USER_PAIR}\n{{real}}"),
        ("complete user pair after", f"# Rules\n\n{{real}}\n{_USER_PAIR}"),
        ("user pairs on both sides", f"{_USER_PAIR}\n{{real}}\n{_USER_PAIR}"),
        # A stray START widens any forward-paired span; the generated block then
        # sits *inside* it, so even a content check on the span still passed.
        ("stray START only", f"# Rules\n\n{_START}\n{_KEEP}\n\n## section\n"),
        ("stray END only", f"# Rules\n\n{_KEEP}\n{_END}\n"),
        ("reversed markers", f"{_END}\n{_KEEP}\n{_START}\n"),
        ("user quotes our block verbatim", f"# Rules\n{_KEEP}\n\n{{real}}\n{{real}}"),
    ],
)
def test_user_content_survives_refresh_and_unwrap(tmp_path: Path, label: str, content: str) -> None:
    p = tmp_path / "copilot-instructions.md"
    p.write_text(content.replace("{real}", _real_block()), encoding="utf-8")
    after, final = _refresh_then_unwrap(p)
    assert _KEEP in after, f"{label}: user content destroyed by refresh"
    assert _KEEP in final, f"{label}: user content destroyed by unwrap"


def test_lone_user_pair_with_no_generated_block_is_never_touched(tmp_path: Path) -> None:
    """Nothing of ours is present, so neither refresh nor unwrap may edit the pair."""
    p = tmp_path / "copilot-instructions.md"
    p.write_text(f"# Rules\n\n{_USER_PAIR}", encoding="utf-8")
    assert _remove_copilot_models_instructions(p) is False
    assert _KEEP in p.read_text(encoding="utf-8")
    after, final = _refresh_then_unwrap(p)
    assert _KEEP in after
    assert _KEEP in final


def test_ambiguous_duplicate_generated_blocks_refuse_to_mutate(tmp_path: Path) -> None:
    """Two blocks that both look generated: there is no safe way to pick one."""
    from headroom.cli.wrap import _marked_block_is_ambiguous

    p = tmp_path / "copilot-instructions.md"
    body = f"# Rules\n{_KEEP}\n\n{_real_block()}\n{_real_block()}"
    p.write_text(body, encoding="utf-8")
    assert _marked_block_is_ambiguous(body.replace("\r\n", "\n")) is True
    assert _inject_copilot_models_instructions(p, ["gpt-5.4"]) is False
    assert p.read_text(encoding="utf-8") == body, "file was mutated while ambiguous"


def test_invalid_utf8_fails_closed(tmp_path: Path) -> None:
    """`errors="replace"` + whole-file rewrite would substitute U+FFFD everywhere."""
    p = tmp_path / "copilot-instructions.md"
    p.write_bytes(b"# Rules\n\xff\xfe raw bytes " + _KEEP.encode() + b"\n")
    before = p.read_bytes()
    assert _inject_copilot_models_instructions(p, ["gpt-5.4"]) is False
    assert p.read_bytes() == before
    assert _remove_copilot_models_instructions(p) is False
    assert p.read_bytes() == before


def test_normal_lifecycle_still_works(tmp_path: Path) -> None:
    """Guard against over-correcting into never updating anything."""
    p = tmp_path / "copilot-instructions.md"
    p.write_text("# Mine\nkeep me\n", encoding="utf-8")
    _inject_copilot_models_instructions(p, ["gpt-5.4"])
    assert "gpt-5.4" in p.read_text(encoding="utf-8")
    _inject_copilot_models_instructions(p, ["gpt-5.5"])
    text = p.read_text(encoding="utf-8")
    assert "gpt-5.5" in text and "gpt-5.4" not in text
    assert text.count(_START) == 1, "refresh duplicated the block"
    _remove_copilot_models_instructions(p)
    final = p.read_text(encoding="utf-8")
    assert "keep me" in final and _START not in final
