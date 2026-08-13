"""Ownership-safe lifecycle for the shared Pi/OMP extension package."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from subprocess import CalledProcessError
from typing import Any, Literal, cast

import click

from headroom._subprocess import run
from headroom._version import normalize_release_version
from headroom.install.models import ArtifactRecord

PACKAGE_NAME = "@headroomlabs/pi-extension-headroom"
HostName = Literal["pi", "omp"]


@dataclass(frozen=True)
class PackageState:
    version: str
    source: str | None


def extension_release_version(version_label: str) -> str:
    """Return the exact extension release matching a released Headroom build."""
    version = normalize_release_version(version_label)
    if version is None:
        raise click.ClickException(
            "Durable Pi/OMP init requires a released Headroom version; "
            f"current version is {version_label!r}."
        )
    return version


def _read_json(path: Path, description: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Could not read valid {description} at {path}: {exc}") from exc


def _npm_state(source: str) -> PackageState | None:
    prefix = f"npm:{PACKAGE_NAME}"
    if source == prefix:
        return PackageState("unknown", "npm")
    version_prefix = f"{prefix}@"
    if source.startswith(version_prefix):
        return PackageState(source.removeprefix(version_prefix) or "unknown", "npm")
    return None


def _local_state(source: str, settings_path: Path) -> PackageState | None:
    package_dir = Path(source).expanduser()
    if not package_dir.is_absolute():
        package_dir = settings_path.parent / package_dir
    manifest_path = package_dir / "package.json"
    if not manifest_path.is_file():
        return None
    payload = _read_json(manifest_path, "local package.json")
    if not isinstance(payload, dict) or payload.get("name") != PACKAGE_NAME:
        return None
    version = payload.get("version")
    return PackageState(version if isinstance(version, str) else "unknown", "local")


def _inspect_pi_settings(path: Path) -> PackageState | None:
    """Inspect Pi's persisted package settings without parsing human CLI output."""
    if not path.exists():
        return None
    payload = _read_json(path, "Pi settings.json")
    if not isinstance(payload, dict):
        raise click.ClickException(f"Pi settings.json at {path} must contain an object.")
    packages = payload.get("packages", [])
    if not isinstance(packages, list):
        raise click.ClickException(f"Pi settings.json at {path} has an invalid packages value.")

    for entry in packages:
        source = (
            entry
            if isinstance(entry, str)
            else entry.get("source")
            if isinstance(entry, dict)
            else None
        )
        if not isinstance(source, str):
            continue
        state = _npm_state(source)
        if state is not None:
            return state
        state = _local_state(source, path)
        if state is not None:
            return state
    return None


def _run(command: list[str], description: str) -> str:
    try:
        result = run(command, capture_output=True, text=True, check=True)
    except (CalledProcessError, OSError) as exc:
        raise click.ClickException(f"Failed to {description}: {exc}") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise click.ClickException(f"Failed to {description}{detail}")
    return cast(str, result.stdout or "")


def inspect_host_package(host: HostName, binary: str) -> PackageState | None:
    """Return the enabled package state persisted by a supported host."""
    if host == "pi":
        config_dir = Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()
        return _inspect_pi_settings(config_dir / "settings.json")

    stdout = _run([binary, "plugin", "list", "--json"], "run OMP plugin list --json")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Could not parse OMP plugin list --json output: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("npm", []), list):
        raise click.ClickException("OMP plugin list --json returned an invalid payload.")
    for entry in payload.get("npm", []):
        if (
            isinstance(entry, dict)
            and entry.get("name") == PACKAGE_NAME
            and entry.get("enabled") is not False
        ):
            version = entry.get("version")
            return PackageState(version if isinstance(version, str) else "unknown", "npm")
    return None


def _install_command(host: HostName, binary: str, version: str) -> list[str]:
    spec = f"{PACKAGE_NAME}@{version}"
    if host == "pi":
        return [binary, "install", f"npm:{spec}"]
    return [binary, "plugin", "install", spec, "--json"]


def _remove_command(host: HostName, binary: str) -> list[str]:
    if host == "pi":
        return [binary, "remove", f"npm:{PACKAGE_NAME}"]
    return [binary, "plugin", "uninstall", PACKAGE_NAME, "--json"]


def _artifact(host: HostName, version: str, *, owned: bool, source: str | None) -> ArtifactRecord:
    return ArtifactRecord(
        kind="pi-extension-package",
        path=host,
        metadata={
            "package": PACKAGE_NAME,
            "version": version,
            "owned": owned,
            "source": source,
        },
    )


def _owns_state(artifact: ArtifactRecord | None, host: HostName, state: PackageState) -> bool:
    return bool(
        artifact is not None
        and artifact.kind == "pi-extension-package"
        and artifact.path == host
        and artifact.metadata.get("package") == PACKAGE_NAME
        and artifact.metadata.get("owned") is True
        and artifact.metadata.get("version") == state.version
        and artifact.metadata.get("source") == state.source
    )


def _rollback_new_install(host: HostName, binary: str) -> None:
    _run(_remove_command(host, binary), f"roll back {host} package install")
    if inspect_host_package(host, binary) is not None:
        raise click.ClickException(f"Failed to verify rollback of the {host} package install.")


def _rollback_owned_upgrade(host: HostName, binary: str, previous: PackageState) -> None:
    _run(
        _install_command(host, binary, previous.version),
        f"restore {host} package version {previous.version}",
    )
    if inspect_host_package(host, binary) != previous:
        raise click.ClickException(
            f"Failed to verify rollback to {host} package version {previous.version}."
        )


def ensure_host_package(
    host: HostName,
    binary: str,
    version: str,
    existing_artifact: ArtifactRecord | None,
) -> ArtifactRecord:
    """Ensure an exact package version without claiming user-owned installations."""
    version = extension_release_version(version)
    previous = inspect_host_package(host, binary)
    if previous is not None and previous.version == version:
        owned = _owns_state(existing_artifact, host, previous)
        return _artifact(host, version, owned=owned, source=previous.source)
    if previous is not None and not _owns_state(existing_artifact, host, previous):
        raise click.ClickException(
            f"Refusing to overwrite pre-existing {host} package version {previous.version}."
        )

    try:
        _run(_install_command(host, binary, version), f"install {host} package version {version}")
        installed = inspect_host_package(host, binary)
        if installed != PackageState(version, "npm"):
            raise click.ClickException(
                f"Could not verify exact {host} package version {version} after install."
            )
    except click.ClickException as install_error:
        try:
            if previous is None:
                _rollback_new_install(host, binary)
            else:
                _rollback_owned_upgrade(host, binary, previous)
        except click.ClickException as rollback_error:
            raise click.ClickException(
                f"{install_error} Rollback also failed: {rollback_error}"
            ) from rollback_error
        raise install_error

    return _artifact(host, version, owned=True, source="npm")


def remove_owned_host_package(
    host: HostName, binary: str, artifact: ArtifactRecord
) -> Literal["removed", "preserved", "absent"]:
    """Remove a package only while it still matches Headroom's ownership record."""
    if artifact.metadata.get("owned") is not True:
        return "preserved"
    current = inspect_host_package(host, binary)
    if current is None:
        return "absent"
    if not _owns_state(artifact, host, current):
        return "preserved"

    _run(_remove_command(host, binary), f"remove owned {host} package")
    if inspect_host_package(host, binary) is not None:
        raise click.ClickException(f"Could not verify removal of the owned {host} package.")
    return "removed"
