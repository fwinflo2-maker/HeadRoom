"""SIGHUP must tear the proxy down on every wrap path, not just ``claude``.

Closing a terminal (or ``tmux kill-session``) delivers SIGHUP, not SIGTERM.
``claude()`` learned to catch it in #1768/#3205, but the two shared paths that
every other wrapped tool goes through did not:

* ``_launch_tool`` -- Pattern-A (codex, aider, copilot, goose, openhands, ...)
* ``_run_proxy_only_watcher`` -- Pattern-B (cursor, cline, continue)

Unhandled SIGHUP kills the wrapper outright, so the ``finally: cleanup()`` that
terminates the proxy never runs. The proxy is in its own session, survives, and
is reparented to PID 1 -- a leaked listener that no later wrap invocation will
ever reap.
"""

from __future__ import annotations

import inspect
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from headroom.cli import wrap as wrap_cli


def _wait_for(predicate, timeout: float = 15.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_launch_tool_registers_sighup_next_to_sigterm() -> None:
    """Pattern-A tools (codex et al.) share ``_launch_tool``'s handlers."""
    src = inspect.getsource(wrap_cli._launch_tool)
    assert 'hasattr(signal, "SIGHUP")' in src
    assert "signal.signal(signal.SIGHUP, _exit_on_signal)" in src
    assert "signal.signal(signal.SIGTERM, _exit_on_signal)" in src


def test_proxy_only_watcher_registers_sighup_next_to_sigterm() -> None:
    """Pattern-B tools (cursor et al.) share ``_run_proxy_only_watcher``.

    This path already special-cased Windows' SIGBREAK while leaving the POSIX
    terminal-close signal unhandled.
    """
    src = inspect.getsource(wrap_cli._run_proxy_only_watcher)
    assert 'hasattr(signal, "SIGHUP")' in src
    assert "signal.signal(signal.SIGHUP, _signal_shutdown)" in src
    assert "signal.signal(signal.SIGTERM, _signal_shutdown)" in src


# Harness driving the real `_launch_tool` under a real SIGHUP. Only
# `_ensure_proxy` is stubbed -- to a live child process standing in for the
# proxy -- so the signal handler, the `finally`, and `_make_cleanup`'s
# terminate are all the shipping implementations.
_HARNESS = textwrap.dedent(
    """
    import os, subprocess, sys
    from headroom.cli import wrap

    port = 18787
    proxy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    open(sys.argv[1], "w").write(str(proxy.pid))

    wrap._ensure_proxy = lambda *a, **k: (proxy, port)
    wrap._live_proxy_clients = lambda *a, **k: []   # no other clients -> may reap
    wrap._push_runtime_env = lambda *a, **k: None

    open(sys.argv[2], "w").close()                  # ready
    wrap._launch_tool(
        binary=sys.executable,
        args=("-c", "import time; time.sleep(300)"),
        env=dict(os.environ),
        port=port,
        no_proxy=False,
        tool_label="sighup-test",
        env_vars_display=[],
    )
    """
)


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX-only signal")
def test_sighup_on_launch_tool_reaps_the_proxy(tmp_path: Path) -> None:
    """End-to-end: real SIGHUP to a real wrapper must not leak the proxy."""
    harness = tmp_path / "harness.py"
    harness.write_text(_HARNESS, encoding="utf-8")
    pid_file = tmp_path / "proxy.pid"
    ready = tmp_path / "ready"

    env = dict(os.environ)
    env["HEADROOM_WORKSPACE_DIR"] = str(tmp_path / "workspace")

    # Own session/process group: the wrapper's children (stand-in proxy and
    # stand-in CLI) inherit it, so the cleanup below can reap the whole tree
    # without signalling the pytest process.
    wrapper = subprocess.Popen(
        [sys.executable, str(harness), str(pid_file), str(ready)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert _wait_for(ready.exists), "harness never reached _launch_tool"
        proxy_pid = int(pid_file.read_text())
        assert _pid_alive(proxy_pid), "stand-in proxy should be running"

        os.kill(wrapper.pid, signal.SIGHUP)

        assert _wait_for(lambda: wrapper.poll() is not None), "wrapper survived SIGHUP"
        assert _wait_for(lambda: not _pid_alive(proxy_pid)), (
            f"proxy {proxy_pid} outlived the wrapper -- it would be reparented to PID 1 and leak"
        )
    finally:
        try:
            os.killpg(wrapper.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        wrapper.wait(timeout=10)
