from __future__ import annotations

import json
import sys
from pathlib import Path

from click.testing import CliRunner

from headroom.cli.main import main
from headroom.mcp_registry import ClaudeRegistrar, build_serena_spec_for_agent
from headroom.mcp_registry.base import RegisterResult, RegisterStatus


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


def test_validation_rejects_unknown_server(monkeypatch, tmp_path: Path):
    _setup(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["mcp", "reconcile", "--server", "other"])
    assert result.exit_code != 0
    assert "only --server serena" in result.output


def test_clear_acknowledgement_and_reject_absent_entry(monkeypatch, tmp_path: Path):
    config, _ = _setup(monkeypatch, tmp_path)
    acknowledged = CliRunner().invoke(main, ["mcp", "reconcile", "--acknowledge"])
    assert acknowledged.exit_code == 0, acknowledged.output
    cleared = CliRunner().invoke(main, ["mcp", "reconcile", "--clear"])
    assert cleared.exit_code == 0, cleared.output

    data = json.loads(config.read_text())
    del data["mcpServers"]["serena"]
    config.write_text(json.dumps(data))
    absent = CliRunner().invoke(main, ["mcp", "reconcile", "--acknowledge"])
    assert absent.exit_code != 0
    assert "absent Serena entry" in absent.output


def test_malformed_ledger_is_treated_as_unacknowledged(monkeypatch, tmp_path: Path):
    _setup(monkeypatch, tmp_path)
    (tmp_path / "ledger.json").write_text("not json")
    result = CliRunner().invoke(main, ["mcp", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "none/stale" in result.output


def test_ordinary_install_forwards_force_without_reconcile(monkeypatch, tmp_path: Path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "mcp", object())
    calls: list[dict] = []

    def fake_install_everywhere(**kwargs):
        calls.append(kwargs)
        return [RegisterResult(RegisterStatus.REGISTERED, "installed")]

    monkeypatch.setattr("headroom.mcp_registry.install_everywhere", fake_install_everywhere)
    monkeypatch.setattr(
        "headroom.mcp_registry.format_results", lambda results, **kwargs: ["installed"]
    )
    monkeypatch.setattr("headroom.mcp_registry.any_succeeded", lambda results: True)
    result = CliRunner().invoke(main, ["mcp", "install", "--agent", "claude", "--force"])
    assert result.exit_code == 0, result.output
    assert calls == [{"proxy_url": "http://127.0.0.1:8787", "agents": ["claude"], "force": True}]


def test_ordinary_install_has_no_reconcile_flags():
    result = CliRunner().invoke(main, ["mcp", "install", "--help"])
    assert result.exit_code == 0
    assert "reconcile" not in result.output
