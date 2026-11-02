"""Minimal VictoriaLogs client for the LogsQL HTTP API.

Stdlib only, for the same reason `rpc.py` in the node server is: this process
talks to one log store on the podman network, so its whole transport is an
HTTP GET with a URL-encoded LogsQL query -- no third-party client. Every call
opens a connection, reads the answer and closes it; the store is stateless
between requests, so every tool body stays synchronous.

One exception type covers every failure -- an HTTP error from the store, a
connection failure, a timeout, a body that is not what the API promises --
because that is what a tool body needs to catch. `LogsError.status` carries
the store's HTTP status when it sent one.

The API shapes this module pins (all read-only):

  /select/logsql/query          one JSON object per line (NDJSON)
  /select/logsql/field_values   {"values": [{"value": ..., "hits": ...}]}
  /health                       the literal text "OK"

Queries are built by the caller; the two helpers here are the only place a
value is quoted, so an operator-supplied container name or regex cannot break
out of a LogsQL phrase.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_TIMEOUT = 30.0

# A response is read into memory whole, so it needs a ceiling: the store is on
# the podman network, but a wedged peer or a query that forgot its limit must
# not be able to make this process allocate without bound. The server's tools
# cap `limit` long before this; this is the transport's backstop.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class LogsError(Exception):
    """A failed LogsQL request.

    `message` is the store's own text (or this module's description of a
    transport failure); `status` is the HTTP status when the store answered.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def escape_ql(value: str) -> str:
    """Escape a value for a double-quoted LogsQL phrase.

    LogsQL uses backslash escapes inside `"..."`, so a backslash and a quote
    are the only two characters that must be escaped. Newlines and control
    characters are left alone: they are valid inside a phrase and the store
    matches them literally.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def quote_ql(value: str) -> str:
    """A LogsQL phrase, quoted and escaped."""
    return f'"{escape_ql(value)}"'


class LogsClient:
    """One log store, addressed by its base URL."""

    def __init__(self, url: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, str]) -> bytes:
        """GET one API path, returning the raw body.

        Every failure becomes a `LogsError`: a store that answers 400 has a
        parse error worth showing verbatim, and one that is unreachable has
        only the URL to report.
        """
        query = urllib.parse.urlencode(params)
        url = f"{self.url}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/stream+json, application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as err:
            detail = _error_detail(err)
            raise LogsError(
                f"VictoriaLogs refused the query ({err.code}): {detail}", err.code
            ) from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise LogsError(f"VictoriaLogs is unreachable at {self.url}: {err}") from err
        if len(body) > MAX_RESPONSE_BYTES:
            raise LogsError(
                "VictoriaLogs returned more than "
                f"{MAX_RESPONSE_BYTES} bytes; narrow the query or lower the limit"
            )
        return body

    def query(self, query: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Run a LogsQL query and return its rows.

        The `/query` endpoint streams one JSON object per line, so the body is
        split rather than parsed whole. A line that is not an object is a
        protocol error, never silently skipped: a partial answer that looks
        complete is worse than a failed call.
        """
        params = {"query": query}
        if limit is not None:
            params["limit"] = str(limit)
        body = self._get("/select/logsql/query", params)
        rows: list[dict[str, Any]] = []
        for line in body.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as err:
                raise LogsError(
                    f"VictoriaLogs returned a line that is not JSON: {line[:200]!r}"
                ) from err
            if not isinstance(row, dict):
                raise LogsError(
                    f"VictoriaLogs returned a row that is not an object: {line[:200]!r}"
                )
            rows.append(row)
        return rows

    def field_values(self, field: str, query: str) -> list[dict[str, Any]]:
        """Distinct values of `field` within `query`, most frequent first.

        The API's own `limit` parameter is deliberately not sent: on this
        store it makes the endpoint return an arbitrary sample with `hits: 0`
        rather than the most frequent values, which is the opposite of what a
        caller asking for the top containers wants. The caller slices instead.
        """
        body = self._get(
            "/select/logsql/field_values",
            {"field": field, "query": query},
        )
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError as err:
            raise LogsError(
                f"VictoriaLogs returned an unparseable field_values body: {body[:200]!r}"
            ) from err
        values = payload.get("values") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise LogsError(
                "VictoriaLogs returned an unexpected field_values body "
                f"(no values array): {body[:200]!r}"
            )
        return [value for value in values if isinstance(value, dict)]

    def health(self) -> str:
        """The store's liveness answer (the literal `OK` when it is serving)."""
        return self._get("/health", {}).decode("utf-8", "replace").strip()


def _error_detail(err: urllib.error.HTTPError) -> str:
    """The store's error text, or the reason phrase when the body is empty."""
    try:
        body = err.read(2048).decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001 - a body that cannot be read adds nothing
        body = ""
    return body or (err.reason if isinstance(err.reason, str) else "no detail")
