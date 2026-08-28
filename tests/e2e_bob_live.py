"""Live end-to-end check: the real `bob` binary through a real Headroom proxy.

Mocked tests pin routing; only this pins that IBM *accepts* what we forward.
The failure it exists for was invisible to unit tests: a proxy whose OpenAI
target was left at the default forwarded Bob's ``Authorization: apikey …`` to
api.openai.com, which answered 401 to every ``/inference/v1/chat/completions``.
Every layer looked healthy — the proxy was up, the route matched, compression
ran — and only the upstream status told the truth.

Bob's stored access token expires and is refreshed by Bob itself, so this
drives the real binary rather than replaying a captured credential.

Run::

    python tests/e2e_bob_live.py                     # token + cache
    python tests/e2e_bob_live.py --modes token
    python tests/e2e_bob_live.py --prompt "say hi"

Requires a logged-in `bob` on PATH. Exits non-zero on the first failing mode.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from headroom.paths import proxy_log_path  # noqa: E402
from headroom.providers.bob import (  # noqa: E402
    DEFAULT_API_URL,
    GATEWAY_CHAT_COMPLETIONS_PATH,
    PROXY_ENV_KEY,
)

PROJECT = "bob-live-e2e"
# Statuses that mean the upstream rejected us. 401 is the exact symptom of the
# misrouted-upstream bug; the rest would equally invalidate a savings number.
FATAL_STATUSES = {401, 403, 404, 407, 500, 502, 503}


@dataclass
class ModeResult:
    mode: str
    ok: bool
    bob_returncode: int | None = None
    chat_requests: int = 0
    bad_statuses: dict[int, int] = field(default_factory=dict)
    requests_delta: int = 0
    tokens_saved_delta: int = 0
    failure: str = ""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json(url: str, timeout: float = 5.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def _wait_ready(port: int, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _get_json(f"http://127.0.0.1:{port}/readyz") is not None:
            return True
        time.sleep(0.5)
    return False


def _project_stats(port: int) -> tuple[int, int]:
    """(requests, tokens_saved) recorded for this script's project so far.

    ``savings.per_project`` is persisted across proxy restarts, so callers must
    compare a delta rather than an absolute.
    """
    stats = _get_json(f"http://127.0.0.1:{port}/stats") or {}
    row = (stats.get("savings", {}).get("per_project", {}) or {}).get(PROJECT, {})
    return int(row.get("requests", 0)), int(row.get("tokens_saved", 0))


def _scan_log(from_offset: int) -> tuple[int, dict[int, int]]:
    """Count chat-completion responses and upstream rejections since an offset.

    The proxy's structured request log is the only place the *upstream* status
    is visible; Bob can exit 0 while every one of its calls was rejected.
    """
    log_path = proxy_log_path()
    if not log_path.exists():
        return 0, {}
    with log_path.open("r", errors="replace") as handle:
        handle.seek(from_offset)
        tail = handle.read()

    chat_requests = 0
    bad: dict[int, int] = {}
    for line in tail.splitlines():
        if "event=proxy_inbound_response" not in line:
            continue
        if GATEWAY_CHAT_COMPLETIONS_PATH not in line:
            continue
        chat_requests += 1
        for field_ in line.split():
            if field_.startswith("status="):
                try:
                    status = int(field_.split("=", 1)[1])
                except ValueError:
                    break
                if status in FATAL_STATUSES:
                    bad[status] = bad.get(status, 0) + 1
                break
    return chat_requests, bad


def _log_offset() -> int:
    log_path = proxy_log_path()
    return log_path.stat().st_size if log_path.exists() else 0


def run_mode(mode: str, prompt: str, bob_timeout: float) -> ModeResult:
    result = ModeResult(mode=mode, ok=False)
    port = _free_port()

    proxy = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "headroom.cli",
            "proxy",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--mode",
            mode,
            "--openai-api-url",
            DEFAULT_API_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_ready(port):
            result.failure = f"proxy on port {port} never became ready"
            return result

        # Confirm the treatment took before spending a real API call on it: a
        # proxy that quietly kept the default upstream is the whole bug.
        health = _get_json(f"http://127.0.0.1:{port}/health") or {}
        running_url = (health.get("config") or {}).get("openai_api_url")
        if running_url != DEFAULT_API_URL:
            result.failure = f"proxy upstream is {running_url!r}, expected {DEFAULT_API_URL!r}"
            return result

        before_requests, before_saved = _project_stats(port)
        offset = _log_offset()

        env = dict(os.environ)
        env[PROXY_ENV_KEY] = f"http://127.0.0.1:{port}/p/{PROJECT}"
        try:
            completed = subprocess.run(  # noqa: S603
                ["bob", "run", prompt],
                env=env,
                capture_output=True,
                text=True,
                timeout=bob_timeout,
            )
            result.bob_returncode = completed.returncode
            bob_output = (completed.stdout or "") + (completed.stderr or "")
        except subprocess.TimeoutExpired:
            result.failure = f"bob did not finish within {bob_timeout:.0f}s"
            return result

        # Give the proxy a moment to flush its response log line.
        time.sleep(1.0)
        result.chat_requests, result.bad_statuses = _scan_log(offset)
        after_requests, after_saved = _project_stats(port)
        result.requests_delta = after_requests - before_requests
        result.tokens_saved_delta = after_saved - before_saved

        if result.bob_returncode != 0:
            result.failure = f"bob exited {result.bob_returncode}: {bob_output.strip()[:300]}"
            return result
        if result.chat_requests == 0:
            result.failure = "bob made no chat-completion calls through the proxy"
            return result
        if result.bad_statuses:
            rendered = ", ".join(
                f"{status}x{count}" for status, count in result.bad_statuses.items()
            )
            result.failure = f"upstream rejected Bob traffic: {rendered}"
            return result
        if result.requests_delta < 1:
            result.failure = "proxy recorded no requests for this project (observability broken)"
            return result

        result.ok = True
        return result
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proxy.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modes", default="token,cache", help="comma-separated proxy modes")
    parser.add_argument("--prompt", default="Reply with the single word: ready.")
    parser.add_argument("--timeout", type=float, default=300.0, help="per-mode bob timeout")
    args = parser.parse_args()

    if not shutil.which("bob"):
        print("SKIP: 'bob' not found on PATH (npm install -g bobshell)")
        return 0

    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    results: list[ModeResult] = []
    for mode in modes:
        print(f"\n=== mode={mode} ===")
        result = run_mode(mode, args.prompt, args.timeout)
        results.append(result)
        if result.ok:
            print(
                f"  PASS  chat_requests={result.chat_requests} "
                f"recorded_requests=+{result.requests_delta} "
                f"tokens_saved=+{result.tokens_saved_delta}"
            )
        else:
            print(f"  FAIL  {result.failure}")

    print("\n--- summary ---")
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"  {result.mode:<8} {status:<5} {result.failure}")

    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
