# victoria-logs-mcp — agent notes

Flat Python MCP server (not a package). `server.py` is the app — config,
tools, auth middleware; `victorialogs.py` is its **stdlib-only** LogsQL HTTP
client (urllib, json — no HTTP library); `healthcheck.py` is the container
healthcheck probe. Shipped as a rootless podman/quadlet container; the runtime
— its unit, the `npm-network` it shares and the env wiring — is owned by the
`stack` repo (https://github.com/regularnmae/stack), not here. Toolchain is uv
only — no pip in CI, in the build, or in the image (CI asserts it). `mcp` is
the 2.x line: the app object is `MCPServer` (the v2 rename of `FastMCP`).

This is a sibling of `fulcrum-mcp`, `bitcoin-node-mcp`, `monero-node-mcp`,
`monero-wallet-mcp`, `lightning-mcp` and `searxng-mcp`. Follow their structure
and conventions. Env vars, tool list and the security model are documented in
`README.md`; don't re-derive them from the code.

## Commands

```sh
uv run ruff check .            # lint (CI gate). No `ruff format`; line-length 100, rules E,F,I,UP,B,SIM
uv run pyright                 # typecheck (CI gate); include list in [tool.pyright]
uv run pytest -q               # no network, no store
uv run python scripts/smoke.py # boots the real app on loopback: /health, /ready, 401s, handshake
uv add <pkg>                   # edits pyproject.toml AND re-locks — commit the uv.lock change
uv lock --check                # CI drift gate
podman build -t localhost/victoria-logs-mcp:latest .   # single-stage Containerfile
```

Deployment is owned by the `stack` repo: `cd ~/build/stack && ./deploy.sh
--only victoria-logs-mcp` rebuilds the image, restarts
`victoria-logs-mcp.service` and waits for "(healthy)"; `./deploy.sh` deploys the
whole stack. The unit and `npm-network` live there, not here.

## Facts that are easy to get wrong

- **The store is not authenticated, and this server is the only gate.** Keep it
  that way: the tools build LogsQL and call the store's read-only `/select/*`
  endpoints. Never proxy the store's HTTP API through this server, and never
  add a tool that writes (there is no such endpoint today, but a future
  VictoriaLogs could grow one).
- **`since` is interpolated, not parameterised.** `_time_filter` accepts only a
  duration (`15m`, `2h`, `7d`) or an RFC3339 instant, by regex, because the
  value goes into the query string. A container name is quoted through
  `victorialogs.quote_ql`; a field name is validated by `_FIELD` before it is
  sent as a request parameter. Do not add a tool that takes a raw LogsQL
  fragment and interpolates it unquoted.
- **The query endpoint returns NDJSON**, one object per line, and the store
  sorts by `_time` descending by default *when a `limit` parameter is given*.
  `get_log_health` and `get_context` ask for an explicit `| sort by (_time)`
  because they must not depend on that. `_time` values are RFC3339 UTC.
- **`stats` rows have no `_msg`.** A tool that returns rows from a `| stats`
  pipe returns the aggregate fields as the store sends them — `_entry` is only
  for log rows.
- **`error_summary` is two stats on purpose.** journald writes one entry per
  line, so a 20-line traceback is 20 "errors". The query folds by
  `(_time:1m, container, unit)` first, then aggregates to `minutes` (buckets
  with at least one error), `entries` (lines) and `peak` (busiest minute).
  Reverting to a single `stats by (container, unit) count()` makes the count a
  line count again, which is what it was built to stop being.
- **Caps live in `server.py`**: `DEFAULT_LIMIT` 100, `MAX_LIMIT` 1000,
  `MAX_MESSAGE_BYTES` 8 KiB, `MAX_RESPONSE_BYTES` 512 KiB. The schema's
  `Field(le=…)` and the `_limit` clamp must agree; `tests/test_schemas.py`
  pins the advertised bounds.
- Every `@mcp.tool` passes `structured_output=False` and
  `annotations=_LOGS_TOOLS`; `tests/test_schemas.py` asserts this and the exact
  set of tool names — adding a tool means updating `TOOLS` there.
- `_guard` raises `ToolError` rather than returning error text, so a failure is
  `is_error=True` with the same message. A store body that is not what the API
  promises is an error, never an empty list.
- Tool bodies are synchronous (`def`, not `async def`); anyio runs them on
  worker threads. `LogsClient` keeps no state between calls, so that is safe.
- A new top-level module must be added to the `Containerfile`'s `COPY` lines
  **and** pyright's `include`, or it won't ship or typecheck.
- `.containerignore` excludes `tests/`, `scripts/`, `*.md`, `.git`.
- The image declares no `HEALTHCHECK`; the `stack` unit supplies
  `HealthCmd=python /app/healthcheck.py`. `stack` writes this server's env
  files (`token.env`, `upstream.env`); see that repo for the unit, the env-file
  location and the host port (8111).
- The server binds `0.0.0.0:8000` and serves `/mcp` (token-gated); `/health` is
  open and answers exactly `{"status":"ok"}`. `/ready` is also open and probes
  the store (`200` when it answers `OK`, `503` otherwise, 3s timeout), cached
  for `READY_CACHE_TTL` (5s) because the path is open. Both answer `GET`/`HEAD`
  only and `405` anything else. The podman healthcheck is `healthcheck.py`
  against `/mcp` (an authenticated initialize) and must not be moved to
  `/ready`, or a store outage restart-loops the container.
- The bearer-token env var is **`LOGS_MCP_TOKEN`**, not a shared name: each
  sibling server has its own token.

## Testing quirks

- Tests talk to the server through the SDK's in-memory `Client(mcp)` — no test
  opens a socket. `fake_client` (in `tests/conftest.py`) replaces
  `server.client.query` / `.field_values` / `.health` with a scripted fake that
  records the LogsQL; `answers` is keyed by a substring, so the two questions
  `get_log_health` asks can be scripted differently.
- `tests/test_victorialogs.py` does fake the transport, by monkeypatching
  `urllib.request.urlopen`, so URL encoding, NDJSON parsing and the error
  mapping are covered without a store.
- `pytest` runs with `pythonpath = ["."]`, so `import server` and `import
  victorialogs` work as top-level modules. Do **not** `from conftest import
  ...` in a test — `tests` is a package and that import fails; use the
  fixtures.
