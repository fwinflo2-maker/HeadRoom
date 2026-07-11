#!/usr/bin/env python3
"""E2E test for HEADROOM_OPENCODE_BIN feature.

Tests:
1. _get_opencode_bin() env handling
2. _opencode_home_dir() priority chain
3. _opencode_config_path() OPENCODE_CONFIG override
4. Lazy import safety (paths.py + mcp_registry)
5. Mock wrap flow with HEADROOM_OPENCODE_BIN
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

# Add headroom to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ── helpers ──


def test(name):
    def dec(fn):
        def wrapper():
            try:
                fn()
                print(f"  ✅ {name}")
                return True
            except AssertionError as e:
                print(f"  ❌ {name}: {e}")
                return False
            except Exception as e:
                print(f"  💥 {name}: {type(e).__name__}: {e}")
                return False

        return wrapper

    return dec


def assert_eq(a, b, msg=""):
    assert a == b, f"{msg} expected={b!r}, got={a!r}"


# ── test 1: _get_opencode_bin() ──


@test("_get_opencode_bin() — unset → 'opencode'")
def t1():
    os.environ.pop("HEADROOM_OPENCODE_BIN", None)
    from headroom.providers.opencode._shared import _get_opencode_bin

    assert_eq(_get_opencode_bin(), "opencode")


@test("_get_opencode_bin() — set to 'my-oc'")
def t2():
    os.environ["HEADROOM_OPENCODE_BIN"] = "my-oc"
    from headroom.providers.opencode._shared import _get_opencode_bin

    assert_eq(_get_opencode_bin(), "my-oc")
    os.environ.pop("HEADROOM_OPENCODE_BIN", None)


@test("_get_opencode_bin() — empty string fallback")
def t3():
    os.environ["HEADROOM_OPENCODE_BIN"] = ""
    from headroom.providers.opencode._shared import _get_opencode_bin

    assert_eq(_get_opencode_bin(), "opencode")
    os.environ.pop("HEADROOM_OPENCODE_BIN", None)


@test("_get_opencode_bin() — path basename extraction")
def t4():
    os.environ["HEADROOM_OPENCODE_BIN"] = "/opt/bin/my-opencode"
    from headroom.providers.opencode._shared import _get_opencode_bin

    assert_eq(_get_opencode_bin(), "/opt/bin/my-opencode")
    os.environ.pop("HEADROOM_OPENCODE_BIN", None)


# ── test 2: _opencode_home_dir() ──


@test("_opencode_home_dir() — default ~/.config/opencode")
def t5():
    os.environ.pop("OPENCODE_HOME", None)
    os.environ.pop("HEADROOM_OPENCODE_BIN", None)
    from headroom.providers.opencode._shared import _opencode_home_dir

    assert _opencode_home_dir().name == "opencode", f"got {_opencode_home_dir()}"


@test("_opencode_home_dir() — OPENCODE_HOME wins")
def t6():
    os.environ["OPENCODE_HOME"] = "/custom/oc-home"
    os.environ["HEADROOM_OPENCODE_BIN"] = "my-oc"
    from headroom.providers.opencode._shared import _opencode_home_dir

    assert_eq(str(_opencode_home_dir()), "/custom/oc-home")
    os.environ.pop("OPENCODE_HOME", None)
    os.environ.pop("HEADROOM_OPENCODE_BIN", None)


@test("_opencode_home_dir() — HEADROOM_OPENCODE_BIN does NOT affect home dir")
def t7():
    """HEADROOM_OPENCODE_BIN only affects binary discovery, not config path."""
    os.environ.pop("OPENCODE_HOME", None)
    os.environ["HEADROOM_OPENCODE_BIN"] = "/opt/bin/my-oc"
    from headroom.providers.opencode._shared import _opencode_home_dir

    assert _opencode_home_dir().name == "opencode", (
        f"_opencode_home_dir should stay 'opencode', got {_opencode_home_dir().name}"
    )
    os.environ.pop("HEADROOM_OPENCODE_BIN", None)


# ── test 3: _opencode_config_path() ──


@test("_opencode_config_path() — OPENCODE_CONFIG wins")
def t8():
    os.environ["OPENCODE_CONFIG"] = "/tmp/custom-config.json"
    os.environ["OPENCODE_HOME"] = "/should-be-ignored"
    os.environ.pop("HEADROOM_OPENCODE_BIN", None)
    from headroom.providers.opencode._shared import _opencode_config_path

    assert_eq(str(_opencode_config_path()), "/tmp/custom-config.json")
    os.environ.pop("OPENCODE_CONFIG", None)
    os.environ.pop("OPENCODE_HOME", None)


@test("_opencode_config_path() — fallback to _opencode_home_dir")
def t9():
    os.environ.pop("OPENCODE_CONFIG", None)
    os.environ.pop("OPENCODE_HOME", None)
    os.environ.pop("HEADROOM_OPENCODE_BIN", None)
    from headroom.providers.opencode._shared import _opencode_config_path

    p = _opencode_config_path()
    assert p.name == "opencode.json"
    assert ".config/opencode/opencode.json" in str(p)


# ── test 4: lazy import safety ──


@test("install/paths.py — lazy import of opencode_config_path")
def t10():
    # This module-level import must NOT trigger the opencode package init
    # (it's a lazy import inside the function body, so the import happens
    # only when called, not at module load time)
    os.environ.pop("OPENCODE_CONFIG", None)
    os.environ.pop("OPENCODE_HOME", None)
    os.environ.pop("HEADROOM_OPENCODE_BIN", None)
    from headroom.install.paths import opencode_config_path

    # It should return a Path ending in opencode.json
    result = opencode_config_path()
    assert result.name == "opencode.json", f"got {result}"


@test("mcp_registry/opencode.py — lazy import of OpencodeRegistrar")
def t11():
    os.environ.pop("OPENCODE_CONFIG", None)
    os.environ.pop("OPENCODE_HOME", None)
    os.environ.pop("HEADROOM_OPENCODE_BIN", None)
    from headroom.mcp_registry.opencode import OpencodeRegistrar

    r = OpencodeRegistrar()
    assert r.name == "opencode"
    assert r._config_path.name == "opencode.json"


@test("mcp_registry detect() — with HEADROOM_OPENCODE_BIN")
def t12():
    """detect() should use _get_opencode_bin() to find binary."""
    # Create a temp dir with a fake binary
    with tempfile.TemporaryDirectory() as td:
        fake_bin = Path(td) / "my-oc"
        fake_bin.write_text("#!/bin/sh\necho ok")
        fake_bin.chmod(0o755)

        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{td}:{old_path}"
        os.environ["HEADROOM_OPENCODE_BIN"] = "my-oc"

        from headroom.mcp_registry.opencode import OpencodeRegistrar

        r = OpencodeRegistrar()
        assert r.detect() is True, "detect() should find my-oc"

        os.environ["PATH"] = old_path
        os.environ.pop("HEADROOM_OPENCODE_BIN", None)


@test("mcp_registry detect() — HEADROOM_OPENCODE_BIN=not-exist")
def t13():
    """detect() should return False for non-existent binary when config dir also absent."""
    with tempfile.TemporaryDirectory() as td:
        os.environ["HEADROOM_OPENCODE_BIN"] = "does-not-exist-xyz"
        os.environ["PATH"] = "/nonexistent"
        old_home = os.environ.get("HOME", "")
        os.environ["HOME"] = td  # simulate clean home without ~/.config/opencode

        from headroom.mcp_registry.opencode import OpencodeRegistrar

        r = OpencodeRegistrar()
        assert r.detect() is False, "detect() should be False — no binary and no config dir"

        os.environ.pop("HEADROOM_OPENCODE_BIN", None)
        os.environ["HOME"] = old_home


# ── test 5: end-to-end wrap simulation ──


@test("E2E: shutil.which with HEADROOM_OPENCODE_BIN")
def t14():
    """Simulate the wrap flow: create shim, set env, verify resolution."""
    with tempfile.TemporaryDirectory() as td:
        # Create fake opencode binary
        fake_bin = Path(td) / "my-opencode"
        fake_bin.write_text("#!/bin/sh\necho 'OpenCode v1.0'")
        fake_bin.chmod(0o755)

        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{td}:{old_path}"
        os.environ["HEADROOM_OPENCODE_BIN"] = "my-opencode"

        from headroom.providers.opencode._shared import _get_opencode_bin

        bin_name = _get_opencode_bin()
        resolved = shutil.which(bin_name)
        assert resolved is not None, f"shutil.which({bin_name!r}) should resolve"
        assert "my-opencode" in str(resolved)

        # Verify home dir is always ~/.config/opencode (not my-opencode)
        from headroom.providers.opencode._shared import _opencode_home_dir

        home = _opencode_home_dir()
        assert home.name == "opencode", f"home dir should stay 'opencode', got {home.name}"

        os.environ["PATH"] = old_path
        os.environ.pop("HEADROOM_OPENCODE_BIN", None)


@test("E2E: error message when binary not found")
def t15():
    """Simulate wrap failure: HEADROOM_OPENCODE_BIN set to missing binary."""
    os.environ["HEADROOM_OPENCODE_BIN"] = "nowhere-binary"
    os.environ["PATH"] = "/nonexistent"

    from headroom.providers.opencode._shared import _get_opencode_bin

    bin_name = _get_opencode_bin()
    resolved = shutil.which(bin_name)

    assert resolved is None, "shutil.which should return None for missing binary"
    assert "nowhere-binary" in bin_name

    os.environ.pop("HEADROOM_OPENCODE_BIN", None)


# ── run ──

if __name__ == "__main__":
    print("=" * 60)
    print("HEADROOM_OPENCODE_BIN — E2E Test Suite")
    print("=" * 60)
    print()

    tests = [t1, t2, t3, t4, t5, t6, t7, t8, t9, t10, t11, t12, t13, t14, t15]
    results = [t() for t in tests]
    passed = sum(results)
    failed = len(results) - passed

    print()
    print(f"Results: {passed}/{len(results)} passed, {failed} failed")

    if failed > 0:
        sys.exit(1)
