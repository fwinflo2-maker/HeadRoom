"""Headroom-managed rtk binary integration."""

from __future__ import annotations

import platform
import shutil
from pathlib import Path

from headroom import paths as _paths

RTK_VERSION = "v0.42.4"
RTK_BIN_DIR = _paths.bin_dir()
_RTK_NAME = "rtk.exe" if platform.system() == "Windows" else "rtk"
RTK_BIN_PATH = RTK_BIN_DIR / _RTK_NAME


def _managed_rtk_candidates() -> list[Path]:
    candidates = [RTK_BIN_PATH]
    for name in ("rtk", "rtk.exe"):
        path = RTK_BIN_DIR / name
        if path not in candidates:
            candidates.append(path)
    return candidates


def get_rtk_path() -> Path | None:
    system_rtk = shutil.which("rtk")
    if system_rtk:
        return Path(system_rtk)
    for candidate in _managed_rtk_candidates():
        if candidate.is_file():
            return candidate
    return None


def is_rtk_installed() -> bool:
    return get_rtk_path() is not None
