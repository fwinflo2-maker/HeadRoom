from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from headroom.cli.main import main
from headroom.mcp_registry import ClaudeRegistrar, build_serena_spec_for_agent


def _setup(monkeypatch, tmp_path: Path):
    config = tmp_path / ".claude.json"
    config.write_text(
        json.dumps(
            {
                "oauthAccount": {"email": "user@example.com"},
                "mcpServers": {
                    "serena": {"command": "uvx", "args": ["--from", "old-serena"]},
                    "other": {"command": "other", "args": []},
                },
                "projects": {"/repo": {"trust": True}},
            }
        )
    )
    registrar = ClaudeRegistrar(claude_cli=None, home_dir=tmp_path)
    monkeypatch.setattr("headroom.mcp_registry.ClaudeRegistrar", lambda: registrar)
    monkeypatch.setattr(
        "headroom.mcp_registry.ledger.ledger_path", lambda: tmp_path / "ledger.json"
    )
    return config, registrar


def test_read_only_does_not_write_claude_config(monkeypatch, tmp_path: Path):
    config, _ = _setup(monkeypatch, tmp_path)
    before = config.read_bytes()
    result = CliRunner().invoke(main, ["mcp", "reconcile"])
    assert result.exit_code == 0, result.output
    assert config.read_bytes() == before
    assert "--adopt" in result.output


def test_adopt_preserves_unrelated_config_and_records_ownership(monkeypatch, tmp_path: Path):
    config, _ = _setup(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["mcp", "reconcile", "--adopt"])
    assert result.exit_code == 0, result.output
    data = json.loads(config.read_text())
    assert data["oauthAccount"] == {"email": "user@example.com"}
    assert data["projects"] == {"/repo": {"trust": True}}
    assert data["mcpServers"]["other"] == {"command": "other", "args": []}
    assert data["mcpServers"]["serena"]["args"] == list(build_serena_spec_for_agent("claude").args)
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["agents"]["claude"]["serena"]["fingerprint"]


def test_validation_rejects_multiple_actions(monkeypatch, tmp_path: Path):
    _setup(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["mcp", "reconcile", "--acknowledge", "--clear"])
    assert result.exit_code != 0
    assert "at most one" in result.output


def test_ordinary_install_has_no_reconcile_flags():
    result = CliRunner().invoke(main, ["mcp", "install", "--help"])
    assert result.exit_code == 0
    assert "reconcile" not in result.output
