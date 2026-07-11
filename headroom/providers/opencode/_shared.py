"""Shared OpenCode utilities — zero internal dependencies, safe to import from anywhere."""
from __future__ import annotations

import os
from pathlib import Path


def _get_opencode_bin() -> str:
    """Return the opencode binary name, honoring HEADROOM_OPENCODE_BIN."""
    return os.environ.get("HEADROOM_OPENCODE_BIN", "").strip() or "opencode"


def _opencode_home_dir() -> Path:
    """Return the opencode config directory.

    Priority: OPENCODE_HOME > ~/.config/opencode

    ``HEADROOM_OPENCODE_BIN`` does **not** affect the config directory — it only
    controls which binary ``shutil.which`` resolves. Use ``OPENCODE_HOME``
    (or ``OPENCODE_CONFIG``) to point headroom at a fork's custom config
    location.
    """
    env_path = os.environ.get("OPENCODE_HOME", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".config" / "opencode"


def _opencode_config_path() -> Path:
    """Return the opencode config file path.

    Priority: OPENCODE_CONFIG > opencode.jsonc (if exists) > opencode.json
    """
    env_path = os.environ.get("OPENCODE_CONFIG", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    home_dir = _opencode_home_dir()
    jsonc = home_dir / "opencode.jsonc"
    if jsonc.exists():
        return jsonc
    return home_dir / "opencode.json"
