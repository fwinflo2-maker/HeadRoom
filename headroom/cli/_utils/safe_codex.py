"""Shared helpers for the safe-codex CLI profile."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from headroom.proxy.modes import PROXY_MODE_CACHE

SAFE_CODEX_PROFILE = "safe-codex"
SAFE_CODEX_HOST = "127.0.0.1"
_TRUTHY = {"1", "true", "yes", "on"}
_DISABLED_VALUES = {"", "0", "false", "off", "none", "default"}
_PROMPT_CACHE_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SECRET_MARKERS = (
    "authorization",
    "bearer",
    "api_key",
    "apikey",
    "token",
    "sk-",
    "ghp_",
    "github_pat_",
)
_PROMPT_CACHE_RETENTION_ALIASES: dict[str, str | None] = {
    "": None,
    "0": None,
    "false": None,
    "off": None,
    "none": None,
    "default": None,
    "in_memory": "in-memory",
    "in-memory": "in-memory",
    "24h": "24h",
}


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


def _auto_prompt_cache_key(*, cwd: Path | None = None) -> str:
    """Build a stable opaque cache key without exposing the raw local path."""
    base = cwd if cwd is not None else Path.cwd()
    try:
        seed = str(base.expanduser().resolve())
    except OSError:
        seed = str(base.absolute())
    digest = hashlib.sha256(seed.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
    return f"codex:{digest}"


def resolve_prompt_cache_key(value: str | None, *, cwd: Path | None = None) -> str | None:
    """Resolve a safe OpenAI prompt_cache_key value."""
    raw = (value or "").strip()
    lowered = raw.lower()
    if lowered in _DISABLED_VALUES:
        return None
    if lowered == "auto":
        return _auto_prompt_cache_key(cwd=cwd)
    if not _PROMPT_CACHE_KEY_RE.fullmatch(raw):
        raise click.UsageError(
            "--prompt-cache-key must be 'auto' or a 1-128 character value using "
            "only letters, numbers, '.', '_', '-', or ':'"
        )
    if any(marker in lowered for marker in _SECRET_MARKERS):
        raise click.UsageError("--prompt-cache-key must not contain token/API-key-like text")
    return raw


def normalize_prompt_cache_retention(value: str | None) -> str | None:
    """Normalize safe-codex prompt cache retention CLI values."""
    raw = (value or "").strip().lower()
    if raw not in _PROMPT_CACHE_RETENTION_ALIASES:
        raise click.UsageError(
            "--prompt-cache-retention must be one of: default, in_memory, in-memory, 24h"
        )
    return _PROMPT_CACHE_RETENTION_ALIASES[raw]


def resolve_prompt_cache_options(
    *,
    prompt_cache_key: str | None,
    prompt_cache_retention: str | None,
    cwd: Path | None = None,
) -> tuple[str | None, str | None]:
    """Resolve prompt caching options for safe-codex."""
    return (
        resolve_prompt_cache_key(prompt_cache_key, cwd=cwd),
        normalize_prompt_cache_retention(prompt_cache_retention),
    )


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


def reject_safe_codex_learn_apply(
    *,
    apply: bool,
    allow_context_write: bool,
    profile: str | None = None,
) -> None:
    """Reject headroom learn --apply for safe-codex unless explicitly allowed."""
    if not apply:
        return
    effective_profile = profile if profile is not None else os.environ.get("HEADROOM_PROFILE")
    if not is_safe_codex_profile(effective_profile):
        return
    if allow_context_write:
        return
    raise click.UsageError(
        "--apply is not allowed with safe-codex unless --allow-context-write is specified"
    )
