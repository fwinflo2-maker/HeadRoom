"""`wrap copilot --native`: route Copilot's own API through Headroom.

Why this mode exists
--------------------
BYOK is the CLI's provider override, and it *replaces* Copilot's model routing:
it accepts exactly one model, so the `/models` picker collapses to that model and
the MAIN agent cannot be switched mid-session. Native mode redirects the CLI's
own GitHub-authenticated API surface instead, leaving Copilot's routing intact —
the CLI still fetches `GET /models`, so the picker stays populated.

The tests below pin the two things that make the mode work and the one thing that
makes it safe: the BYOK path must be completely unaffected.
"""

from __future__ import annotations

import pytest

from headroom.providers.copilot.wrap import (
    COPILOT_BYOK_ENV_VARS,
    COPILOT_NATIVE_API_URL_ENV,
    build_launch_env,
    build_native_launch_env,
    native_api_url_supported,
)


def test_native_env_redirects_the_cli_api_surface() -> None:
    env, display = build_native_launch_env(port=8890, environ={}, project=None)
    assert env[COPILOT_NATIVE_API_URL_ENV] == "http://127.0.0.1:8890"
    assert any(COPILOT_NATIVE_API_URL_ENV in line for line in display)


def test_native_env_clears_every_byok_variable() -> None:
    """Any leftover BYOK var keeps the CLI single-model — the exact bug being fixed.

    That failure would be silent: the session would look fine and simply behave
    like BYOK, so it is asserted over the whole documented variable set rather
    than the few that happen to be set today.
    """
    seeded = dict.fromkeys(COPILOT_BYOK_ENV_VARS, "leftover")
    env, _ = build_native_launch_env(port=8890, environ=seeded, project=None)
    still_set = [v for v in COPILOT_BYOK_ENV_VARS if v in env]
    assert not still_set, f"BYOK vars survived into native mode: {still_set}"


def test_native_env_keeps_the_project_prefix() -> None:
    """Per-project savings ride the base URL: the CLI cannot send custom headers."""
    env, _ = build_native_launch_env(port=8890, environ={}, project="myproj")
    assert env[COPILOT_NATIVE_API_URL_ENV] == "http://127.0.0.1:8890/p/myproj"


def test_native_env_preserves_unrelated_environment() -> None:
    env, _ = build_native_launch_env(
        port=8890, environ={"PATH": "/usr/bin", "MY_VAR": "keep"}, project=None
    )
    assert env["PATH"] == "/usr/bin"
    assert env["MY_VAR"] == "keep"


def test_native_support_probe_is_tri_state(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown must be distinguishable from unsupported.

    If the variable ever stops working the failure is silent — the CLI talks
    straight to GitHub and Headroom simply sees nothing — so "no bundle found"
    (proceed with a note) and "bundle found, variable absent" (refuse) have to be
    different answers.
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nothing-here"))
    assert native_api_url_supported(environ={"LOCALAPPDATA": str(tmp_path / "none")}) is None

    pkg = tmp_path / "copilot" / "pkg" / "win32-x64" / "1.0.0"
    pkg.mkdir(parents=True)
    (pkg / "app.js").write_text("var x = 1;  // no override support\n", encoding="utf-8")
    assert native_api_url_supported(environ={"LOCALAPPDATA": str(tmp_path)}) is False

    (pkg / "app.js").write_text(
        "function qc(){return process.env.COPILOT_API_URL?1:2}\n", encoding="utf-8"
    )
    assert native_api_url_supported(environ={"LOCALAPPDATA": str(tmp_path)}) is True


# ---------------------------------------------------------------------------
# The BYOK path must be untouched — it works today and is still the only option
# for genuine third-party keys.
# ---------------------------------------------------------------------------


def test_byok_env_builder_is_unchanged_by_native_mode() -> None:
    env, display = build_launch_env(
        port=8787, provider_type="openai", wire_api="responses", environ={}, project=None
    )
    assert env["COPILOT_PROVIDER_TYPE"] == "openai"
    assert env["COPILOT_PROVIDER_BASE_URL"] == "http://127.0.0.1:8787/v1"
    assert env["COPILOT_PROVIDER_WIRE_API"] == "responses"
    # And it must NOT start setting the native override.
    assert COPILOT_NATIVE_API_URL_ENV not in env
    assert any("COPILOT_PROVIDER_BASE_URL" in line for line in display)


def test_byok_and_native_are_mutually_exclusive_shapes() -> None:
    """The two modes must never both be configured on one launch."""
    byok, _ = build_launch_env(
        port=8787, provider_type="openai", wire_api=None, environ={}, project=None
    )
    native, _ = build_native_launch_env(port=8787, environ={}, project=None)
    assert "COPILOT_PROVIDER_BASE_URL" in byok
    assert COPILOT_NATIVE_API_URL_ENV not in byok
    assert COPILOT_NATIVE_API_URL_ENV in native
    assert "COPILOT_PROVIDER_BASE_URL" not in native


# ---------------------------------------------------------------------------
# CLI wiring: flag validation and launch plumbing
# ---------------------------------------------------------------------------


def _invoke_copilot(monkeypatch: pytest.MonkeyPatch, args: list[str]):
    """Run `wrap copilot` with the launch intercepted, returning what it would do."""
    import shutil

    from click.testing import CliRunner

    from headroom.cli import wrap as wrap_mod
    from headroom.cli.main import main

    captured: dict[str, object] = {}

    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/copilot")
    monkeypatch.setattr(wrap_mod, "_check_proxy", lambda _p: False)

    class _Res:
        token = "gho_test"
        api_url = "https://api.githubcopilot.com"
        refresh_oauth_token = None
        api_token_expires_at = None

    monkeypatch.setattr(wrap_mod, "_require_copilot_subscription_resolution", lambda: _Res())
    monkeypatch.setattr(wrap_mod, "resolve_client_bearer_token", lambda: "gho_test")
    monkeypatch.setattr(wrap_mod, "_native_api_url_supported", lambda **_k: True)
    monkeypatch.setattr(wrap_mod, "_live_copilot_model_ids", lambda *_a, **_k: [])
    monkeypatch.setattr(
        wrap_mod, "_resolve_copilot_wire_api_for_model", lambda *_a, **_k: "responses"
    )

    def _fake_launch(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(wrap_mod, "_launch_tool", _fake_launch)
    result = CliRunner().invoke(main, ["wrap", "copilot", *args])
    return result, captured


def test_native_flag_sets_the_override_and_no_byok_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    result, captured = _invoke_copilot(monkeypatch, ["--native", "--port", "8890"])
    assert result.exit_code == 0, result.output
    env = captured["env"]
    assert isinstance(env, dict)
    assert env[COPILOT_NATIVE_API_URL_ENV].startswith("http://127.0.0.1:8890")
    assert "COPILOT_PROVIDER_BASE_URL" not in env


def test_native_points_the_anthropic_upstream_at_copilot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The native CLI sends Claude traffic on /v1/messages.

    Without this the proxy forwards it to api.anthropic.com and the session dies
    with 401 — reproduced live before the fix.
    """
    _result, captured = _invoke_copilot(monkeypatch, ["--native", "--port", "8890"])
    assert captured["anthropic_api_url"] == "https://api.githubcopilot.com"
    assert captured["openai_api_url"] == "https://api.githubcopilot.com"


def test_byok_launch_leaves_the_anthropic_upstream_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: BYOK must not start rewriting the Anthropic upstream."""
    _result, captured = _invoke_copilot(
        monkeypatch, ["--subscription", "--port", "8890", "--", "--model", "gpt-5.4"]
    )
    assert captured["anthropic_api_url"] is None
    env = captured["env"]
    assert isinstance(env, dict)
    assert "COPILOT_PROVIDER_BASE_URL" in env
    assert COPILOT_NATIVE_API_URL_ENV not in env


def test_native_requires_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--model` is mandatory under BYOK and pointless in native mode."""
    result, _captured = _invoke_copilot(monkeypatch, ["--native", "--port", "8890"])
    assert result.exit_code == 0, result.output
    assert "requires a model" not in result.output


def test_native_rejects_wire_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """--wire-api pins one wire for the session, which native mode exists to avoid."""
    result, _captured = _invoke_copilot(
        monkeypatch, ["--native", "--wire-api", "responses", "--port", "8890"]
    )
    assert result.exit_code != 0
    assert "--wire-api" in result.output


def test_native_rejects_anthropic_provider_type(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _captured = _invoke_copilot(
        monkeypatch, ["--native", "--provider-type", "anthropic", "--port", "8890"]
    )
    assert result.exit_code != 0
    assert "provider-type anthropic" in result.output


def test_native_refuses_when_cli_lacks_support(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refusing beats silently bypassing Headroom and losing compression.

    If a future CLI drops ``COPILOT_API_URL`` the failure is invisible: the CLI
    would talk straight to GitHub, the session would work, and Headroom would
    simply never see the traffic. A hard error is the only honest outcome.
    """
    import shutil

    from click.testing import CliRunner

    from headroom.cli import wrap as wrap_mod
    from headroom.cli.main import main

    class _Res:
        token = "gho_test"
        api_url = "https://api.githubcopilot.com"
        refresh_oauth_token = None
        api_token_expires_at = None

    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/copilot")
    monkeypatch.setattr(wrap_mod, "_check_proxy", lambda _p: False)
    monkeypatch.setattr(wrap_mod, "_require_copilot_subscription_resolution", lambda: _Res())
    monkeypatch.setattr(wrap_mod, "_launch_tool", lambda **_k: None)
    # The decisive stub: report the installed CLI as NOT supporting the override.
    monkeypatch.setattr(wrap_mod, "_native_api_url_supported", lambda **_k: False)

    result = CliRunner().invoke(main, ["wrap", "copilot", "--native", "--port", "8890"])
    assert result.exit_code != 0, result.output
    assert "COPILOT_API_URL" in result.output
    assert "without --native" in result.output


def test_native_skips_the_model_list_injection(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Native mode has a real picker, so the instruction-file workaround is not needed."""
    import os

    monkeypatch.chdir(tmp_path)
    _result, _captured = _invoke_copilot(monkeypatch, ["--native", "--port", "8890"])
    assert not os.path.exists(tmp_path / ".github" / "copilot-instructions.md")
