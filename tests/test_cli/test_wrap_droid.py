"""Tests for `headroom wrap droid`.

`wrap droid` routes Factory Droid through Headroom by pointing Droid's gateway
at the local proxy via ``FACTORY_API_BASE_URL`` and forwarding to the resolved
Factory upstream. These tests pin the env wiring and upstream precedence; every
test runs from a tmp cwd so the real project ``AGENTS.md`` is never touched.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from headroom.cli.main import main
from headroom.providers.droid import (
    DEFAULT_FACTORY_API_URL,
    proxy_base_url,
    resolve_factory_upstream,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _tmp_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FACTORY_API_BASE_URL", raising=False)


# ---------------------------------------------------------------------------
# runtime helpers
# ---------------------------------------------------------------------------


def test_proxy_base_url_targets_loopback_port() -> None:
    assert proxy_base_url(9999) == "http://127.0.0.1:9999"


def test_resolve_factory_upstream_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_API_BASE_URL", raising=False)
    assert resolve_factory_upstream(None) == DEFAULT_FACTORY_API_URL

    monkeypatch.setenv("FACTORY_API_BASE_URL", "https://eu.factory.example/")
    assert resolve_factory_upstream(None) == "https://eu.factory.example"

    # Explicit flag beats the ambient env var and trailing slashes are trimmed.
    assert resolve_factory_upstream("https://custom.factory.test/") == "https://custom.factory.test"


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_wrap_droid_missing_binary_exits_with_install_hint(runner: CliRunner) -> None:
    with patch("headroom.cli.wrap.shutil.which", return_value=None):
        result = runner.invoke(main, ["wrap", "droid", "--no-context-tool"])

    assert result.exit_code == 1
    assert "docs.factory.ai" in result.output


def test_wrap_droid_points_child_at_proxy_and_forwards_default_upstream(
    runner: CliRunner,
) -> None:
    captured: dict[str, object] = {}

    def fake_launch_tool(**kwargs: object) -> None:
        captured.update(kwargs)

    with (
        patch("headroom.cli.wrap.shutil.which", return_value="droid"),
        patch("headroom.cli.wrap._launch_tool", side_effect=fake_launch_tool),
    ):
        result = runner.invoke(main, ["wrap", "droid", "--no-context-tool", "--", "exec", "say hi"])

    assert result.exit_code == 0, result.output
    assert captured["tool_label"] == "DROID"
    assert captured["agent_type"] == "droid"
    assert captured["args"] == ("exec", "say hi")
    # Proxy forwards to the public Factory gateway by default...
    assert captured["factory_api_url"] == DEFAULT_FACTORY_API_URL
    # ...and Droid is redirected at the local proxy.
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["FACTORY_API_BASE_URL"] == "http://127.0.0.1:8787"
    display = captured["env_vars_display"]
    assert isinstance(display, list)
    assert "FACTORY_API_BASE_URL=http://127.0.0.1:8787" in display


def test_wrap_droid_explicit_upstream_and_custom_port(runner: CliRunner) -> None:
    captured: dict[str, object] = {}

    with (
        patch("headroom.cli.wrap.shutil.which", return_value="droid"),
        patch("headroom.cli.wrap._launch_tool", side_effect=lambda **kw: captured.update(kw)),
    ):
        result = runner.invoke(
            main,
            [
                "wrap",
                "droid",
                "--no-context-tool",
                "--port",
                "9191",
                "--factory-api-url",
                "https://custom.factory.test/",
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured["factory_api_url"] == "https://custom.factory.test"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["FACTORY_API_BASE_URL"] == "http://127.0.0.1:9191"


def test_wrap_droid_inherits_ambient_factory_base_url_as_upstream(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORY_API_BASE_URL", "https://eu.factory.example")
    captured: dict[str, object] = {}

    with (
        patch("headroom.cli.wrap.shutil.which", return_value="droid"),
        patch("headroom.cli.wrap._launch_tool", side_effect=lambda **kw: captured.update(kw)),
    ):
        result = runner.invoke(main, ["wrap", "droid", "--no-context-tool"])

    assert result.exit_code == 0, result.output
    # The caller's existing gateway becomes the upstream the proxy forwards to,
    # while the child is still redirected at the local proxy.
    assert captured["factory_api_url"] == "https://eu.factory.example"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["FACTORY_API_BASE_URL"] == "http://127.0.0.1:8787"


def test_wrap_droid_prepare_only_reports_wiring(runner: CliRunner) -> None:
    with patch("headroom.cli.wrap.shutil.which", return_value="droid"):
        result = runner.invoke(main, ["wrap", "droid", "--no-context-tool", "--prepare-only"])

    assert result.exit_code == 0, result.output
    assert "FACTORY_API_BASE_URL=http://127.0.0.1:8787" in result.output
    assert f"upstream={DEFAULT_FACTORY_API_URL}" in result.output


def test_wrap_droid_no_context_tool_skips_agents_md(runner: CliRunner, tmp_path: Path) -> None:
    with (
        patch("headroom.cli.wrap.shutil.which", return_value="droid"),
        patch("headroom.cli.wrap._launch_tool"),
    ):
        result = runner.invoke(main, ["wrap", "droid", "--no-context-tool"])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "AGENTS.md").exists()
