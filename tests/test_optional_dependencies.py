from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
MACOS_X86_64_TORCH_GUARD = "sys_platform != 'darwin' or platform_machine != 'x86_64'"


def test_all_extra_does_not_require_torch_on_macos_x86_64() -> None:
    """Keep `headroom-ai[all]` resolvable where PyTorch publishes no wheel."""

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    optional_deps = pyproject["project"]["optional-dependencies"]

    assert "ml" in optional_deps["all"][0]
    assert "voice" in optional_deps["all"][0]

    torch_deps = [
        dep
        for extra_name in ("ml", "voice")
        for dep in optional_deps[extra_name]
        if dep.startswith("torch")
    ]

    assert torch_deps
    assert all(MACOS_X86_64_TORCH_GUARD in dep for dep in torch_deps)
