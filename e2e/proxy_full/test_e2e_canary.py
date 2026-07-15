"""E2E canary test for the Rust proxy binary (Phase H1).

Starts ``headroom-proxy`` (the Rust binary), sends a real /healthz probe,
sends a minimal /v1/messages request through a mock upstream, and asserts:

- /healthz → 200 OK
- /v1/messages → 200 OK, no 5xx
- Response body is valid JSON with ``type == "message"``

The test is intentionally stateless: it creates a real OS process, binds to a
free port, and tears it down on exit.  No Docker required.

Usage::

    pytest e2e/proxy_full/test_e2e_canary.py -v

CI: ``make ci-precheck`` runs this before any merge to main.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parents[2]
_BINARY_CANDIDATES = [
    shutil.which("headroom-proxy"),
    str(_REPO_ROOT / "target" / "release" / "headroom-proxy"),
    str(_REPO_ROOT / "target" / "debug" / "headroom-proxy"),
]


def _find_binary() -> str | None:
    for c in _BINARY_CANDIDATES:
        if c and Path(c).is_file():
            return c
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Minimal mock Anthropic upstream
# ---------------------------------------------------------------------------

_MOCK_RESPONSE = json.dumps(
    {
        "id": "msg_canary",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Hello from mock upstream."}],
        "model": "claude-3-5-sonnet-20241022",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 6},
    }
).encode()


class _MockHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_MOCK_RESPONSE)))
        self.end_headers()
        self.wfile.write(_MOCK_RESPONSE)

    def log_message(self, *_: object) -> None:  # suppress noisy output
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mock_upstream():
    """Start a mock Anthropic upstream on a free port; yield its URL."""
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture(scope="module")
def rust_proxy(mock_upstream: str):
    """Start the Rust headroom-proxy binary; yield its base URL.

    Skips the entire module if the binary cannot be found.
    """
    binary = _find_binary()
    if binary is None:
        pytest.skip("headroom-proxy binary not found; run `cargo build --release` first")

    proxy_port = _free_port()
    env = {
        **os.environ,
        "HEADROOM_PROXY_LISTEN": f"127.0.0.1:{proxy_port}",
        "HEADROOM_PROXY_UPSTREAM": mock_upstream,
        # Disable compression so we get byte-faithful pass-through in the canary.
        "HEADROOM_PROXY_COMPRESSION": "off",
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        proc = subprocess.Popen(
            [binary],
            env=env,
            cwd=tmpdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        base_url = f"http://127.0.0.1:{proxy_port}"

        # Wait up to 5 s for /healthz to respond.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                import urllib.request

                urllib.request.urlopen(f"{base_url}/healthz", timeout=1)
                break
            except Exception:
                if proc.poll() is not None:
                    stdout, stderr = proc.communicate()
                    pytest.fail(
                        f"headroom-proxy exited early (rc={proc.returncode})\n"
                        f"stdout: {stdout.decode()}\n"
                        f"stderr: {stderr.decode()}"
                    )
                time.sleep(0.1)
        else:
            proc.send_signal(signal.SIGTERM)
            pytest.fail("headroom-proxy did not start within 5 seconds")

        yield base_url

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_healthz_returns_200(rust_proxy: str) -> None:
    """/healthz must return 200 OK."""
    import urllib.request

    resp = urllib.request.urlopen(f"{rust_proxy}/healthz", timeout=5)
    assert resp.status == 200


def test_messages_endpoint_returns_valid_json(rust_proxy: str) -> None:
    """/v1/messages must return 200 with a valid Anthropic message body."""
    import urllib.request

    payload = json.dumps(
        {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()

    req = urllib.request.Request(
        f"{rust_proxy}/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer sk-canary-test",
        },
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=10)
    assert resp.status == 200

    body = json.loads(resp.read())
    assert body.get("type") == "message"
    assert "content" in body


def test_no_5xx_on_minimal_request(rust_proxy: str) -> None:
    """A basic request must not produce a 5xx from the proxy."""
    import urllib.error
    import urllib.request

    payload = json.dumps(
        {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "ping"}],
        }
    ).encode()

    req = urllib.request.Request(
        f"{rust_proxy}/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer sk-canary-test",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.status < 500
    except urllib.error.HTTPError as e:
        assert e.code < 500, f"Unexpected 5xx from proxy: {e.code}"
