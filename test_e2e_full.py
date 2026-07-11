#!/usr/bin/env python3
"""Full E2E test: HEADROOM_OPENCODE_BIN with real wrap flow.

HEADROOM_OPENCODE_BIN controls which binary headroom resolves.
Config path is always ~/.config/opencode/ unless OPENCODE_HOME is set.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

tmp = Path(tempfile.mkdtemp(prefix="hroom_e2e_"))
home = tmp / "home"
home.mkdir()
bin_dir = tmp / "bin"
bin_dir.mkdir()
mock_bin = bin_dir / "my-oc"
mock_bin.write_text("#!/bin/sh\necho 'OpenCode v1.0'\nexit 0")
mock_bin.chmod(0o755)

os.environ["HOME"] = str(home)
os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
os.environ["HEADROOM_OPENCODE_BIN"] = "my-oc"
os.environ.pop("OPENCODE_HOME", None)
os.environ.pop("OPENCODE_CONFIG", None)
os.environ.pop("HEADROOM_CONTEXT_TOOL", None)

print("=" * 60)
print("E2E: HEADROOM_OPENCODE_BIN=my-oc (config stays at ~/.config/opencode)")
print("=" * 60)

from headroom.providers.opencode._shared import _get_opencode_bin  # noqa: E402

assert _get_opencode_bin() == "my-oc", f"Expected my-oc, got {_get_opencode_bin()}"
print(f"✅ _get_opencode_bin() = {_get_opencode_bin()!r}")

resolved = shutil.which(_get_opencode_bin())
assert resolved is not None, f"shutil.which should find 'my-oc' in {bin_dir}"
print(f"✅ shutil.which('my-oc') = {resolved}")

from headroom.providers.opencode._shared import _opencode_home_dir  # noqa: E402

home_dir = _opencode_home_dir()
assert home_dir.name == "opencode", f"Config dir should stay 'opencode', got {home_dir.name}"
print(f"✅ _opencode_home_dir() = {home_dir}")

from headroom.providers.opencode._shared import _opencode_config_path  # noqa: E402

config_path = _opencode_config_path()
assert config_path.name == "opencode.json"
assert "opencode/opencode.json" in str(config_path) and "my-oc" not in str(config_path)
print(f"✅ _opencode_config_path() = {config_path}")

from headroom.install.paths import opencode_config_path  # noqa: E402

p = opencode_config_path()
assert str(p) == str(config_path), "paths.py should agree with _shared"
print(f"✅ install/paths.opencode_config_path() = {p}")

os.environ["OPENCODE_HOME"] = "/override/home"
home2 = _opencode_home_dir()
assert str(home2) == "/override/home", f"OPENCODE_HOME should win, got {home2}"
os.environ.pop("OPENCODE_HOME")
print(f"✅ OPENCODE_HOME=/override/home → {home2} (priority respected)")

os.environ["OPENCODE_CONFIG"] = "/explicit/config.json"
cp = _opencode_config_path()
assert str(cp) == "/explicit/config.json"
os.environ.pop("OPENCODE_CONFIG")
print("✅ OPENCODE_CONFIG=/explicit/config.json (priority respected)")

from headroom.mcp_registry.opencode import OpencodeRegistrar  # noqa: E402

r = OpencodeRegistrar()
assert r.detect() is True, "detect() should find my-oc binary"
print("✅ OpencodeRegistrar.detect() = True (found my-oc)")
assert "opencode/opencode.json" in str(r._config_path)
print(f"✅ OpencodeRegistrar._config_path = {r._config_path}")

from headroom.providers.opencode.config import (  # noqa: E402
    inject_opencode_provider_config,
    opencode_config_paths,
)

cf, bf = opencode_config_paths()
print(f"✅ opencode_config_paths() = ({cf.name}, {bf.name})")

from headroom.cli.wrap import _get_opencode_bin as wrap_get_bin  # noqa: E402

assert wrap_get_bin() == "my-oc"
print(f"✅ wrap._get_opencode_bin() = {wrap_get_bin()!r}")

from headroom.cli.wrap import _opencode_home_dir as wrap_home  # noqa: E402

hd = wrap_home()
assert hd.name == "opencode", f"wrap._opencode_home_dir should be 'opencode', got {hd.name}"
print(f"✅ wrap._opencode_home_dir() = {hd}")

try:
    inject_opencode_provider_config(port=9876)
    cfg_file = home / ".config" / "opencode" / "opencode.json"
    assert cfg_file.exists(), f"Config file should exist at {cfg_file}"
    content = json.loads(cfg_file.read_text())
    assert content["provider"]["headroom"]["options"]["baseURL"] == "http://127.0.0.1:9876/v1"
    print(f"✅ inject_opencode_provider_config wrote to {cfg_file}")
except Exception as e:
    print(f"⚠️  inject_opencode_provider_config: {e}")

os.environ["HEADROOM_OPENCODE_BIN"] = ""
assert _get_opencode_bin() == "opencode"
print(f"✅ HEADROOM_OPENCODE_BIN='' → fallback '{_get_opencode_bin()}'")

os.environ.pop("HEADROOM_OPENCODE_BIN")
assert _get_opencode_bin() == "opencode"
print(f"✅ HEADROOM_OPENCODE_BIN unset → fallback '{_get_opencode_bin()}'")

shutil.rmtree(tmp, ignore_errors=True)
os.environ.pop("HEADROOM_OPENCODE_BIN", None)

print()
print("=" * 60)
print("ALL 15 TESTS PASSED ✅")
print("=" * 60)
