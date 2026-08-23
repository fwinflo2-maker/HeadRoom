"""Headroom-owned MCP install ledger.

The ledger tracks MCP servers that Headroom registered on the user's behalf
when the target agent config cannot carry Headroom-specific ownership markers.
It lets unwrap remove only entries still matching the spec Headroom installed,
preserving user-managed MCP servers with the same name.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from headroom import paths

from .base import ServerSpec

_LEDGER_FILE = "mcp_installs.json"
_ACKNOWLEDGEMENTS = "acknowledgements"


class LedgerMutationError(ValueError):
    """Raised when a ledger cannot be safely updated."""


def ledger_path() -> Path:
    """Return the Headroom MCP install ledger path."""
    return paths.workspace_dir() / _LEDGER_FILE


def spec_fingerprint(spec: ServerSpec) -> str:
    """Stable fingerprint for a registered MCP server spec."""
    payload = {
        "name": spec.name,
        "command": spec.command,
        "args": list(spec.args),
        "env": dict(sorted(spec.env.items())),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def record_install(agent: str, spec: ServerSpec, *, path: Path | None = None) -> None:
    """Record that Headroom installed ``spec`` for ``agent``."""
    ledger_file = path or ledger_path()
    data = _read_ledger(ledger_file, for_mutation=True)
    agents = data.setdefault("agents", {})
    agent_entry = agents.setdefault(agent, {})
    agent_entry[spec.name] = {
        "fingerprint": spec_fingerprint(spec),
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_ledger(ledger_file, data)


def clear_install(agent: str, server_name: str, *, path: Path | None = None) -> None:
    """Remove one ledger entry if present."""
    ledger_file = path or ledger_path()
    data = _read_ledger(ledger_file, for_mutation=True)
    agents = data.get("agents")
    if not isinstance(agents, dict):
        return
    agent_entry = agents.get(agent)
    if not isinstance(agent_entry, dict) or server_name not in agent_entry:
        return
    del agent_entry[server_name]
    if not agent_entry:
        del agents[agent]
    if not agents:
        data.pop("agents", None)
    _write_ledger(ledger_file, data)


def record_acknowledgement(
    agent: str,
    server_name: str,
    recommended: ServerSpec,
    observed: ServerSpec,
    *,
    path: Path | None = None,
) -> None:
    """Remember a user's acknowledgement without changing install ownership."""
    ledger_file = path or ledger_path()
    data = _read_ledger(ledger_file, for_mutation=True)
    acknowledgements = data.setdefault(_ACKNOWLEDGEMENTS, {})
    agent_entry = acknowledgements.setdefault(agent, {})
    agent_entry[server_name] = {
        "recommended_fingerprint": spec_fingerprint(recommended),
        "observed_fingerprint": spec_fingerprint(observed),
        "acknowledged_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_ledger(ledger_file, data)


def get_acknowledgement(
    agent: str, server_name: str, *, path: Path | None = None
) -> dict[str, Any] | None:
    """Return the acknowledgement record, if one exists."""
    data = _read_ledger(path or ledger_path())
    try:
        record = data[_ACKNOWLEDGEMENTS][agent][server_name]
    except (KeyError, TypeError):
        return None
    return record if isinstance(record, dict) else None


def acknowledgement_matches(
    agent: str,
    server_name: str,
    recommended: ServerSpec,
    observed: ServerSpec | None,
    *,
    path: Path | None = None,
) -> bool:
    """Return true only when both sides still match the acknowledged pair."""
    if observed is None:
        return False
    record = get_acknowledgement(agent, server_name, path=path)
    return bool(
        record
        and record.get("recommended_fingerprint") == spec_fingerprint(recommended)
        and record.get("observed_fingerprint") == spec_fingerprint(observed)
    )


def clear_acknowledgement(agent: str, server_name: str, *, path: Path | None = None) -> None:
    """Clear acknowledgement state while leaving install ownership intact."""
    ledger_file = path or ledger_path()
    data = _read_ledger(ledger_file, for_mutation=True)
    acknowledgements = data.get(_ACKNOWLEDGEMENTS)
    if not isinstance(acknowledgements, dict):
        return
    agent_entry = acknowledgements.get(agent)
    if not isinstance(agent_entry, dict) or server_name not in agent_entry:
        return
    del agent_entry[server_name]
    if not agent_entry:
        del acknowledgements[agent]
    if not acknowledgements:
        data.pop(_ACKNOWLEDGEMENTS, None)
    _write_ledger(ledger_file, data)


def headroom_installed_matching(
    agent: str,
    current_spec: ServerSpec | None,
    *,
    path: Path | None = None,
) -> bool:
    """Return True when the ledger says Headroom installed ``current_spec``."""
    if current_spec is None:
        return False
    ledger_file = path or ledger_path()
    data = _read_ledger(ledger_file)
    try:
        entry = data["agents"][agent][current_spec.name]
    except (KeyError, TypeError):
        return False
    if not isinstance(entry, dict):
        return False
    return entry.get("fingerprint") == spec_fingerprint(current_spec)


def validate_ledger_for_mutation(path: Path | None = None) -> None:
    """Reject malformed ledger structure before a config mutation."""
    _read_ledger(path or ledger_path(), for_mutation=True)


def _read_ledger(path: Path, *, for_mutation: bool = False) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        if for_mutation:
            raise LedgerMutationError(f"MCP install ledger is invalid JSON: {path}") from exc
        return {}
    if not isinstance(data, dict):
        if for_mutation:
            raise LedgerMutationError("MCP install ledger must contain a JSON object")
        return {}
    if for_mutation:
        for section in ("agents", _ACKNOWLEDGEMENTS):
            section_data = data.get(section)
            if section_data is None:
                continue
            if not isinstance(section_data, dict) or any(
                not isinstance(entry, dict) for entry in section_data.values()
            ):
                raise LedgerMutationError(f"MCP install ledger section {section!r} is malformed")
    return data


def _write_ledger(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
