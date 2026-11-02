"""What the server advertises in `tools/list`.

The schema is the contract a client validates against. Every tool here is
text-only and returns `str`, so every tool must pass `structured_output=False`:
a tool returning `str` that does not would publish an output schema and send
its payload twice, once as text and once as structured content.
"""

from __future__ import annotations

import anyio
import pytest
from mcp import Client

import server

TOOLS = {
    "search_logs",
    "tail_logs",
    "top_sources",
    "error_summary",
    "list_field_values",
    "get_log_health",
    "get_context",
}


@pytest.fixture
def advertised() -> dict:
    """The tools/list result, fetched once per test through a real client."""

    async def _list():
        async with Client(server.mcp) as c:
            return await c.list_tools()

    listing = anyio.run(_list)
    return {tool.name: tool for tool in listing.tools}


def test_every_tool_is_advertised(advertised: dict) -> None:
    assert set(advertised) == TOOLS


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_text_tools_publish_no_output_schema(advertised: dict, name: str) -> None:
    assert advertised[name].output_schema is None, (
        f"{name} publishes an output schema, which means its str return is "
        "serialised twice; pass structured_output=False to @mcp.tool()"
    )


def test_every_tool_declares_itself_read_only(advertised: dict) -> None:
    for name, tool in advertised.items():
        assert tool.annotations is not None, f"{name} publishes no annotations"
        assert tool.annotations.read_only_hint is True, f"{name} is not marked read-only"
        # This is the host's own state, not an open-world network.
        assert tool.annotations.open_world_hint is False, f"{name} claims open-world"


def test_search_limits_are_bounded(advertised: dict) -> None:
    properties = advertised["search_logs"].input_schema["properties"]
    assert properties["limit"]["maximum"] == 1000
    assert properties["limit"]["minimum"] == 1
    assert properties["limit"]["default"] == 100


def test_the_context_window_is_bounded(advertised: dict) -> None:
    properties = advertised["get_context"].input_schema["properties"]
    assert properties["before"]["maximum"] == 3600
    assert properties["after"]["maximum"] == 3600
    # A context read defaults to "now", so the instant is optional.
    assert "at" not in advertised["get_context"].input_schema.get("required", [])
