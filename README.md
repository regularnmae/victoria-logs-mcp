# victoria-logs-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives
a client read-only access to this host's
[VictoriaLogs](https://docs.victoriametrics.com/victorialogs/) store: LogsQL
search, per-container counts, error summaries, and whether the log pipeline is
still shipping. Nothing here writes, deletes or changes anything.

It exists because the store is deliberately closed — VictoriaLogs has no
authentication of its own, so the stack keeps it on the host's loopback and
reads it through `scripts/logs.sh` or an SSH tunnel. This server is the
authenticated front door: one bearer token, a bounded tool surface, and no way
to reach the store's HTTP API directly.

Seven tools, all read-only:

| Tool | What it does |
| --- | --- |
| `search_logs` | A LogsQL query inside a time window, newest entries first |
| `tail_logs` | The unfiltered recent view |
| `top_sources` | Entries per container, busiest first |
| `error_summary` | Error-and-worse lines as bursts per container and unit: `minutes`, `entries`, `peak` |
| `list_field_values` | The distinct containers, units, hosts or priorities |
| `get_log_health` | Whether entries are still arriving, and how old the newest is |
| `get_context` | The entries around one instant, oldest first |

Each tool returns text — a scalar as-is, an object as pretty-printed JSON.
They are all marked read-only and *not* open-world, so a well-behaved client
skips the confirmation prompt. A failure comes back as a tool error
(`is_error=True`) with the same readable message, so a client can tell "the
store did not answer" from "nothing matched".

## The store

`vector` ships journald into VictoriaLogs, so every container's stdout and
every host script's output is in there, labelled with the fields this server
returns:

| Field | Meaning |
| --- | --- |
| `_time` | RFC3339 UTC timestamp (the store's clock) |
| `_msg` | The log line |
| `container` | The container name, for container output |
| `unit` | The systemd unit, for host-script output |
| `host` | The machine-id-derived host label |
| `priority` | `emerg` … `debug`; BoltMesh's app level overrides journald's |

Retention is time-based and size-capped (14 days / 10 GiB by default in the
stack), so an empty answer for an old window may mean the entries were
deleted, not that nothing happened. `get_log_health` is the check that tells
the two apart.

### LogsQL

`search_logs` prepends `since` as `_time:<since>`, so a query cannot scan the
whole retention by accident. Everything after that is the store's own
language:

```
container:"lightningd"                       one container
unit:"monitor.service"                       one host script
priority:in(emerg,alert,crit,err)            error and worse
~"timeout|refused"                           a regex over the message
container:"bitcoind" | stats count() as n    a pipe
```

The field names above are the ones to filter on; anything a source attached
beyond them (suricata's `http.url`, say) is not returned by these tools, which
keep the answer to a size a model can read.

Caps are the same everywhere: 100 entries by default, 1000 at most, a message
truncated at 8 KiB, and a 512 KiB response budget. A response that hits the
budget says `"truncated": true` rather than quietly returning less.

`error_summary` counts bursts rather than lines. journald writes one entry per
line, so a 20-line traceback is 20 "errors"; the tool therefore folds entries
into one-minute buckets first and reports `minutes` (buckets with at least one
error — the incident-shaped number), `entries` (the raw line volume) and `peak`
(the busiest single minute). `window_minutes` is the window's own length, so
`minutes: 360` over `since: "6h"` means every minute: background noise, not
thirty incidents.

## Running it

```sh
uv sync                 # installs from uv.lock
LOGS_MCP_TOKEN=<a long random token> \
VICTORIA_LOGS_URL=http://127.0.0.1:9428 \
uv run python server.py
```

Or, inside the container, with no uv anywhere:

```sh
podman build -t localhost/victoria-logs-mcp:latest .
podman run -e LOGS_MCP_TOKEN=<token> \
  -e VICTORIA_LOGS_URL=http://victoria-logs:9428 \
  -p 8111:8000 localhost/victoria-logs-mcp:latest
```

It listens on `0.0.0.0:8000` and serves streamable HTTP at `/mcp`. `GET
/health` is unauthenticated and answers `{"status":"ok"}` — nothing more, so a
poller can say "up" without holding the token. `GET /ready` is open too and
adds whether the store answers: `200` with `{"status":"ready","store":"ok"}`,
or `503` with the reason, probed on a 3-second timeout and cached for five
seconds. Both paths answer `GET` and `HEAD`; any other method is a `405`.

Keep the container healthcheck off `/ready`: `healthcheck.py` probes `/mcp`
with an authenticated initialize, which tests the process and the endpoint
without touching the store. `/ready` stays `503` while VictoriaLogs is down or
restarting, so gating podman on it would restart this server in a loop instead
of reporting the store's state.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `VICTORIA_LOGS_URL` | `http://victoria-logs:9428` | Store base URL |
| `VICTORIA_LOGS_TIMEOUT` | `30` | Request timeout in seconds |
| `LOGS_MCP_TOKEN` | – | Bearer token. **Required** unless `MCP_ALLOW_ANONYMOUS=1` |
| `LOGS_HEALTH_MAX_AGE_S` | `900` | `get_log_health`'s "stale" threshold |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | Listen address |
| `MCP_ALLOW_ANONYMOUS` | – | Set to `1` to serve with no token (loud warning) |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `LOGS_MCP_URL` | `http://127.0.0.1:8000/mcp` | Where `healthcheck.py` probes |

Without `LOGS_MCP_TOKEN` the server refuses to start rather than serving
unauthenticated — the port is published on the LAN.

## Security model

This container shares `npm-network` with `victoria-logs`, `vector` and the rest
of the stack, and publishes its port on the LAN. It speaks only to the store's
read-only HTTP API, so a prompt-injected client cannot write, delete or change
retention.

The bearer token gates every request except `/health` and `/ready`, which
report status only; the comparison is constant-time. Startup fails without a
token unless `MCP_ALLOW_ANONYMOUS=1`.

The token is the whole boundary around the log history, and the history is
sensitive: every container's stdout is in there, including a token or a
password a service once echoed. Treat `LOGS_MCP_TOKEN` like the data itself,
and rotate it if it leaks (the stack's `scripts/rotate.sh` has a
`token:victoria-logs-mcp` target).

## Development

Dependencies are managed with [uv](https://docs.astral.sh/uv/) from
`pyproject.toml`; `uv.lock` is the exact closure (versions *and* hashes).

```sh
uv run pytest -q                # tests, no network
uv run ruff check .             # lint
uv run pyright                  # typecheck
uv run python scripts/smoke.py  # boots the real app on loopback
```

The store is replaced in tests by a scripted fake (`tests/conftest.py`), so the
suite pins the LogsQL each tool builds without a live VictoriaLogs.
