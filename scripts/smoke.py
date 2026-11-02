"""End-to-end smoke check: boot the real ASGI app and hit it.

Kept separate from the pytest suite on purpose: it binds a port and spawns
uvicorn, which is a deployment concern, not a unit one. Run it with
`uv run python scripts/smoke.py` after a build.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

TOKEN = "smoke-test-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(url: str, token: str | None = None, method: str = "GET", body=None):
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


def main() -> int:
    port = free_port()
    env = {
        **os.environ,
        "LOGS_MCP_TOKEN": TOKEN,
        "LOG_LEVEL": "INFO",
        # Nothing is listening on port 1, so /ready has a deterministic
        # "store is down" answer to report.
        "VICTORIA_LOGS_URL": "http://127.0.0.1:1",
    }
    process = subprocess.Popen(
        [sys.executable, "server.py"],
        env={**env, "MCP_HOST": "127.0.0.1", "MCP_PORT": str(port)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        # Wait for the port to accept connections.
        for _ in range(100):
            if process.poll() is not None:
                print(process.stdout.read() if process.stdout else "")
                print("FAIL: server exited during startup")
                return 1
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            print("FAIL: server never listened")
            return 1

        failures = []

        status, _ = request(f"{base}/health")
        if status != 200:
            failures.append(f"/health returned {status}, want 200")

        # A status path answers reads only; a write method is a client bug.
        status, _ = request(f"{base}/health", method="POST", body=b"{}")
        if status != 405:
            failures.append(f"POST /health returned {status}, want 405")

        # Liveness and readiness differ on purpose: the process is up while
        # the (deliberately unreachable) store is down, so /ready is 503.
        status, body = request(f"{base}/ready")
        if status != 503:
            failures.append(f"/ready with no store returned {status}, want 503")
        elif '"unready"' not in body:
            failures.append(f"/ready body was not an unready report: {body[:200]}")

        status, body = request(f"{base}/mcp")
        if status != 401:
            failures.append(f"/mcp without a token returned {status}, want 401")

        status, _ = request(f"{base}/mcp", token="wrong-token", body=b"{}", method="POST")
        if status != 401:
            failures.append(f"/mcp with a bad token returned {status}, want 401")

        handshake = (
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
            b'{"protocolVersion":"2025-06-18","capabilities":{},'
            b'"clientInfo":{"name":"smoke","version":"1"}}}'
        )
        status, body = request(f"{base}/mcp", token=TOKEN, method="POST", body=handshake)
        if status != 200 or "result" not in body:
            failures.append(f"authenticated handshake returned {status}: {body[:200]}")

        if failures:
            for line in failures:
                print("FAIL:", line)
            return 1
        print(
            "smoke: health open (GET only), ready 503 without a store, "
            "mcp gated, handshake ok"
        )
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
