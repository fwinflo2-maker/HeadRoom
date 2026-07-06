"""Tests for `headroom wrap zcode` and `headroom unwrap zcode` commands.

ZCode is a desktop Electron app (zcode.z.ai) with no CLI binary. The wrap
command follows the Pattern-B (proxy-only watcher) approach: it starts the
proxy, injects RTK guidance into AGENTS.md at the project root, and prints
the ZCode settings the user should configure in the app's settings UI.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from headroom.cli import wrap as wrap_mod
from headroom.cli.main import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Wrap: --prepare-only RTK injection into AGENTS.md
# ---------------------------------------------------------------------------


def test_prepare_only_injects_rtk_into_agents_md(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wrap zcode --prepare-only`` writes the RTK block to AGENTS.md at cwd."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)

    with patch.object(wrap_mod, "_ensure_rtk_binary", return_value=Path("/tmp/rtk")):
        result = runner.invoke(main, ["wrap", "zcode", "--prepare-only"])

    assert result.exit_code == 0, result.output
    marker = tmp_path / "AGENTS.md"
    assert marker.exists(), "AGENTS.md should be created"
    content = marker.read_text(encoding="utf-8")
    assert wrap_mod._RTK_MARKER in content
    assert "RTK (Rust Token Killer)" in content


def test_prepare_only_idempotent_no_duplicate_block(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running prepare-only twice must not duplicate the RTK block in AGENTS.md."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)

    with patch.object(wrap_mod, "_ensure_rtk_binary", return_value=Path("/tmp/rtk")):
        runner.invoke(main, ["wrap", "zcode", "--prepare-only"])
        runner.invoke(main, ["wrap", "zcode", "--prepare-only"])

    content = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert content.count(wrap_mod._RTK_MARKER) == 1


def test_no_context_tool_does_not_create_agents_md(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--no-context-tool must not create AGENTS.md and must not invoke rtk."""
    monkeypatch.chdir(tmp_path)

    with patch.object(wrap_mod, "_ensure_rtk_binary") as ensure:
        result = runner.invoke(main, ["wrap", "zcode", "--prepare-only", "--no-context-tool"])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "AGENTS.md").exists()
    ensure.assert_not_called()


def test_preserves_existing_agents_md_content(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing AGENTS.md content must be preserved when RTK is appended."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)
    agents_md = tmp_path / "AGENTS.md"
    original = "# Project conventions\n\nAlways use Python 3.12.\n"
    agents_md.write_text(original, encoding="utf-8")

    with patch.object(wrap_mod, "_ensure_rtk_binary", return_value=Path("/tmp/rtk")):
        result = runner.invoke(main, ["wrap", "zcode", "--prepare-only"])

    assert result.exit_code == 0, result.output
    content = agents_md.read_text(encoding="utf-8")
    assert "Always use Python 3.12." in content
    assert wrap_mod._RTK_MARKER in content


# ---------------------------------------------------------------------------
# Wrap: setup instructions output
# ---------------------------------------------------------------------------


def test_wrap_prints_proxy_urls(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrap command must print the proxy URLs for ZCode configuration."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)

    fake_rtk = Path("/tmp/rtk")

    def fake_watcher(**kwargs):  # noqa: ANN003
        print_fn = kwargs.get("print_setup_lines")
        if callable(print_fn):
            print_fn()

    with patch.object(wrap_mod, "_ensure_rtk_binary", return_value=fake_rtk):
        with patch.object(wrap_mod, "_run_proxy_only_watcher", side_effect=fake_watcher):
            result = runner.invoke(main, ["wrap", "zcode", "--port", "9000"])

    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:9000/v1" in result.output
    assert "http://127.0.0.1:9000" in result.output
    assert "Settings > Model Settings" in result.output


# ---------------------------------------------------------------------------
# Unwrap: RTK removal from AGENTS.md
# ---------------------------------------------------------------------------


def test_unwrap_removes_rtk_from_agents_md(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``unwrap zcode`` removes RTK instructions from AGENTS.md."""
    monkeypatch.chdir(tmp_path)
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text(
        "# Project\n\nSome content.\n\n" + wrap_mod.RTK_INSTRUCTIONS_BLOCK + "\n",
        encoding="utf-8",
    )

    with patch.object(wrap_mod, "_stop_local_proxy_for_unwrap", return_value="stopped"):
        result = runner.invoke(main, ["unwrap", "zcode"])

    assert result.exit_code == 0, result.output
    assert "Removed Headroom rtk instructions" in result.output
    content = agents_md.read_text(encoding="utf-8")
    assert wrap_mod._RTK_MARKER not in content
    assert "Some content." in content


def test_unwrap_deletes_empty_agents_md(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``unwrap zcode`` deletes AGENTS.md if it only contained RTK instructions."""
    monkeypatch.chdir(tmp_path)
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text(wrap_mod.RTK_INSTRUCTIONS_BLOCK + "\n", encoding="utf-8")

    with patch.object(wrap_mod, "_stop_local_proxy_for_unwrap", return_value="stopped"):
        result = runner.invoke(main, ["unwrap", "zcode"])

    assert result.exit_code == 0, result.output
    assert not agents_md.exists(), "AGENTS.md should be deleted when only RTK content"


def test_unwrap_noop_when_no_markers(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``unwrap zcode`` is a safe no-op when AGENTS.md has no Headroom markers."""
    monkeypatch.chdir(tmp_path)
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("# Project\n\nSome content.\n", encoding="utf-8")

    with patch.object(wrap_mod, "_stop_local_proxy_for_unwrap", return_value="stopped"):
        result = runner.invoke(main, ["unwrap", "zcode"])

    assert result.exit_code == 0, result.output
    assert "Nothing to undo" in result.output
    content = agents_md.read_text(encoding="utf-8")
    assert content == "# Project\n\nSome content.\n"


def test_unwrap_noop_when_no_agents_md(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``unwrap zcode`` is a safe no-op when AGENTS.md does not exist."""
    monkeypatch.chdir(tmp_path)

    with patch.object(wrap_mod, "_stop_local_proxy_for_unwrap", return_value="stopped"):
        result = runner.invoke(main, ["unwrap", "zcode"])

    assert result.exit_code == 0, result.output
    assert "Nothing to undo" in result.output


# ---------------------------------------------------------------------------
# Runtime: proxy targets
# ---------------------------------------------------------------------------


def test_build_proxy_targets() -> None:
    """build_proxy_targets returns correct OpenAI and Anthropic URLs."""
    from headroom.providers.zcode.runtime import build_proxy_targets

    targets = build_proxy_targets(8787)
    assert targets.openai_base_url == "http://127.0.0.1:8787/v1"
    assert targets.anthropic_base_url == "http://127.0.0.1:8787"


def test_build_proxy_targets_custom_port() -> None:
    """build_proxy_targets respects custom port."""
    from headroom.providers.zcode.runtime import build_proxy_targets

    targets = build_proxy_targets(9999)
    assert targets.openai_base_url == "http://127.0.0.1:9999/v1"
    assert targets.anthropic_base_url == "http://127.0.0.1:9999"


def test_render_setup_lines_includes_mcp_instruction() -> None:
    """render_setup_lines includes the MCP paste JSON for user convenience."""
    from headroom.providers.zcode.runtime import render_setup_lines

    lines = render_setup_lines(8787)
    joined = "\n".join(lines)
    assert "headroom" in joined.lower()
    assert "MCP" in joined
    assert '"stdio"' in joined
