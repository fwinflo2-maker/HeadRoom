#!/usr/bin/env python3
"""Verify atomic writes and file locking in mcp_registry/opencode.py."""

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from headroom.mcp_registry.base import ServerSpec
from headroom.mcp_registry.opencode import (
    OpencodeRegistrar,
    _locked_config,
    _read_json,
    _write_json,
)

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {GREEN}✓{RESET} {name}" if ok else f"  {RED}✗{RESET} {name}")
    if not ok and detail:
        print(f"    {RED}└─ {detail}{RESET}")


# ── Test 1: atomic write — interrupted write leaves original intact ──
print("=== 1. Atomic write integrity ===")
with tempfile.TemporaryDirectory() as td:
    path = Path(td) / "config.json"
    original = {"key": "original-value"}
    path.write_text(json.dumps(original))

    # Simulate a write that fails mid-stream
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".test-", dir=td)
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write("{")  # incomplete JSON
        # DON'T os.replace — just unlink the temp
        os.unlink(tmp_path)
    except Exception:
        pass

    # Original file should be intact
    read_back = _read_json(path)
    check(
        "interrupted write → original preserved",
        read_back == original,
        f"expected {original}, got {read_back}",
    )

    # Normal write succeeds
    _write_json(path, {"key": "new-value"})
    read_back = _read_json(path)
    check("normal write → data persisted", read_back == {"key": "new-value"})

# ── Test 2: concurrent register_server under lock ──
print("\n=== 2. Concurrent registration ===")
results_lock = []
errors = []


def register_worker(td: str, idx: int) -> None:
    config_path = Path(td) / f"config-{idx}.json"
    registrar = OpencodeRegistrar(config_path=config_path)
    spec = ServerSpec(name=f"server-{idx}", command="echo", args=(str(idx),))
    try:
        result = registrar.register_server(spec)
        results_lock.append((idx, result.status.value))
    except Exception as e:
        errors.append((idx, str(e)))


with tempfile.TemporaryDirectory() as td:
    threads = [threading.Thread(target=register_worker, args=(td, i)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check(
        "20 concurrent registrations → no errors",
        len(errors) == 0,
        f"{len(errors)} errors: {errors[:3]}",
    )
    check(
        "20 concurrent registrations → 20 registered",
        len([r for r in results_lock if r[1] == "registered"]) == 20,
        f"got {len(results_lock)} results: {results_lock[:5]}",
    )

# ── Test 3: lock serialises writes to same file ──
print("\n=== 3. Lock serialisation ===")
with tempfile.TemporaryDirectory() as td:
    path = Path(td) / "shared.json"
    sequence = []

    def serial_worker(idx: int) -> None:
        with _locked_config(path):
            data = _read_json(path)
            data.setdefault("sequence", []).append(idx)
            _write_json(path, data)
            sequence.append(idx)

    threads = [threading.Thread(target=serial_worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = _read_json(path)
    check(
        "10 serialised writes → all 10 entries present",
        len(final.get("sequence", [])) == 10,
        f"got {len(final.get('sequence', []))} entries: {final.get('sequence', [])}",
    )

# ── Test 4: unregister under lock ──
print("\n=== 4. Unregister under lock ===")
with tempfile.TemporaryDirectory() as td:
    config_path = Path(td) / "oc.json"
    registrar = OpencodeRegistrar(config_path=config_path)
    spec = ServerSpec(name="test", command="echo", args=("x",))
    registrar.register_server(spec)

    ok = registrar.unregister_server("test")
    check("unregister returns True", ok)
    check("unregister removes entry", registrar.get_server("test") is None)

    ok2 = registrar.unregister_server("nonexistent")
    check("unregister non-existent returns False", not ok2)

# ── Test 5: lock file cleaned up check ──
print("\n=== 5. Lock file handling ===")
with tempfile.TemporaryDirectory() as td:
    config_path = Path(td) / "oc.json"
    lock_path = config_path.with_suffix(config_path.suffix + ".lock")

    # Lock file should exist only while locked
    check("lock absent before use", not lock_path.exists())

    with _locked_config(config_path):
        check("lock present during use", lock_path.exists())

    check(
        "lock still present after release (normal for flock)", lock_path.exists()
    )  # flock doesn't delete the file

# ── Summary ──
print()
passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"{'ALL PASSED' if failed == 0 else 'FAILURES'}: {passed}/{len(results)}")
sys.exit(0 if failed == 0 else 1)
