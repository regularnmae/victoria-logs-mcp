"""Shared fixtures.

Every test talks to the server through the SDK's in-memory client, so no test
opens a socket. VictoriaLogs itself is replaced by `fake_client`, which records
the LogsQL queries a tool built and answers from a per-substring script -- the
tools, their query construction and their output caps are what is under test,
not a live store.

The in-memory session is opened inside `anyio.run` rather than by an async
fixture: pytest 9 refuses to resolve an async fixture for a sync test, and
these tests are sync on purpose (a tool call is one blocking round trip).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anyio
import pytest
from mcp import Client

import server


def _tool_call(name: str, arguments: dict[str, Any]) -> Any:
    """One tool call over a fresh in-memory MCP session."""

    async def _run() -> Any:
        async with Client(server.mcp, raise_exceptions=True) as c:
            return await c.call_tool(name, arguments)

    return anyio.run(_run)


class FakeLogsClient:
    """Stands in for `server.client`.

    `answers` maps a substring of the query to the rows to return (or an
    exception to raise), so a test can script the two questions
    `get_log_health` asks differently. A query with no matching entry answers
    `rows`, which defaults to empty -- an empty store is a legitimate answer,
    not a failure.
    """

    def __init__(self) -> None:
        self.queries: list[tuple[str, int | None]] = []
        self.field_calls: list[tuple[str, str]] = []
        self.health_calls = 0
        self.answers: dict[str, Any] = {}
        self.rows: list[dict[str, Any]] = []
        self.values: list[dict[str, Any]] = []
        self.health_answer: Any = "OK"

    def query(self, query: str, limit: int | None = None) -> list[dict[str, Any]]:
        self.queries.append((query, limit))
        for needle, answer in self.answers.items():
            if needle in query:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return self.rows

    def field_values(self, field: str, query: str) -> list[dict[str, Any]]:
        self.field_calls.append((field, query))
        return self.values

    def health(self) -> str:
        self.health_calls += 1
        if isinstance(self.health_answer, Exception):
            raise self.health_answer
        return self.health_answer


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeLogsClient:
    """Replace the live store with a scripted fake, restoring it after."""
    fake = FakeLogsClient()
    monkeypatch.setattr(server.client, "query", fake.query)
    monkeypatch.setattr(server.client, "field_values", fake.field_values)
    monkeypatch.setattr(server.client, "health", fake.health)
    return fake


@pytest.fixture
def call() -> Callable[..., Any]:
    """Call a tool and hand back the text the model would read."""

    def _call(name: str, **arguments: Any) -> str:
        result = _tool_call(name, arguments)
        assert not result.is_error, f"tool {name} failed: {result.content}"
        return result.content[0].text  # type: ignore[union-attr]

    return _call


@pytest.fixture
def error_call() -> Callable[..., Any]:
    """Call a tool that is expected to fail, and return its error text.

    A failure is an `is_error=True` result rather than a successful one, so a
    client can tell "the store did not answer" from "nothing matched".
    """

    def _call(name: str, **arguments: Any) -> str:
        result = _tool_call(name, arguments)
        assert result.is_error, f"tool {name} succeeded, want a tool error"
        return result.content[0].text  # type: ignore[union-attr]

    return _call
