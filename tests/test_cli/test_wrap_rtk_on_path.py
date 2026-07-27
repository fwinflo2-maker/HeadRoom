"""Tests for ``_ensure_rtk_on_path``.

``rtk init --global --auto-patch`` writes ``~/.claude/hooks/rtk-rewrite.sh``,
and ``rtk rewrite`` emits a bare ``rtk`` token at runtime that the hook feeds
back to the shell — so bare ``rtk`` must resolve on PATH. Since
``~/.headroom/bin`` is not on PATH by default, that lookup fails and token
compression never runs (issue #487).

The earlier fix rewrote the generated hook to hard-code rtk's absolute path,
but that mutates the hook after ``rtk init`` bakes in its expected SHA-256, so
rtk's integrity guard rejects it (issue #1631). ``_ensure_rtk_on_path`` instead
leaves the canonical hook untouched and links the managed binary into a PATH
directory so bare ``rtk`` resolves.

A GUI-launched agent (e.g. Claude Code) runs its hook with a minimal PATH that
may exclude the dir where ``rtk`` resolves interactively, so the hook fails even
though ``rtk`` is "on PATH" (issue #1955). The function therefore always keeps a
managed ``~/.local/bin/rtk`` link, and additionally links a PATH dir when bare
``rtk`` does not already resolve.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from headroom.cli import wrap
from headroom.cli.wrap import _ensure_rtk_on_path


@pytest.fixture
def rtk_binary(tmp_path: Path) -> Path:
    managed = tmp_path / ".headroom" / "bin" / "rtk"
    managed.parent.mkdir(parents=True)
    managed.write_text("#!/bin/sh\n")
    managed.chmod(0o755)
    return managed


def _patch_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setattr(wrap.Path, "home", classmethod(lambda _cls: home))


def test_noop_on_windows(rtk_binary: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wrap.sys, "platform", "win32")

    assert _ensure_rtk_on_path(rtk_binary, path_dirs=["C:\\bin"]) is None


def test_links_local_bin_even_when_rtk_already_resolves(
    rtk_binary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A GUI hook's PATH may not include the dir where `rtk` resolves interactively,
    # so we still guarantee a managed ~/.local/bin/rtk link (#1955).
    monkeypatch.setattr(wrap.sys, "platform", "linux")
    monkeypatch.setattr(wrap.shutil, "which", lambda _cmd: "/usr/bin/rtk")
    home = tmp_path / "home"
    _patch_home(monkeypatch, home)

    link = _ensure_rtk_on_path(rtk_binary, path_dirs=["/usr/bin"])

    assert link == home / ".local" / "bin" / "rtk"
    assert link.is_symlink()
    assert link.resolve() == rtk_binary.resolve()


def test_links_into_local_bin_when_missing(
    rtk_binary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wrap.sys, "platform", "linux")
    monkeypatch.setattr(wrap.shutil, "which", lambda _cmd: None)
    home = tmp_path / "home"
    _patch_home(monkeypatch, home)
    local_bin = home / ".local" / "bin"

    # ~/.local/bin does not exist yet — it is created on demand.
    link = _ensure_rtk_on_path(rtk_binary, path_dirs=[str(local_bin)])

    assert link == local_bin / "rtk"
    assert link.is_symlink()
    assert link.resolve() == rtk_binary.resolve()


def test_also_links_path_dir_when_local_bin_not_on_path(
    rtk_binary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wrap.sys, "platform", "linux")
    monkeypatch.setattr(wrap.shutil, "which", lambda _cmd: None)
    home = tmp_path / "home"
    _patch_home(monkeypatch, home)
    other = tmp_path / "other-bin"
    other.mkdir()
    local_bin = home / ".local" / "bin"

    link = _ensure_rtk_on_path(rtk_binary, path_dirs=[str(other)])

    # Primary link is ~/.local/bin; the on-PATH dir is also linked so a
    # terminal-launched hook resolves it too.
    assert link == local_bin / "rtk"
    assert link.is_symlink()
    assert (other / "rtk").is_symlink()
    assert (other / "rtk").resolve() == rtk_binary.resolve()


def test_idempotent_second_run(
    rtk_binary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wrap.sys, "platform", "linux")
    monkeypatch.setattr(wrap.shutil, "which", lambda _cmd: None)
    home = tmp_path / "home"
    _patch_home(monkeypatch, home)
    other = tmp_path / "other-bin"
    other.mkdir()

    first = _ensure_rtk_on_path(rtk_binary, path_dirs=[str(other)])
    second = _ensure_rtk_on_path(rtk_binary, path_dirs=[str(other)])

    assert first == second == home / ".local" / "bin" / "rtk"
    assert second.resolve() == rtk_binary.resolve()


def test_does_not_clobber_existing_local_bin_file(
    rtk_binary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wrap.sys, "platform", "linux")
    monkeypatch.setattr(wrap.shutil, "which", lambda _cmd: None)
    home = tmp_path / "home"
    _patch_home(monkeypatch, home)
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    foreign = local_bin / "rtk"
    foreign.write_text("#!/bin/sh\n# a different rtk\n")
    fallback = tmp_path / "fallback-bin"
    fallback.mkdir()

    link = _ensure_rtk_on_path(rtk_binary, path_dirs=[str(fallback)])

    # The real file in ~/.local/bin is left untouched; the link lands in the
    # next writable PATH dir.
    assert foreign.read_text() == "#!/bin/sh\n# a different rtk\n"
    assert not foreign.is_symlink()
    assert link == fallback / "rtk"
    assert link.is_symlink()


def test_noop_when_no_writable_target(
    rtk_binary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wrap.sys, "platform", "linux")
    monkeypatch.setattr(wrap.shutil, "which", lambda _cmd: None)
    # Home is a file, so ~/.local/bin cannot be created; and the only PATH dir
    # does not exist — nothing to link into.
    home_file = tmp_path / "home-is-a-file"
    home_file.write_text("x")
    _patch_home(monkeypatch, home_file)

    assert _ensure_rtk_on_path(rtk_binary, path_dirs=[str(tmp_path / "ghost")]) is None
