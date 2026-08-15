from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ISSUE_ARTIFACT = Path(
    os.environ.get(
        "HEADROOM_REPRO_ARTIFACT",
        r"D:\Repos\.claude\pr-sweep\bodies\headroom-issue-3039.json",
    )
)


def _manifest_commands() -> list[str]:
    manifest = json.loads(
        (REPO_ROOT / "plugins/headroom-agent-hooks/hooks/hooks.json").read_text(encoding="utf-8")
    )
    return [
        entry["hooks"][0]["command"] for entries in manifest["hooks"].values() for entry in entries
    ]


LAUNCHER = REPO_ROOT / "plugins/headroom-agent-hooks/bin/headroom-hook.sh"


def _posix(path: Path) -> str:
    value = path.resolve().as_posix()
    if len(value) > 2 and value[1] == ":":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def _write_recorder(path: Path, receipt: Path) -> None:
    path.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >>"$HEADROOM_RECEIPT"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_launcher(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    environment = {
        "HOME": _posix(tmp_path / "home"),
        "PATH": "/no-such-bin",
        "HEADROOM_RECEIPT": _posix(tmp_path / "receipt"),
        **extra,
    }
    return subprocess.run(
        ["sh", _posix(LAUNCHER)],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )


def _receipt(tmp_path: Path) -> list[str]:
    receipt = tmp_path / "receipt"
    return receipt.read_text(encoding="utf-8").splitlines() if receipt.exists() else []


def test_manifest_command_recovers_reported_non_login_environment(tmp_path: Path) -> None:
    artifact = json.loads(ISSUE_ARTIFACT.read_text(encoding="utf-8"))
    body = artifact["body"]
    reporter_path = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    assert f"PATH={reporter_path}" in body
    assert "headroom: command not found" in body

    home = tmp_path / "home"
    headroom = home / ".local/bin/headroom"
    headroom.parent.mkdir(parents=True)
    receipt = tmp_path / "receipt"
    _write_recorder(headroom, receipt)

    environment = {
        "HOME": _posix(home),
        "PATH": reporter_path,
        "HEADROOM_RECEIPT": _posix(receipt),
        "CLAUDE_PLUGIN_ROOT": _posix(REPO_ROOT / "plugins/headroom-agent-hooks"),
    }
    for command in _manifest_commands():
        result = subprocess.run(
            ["sh", "-c", command],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    assert receipt.read_text(encoding="utf-8").splitlines() == [
        "init hook ensure",
        "init hook ensure",
    ]
    assert "command not found" not in result.stderr


def test_launcher_resolution_precedence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    override = tmp_path / "override"
    path_dir = tmp_path / "path"
    prefix = home / ".local/bin/headroom"
    path_dir.mkdir(parents=True)
    prefix.parent.mkdir(parents=True)
    _write_recorder(override, tmp_path / "receipt")
    _write_recorder(path_dir / "headroom", tmp_path / "receipt")
    _write_recorder(prefix, tmp_path / "receipt")

    result = _run_launcher(
        tmp_path,
        HOME=_posix(home),
        PATH=_posix(path_dir),
        HEADROOM_BIN=_posix(override),
    )

    assert result.returncode == 0
    assert _receipt(tmp_path) == ["init hook ensure"]


def test_launcher_resolves_standard_prefixes(tmp_path: Path) -> None:
    prefixes = [
        tmp_path / "home/.local/bin",
        tmp_path / "home/.local/share/uv/tools/headroom-ai/bin",
        tmp_path / "home/.local/pipx/venvs/headroom-ai/bin",
    ]
    for index, prefix in enumerate(prefixes):
        prefix.mkdir(parents=True)
        name = "headroom.exe" if index == 0 else "headroom"
        _write_recorder(prefix / name, tmp_path / "receipt")
        result = _run_launcher(tmp_path)
        assert result.returncode == 0
        assert _receipt(tmp_path) == ["init hook ensure"]
        (tmp_path / "receipt").unlink()
        (prefix / name).unlink()


def test_launcher_uses_importable_python_module(tmp_path: Path) -> None:
    interpreter = tmp_path / "python-fallback"
    interpreter.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        'printf \'%s\\n\' "$*" >"$HEADROOM_RECEIPT"\n',
        encoding="utf-8",
    )
    interpreter.chmod(0o755)

    result = _run_launcher(tmp_path, HEADROOM_PYTHON=_posix(interpreter))

    assert result.returncode == 0
    assert _receipt(tmp_path) == ["-m headroom.cli init hook ensure"]


def test_launcher_missing_cli_is_nonblocking(tmp_path: Path) -> None:
    result = _run_launcher(tmp_path)

    assert result.returncode == 0
    assert _receipt(tmp_path) == []
    assert result.stderr.splitlines() == [
        "headroom: CLI not found; install with 'uv tool install headroom-ai' or set HEADROOM_BIN; compression hooks are inactive."
    ]
