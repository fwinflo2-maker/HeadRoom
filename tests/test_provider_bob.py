from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from headroom.cli.main import main
from headroom.providers.bob import (
    DEFAULT_API_URL,
    GATEWAY_CHAT_COMPLETIONS_PATH,
    PROXY_ENV_KEY,
    build_launch_env,
    proxy_base_url,
)
from headroom.providers.bob.install import build_install_env
from headroom.providers.registry import _normalize_api_url
from headroom.providers.route_specs import OPENAI_HANDLER_ROUTES


def test_bob_proxy_base_url_has_no_version_suffix() -> None:
    # Bob appends `/inference/v1/...` itself; a `/v1` base would double it.
    assert proxy_base_url(8787) == "http://127.0.0.1:8787"


def test_bob_build_launch_env_sets_gateway_url() -> None:
    env, display = build_launch_env(9999, environ={})

    assert env[PROXY_ENV_KEY] == "http://127.0.0.1:9999"
    assert display == [f"{PROXY_ENV_KEY}=http://127.0.0.1:9999"]


def test_bob_build_launch_env_preserves_existing_environment() -> None:
    env, _display = build_launch_env(8787, environ={"HOME": "/home/dev"})

    assert env["HOME"] == "/home/dev"


def test_bob_build_launch_env_applies_project_prefix() -> None:
    env, _display = build_launch_env(8787, environ={}, project="frontend")

    assert env[PROXY_ENV_KEY] == "http://127.0.0.1:8787/p/frontend"


def test_bob_install_env_returns_gateway_url() -> None:
    assert build_install_env(port=7654, backend="ignored") == {
        PROXY_ENV_KEY: "http://127.0.0.1:7654",
    }


def test_bob_inference_path_routes_to_openai_chat_handler() -> None:
    """Bob's `/inference/v1` prefix must reach the compressing handler.

    Without this route the request matches only the generic catch-all and is
    forwarded upstream uncompressed -- the wrap would silently save nothing.
    """
    routes = {(route.method, route.path): route.handler_name for route in OPENAI_HANDLER_ROUTES}

    assert routes[("POST", GATEWAY_CHAT_COMPLETIONS_PATH)] == "handle_openai_chat"


def test_bob_upstream_target_round_trips_to_bob_inference_path() -> None:
    """`DEFAULT_API_URL` must survive normalization back to Bob's real path.

    `_normalize_api_url` strips a trailing `/v1` and `handle_openai_chat`
    re-appends `/v1/chat/completions`, so the two must compose back into the
    `/inference/v1/chat/completions` endpoint Bob's gateway actually serves.
    """
    normalized = _normalize_api_url(DEFAULT_API_URL, default="https://unused.invalid")

    assert normalized == "https://api.us-east.bob.ibm.com/inference"
    assert normalized + "/v1/chat/completions" == (
        f"https://api.us-east.bob.ibm.com{GATEWAY_CHAT_COMPLETIONS_PATH}"
    )


def test_wrap_bob_prepare_only_skips_host_binary_lookup(monkeypatch) -> None:
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)
    runner = CliRunner()

    with patch("headroom.cli.wrap.shutil.which") as which_mock:
        result = runner.invoke(main, ["wrap", "bob", "--prepare-only"])

    assert result.exit_code == 0, result.output
    which_mock.assert_not_called()


def test_wrap_bob_missing_binary_gives_install_hint(monkeypatch) -> None:
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)
    runner = CliRunner()

    with patch("headroom.cli.wrap.shutil.which", return_value=None):
        result = runner.invoke(main, ["wrap", "bob"])

    assert result.exit_code == 1
    assert "'bob' not found" in result.output


def test_unwrap_bob_reports_nothing_to_restore(monkeypatch) -> None:
    monkeypatch.delenv("HEADROOM_CONTEXT_TOOL", raising=False)
    runner = CliRunner()

    result = runner.invoke(main, ["unwrap", "bob", "--no-stop-proxy"])

    assert result.exit_code == 0, result.output
    assert "BOB_GATEWAY_URL" in result.output
    assert "no longer routed" in result.output
