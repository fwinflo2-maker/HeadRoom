"""OpenCode MCP registrar.

OpenCode stores MCP server configuration in ``~/.config/opencode/opencode.json``
under the top-level ``mcp`` key. This registrar edits that JSON file directly.

All mutations are **atomic** (temp-file + rename) and acquire an advisory
``flock`` on ``opencode.json.lock`` to prevent data corruption from
concurrent headroom processes.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]

from .base import MCPRegistrar, RegisterResult, RegisterStatus, ServerSpec

logger = logging.getLogger(__name__)


def _opencode_config_path() -> Path:
    """Return the active OpenCode config path."""
    from headroom.providers.opencode._shared import (
        _opencode_config_path as _shared_config_path,  # lazy
    )

    return _shared_config_path()


def _lock_path(config_path: Path) -> Path:
    """Return the companion lock-file path for *config_path*."""
    return config_path.with_suffix(config_path.suffix + ".lock")


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON file, falling back to empty dict on any error.

    Handles JSONC-style ``//`` comments by stripping them when strict
    JSON parsing fails, so the registrar can safely operate on config
    files that were written by a fork or manually edited with comments.
    """
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        try:
            raw = path.read_text(encoding="utf-8")
            # Strip // line comments (JSONC) and retry.
            cleaned = re.sub(r"^\s*//[^\n]*", "", raw, flags=re.MULTILINE)
            cleaned = re.sub(r",\s*//[^\n]*", ",", cleaned)
            data = json.loads(cleaned)
        except (OSError, json.JSONDecodeError):
            return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON data using temp-file + rename.

    Writes to a temp file in the same directory, then calls ``os.replace()``
    so the target file is never observed in a partially-written state — even
    if the process crashes mid-write the original file stays intact.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".headroom-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── file-level advisory locking (POSIX) ────────────────────────────


@contextlib.contextmanager
def _locked_config(config_path: Path) -> Generator[None, None, None]:
    """Exclusive advisory lock on the companion ``.lock`` file.

    Acquires a ``fcntl.FLOCK_EX`` lock, then yields.  Nesting reads/writes
    inside the block are safe from concurrent mutation by another locked
    writer (or this same process if the lock is re-entered — note that
    ``fcntl.flock`` is NOT reentrant when called from separate fd's).

    On platforms without ``fcntl`` this is a no-op.
    """
    if fcntl is None:  # Windows — no-op
        yield
        return

    lock_path = _lock_path(config_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Open for read/write; create if absent (never truncated).
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ── spec helpers ───────────────────────────────────────────────────


def _entry_to_spec(name: str, entry: dict[str, Any]) -> ServerSpec:
    command_value = entry.get("command")
    if isinstance(command_value, list) and command_value:
        args = tuple(str(x) for x in command_value[1:])
        command = str(command_value[0])
    else:
        command = str(command_value) if command_value else ""
        args = ()
    env_value = entry.get("environment", entry.get("env", {}))
    env: dict[str, str] = {}
    if isinstance(env_value, dict):
        env = {str(k): str(v) for k, v in env_value.items()}
    return ServerSpec(name=name, command=command, args=args, env=env)


def _spec_to_entry(spec: ServerSpec) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "local",
        "command": [spec.command, *spec.args],
        "enabled": True,
    }
    if spec.env:
        entry["environment"] = dict(spec.env)
    return entry


def _specs_equivalent(a: ServerSpec, b: ServerSpec) -> bool:
    return (
        a.name == b.name
        and a.command == b.command
        and tuple(a.args) == tuple(b.args)
        and dict(a.env) == dict(b.env)
    )


def _diff_specs(existing: ServerSpec, requested: ServerSpec) -> str:
    parts: list[str] = []
    if existing.command != requested.command:
        parts.append(f"command {existing.command!r} -> {requested.command!r}")
    if tuple(existing.args) != tuple(requested.args):
        parts.append(f"args {list(existing.args)} -> {list(requested.args)}")
    if dict(existing.env) != dict(requested.env):
        parts.append(f"env {dict(existing.env)} -> {dict(requested.env)}")
    if not parts:
        return "spec differs in unidentified field(s)"
    return "; ".join(parts)


# ── registrar ──────────────────────────────────────────────────────


class OpencodeRegistrar(MCPRegistrar):
    """Register MCP servers with OpenCode.

    Mutations are serialised via an advisory ``flock`` on a companion
    ``.lock`` file and written atomically via temp-file + rename.
    """

    name = "opencode"
    display_name = "OpenCode"

    def __init__(self, *, config_path: Path | None = None) -> None:
        self._config_path = config_path or _opencode_config_path()

    # ── detection ───────────────────────────────────────────────

    def detect(self) -> bool:
        from headroom.providers.opencode._shared import _get_opencode_bin  # lazy — avoids cycle

        if shutil.which(_get_opencode_bin()):
            return True
        return self._config_path.parent.is_dir()

    # ── server CRUD ─────────────────────────────────────────────

    def get_server(self, server_name: str) -> ServerSpec | None:
        with _locked_config(self._config_path):
            data = _read_json(self._config_path)
        mcp = data.get("mcp", {})
        if not isinstance(mcp, dict):
            return None
        entry = mcp.get(server_name)
        if not isinstance(entry, dict):
            return None
        return _entry_to_spec(server_name, entry)

    def register_server(self, spec: ServerSpec, *, force: bool = False) -> RegisterResult:
        with _locked_config(self._config_path):
            data = _read_json(self._config_path)
            mcp = data.setdefault("mcp", {})
            if not isinstance(mcp, dict):
                mcp = {}
                data["mcp"] = mcp
            existing_entry = mcp.get(spec.name)

            if existing_entry is not None:
                existing_spec = _entry_to_spec(spec.name, existing_entry)
                if _specs_equivalent(existing_spec, spec):
                    return RegisterResult(RegisterStatus.ALREADY, "matches current configuration")
                if not force:
                    return RegisterResult(
                        RegisterStatus.MISMATCH,
                        _diff_specs(existing_spec, spec),
                    )

            mcp[spec.name] = _spec_to_entry(spec)
            try:
                _write_json(self._config_path, data)
            except OSError as exc:
                return RegisterResult(
                    RegisterStatus.FAILED, f"could not write {self._config_path}: {exc}"
                )
            return RegisterResult(RegisterStatus.REGISTERED, f"wrote to {self._config_path}")

    def unregister_server(self, server_name: str) -> bool:
        with _locked_config(self._config_path):
            data = _read_json(self._config_path)
            mcp = data.get("mcp", {})
            if not isinstance(mcp, dict) or server_name not in mcp:
                return False
            del mcp[server_name]
            if not mcp:
                data.pop("mcp", None)
            try:
                _write_json(self._config_path, data)
            except OSError:
                return False
            return True
