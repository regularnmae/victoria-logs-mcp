import asyncio
import hmac
import json
import logging
import math
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from victorialogs import LogsClient, LogsError, quote_ql

logger = logging.getLogger("victoria-logs-mcp")

# The SDK types log_level as a closed set, so an unrecognised LOG_LEVEL has to
# be resolved to one of its levels here -- the raw env string is not
# assignable, and an unknown level falling back to INFO beats the import-time
# raise that the SDK would otherwise produce.
_LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_LEVELS: tuple[_LogLevel, ...] = get_args(_LogLevel)


def _log_level() -> _LogLevel:
    requested = os.environ.get("LOG_LEVEL", "INFO").upper()
    return requested if requested in _LEVELS else "INFO"


def _env_float(name: str, default: float) -> float:
    """A float env var, or the default when unset or unparseable.

    A typo'd timeout should not stop the server from starting, so a bad value
    warns and falls back rather than raising at import.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ignoring non-numeric %s=%r; using %s", name, raw, default)
        return default


mcp = MCPServer(
    "VictoriaLogs",
    instructions=(
        "Read-only search over this host's log store, which holds every "
        "container's stdout and the host scripts' output, shipped from "
        "journald. search_logs runs a LogsQL query inside a time window; "
        "tail_logs is the same with no filter; top_sources counts entries per "
        "container; error_summary groups error-and-worse entries; "
        "list_field_values lists the containers, units or priorities that "
        "appear; get_context reads the entries around one instant; "
        "get_log_health says whether the pipeline is still shipping. Start "
        "with get_log_health or tail_logs when you do not know the field "
        "names. Retention is 14 days by default. Nothing here writes, "
        "deletes or changes anything."
    ),
    log_level=_log_level(),
)

# Every tool here only reads. Saying so lets a well-behaved client skip a
# confirmation prompt before running one of them. This is the host's own
# state, not an open-world network, hence open_world_hint=False.
_LOGS_TOOLS = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# Reached over the podman npm-network (the container name resolves without any
# host publishing); VICTORIA_LOGS_URL overrides. The store has no
# authentication of its own -- it is loopback-only on the host -- so this
# server is the authenticated front door to it.
VICTORIA_LOGS_URL = os.environ.get("VICTORIA_LOGS_URL", "http://victoria-logs:9428")
VICTORIA_LOGS_TIMEOUT = _env_float("VICTORIA_LOGS_TIMEOUT", 30.0)

# Set to "1" to serve the endpoint with no token at all. Without it a missing
# token is a startup error, not a warning.
MCP_ALLOW_ANONYMOUS = os.environ.get("MCP_ALLOW_ANONYMOUS", "") == "1"

# Unauthenticated liveness path, for pollers that hold no token.
HEALTH_PATH = "/health"

# Readiness path: /health says the process is up, this says the store answers.
# Deliberately separate -- a store outage must not fail a liveness probe, or
# the container restarts in a loop while VictoriaLogs recovers.
READY_PATH = "/ready"
READY_TIMEOUT = 3.0
# /ready is unauthenticated, and every probe is a request to the store, so the
# answer is cached for a few seconds: a poller hammering the path then costs
# one round trip per TTL instead of one per request.
READY_CACHE_TTL = 5.0
_ready_cache: tuple[float, dict[str, Any], int] | None = None
# Created lazily, inside the running loop: an asyncio.Lock binds to the loop
# that first awaits it, and this server has exactly one in production.
_ready_lock: asyncio.Lock | None = None

# The env var the bearer token is read from (also the name in the quadlet
# unit's EnvironmentFile and in the client configs).
TOKEN_ENV = "LOGS_MCP_TOKEN"

client = LogsClient(VICTORIA_LOGS_URL, timeout=VICTORIA_LOGS_TIMEOUT)

# --- output caps -------------------------------------------------------
# A log search is the one tool here whose answer is unbounded by nature, so
# every tool that returns entries caps both the number and the bytes. The
# defaults are what a model can read in one go; the maximum is a guard against
# a caller that asks for everything.
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
DEFAULT_SINCE = "15m"
MAX_QUERY_CHARS = 4096
MAX_MESSAGE_BYTES = 8192
MAX_RESPONSE_BYTES = 512 * 1024

# The health tool's own window and threshold. The threshold matches the
# monitor's MONITOR_LOGS_MAX_AGE_S default on purpose: "the pipeline is alive"
# should mean the same thing to an agent and to the alert.
HEALTH_WINDOW = "15m"
HEALTH_MAX_AGE_S = _env_float("LOGS_HEALTH_MAX_AGE_S", 900.0)

# LogsQL's relative time syntax: `15m`, `2h`, `7d`, `1w`. VictoriaLogs also
# accepts an RFC3339 instant (`_time:2026-11-02T10:00:00Z`), which is useful
# for "since that deploy", so both are allowed -- and nothing else, because
# `since` is interpolated into the query rather than sent as a parameter.
_DURATION = re.compile(r"^([0-9]+)(ms|s|m|h|d|w|y)$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")
# Seconds per LogsQL duration unit, for turning `since` into a bucket count.
_DURATION_SECONDS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "y": 31536000,
}
# A field name is sent as its own request parameter, so this is defence in
# depth rather than the only guard: it keeps a typo from becoming a 400 with
# a store-internal message.
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

# The fields `vector` labels every entry with (see the stack's vector.toml
# remap): the time, the message, where it came from, and what kind of line it
# is. Anything else a source attached (suricata's `http.url`, an nginx access
# line's `path`/`http_status`, say) is left out of the summary on purpose -- a
# raw row can carry twenty fields and fill a context window.
#
# `log_type` earns a place here because it is the one label that separates a
# file source's rows from the same container's stdout: nginx-proxy-manager
# writes its own output to journald AND its access/error logs to files, and both
# arrive as container=nginx-proxy-manager. journald rows never set it, so it
# appears only where a source does (npm_access, npm_error, npm_raw).
_CORE_FIELDS = ("_time", "container", "unit", "host", "priority", "log_type")


def _time_filter(since: str) -> str:
    """`since` as a LogsQL time filter, or a ValueError naming the problem."""
    value = since.strip()
    if _DURATION.match(value) or _TIMESTAMP.match(value):
        return f"_time:{value}"
    raise ValueError(
        f"since must be a duration like 15m, 2h or 7d, or an RFC3339 instant "
        f"like 2026-11-02T10:00:00Z, not {since!r}"
    )


def _limit(value: int) -> int:
    """Clamp a limit into the range the tools advertise."""
    return max(1, min(int(value), MAX_LIMIT))


def _duration_minutes(since: str) -> int | None:
    """How many one-minute buckets a `since` window covers, if it is a duration.

    An RFC3339 instant has no length, so the answer is None and the caller
    leaves the field out rather than inventing one.
    """
    match = _DURATION.match(since.strip())
    if not match:
        return None
    value, unit = match.groups()
    seconds = int(value) * _DURATION_SECONDS[unit]
    return max(1, math.ceil(seconds / 60))


def _render(payload: Any) -> str:
    """A tool result as text."""
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _truncate(text: str) -> str:
    """Cap one message at MAX_MESSAGE_BYTES of UTF-8, never splitting a char."""
    raw = text.encode("utf-8")
    if len(raw) <= MAX_MESSAGE_BYTES:
        return text
    kept = raw[:MAX_MESSAGE_BYTES].decode("utf-8", "ignore")
    return f"{kept} … [truncated {len(raw) - MAX_MESSAGE_BYTES} bytes]"


def _entry(row: dict[str, Any]) -> dict[str, str]:
    """One log row reduced to the fields a client can use."""
    entry: dict[str, str] = {}
    for key in _CORE_FIELDS:
        value = row.get(key)
        if isinstance(value, str) and value:
            entry[key] = value
    message = row.get("_msg")
    entry["message"] = _truncate(message if isinstance(message, str) else str(message or ""))
    return entry


def _entries(query: str, since: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Render rows under the per-response byte budget.

    The row cap (`limit`) is applied by the store; this is the second bound,
    because a single entry may carry a message the store's limit says nothing
    about. When the budget is hit the answer says so rather than silently
    returning a shorter list.
    """
    rendered: list[dict[str, str]] = []
    size = 0
    truncated = False
    for row in rows:
        entry = _entry(row)
        size += len(json.dumps(entry, indent=2, ensure_ascii=False))
        if size > MAX_RESPONSE_BYTES:
            truncated = True
            break
        rendered.append(entry)
    payload: dict[str, Any] = {
        "query": query,
        "since": since,
        "returned": len(rendered),
        "entries": rendered,
    }
    if truncated:
        payload["truncated"] = True
    return payload


def _parse_instant(value: str) -> datetime:
    """An RFC3339 instant (or a naive one, read as UTC)."""
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as err:
        raise ValueError(
            f"at must be an RFC3339 instant like 2026-11-02T10:00:00Z, not {value!r}"
        ) from err
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _format_instant(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_seconds(stamp: Any) -> int | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        then = _parse_instant(stamp)
    except ValueError:
        return None
    return max(0, int((datetime.now(UTC) - then).total_seconds()))


def _guard(label: str, action: Callable[[], Any]) -> str:
    """Run a tool body, turning its known failures into a tool error.

    `label` names the intent that failed, so a log line says what broke
    without every tool repeating the same four `except` clauses. Failures
    raise `ToolError`, which the SDK returns as `is_error=True` with the same
    text the model would otherwise have read -- so a client can tell "the
    store did not answer" from "nothing matched".
    """
    try:
        return _render(action())
    except ValueError as err:
        raise ToolError(str(err)) from err
    except LogsError as err:
        logger.warning("%s failed: %s", label, err.message)
        raise ToolError(err.message) from err
    except ToolError:
        raise
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        logger.exception("unexpected error in %s", label)
        raise ToolError(f"Unexpected error calling {label}: {err}") from err


def _search(query: str, since: str, limit: int) -> dict[str, Any]:
    window = _time_filter(since)
    combined = f"{window} {query.strip()}"
    return _entries(combined, since, client.query(combined, _limit(limit)))


def _tail(since: str, limit: int) -> dict[str, Any]:
    window = _time_filter(since)
    return _entries(window, since, client.query(window, _limit(limit)))


def _sources(since: str, limit: int) -> dict[str, Any]:
    query = (
        f"{_time_filter(since)} | stats by (container) count() as n"
        f" | sort by (n desc) | limit {_limit(limit)}"
    )
    return {"since": since, "sources": client.query(query)}


def _errors(since: str, limit: int) -> dict[str, Any]:
    """Error-and-worse lines folded into one-minute buckets, then per source.

    Two stages on purpose. Journald splits a multi-line traceback into one
    entry per line, so a raw count says how many *lines* were written, not how
    many times something went wrong -- one traceback of twenty lines looks
    like twenty incidents. Folding by minute first makes `minutes` the
    incident-shaped number (minutes in which at least one error appeared),
    while `entries` keeps the raw volume and `peak` says how dense the worst
    minute was. A source that logs one line every minute for the whole window
    is background; thirty minutes at 500 lines each is a fault.
    """
    query = (
        f"{_time_filter(since)} priority:in(emerg,alert,crit,err)"
        f" | stats by (_time:1m, container, unit) count() as n"
        f" | stats by (container, unit) count() as minutes, sum(n) as entries,"
        f" max(n) as peak"
        f" | sort by (minutes desc, entries desc) | limit {_limit(limit)}"
    )
    payload: dict[str, Any] = {"since": since, "groups": client.query(query)}
    window = _duration_minutes(since)
    if window is not None:
        payload["window_minutes"] = window
    return payload


def _field_values(field: str, since: str, limit: int) -> dict[str, Any]:
    name = field.strip()
    if not _FIELD.match(name):
        raise ValueError(
            f"field must be a field name like container, unit or priority, not {field!r}"
        )
    window = _time_filter(since)
    return {
        "field": name,
        "since": since,
        "values": client.field_values(name, window)[: _limit(limit)],
    }


def _health() -> dict[str, Any]:
    """Is the pipeline still shipping, and how old is the newest entry?

    Two questions, because they fail differently: an empty recent window is
    the pipeline dying (vector stopped, or the store is not being written),
    while an old newest entry is the same thing with the history intact. The
    answer is a report, not a tool error -- asking about a dead pipeline is a
    legitimate question.
    """
    recent = client.query(f"_time:{HEALTH_WINDOW} | stats count() as n")
    count = 0
    if recent:
        raw = recent[0].get("n")
        try:
            count = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            count = 0
    newest_rows = client.query("_time:14d | sort by (_time desc) | limit 1")
    newest = newest_rows[0].get("_time") if newest_rows else None
    age = _age_seconds(newest)
    if not isinstance(newest, str) or not newest:
        status = "empty"
        newest = None
    elif age is not None and age <= HEALTH_MAX_AGE_S:
        status = "ok"
    else:
        status = "stale"
    return {
        "status": status,
        "newest": newest,
        "age_seconds": age,
        "entries_in_window": count,
        "window": HEALTH_WINDOW,
        "max_age_seconds": int(HEALTH_MAX_AGE_S),
    }


def _context(
    at: str | None, container: str | None, before: int, after: int, limit: int
) -> dict[str, Any]:
    center = _parse_instant(at) if at else datetime.now(UTC)
    start = center - timedelta(seconds=before)
    end = center + timedelta(seconds=after)
    query = f"_time:[{_format_instant(start)}, {_format_instant(end)}]"
    if container:
        query += f" container:{quote_ql(container)}"
    query += " | sort by (_time)"
    return {
        "window": {"start": _format_instant(start), "end": _format_instant(end)},
        "query": query,
        "entries": [_entry(row) for row in client.query(query, _limit(limit))],
    }


@mcp.tool(structured_output=False, annotations=_LOGS_TOOLS)
def search_logs(
    query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_CHARS)],
    since: Annotated[str, Field(min_length=1, max_length=32)] = DEFAULT_SINCE,
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> str:
    """Search the log store with a LogsQL query, newest entries first.

    The window is applied for you: `since` is prepended as `_time:<since>`, so
    a query cannot accidentally scan the whole 14-day retention. Everything
    after that is LogsQL -- `container:"bitcoind"`, `unit:"monitor.service"`,
    `priority:in(emerg,alert,crit,err)`, `~"timeout"` for a regex, or a pipe
    such as `| stats by (container) count() as n`. See
    https://docs.victoriametrics.com/victorialogs/logsql/ for the language.

    Args:
        query: LogsQL filters and pipes, e.g. `container:"lightningd" ~"WARN"`.
        since: Window to search: `15m`, `2h`, `7d`, or an RFC3339 instant.
        limit: Maximum entries to return (1-1000, default 100).
    """
    return _guard("search_logs", lambda: _search(query, since, limit))


@mcp.tool(structured_output=False, annotations=_LOGS_TOOLS)
def tail_logs(
    since: Annotated[str, Field(min_length=1, max_length=32)] = "5m",
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> str:
    """Return the most recent entries from every source, newest first.

    The unfiltered view, for "what is happening right now" before you know
    which container or unit to search.

    Args:
        since: Window to read: `5m`, `1h`, `7d`, or an RFC3339 instant.
        limit: Maximum entries to return (1-1000, default 100).
    """
    return _guard("tail_logs", lambda: _tail(since, limit))


@mcp.tool(structured_output=False, annotations=_LOGS_TOOLS)
def top_sources(
    since: Annotated[str, Field(min_length=1, max_length=32)] = "1h",
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
) -> str:
    """Count log entries per container in the window, busiest first.

    The answer to "who is spamming the journal" -- a crash-looping service
    usually shows up here before it shows up in an alert.

    Args:
        since: Window to count: `1h`, `24h`, `7d`, or an RFC3339 instant.
        limit: Maximum containers to list (1-200, default 20).
    """
    return _guard("top_sources", lambda: _sources(since, limit))


@mcp.tool(structured_output=False, annotations=_LOGS_TOOLS)
def error_summary(
    since: Annotated[str, Field(min_length=1, max_length=32)] = "1h",
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
) -> str:
    """Summarise error-and-worse lines as bursts per container and unit.

    "Error and worse" is `emerg`, `alert`, `crit` and `err`. Each group reports
    `minutes` (one-minute buckets in which at least one such line appeared --
    the incident-shaped number), `entries` (the raw lines, so a multi-line
    traceback counts many) and `peak` (the busiest single minute). Host scripts
    have a `unit` and no `container`, so both are grouped; an empty string
    means the source did not set that field. `window_minutes` is the window's
    own length, so `minutes: 360` over `since:"6h"` means every minute.

    Args:
        since: Window to summarise: `1h`, `24h`, `7d`, or an RFC3339 instant.
        limit: Maximum groups to list (1-200, default 20).
    """
    return _guard("error_summary", lambda: _errors(since, limit))


@mcp.tool(structured_output=False, annotations=_LOGS_TOOLS)
def list_field_values(
    field: Annotated[str, Field(min_length=1, max_length=100)] = "container",
    since: Annotated[str, Field(min_length=1, max_length=32)] = "24h",
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> str:
    """List the distinct values of a field, most frequent first.

    Use it to find the exact name of a container or unit before searching --
    `bitcoind` and `bitcoin-node-mcp` are different sources, and a name that
    does not exist returns an empty search rather than an error.

    Args:
        field: Field to list: `container`, `unit`, `host` or `priority`.
        since: Window to look in: `24h`, `7d`, or an RFC3339 instant.
        limit: Maximum values to return (1-500, default 50).
    """
    return _guard("list_field_values", lambda: _field_values(field, since, limit))


@mcp.tool(structured_output=False, annotations=_LOGS_TOOLS)
def get_log_health() -> str:
    """Report whether the log pipeline is still shipping.

    `ok` means an entry arrived within the last 15 minutes and the newest entry
    is no older than the threshold (900 seconds by default); `stale` means the
    newest entry is older than that; `empty` means the store holds nothing at
    all. The stack's monitor fails on the same condition, so this is the
    question to ask before trusting an empty search.
    """
    return _guard("get_log_health", _health)


@mcp.tool(structured_output=False, annotations=_LOGS_TOOLS)
def get_context(
    at: Annotated[str | None, Field(max_length=40)] = None,
    container: Annotated[str | None, Field(max_length=200)] = None,
    before: Annotated[int, Field(ge=0, le=3600)] = 60,
    after: Annotated[int, Field(ge=0, le=3600)] = 60,
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> str:
    """Read the entries around one instant, oldest first.

    The timeline view: "what did everything do around 03:10?". Give the
    instant an alert or a log line mentioned and it returns the window around
    it, optionally only one container's entries.

    Args:
        at: RFC3339 instant to centre on (default: now).
        container: Only this container (default: every source).
        before: Seconds of history before the instant (0-3600, default 60).
        after: Seconds after the instant (0-3600, default 60).
        limit: Maximum entries to return (1-1000, default 100).
    """
    return _guard(
        "get_context", lambda: _context(at, container, before, after, limit)
    )


async def _readiness() -> tuple[dict[str, Any], int]:
    """`/ready`'s answer, cached for READY_CACHE_TTL seconds.

    The path is unauthenticated, so without the cache any client could turn
    one HTTP request into one store request. The cache is a plain module
    global: a five-second-old liveness answer is still the right answer. The
    lock means a burst of concurrent requests produces one probe rather than
    one each; the later callers read its answer.
    """
    global _ready_cache, _ready_lock
    if _ready_lock is None:
        _ready_lock = asyncio.Lock()
    async with _ready_lock:
        cached = _ready_cache
        if cached is not None and time.monotonic() - cached[0] < READY_CACHE_TTL:
            return cached[1], cached[2]
        body, status_code = await _probe_readiness()
        _ready_cache = (time.monotonic(), body, status_code)
        return body, status_code


async def _probe_readiness() -> tuple[dict[str, Any], int]:
    """Ask the store for its health, off the event loop with a short timeout."""
    try:
        answer = await asyncio.to_thread(client.health)
    except LogsError as err:
        logger.warning("/ready: VictoriaLogs is not answering: %s", err.message)
        return {"status": "unready", "error": err.message}, 503
    except Exception as err:  # noqa: BLE001 - reported, never raised at a probe
        logger.exception("/ready: unexpected error")
        return {"status": "unready", "error": str(err)}, 503
    if answer.upper() != "OK":
        return {
            "status": "unready",
            "error": f"VictoriaLogs /health answered {answer[:80]!r}",
        }, 503
    return {"status": "ready", "store": "ok"}, 200


async def _json_response(
    scope: Any,
    receive: Any,
    send: Any,
    body: dict[str, Any],
    status_code: int,
    headers: dict[str, str] | None = None,
) -> None:
    """Send one JSON response, with no body for a HEAD request."""
    from starlette.responses import JSONResponse

    response = JSONResponse(body, status_code=status_code, headers=headers)
    if scope.get("method") == "HEAD":
        # JSONResponse has already set Content-Length for the body it would
        # have sent; HEAD promises the headers and no body.
        response.body = b""
    await response(scope, receive, send)


class BearerAuthMiddleware:
    """Pure-ASGI bearer auth in front of the streamable-HTTP app.

    The endpoint is published on the LAN, so a missing token is a hard error
    rather than a warning: serving every tool unauthenticated for as long as
    the env var is absent is not a failure mode worth keeping. Set
    MCP_ALLOW_ANONYMOUS=1 to run it open on purpose, which is what debugging
    from inside npm-network wants.

    GET /health stays unauthenticated so a poller can say "up" without holding
    the token; it says nothing else. GET /ready does the same and adds whether
    the store answers, on a short timeout, for the same reason. Both answer
    only GET and HEAD -- a write method on a status path is a client bug, not
    something to answer 200 to.
    """

    def __init__(self, app: Any, token: str | None) -> None:
        self.app = app
        self.token = token or ""
        if not self.token:
            if not MCP_ALLOW_ANONYMOUS:
                raise RuntimeError(
                    f"{TOKEN_ENV} is not set, and this endpoint is published "
                    "on the LAN. Set the token, or set MCP_ALLOW_ANONYMOUS=1 "
                    "to serve without authentication."
                )
            logger.warning(
                "%s not set and MCP_ALLOW_ANONYMOUS=1: endpoint is UNAUTHENTICATED",
                TOKEN_ENV,
            )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") in (HEALTH_PATH, READY_PATH):
            if scope.get("method") not in ("GET", "HEAD"):
                await _json_response(
                    scope,
                    receive,
                    send,
                    {"error": "method not allowed"},
                    405,
                    {"allow": "GET, HEAD"},
                )
                return
            if scope.get("path") == HEALTH_PATH:
                await _json_response(scope, receive, send, {"status": "ok"}, 200)
                return
            body, status_code = await _readiness()
            await _json_response(scope, receive, send, body, status_code)
            return

        if self.token and not self._authorised(scope):
            await _json_response(scope, receive, send, {"error": "unauthorized"}, 401)
            return

        await self.app(scope, receive, send)

    def _authorised(self, scope: Any) -> bool:
        """Compare the presented credential in constant time."""
        headers = dict(scope.get("headers") or [])
        header = headers.get(b"authorization", b"").decode("latin-1").strip()
        # RFC 7235 makes the scheme case-insensitive; a client sending
        # "bearer <token>" is not a typo worth a 401, and neither is a stray
        # space inside the header.
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(presented.strip().encode(), self.token.encode())


if __name__ == "__main__":
    import uvicorn

    # Accept any Host header: DNS-rebinding protection is disabled because the
    # bearer token gates every request.
    app = BearerAuthMiddleware(
        mcp.streamable_http_app(
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )
        ),
        os.environ.get(TOKEN_ENV),
    )

    # 0.0.0.0 is what the container publishes; loopback is what running it for
    # a smoke test wants, hence the env overrides.
    uvicorn.run(
        app,
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8000")),
        log_level=_log_level().lower(),
    )
