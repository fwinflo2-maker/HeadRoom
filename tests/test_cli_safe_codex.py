from pathlib import Path

from click.testing import CliRunner


def test_proxy_safe_codex_rejects_non_loopback_host() -> None:
    from headroom.cli.main import main

    result = CliRunner().invoke(main, ["proxy", "--profile", "safe-codex", "--host", "0.0.0.0"])

    assert result.exit_code != 0
    assert "only allows loopback host" in result.output


def test_proxy_safe_codex_rejects_log_messages() -> None:
    from headroom.cli.main import main

    result = CliRunner().invoke(main, ["proxy", "--profile", "safe-codex", "--log-messages"])

    assert result.exit_code != 0
    assert "log-messages is not allowed" in result.output


def test_proxy_safe_codex_rejects_codex_wire_debug() -> None:
    from headroom.cli.main import main

    result = CliRunner().invoke(main, ["proxy", "--profile", "safe-codex", "--codex-wire-debug"])

    assert result.exit_code != 0
    assert "codex-wire-debug is not allowed" in result.output


def test_start_proxy_passes_safe_codex_profile(monkeypatch, tmp_path: Path) -> None:
    from headroom.cli import wrap

    captured: dict[str, object] = {}

    class DummyProc:
        returncode = None

        def poll(self) -> int | None:
            return None

        def kill(self) -> None:
            captured["killed"] = True

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return DummyProc()

    monkeypatch.setattr(wrap, "_get_log_path", lambda: tmp_path / "proxy.log")
    monkeypatch.setattr(wrap, "_get_proxy_stdio_log_path", lambda: tmp_path / "proxy-stdio.log")
    monkeypatch.setattr(wrap, "_resolve_wrap_proxy_timeout_seconds", lambda: 1)
    monkeypatch.setattr(wrap, "_check_proxy", lambda port: True)
    monkeypatch.setattr(wrap.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(wrap.subprocess, "Popen", fake_popen)

    wrap._start_proxy(8787, safe_profile=True)

    assert captured["cmd"] == [
        wrap.sys.executable,
        "-m",
        "headroom.cli",
        "proxy",
        "--port",
        "8787",
        "--profile",
        "safe-codex",
    ]


def test_wrap_codex_safe_prepare_only_skips_context_writes(monkeypatch, tmp_path: Path) -> None:
    from headroom.cli import wrap

    called: list[str] = []

    def forbidden(name: str):
        def _inner(*_args, **_kwargs):
            raise AssertionError(f"{name} should not be called in safe mode")

        return _inner

    monkeypatch.setattr(
        wrap,
        "_codex_config_paths",
        lambda: (tmp_path / "config.toml", tmp_path / "config.toml.bak"),
    )
    monkeypatch.setattr(wrap, "_snapshot_codex_config_if_unwrapped", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(wrap, "_setup_lean_ctx_agent", forbidden("lean-ctx"))
    monkeypatch.setattr(wrap, "_ensure_rtk_binary", forbidden("rtk"))
    monkeypatch.setattr(wrap, "_setup_headroom_mcp", forbidden("headroom mcp"))
    monkeypatch.setattr(wrap, "_setup_coding_compressor", forbidden("coding compressor"))
    monkeypatch.setattr(wrap, "_inject_memory_mcp_config", forbidden("memory mcp"))
    monkeypatch.setattr(wrap, "_inject_memory_agents_md", forbidden("memory agents"))
    monkeypatch.setattr(
        wrap, "_inject_codex_provider_config", lambda port: called.append(f"provider:{port}")
    )

    result = CliRunner().invoke(wrap.codex, ["--safe", "--prepare-only", "--port", "9876"])

    assert result.exit_code == 0
    assert called == ["provider:9876"]


def test_wrap_codex_safe_rejects_memory() -> None:
    from headroom.cli import wrap

    result = CliRunner().invoke(wrap.codex, ["--safe", "--memory", "--prepare-only"])

    assert result.exit_code != 0
    assert "--memory is not allowed" in result.output


def test_proxy_safe_codex_sets_proxy_config_safe_mode(monkeypatch) -> None:
    from headroom.cli.main import main

    captured: dict[str, object] = {}

    def fake_run_server(config, **_kwargs):
        captured["config"] = config
        captured["ran"] = True

    monkeypatch.setattr("headroom.proxy.server.run_server", fake_run_server)

    result = CliRunner().invoke(main, ["proxy", "--profile", "safe-codex"])

    assert result.exit_code == 0
    assert captured["ran"] is True
    assert captured["config"].safe_mode is True
    assert captured["config"].log_full_messages is False
