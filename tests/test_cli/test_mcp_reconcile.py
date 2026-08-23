from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from headroom.cli.main import main
from headroom.mcp_registry import ClaudeRegistrar, build_serena_spec

FIXTURE = Path(__file__).parents[1] / "fixtures" / "headroom-issue-3054.json"


def _setup(monkeypatch, tmp_path: Path):
    config = tmp_path / ".claude.json"
    config.write_text(
        json.dumps(
            {
                "oauthAccount": {"email": "user@example.com"},
                "mcpServers": {
                    "serena": {
                        "command": "uvx",
                        "args": json.loads(FIXTURE.read_text())["old_serena_args"],
                    },
                    "other": {"command": "other", "args": []},
                },
                "projects": {"/repo": {"trust": True}},
            }
        )
    )
    registrar = ClaudeRegistrar(claude_cli=None, home_dir=tmp_path)
    monkeypatch.setattr("headroom.mcp_registry.ClaudeRegistrar", lambda: registrar)
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr("headroom.mcp_registry.ledger.ledger_path", lambda: ledger)
    return config, ledger


def test_issue_fixture_reconcile_is_base_fail_head_pass(monkeypatch, tmp_path: Path):
    config, _ = _setup(monkeypatch, tmp_path)
    fixture = json.loads(FIXTURE.read_text())
    recommended = build_serena_spec("claude-code")
    assert list(recommended.args) == fixture["recommended_serena_args"]
    assert CliRunner().invoke(main, ["mcp", "reconcile"]).exit_code == 0
    adopted = CliRunner().invoke(main, ["mcp", "reconcile", "--adopt"])
    assert adopted.exit_code == 0, adopted.output
    assert json.loads(config.read_text())["mcpServers"]["serena"]["args"] == list(recommended.args)


def test_read_only_preserves_config_and_ledger_bytes_and_mtimes(monkeypatch, tmp_path: Path):
    config, ledger = _setup(monkeypatch, tmp_path)
    ledger.write_text("not json")
    before = (
        config.read_bytes(),
        ledger.read_bytes(),
        os.stat(config).st_mtime_ns,
        os.stat(ledger).st_mtime_ns,
    )
    result = CliRunner().invoke(main, ["mcp", "reconcile"])
    assert result.exit_code == 0, result.output
    after = (
        config.read_bytes(),
        ledger.read_bytes(),
        os.stat(config).st_mtime_ns,
        os.stat(ledger).st_mtime_ns,
    )
    assert after == before
    assert "--adopt" in result.output


def test_adopt_preserves_unrelated_config_and_records_ownership(monkeypatch, tmp_path: Path):
    config, ledger = _setup(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["mcp", "reconcile", "--adopt"])
    assert result.exit_code == 0, result.output
    data = json.loads(config.read_text())
    assert data["oauthAccount"] == {"email": "user@example.com"}
    assert data["projects"] == {"/repo": {"trust": True}}
    assert data["mcpServers"]["other"] == {"command": "other", "args": []}
    assert data["mcpServers"]["serena"]["args"] == list(build_serena_spec("claude-code").args)
    assert json.loads(ledger.read_text())["agents"]["claude"]["serena"]["fingerprint"]


@pytest.mark.parametrize(
    "contents", ["not json", "[]", '{"agents": []}', '{"agents": {"claude": []}}']
)
def test_malformed_ledger_blocks_adopt_before_config_write(
    monkeypatch, tmp_path: Path, contents: str
):
    config, ledger = _setup(monkeypatch, tmp_path)
    before = config.read_bytes()
    ledger.write_text(contents)
    result = CliRunner().invoke(main, ["mcp", "reconcile", "--adopt"])
    assert result.exit_code != 0
    assert "ledger" in result.output.lower()
    assert config.read_bytes() == before


def test_corrupt_ledger_is_tolerated_by_read_only(monkeypatch, tmp_path: Path):
    _, ledger = _setup(monkeypatch, tmp_path)
    ledger.write_text('{"agents": []}')
    result = CliRunner().invoke(main, ["mcp", "reconcile"])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("state", ["absent", "matching", "user-drift", "headroom-drift"])
@pytest.mark.parametrize("adopt", [False, True])
def test_reconcile_state_matrix(monkeypatch, tmp_path: Path, state: str, adopt: bool):
    config, ledger = _setup(monkeypatch, tmp_path)
    data = json.loads(config.read_text())
    recommended = build_serena_spec("claude-code")
    if state == "absent":
        del data["mcpServers"]["serena"]
    elif state == "matching":
        data["mcpServers"]["serena"] = {
            "command": recommended.command,
            "args": list(recommended.args),
        }
    elif state == "headroom-drift":
        from headroom.mcp_registry.ledger import record_install

        record_install("claude", recommended, path=ledger)
    config.write_text(json.dumps(data))
    result = CliRunner().invoke(main, ["mcp", "reconcile"] + (["--adopt"] if adopt else []))
    assert result.exit_code == 0, result.output


def test_only_adopt_is_a_reconcile_mutation(monkeypatch, tmp_path: Path):
    _setup(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["mcp", "reconcile", "--help"])
    assert result.exit_code == 0
    assert "--adopt" in result.output
    for option in ("--acknowledge", "--clear", "--agent", "--server"):
        assert option not in result.output


def test_ordinary_install_does_not_adopt_serena(monkeypatch, tmp_path: Path):
    config, _ = _setup(monkeypatch, tmp_path)
    before = config.read_bytes()
    monkeypatch.setitem(sys.modules, "mcp", object())
    monkeypatch.setattr(
        "headroom.mcp_registry.install_everywhere", lambda **kwargs: {"codex": object()}
    )
    monkeypatch.setattr("headroom.mcp_registry.format_results", lambda *args, **kwargs: [])
    monkeypatch.setattr("headroom.mcp_registry.any_succeeded", lambda results: True)
    result = CliRunner().invoke(main, ["mcp", "install", "--agent", "codex"])
    assert result.exit_code == 0, result.output
    assert config.read_bytes() == before
