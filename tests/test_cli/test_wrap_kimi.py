"""Tests for `headroom wrap kimi` command."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from headroom.cli import wrap as wrap_mod
from headroom.cli.main import main
from headroom.providers.kimi.runtime import build_launch_env


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_environment_precedence() -> None:
    source = {
        "KIMI_CODE_BASE_URL": "https://managed.example/v1",
        "KIMI_BASE_URL": "https://legacy.example/v1",
        "KEEP_ME": "yes",
    }

    env, display = build_launch_env(8787, source, project="my project")

    expected = "http://127.0.0.1:8787/p/my%20project/v1"
    assert env["KIMI_CODE_BASE_URL"] == expected
    assert env["KIMI_BASE_URL"] == expected
    assert env["KEEP_ME"] == "yes"
    assert source == {
        "KIMI_CODE_BASE_URL": "https://managed.example/v1",
        "KIMI_BASE_URL": "https://legacy.example/v1",
        "KEEP_ME": "yes",
    }
    assert display == [f"KIMI_CODE_BASE_URL={expected}", f"KIMI_BASE_URL={expected}"]


def test_managed_route_reproduction() -> None:
    direct = "https://api.kimi.com/coding/v1"
    configured = {"base_url": direct, "oauth": {"key": "oauth/kimi-code"}}
    env, _ = build_launch_env(8787, {"KIMI_BASE_URL": direct}, project="repo")

    selected_url = env.get("KIMI_CODE_BASE_URL") or configured["base_url"]

    assert selected_url == "http://127.0.0.1:8787/p/repo/v1"
    assert selected_url != direct


def test_wrap_kimi_launch(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kimi launches with correct configuration."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)

    captured: dict[str, Any] = {}

    def fake_launch_tool(**kwargs: Any) -> None:  # noqa: ANN003
        captured.update(kwargs)

    with patch.object(wrap_mod.shutil, "which", return_value="kimi"):
        with patch.object(wrap_mod, "_launch_tool", side_effect=fake_launch_tool):
            with patch.object(wrap_mod, "_project_name_from_cwd", return_value=None):
                result = runner.invoke(
                    main, ["wrap", "kimi", "--port", "9000", "--", "-m", "kimi-for-coding"]
                )

    assert result.exit_code == 0, result.output
    env = captured["env"]
    assert isinstance(env, dict)
    assert captured["tool_label"] == "KIMI"
    assert captured["agent_type"] == "kimi"
    assert captured["args"] == ("-m", "kimi-for-coding")
    assert captured["openai_api_url"] == "https://api.kimi.com/coding/v1"
    assert env["KIMI_CODE_BASE_URL"] == "http://127.0.0.1:9000/v1"
    assert env["KIMI_BASE_URL"] == "http://127.0.0.1:9000/v1"


def test_project_name(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Project name is encoded in both Kimi endpoint variables."""
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)

    captured: dict[str, Any] = {}

    def fake_launch_tool(**kwargs: Any) -> None:  # noqa: ANN003
        captured.update(kwargs)

    with patch.object(wrap_mod.shutil, "which", return_value="kimi"):
        with patch.object(wrap_mod, "_launch_tool", side_effect=fake_launch_tool):
            result = runner.invoke(main, ["wrap", "kimi", "--port", "7000"])

    assert result.exit_code == 0, result.output
    env = captured["env"]
    assert env["KIMI_CODE_BASE_URL"] == "http://127.0.0.1:7000/p/my-project/v1"
    assert env["KIMI_BASE_URL"] == "http://127.0.0.1:7000/p/my-project/v1"


def test_wrap_kimi_cli_fallback(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falls back to the `kimi-cli` binary when `kimi` is not on PATH."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)

    captured: dict[str, Any] = {}

    def fake_launch_tool(**kwargs: Any) -> None:  # noqa: ANN003
        captured.update(kwargs)

    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/kimi-cli" if name == "kimi-cli" else None

    with patch.object(wrap_mod.shutil, "which", side_effect=fake_which):
        with patch.object(wrap_mod, "_launch_tool", side_effect=fake_launch_tool):
            with patch.object(wrap_mod, "_project_name_from_cwd", return_value=None):
                result = runner.invoke(main, ["wrap", "kimi"])

    assert result.exit_code == 0, result.output
    assert captured["binary"] == "/usr/local/bin/kimi-cli"


def test_legacy_stale_config(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)
    monkeypatch.setenv("KIMI_TEST_UNRELATED", "preserved")
    config = tmp_path / ".kimi-code" / "config.toml"
    config.parent.mkdir()
    config.write_text('base_url = "https://api.kimi.com/coding/v1"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    before = config.read_text(encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_launch_tool(**kwargs: Any) -> None:  # noqa: ANN003
        captured.update(kwargs)

    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/kimi-cli" if name == "kimi-cli" else None

    with patch.object(wrap_mod.shutil, "which", side_effect=fake_which):
        with patch.object(wrap_mod, "_launch_tool", side_effect=fake_launch_tool):
            result = runner.invoke(main, ["wrap", "kimi"])

    assert result.exit_code == 0, result.output
    assert captured["binary"] == "/usr/local/bin/kimi-cli"
    assert captured["env"]["KIMI_TEST_UNRELATED"] == "preserved"
    assert config.read_text(encoding="utf-8") == before


def test_subprocess_launch_environment() -> None:
    env, display = build_launch_env(8787, {"KIMI_TEST_UNRELATED": "preserved"})
    completed = Mock(returncode=0)

    with (
        patch.object(wrap_mod, "_make_cleanup", return_value=lambda: None),
        patch.object(wrap_mod.signal, "signal"),
        patch.object(wrap_mod, "_register_proxy_client"),
        patch.object(wrap_mod, "_ensure_proxy", return_value=(None, 8787)),
        patch.object(wrap_mod, "_push_runtime_env"),
        patch.object(wrap_mod, "_configure_quiet_cli_env", return_value=[]),
        patch.object(wrap_mod, "subprocess") as subprocess_mod,
    ):
        subprocess_mod.run.return_value = completed
        with pytest.raises(SystemExit) as raised:
            wrap_mod._launch_tool(
                binary="kimi",
                args=("--version",),
                env=env,
                port=8787,
                no_proxy=False,
                tool_label="KIMI",
                env_vars_display=display,
            )

    assert raised.value.code == 0
    subprocess_mod.run.assert_called_once_with(["kimi", "--version"], env=env)
    actual_env = subprocess_mod.run.call_args.kwargs["env"]
    assert actual_env["KIMI_CODE_BASE_URL"] == "http://127.0.0.1:8787/v1"
    assert actual_env["KIMI_BASE_URL"] == "http://127.0.0.1:8787/v1"
    assert subprocess_mod.run.call_args.kwargs["env"]["KIMI_TEST_UNRELATED"] == "preserved"


def test_wrap_kimi_not_found(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error message when neither kimi nor kimi-cli is found."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)

    with patch.object(wrap_mod.shutil, "which", return_value=None):
        result = runner.invoke(main, ["wrap", "kimi"])

    assert result.exit_code == 1
    assert "Error: 'kimi' (or 'kimi-cli') not found in PATH" in result.output
    assert "https://github.com/MoonshotAI/kimi-cli" in result.output


def test_port_fallback(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom --port is passed to _launch_tool and appears in both URLs."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)

    captured: dict[str, Any] = {}

    def fake_launch_tool(**kwargs: Any) -> None:  # noqa: ANN003
        captured.update(kwargs)

    with patch.object(wrap_mod.shutil, "which", return_value="kimi"):
        with patch.object(wrap_mod, "_launch_tool", side_effect=fake_launch_tool):
            with patch.object(wrap_mod, "_project_name_from_cwd", return_value=None):
                result = runner.invoke(main, ["wrap", "kimi", "--port", "9999"])

    assert result.exit_code == 0, result.output
    assert captured["port"] == 9999
    assert captured["env"]["KIMI_CODE_BASE_URL"] == "http://127.0.0.1:9999/v1"
    assert captured["env"]["KIMI_BASE_URL"] == "http://127.0.0.1:9999/v1"


def test_port_fallback_rewrites_both_urls() -> None:
    env, display = build_launch_env(8787, {}, project="repo")
    completed = Mock(returncode=0)

    with (
        patch.object(wrap_mod, "_make_cleanup", return_value=lambda: None),
        patch.object(wrap_mod.signal, "signal"),
        patch.object(wrap_mod, "_register_proxy_client"),
        patch.object(wrap_mod, "_ensure_proxy", return_value=(None, 9001)),
        patch.object(wrap_mod, "_unregister_proxy_client"),
        patch.object(wrap_mod, "_push_runtime_env"),
        patch.object(wrap_mod, "_configure_quiet_cli_env", return_value=[]),
        patch.object(wrap_mod, "subprocess") as subprocess_mod,
    ):
        subprocess_mod.run.return_value = completed
        with pytest.raises(SystemExit):
            wrap_mod._launch_tool(
                binary="kimi",
                args=(),
                env=env,
                port=8787,
                no_proxy=False,
                tool_label="KIMI",
                env_vars_display=display,
            )

    actual_env = subprocess_mod.run.call_args.kwargs["env"]
    expected = "http://127.0.0.1:9001/p/repo/v1"
    assert actual_env["KIMI_CODE_BASE_URL"] == expected
    assert actual_env["KIMI_BASE_URL"] == expected
    assert display == [
        f"KIMI_CODE_BASE_URL={expected}",
        f"KIMI_BASE_URL={expected}",
    ]


def test_wrap_kimi_custom_api_url(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--kimi-api-url overrides the upstream endpoint passed to _launch_tool."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)

    captured: dict[str, Any] = {}

    def fake_launch_tool(**kwargs: Any) -> None:  # noqa: ANN003
        captured.update(kwargs)

    with patch.object(wrap_mod.shutil, "which", return_value="kimi"):
        with patch.object(wrap_mod, "_launch_tool", side_effect=fake_launch_tool):
            with patch.object(wrap_mod, "_project_name_from_cwd", return_value=None):
                result = runner.invoke(
                    main,
                    ["wrap", "kimi", "--kimi-api-url", "https://api.moonshot.ai/v1"],
                )

    assert result.exit_code == 0, result.output
    assert captured["openai_api_url"] == "https://api.moonshot.ai/v1"


def test_wrap_kimi_no_proxy(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--no-proxy flag prevents proxy startup."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)

    captured: dict[str, Any] = {}

    def fake_launch_tool(**kwargs: Any) -> None:  # noqa: ANN003
        captured.update(kwargs)

    with patch.object(wrap_mod.shutil, "which", return_value="kimi"):
        with patch.object(wrap_mod, "_launch_tool", side_effect=fake_launch_tool):
            with patch.object(wrap_mod, "_project_name_from_cwd", return_value=None):
                result = runner.invoke(main, ["wrap", "kimi", "--no-proxy"])

    assert result.exit_code == 0, result.output
    assert captured["no_proxy"] is True


def test_wrap_kimi_learn_memory(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--learn and --memory flags are passed to _launch_tool."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)

    captured: dict[str, Any] = {}

    def fake_launch_tool(**kwargs: Any) -> None:  # noqa: ANN003
        captured.update(kwargs)

    with patch.object(wrap_mod.shutil, "which", return_value="kimi"):
        with patch.object(wrap_mod, "_launch_tool", side_effect=fake_launch_tool):
            with patch.object(wrap_mod, "_project_name_from_cwd", return_value=None):
                result = runner.invoke(main, ["wrap", "kimi", "--learn", "--memory"])

    assert result.exit_code == 0, result.output
    assert captured["learn"] is True
    assert captured["memory"] is True
