import json
from subprocess import CompletedProcess

import click
import pytest

import headroom.providers.pi_extension as module
from headroom.install.models import ArtifactRecord
from headroom.providers.pi_extension import (
    PACKAGE_NAME,
    PackageState,
    _inspect_pi_settings,
    ensure_host_package,
    extension_release_version,
    inspect_host_package,
    remove_owned_host_package,
)


def successful(calls: list[list[str]], command: list[str], stdout: str = ""):
    calls.append(command)
    return CompletedProcess(command, 0, stdout, "")


def package_artifact(host: str, version: str, *, owned: bool) -> ArtifactRecord:
    return ArtifactRecord(
        kind="pi-extension-package",
        path=host,
        metadata={
            "package": PACKAGE_NAME,
            "version": version,
            "owned": owned,
            "source": "npm",
        },
    )


def test_release_version_rejects_development_builds() -> None:
    with pytest.raises(click.ClickException, match="released Headroom version"):
        extension_release_version("0.35.0-dev")


@pytest.mark.parametrize(
    "entry",
    [
        "npm:@headroomlabs/pi-extension-headroom@0.34.0",
        {"source": "npm:@headroomlabs/pi-extension-headroom@0.34.0"},
    ],
)
def test_pi_settings_support_string_and_object_entries(tmp_path, entry) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"packages": [entry]}), encoding="utf-8")

    assert _inspect_pi_settings(settings) == PackageState("0.34.0", "npm")


def test_pi_settings_respect_relocated_config_directory(tmp_path, monkeypatch) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"packages": ["npm:@headroomlabs/pi-extension-headroom@0.34.0"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))

    assert inspect_host_package("pi", "/bin/pi") == PackageState("0.34.0", "npm")


def test_pi_local_package_is_identified_by_manifest_name(tmp_path) -> None:
    extension = tmp_path / "extension"
    extension.mkdir()
    (extension / "package.json").write_text(
        json.dumps({"name": PACKAGE_NAME, "version": "0.34.0"}),
        encoding="utf-8",
    )
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"packages": [str(extension)]}), encoding="utf-8")

    assert _inspect_pi_settings(settings) == PackageState("0.34.0", "local")


def test_malformed_pi_settings_are_actionable(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{", encoding="utf-8")

    with pytest.raises(click.ClickException, match="settings.json"):
        _inspect_pi_settings(settings)


def test_omp_list_uses_name_version_and_enabled(monkeypatch) -> None:
    payload = {
        "npm": [
            {"name": PACKAGE_NAME, "version": "0.34.0", "enabled": True},
            {"name": "other", "version": "9.9.9", "enabled": True},
        ],
        "marketplace": [],
    }
    monkeypatch.setattr(
        module,
        "run",
        lambda command, **kwargs: CompletedProcess(command, 0, json.dumps(payload), ""),
    )

    assert inspect_host_package("omp", "/bin/omp") == PackageState("0.34.0", "npm")


def test_disabled_omp_package_is_not_compatible(monkeypatch) -> None:
    payload = {
        "npm": [{"name": PACKAGE_NAME, "version": "0.34.0", "enabled": False}],
        "marketplace": [],
    }
    monkeypatch.setattr(
        module,
        "run",
        lambda command, **kwargs: CompletedProcess(command, 0, json.dumps(payload), ""),
    )

    assert inspect_host_package("omp", "/bin/omp") is None


def test_malformed_omp_json_is_actionable(monkeypatch) -> None:
    monkeypatch.setattr(
        module,
        "run",
        lambda command, **kwargs: CompletedProcess(command, 0, "{", ""),
    )

    with pytest.raises(click.ClickException, match="plugin list"):
        inspect_host_package("omp", "/bin/omp")


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        (
            "pi",
            [
                "/bin/pi",
                "install",
                "npm:@headroomlabs/pi-extension-headroom@0.34.0",
            ],
        ),
        (
            "omp",
            [
                "/bin/omp",
                "plugin",
                "install",
                "@headroomlabs/pi-extension-headroom@0.34.0",
                "--json",
            ],
        ),
    ],
)
def test_missing_package_is_installed_at_exact_version(monkeypatch, host, expected) -> None:
    states = iter([None, PackageState("0.34.0", "npm")])
    monkeypatch.setattr(module, "inspect_host_package", lambda *_: next(states))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        module,
        "run",
        lambda command, **kwargs: successful(calls, command, "{}"),
    )

    artifact = ensure_host_package(host, f"/bin/{host}", "0.34.0", None)

    assert calls == [expected]
    assert artifact.metadata["owned"] is True


def test_compatible_preexisting_package_remains_user_owned(monkeypatch) -> None:
    monkeypatch.setattr(
        module,
        "inspect_host_package",
        lambda *_: PackageState("0.34.0", "local"),
    )

    artifact = ensure_host_package("pi", "/bin/pi", "0.34.0", None)

    assert artifact.metadata["owned"] is False


def test_incompatible_preexisting_package_is_not_overwritten(monkeypatch) -> None:
    monkeypatch.setattr(
        module,
        "inspect_host_package",
        lambda *_: PackageState("0.33.0", "npm"),
    )

    with pytest.raises(click.ClickException, match="pre-existing"):
        ensure_host_package("omp", "/bin/omp", "0.34.0", None)


@pytest.mark.parametrize("previous", ["0.33.0", "0.35.0"])
def test_owned_package_is_moved_to_exact_requested_version(monkeypatch, previous) -> None:
    states = iter([PackageState(previous, "npm"), PackageState("0.34.0", "npm")])
    monkeypatch.setattr(module, "inspect_host_package", lambda *_: next(states))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        module,
        "run",
        lambda command, **kwargs: successful(calls, command, "{}"),
    )

    artifact = ensure_host_package(
        "omp",
        "/bin/omp",
        "0.34.0",
        package_artifact("omp", previous, owned=True),
    )

    assert calls[0][-2] == "@headroomlabs/pi-extension-headroom@0.34.0"
    assert artifact.metadata["version"] == "0.34.0"


def test_failed_new_install_is_removed(monkeypatch) -> None:
    states = iter([None, None, None])
    monkeypatch.setattr(module, "inspect_host_package", lambda *_: next(states))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        module,
        "run",
        lambda command, **kwargs: successful(calls, command, "{}"),
    )

    with pytest.raises(click.ClickException, match="verify"):
        ensure_host_package("pi", "/bin/pi", "0.34.0", None)

    assert calls == [
        [
            "/bin/pi",
            "install",
            "npm:@headroomlabs/pi-extension-headroom@0.34.0",
        ],
        ["/bin/pi", "remove", "npm:@headroomlabs/pi-extension-headroom"],
    ]
    with pytest.raises(StopIteration):
        next(states)


def test_failed_owned_upgrade_restores_previous_version(monkeypatch) -> None:
    states = iter(
        [
            PackageState("0.33.0", "npm"),
            PackageState("0.35.0", "npm"),
            PackageState("0.33.0", "npm"),
        ]
    )
    monkeypatch.setattr(module, "inspect_host_package", lambda *_: next(states))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        module,
        "run",
        lambda command, **kwargs: successful(calls, command, "{}"),
    )

    with pytest.raises(click.ClickException, match="verify"):
        ensure_host_package(
            "omp",
            "/bin/omp",
            "0.34.0",
            package_artifact("omp", "0.33.0", owned=True),
        )

    assert calls == [
        [
            "/bin/omp",
            "plugin",
            "install",
            "@headroomlabs/pi-extension-headroom@0.34.0",
            "--json",
        ],
        [
            "/bin/omp",
            "plugin",
            "install",
            "@headroomlabs/pi-extension-headroom@0.33.0",
            "--json",
        ],
    ]


def test_remove_preserves_user_owned_and_changed_packages(monkeypatch) -> None:
    user_owned = package_artifact("pi", "0.34.0", owned=False)
    assert remove_owned_host_package("pi", "/bin/pi", user_owned) == "preserved"

    owned = package_artifact("pi", "0.34.0", owned=True)
    monkeypatch.setattr(
        module,
        "inspect_host_package",
        lambda *_: PackageState("0.35.0", "npm"),
    )
    assert remove_owned_host_package("pi", "/bin/pi", owned) == "preserved"


@pytest.mark.parametrize(
    ("host", "command"),
    [
        ("pi", ["/bin/pi", "remove", "npm:@headroomlabs/pi-extension-headroom"]),
        (
            "omp",
            [
                "/bin/omp",
                "plugin",
                "uninstall",
                "@headroomlabs/pi-extension-headroom",
                "--json",
            ],
        ),
    ],
)
def test_remove_owned_package_uses_host_native_command(monkeypatch, host, command) -> None:
    states = iter([PackageState("0.34.0", "npm"), None])
    monkeypatch.setattr(module, "inspect_host_package", lambda *_: next(states))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        module,
        "run",
        lambda actual, **kwargs: successful(calls, actual, "{}"),
    )

    result = remove_owned_host_package(
        host,
        f"/bin/{host}",
        package_artifact(host, "0.34.0", owned=True),
    )

    assert result == "removed"
    assert calls == [command]
