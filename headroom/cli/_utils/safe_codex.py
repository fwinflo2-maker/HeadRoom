"""Shared helpers for the safe-codex CLI profile."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import click

from headroom.proxy.modes import PROXY_MODE_CACHE

SAFE_CODEX_PROFILE = "safe-codex"
SAFE_CODEX_HOST = "127.0.0.1"
_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SafeCodexProxyDefaults:
    """Resolved proxy values for the safe-codex profile."""

    mode: str
    host: str
    lossless: bool
    no_ccr_inject_tool: bool
    no_ccr_marker: bool
    code_aware_flag: bool | None
    disable_kompress: bool
    log_messages: bool


def is_safe_codex_profile(profile: str | None) -> bool:
    """Return True when the requested profile is safe-codex."""
    return (profile or "").strip().lower() == SAFE_CODEX_PROFILE


def env_flag_enabled(name: str) -> bool:
    """Parse a truthy environment flag without treating false-like values as enabled."""
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def validate_known_profile(profile: str | None) -> None:
    """Reject unknown profiles while keeping the feature intentionally small."""
    if profile is None or is_safe_codex_profile(profile):
        return
    raise click.UsageError(f"Unknown profile: {profile}. Available profiles: {SAFE_CODEX_PROFILE}")


def validate_safe_codex_proxy_options(
    *,
    host: str,
    log_messages: bool,
    codex_wire_debug: bool,
    codex_wire_debug_dir: str | None,
) -> None:
    """Reject options that would weaken the safe-codex safety boundary."""
    if host != SAFE_CODEX_HOST:
        raise click.UsageError(f"safe-codex profile only allows loopback host: {SAFE_CODEX_HOST}")
    if log_messages or env_flag_enabled("HEADROOM_LOG_MESSAGES"):
        raise click.UsageError(
            "--log-messages is not allowed with safe-codex because it may persist "
            "request/response bodies"
        )
    if (
        codex_wire_debug
        or codex_wire_debug_dir is not None
        or env_flag_enabled("HEADROOM_CODEX_WIRE_DEBUG")
    ):
        raise click.UsageError(
            "--codex-wire-debug is not allowed with safe-codex because it may persist Codex traffic"
        )


def safe_codex_proxy_defaults(
    *,
    mode: str | None,
    host: str,
    lossless: bool,
    no_ccr_inject_tool: bool,
    no_ccr_marker: bool,
    code_aware_flag: bool | None,
    disable_kompress: bool,
    log_messages: bool,
    environ: dict[str, str] | None = None,
) -> SafeCodexProxyDefaults:
    """Apply safe-codex defaults without overriding explicit safe values."""
    env = environ if environ is not None else os.environ
    return SafeCodexProxyDefaults(
        mode=mode or env.get("HEADROOM_MODE") or PROXY_MODE_CACHE,
        host=host or SAFE_CODEX_HOST,
        lossless=True,
        no_ccr_inject_tool=True,
        no_ccr_marker=True,
        code_aware_flag=False if code_aware_flag is None else code_aware_flag,
        disable_kompress=True,
        log_messages=log_messages,
    )


def codex_args_request_wire_debug(args: tuple[Any, ...]) -> bool:
    """Return True when raw Codex args contain Headroom wire-debug flags."""
    return any(str(arg).startswith("--codex-wire-debug") for arg in args)


def reject_safe_codex_wrap_options(*, memory: bool, codex_args: tuple[Any, ...]) -> None:
    """Reject wrap options that write persistent context or enable wire dumps."""
    if memory:
        raise click.UsageError(
            "--memory is not allowed with --safe because it writes persistent memory/context files"
        )
    if env_flag_enabled("HEADROOM_CODEX_WIRE_DEBUG") or codex_args_request_wire_debug(codex_args):
        raise click.UsageError(
            "--codex-wire-debug is not allowed with --safe because it may persist Codex traffic"
        )
