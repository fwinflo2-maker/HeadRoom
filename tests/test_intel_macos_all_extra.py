from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from packaging.markers import default_environment
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
README = ROOT / "README.md"
INSTALLATION_DOC = ROOT / "docs" / "content" / "docs" / "installation.mdx"


def _optional_dependencies() -> dict[str, list[str]]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]["optional-dependencies"]


def _marker_env(*, sys_platform: str, platform_machine: str) -> dict[str, str]:
    env = default_environment()
    env.update(
        {
            "sys_platform": sys_platform,
            "platform_machine": platform_machine,
            "python_version": "3.11",
            "extra": "",
        }
    )
    return env


def _resolved_optional_dependency_names(
    extra: str,
    env: dict[str, str],
    seen: set[str] | None = None,
) -> set[str]:
    optional = _optional_dependencies()
    seen = seen or set()
    if extra in seen:
        return set()
    seen.add(extra)

    names: set[str] = set()
    for spec in optional[extra]:
        requirement = Requirement(spec)
        if requirement.marker is not None and not requirement.marker.evaluate(environment=env):
            continue
        if requirement.name == "headroom-ai" and requirement.extras:
            for nested_extra in requirement.extras:
                names.update(_resolved_optional_dependency_names(nested_extra, env, seen))
            continue
        names.add(requirement.name)
    return names


def test_all_extra_skips_torch_bearing_dependencies_on_intel_macos() -> None:
    names = _resolved_optional_dependency_names(
        "all",
        _marker_env(sys_platform="darwin", platform_machine="x86_64"),
    )

    assert "torch" not in names
    assert "sentence-transformers" not in names
    assert "datasets" not in names
    assert "fastembed" in names
    assert "openpyxl" in names


def test_all_extra_keeps_full_bundle_on_linux() -> None:
    names = _resolved_optional_dependency_names(
        "all",
        _marker_env(sys_platform="linux", platform_machine="x86_64"),
    )

    assert "torch" in names
    assert "sentence-transformers" in names
    assert "datasets" in names


def test_all_extra_keeps_full_bundle_on_apple_silicon() -> None:
    names = _resolved_optional_dependency_names(
        "all",
        _marker_env(sys_platform="darwin", platform_machine="arm64"),
    )

    assert "torch" in names
    assert "sentence-transformers" in names
    assert "datasets" in names


def test_install_docs_note_intel_macos_all_exception() -> None:
    readme = README.read_text(encoding="utf-8")
    installation = INSTALLATION_DOC.read_text(encoding="utf-8")

    assert "On Intel macOS, `[all]` means everything supported on that platform." in readme
    assert (
        "On Intel macOS, `[all]` skips the torch-bearing `ml`, `memory`, `evals`, and `voice` extras"
        in installation
    )
