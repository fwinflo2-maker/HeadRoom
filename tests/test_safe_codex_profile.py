import pytest

from headroom.cli._utils.safe_codex import (
    SAFE_CODEX_HOST,
    SAFE_CODEX_PROFILE,
    env_flag_enabled,
    is_safe_codex_profile,
    reject_safe_codex_wrap_options,
    safe_codex_proxy_defaults,
    validate_known_profile,
    validate_safe_codex_proxy_options,
)


def test_safe_codex_profile_name_matching() -> None:
    assert is_safe_codex_profile(SAFE_CODEX_PROFILE)
    assert is_safe_codex_profile(" SAFE-CODEX ")
    assert not is_safe_codex_profile(None)
    assert not is_safe_codex_profile("default")


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(Exception, match="Unknown profile"):
        validate_known_profile("unsafe")


def test_safe_codex_defaults_are_cache_lossless_and_marker_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HEADROOM_MODE", raising=False)

    defaults = safe_codex_proxy_defaults(
        mode=None,
        host=SAFE_CODEX_HOST,
        lossless=False,
        no_ccr_inject_tool=False,
        no_ccr_marker=False,
        code_aware_flag=None,
        disable_kompress=False,
        log_messages=False,
    )

    assert defaults.mode == "cache"
    assert defaults.host == SAFE_CODEX_HOST
    assert defaults.lossless is True
    assert defaults.no_ccr_inject_tool is True
    assert defaults.no_ccr_marker is True
    assert defaults.code_aware_flag is False
    assert defaults.disable_kompress is True
    assert defaults.log_messages is False


def test_safe_codex_rejects_non_loopback_host() -> None:
    with pytest.raises(Exception, match="only allows loopback host"):
        validate_safe_codex_proxy_options(
            host="0.0.0.0",
            log_messages=False,
            codex_wire_debug=False,
            codex_wire_debug_dir=None,
        )


def test_safe_codex_rejects_message_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEADROOM_LOG_MESSAGES", "1")

    with pytest.raises(Exception, match="log-messages is not allowed"):
        validate_safe_codex_proxy_options(
            host=SAFE_CODEX_HOST,
            log_messages=False,
            codex_wire_debug=False,
            codex_wire_debug_dir=None,
        )


def test_safe_codex_rejects_codex_wire_debug_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEADROOM_CODEX_WIRE_DEBUG", "true")

    with pytest.raises(Exception, match="codex-wire-debug is not allowed"):
        validate_safe_codex_proxy_options(
            host=SAFE_CODEX_HOST,
            log_messages=False,
            codex_wire_debug=False,
            codex_wire_debug_dir=None,
        )


def test_false_like_env_flags_are_not_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEADROOM_LOG_MESSAGES", "0")
    assert env_flag_enabled("HEADROOM_LOG_MESSAGES") is False


def test_wrap_safe_rejects_memory() -> None:
    with pytest.raises(Exception, match="--memory is not allowed"):
        reject_safe_codex_wrap_options(memory=True, codex_args=())


def test_wrap_safe_rejects_wire_debug_arg() -> None:
    with pytest.raises(Exception, match="codex-wire-debug is not allowed"):
        reject_safe_codex_wrap_options(memory=False, codex_args=("--codex-wire-debug",))
