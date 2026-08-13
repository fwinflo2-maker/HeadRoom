"""Tests for the `headroom wrap dsh` command."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from headroom.cli.main import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _capture(captured: dict[str, object]):
    def fake_launch_tool(**kwargs: object) -> None:
        captured.update(kwargs)

    return fake_launch_tool


def test_wrap_dsh_launches_web_with_proxy_env(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr("headroom.cli.wrap._launch_tool", _capture(captured))
    monkeypatch.setattr(
        "headroom.providers.dsh.runtime.shutil.which",
        lambda _name: "/usr/bin/dsh",
    )

    result = runner.invoke(main, ["wrap", "dsh", "--port", "9000"])
    assert result.exit_code == 0, result.output

    env = captured["env"]
    assert env["DEEPSEEK_BASE_URL"] == "http://127.0.0.1:9000/v1"
    assert captured["binary"] == "/usr/bin/dsh"
    assert captured["args"] == ("web",)
    assert captured["agent_type"] == "dsh"


def test_wrap_dsh_headless_profile(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr("headroom.cli.wrap._launch_tool", _capture(captured))
    monkeypatch.setattr(
        "headroom.providers.dsh.runtime.shutil.which",
        lambda _name: "/usr/bin/dsh",
    )

    result = runner.invoke(
        main, ["wrap", "dsh", "--profile", "headless", "explain foo"]
    )
    assert result.exit_code == 0, result.output
    assert captured["args"] == ("--profile", "headless", "explain foo")


def test_wrap_dsh_forwards_deepseek_api_url(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr("headroom.cli.wrap._launch_tool", _capture(captured))
    monkeypatch.setattr(
        "headroom.providers.dsh.runtime.shutil.which",
        lambda _name: "/usr/bin/dsh",
    )

    result = runner.invoke(
        main, ["wrap", "dsh", "--deepseek-api-url", "https://deepseek.internal"]
    )
    assert result.exit_code == 0, result.output
    assert captured["deepseek_api_url"] == "https://deepseek.internal"


def test_wrap_dsh_captures_ambient_deepseek_base_url(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr("headroom.cli.wrap._launch_tool", _capture(captured))
    monkeypatch.setattr(
        "headroom.providers.dsh.runtime.shutil.which", lambda _name: "/usr/bin/dsh"
    )
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://gateway.internal")
    result = runner.invoke(main, ["wrap", "dsh"])
    assert result.exit_code == 0, result.output
    assert captured["deepseek_api_url"] == "https://gateway.internal"


def test_wrap_dsh_missing_binary_fails(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "headroom.providers.dsh.runtime.shutil.which", lambda _name: None
    )

    result = runner.invoke(main, ["wrap", "dsh"])
    assert result.exit_code == 1
    assert "not found in PATH" in result.output


def test_unwrap_dsh_stops_proxy(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "headroom.cli.wrap._stop_local_proxy_for_unwrap",
        lambda _port: "stopped",
    )
    monkeypatch.setattr(
        "headroom.cli.wrap._echo_unwrap_proxy_stop_status",
        lambda _status, _port: None,
    )

    result = runner.invoke(main, ["unwrap", "dsh"])
    assert result.exit_code == 0, result.output
