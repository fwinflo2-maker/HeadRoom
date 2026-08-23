from __future__ import annotations

from pathlib import Path

from headroom.cli import wrap as wrap_cli
from headroom.mcp_registry import build_serena_spec_for_agent
from headroom.mcp_registry.base import RegisterResult, RegisterStatus, ServerSpec
from headroom.mcp_registry.ledger import record_acknowledgement


class _Registrar:
    name = "claude"
    display_name = "Claude Code"

    def __init__(self, current: ServerSpec):
        self.current = current

    def detect(self) -> bool:
        return True

    def get_server(self, name: str) -> ServerSpec | None:
        return self.current if name == "serena" else None

    def register_server(self, spec: ServerSpec, *, force: bool = False) -> RegisterResult:
        if self.current == spec:
            return RegisterResult(RegisterStatus.ALREADY, "matches")
        if not force:
            return RegisterResult(RegisterStatus.MISMATCH, "different")
        self.current = spec
        return RegisterResult(RegisterStatus.REGISTERED, "updated")


def _quiet_serena_side_effects(monkeypatch):
    monkeypatch.setattr(wrap_cli, "_ensure_serena_dashboard_disabled", lambda **kwargs: None)
    monkeypatch.setattr(wrap_cli, "_inject_serena_instructions", lambda *args, **kwargs: None)
    monkeypatch.setattr(wrap_cli, "_serena_project_skip_reason", lambda root: "test")
    monkeypatch.setattr(wrap_cli, "_index_serena_project", lambda **kwargs: None)
    monkeypatch.setattr(wrap_cli.shutil, "which", lambda name: "uvx" if name == "uvx" else None)


def test_reproduction_acknowledgement_suppresses_current_drift_without_mutation(
    tmp_path: Path, monkeypatch, capsys
):
    _quiet_serena_side_effects(monkeypatch)
    ledger = tmp_path / "mcp_installs.json"
    monkeypatch.setattr("headroom.mcp_registry.ledger.ledger_path", lambda: ledger)
    recommended = build_serena_spec_for_agent("claude")
    observed = ServerSpec(name="serena", command="uvx", args=("--from", "old-serena"))
    registrar = _Registrar(observed)

    wrap_cli._setup_serena_mcp(registrar, verbose=True)
    assert "existing config differs" in capsys.readouterr().out

    record_acknowledgement("claude", "serena", recommended, observed)
    wrap_cli._setup_serena_mcp(registrar, verbose=True)
    output = capsys.readouterr().out
    assert "existing config differs" not in output
    assert registrar.current == observed


def test_context_map_recommendation_moved_rearms_acknowledgement(
    tmp_path: Path, monkeypatch, capsys
):
    _quiet_serena_side_effects(monkeypatch)
    ledger = tmp_path / "mcp_installs.json"
    monkeypatch.setattr("headroom.mcp_registry.ledger.ledger_path", lambda: ledger)
    observed = ServerSpec(name="serena", command="uvx", args=("--from", "old-serena"))
    old_recommended = ServerSpec(
        name="serena", command="uvx", args=("--from", "old-recommendation")
    )
    record_acknowledgement("claude", "serena", old_recommended, observed)

    wrap_cli._setup_serena_mcp(_Registrar(observed), verbose=True)
    assert "existing config differs" in capsys.readouterr().out


def test_user_edit_after_acknowledgement_rearms_warning(tmp_path: Path, monkeypatch, capsys):
    _quiet_serena_side_effects(monkeypatch)
    ledger = tmp_path / "mcp_installs.json"
    monkeypatch.setattr("headroom.mcp_registry.ledger.ledger_path", lambda: ledger)
    recommended = build_serena_spec_for_agent("claude")
    observed = ServerSpec(name="serena", command="uvx", args=("--from", "old-serena"))
    record_acknowledgement("claude", "serena", recommended, observed)

    edited = ServerSpec(name="serena", command="uvx", args=("--from", "edited"))
    wrap_cli._setup_serena_mcp(_Registrar(edited), verbose=True)
    assert "existing config differs" in capsys.readouterr().out
