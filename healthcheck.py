"""Container healthcheck: POST an MCP initialize handshake to the local
streamable-HTTP endpoint. Exits non-zero on any failure so podman marks
the container unhealthy. Attaches the bearer token when one is configured
(the endpoint requires it).
"""

import json
import os
import urllib.request

URL = os.environ.get("LOGS_MCP_URL", "http://127.0.0.1:8000/mcp")
TOKEN = os.environ.get("LOGS_MCP_TOKEN", "")

payload = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "healthcheck", "version": "1"},
        },
    }
).encode()

headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
if TOKEN:
    headers["Authorization"] = f"Bearer {TOKEN}"

req = urllib.request.Request(URL, data=payload, headers=headers, method="POST")
with urllib.request.urlopen(req, timeout=5) as resp:
    raw = resp.read().decode()

# Streamable-HTTP answers are SSE-framed by default (json_response=False):
# extract the `data:` line.
body = None
for line in raw.splitlines():
    if line.startswith("data:"):
        body = json.loads(line[len("data:") :].strip())
        break

if not body or "result" not in body:
    raise SystemExit(f"healthcheck failed: {body!r}")
