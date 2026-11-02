"""The tools: the LogsQL they build, the caps they apply, the report they give.

`fake_client` records every query, so these tests pin the exact LogsQL a tool
sends -- the store's language is the contract, and a filter that silently
changes shape (a missing window, an unescaped quote) is the bug worth catching
without a live store.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import server
from victorialogs import LogsError


def _rows(call: str) -> dict[str, Any]:
    return json.loads(call)


def test_search_logs_prepends_the_window(call, fake_client) -> None:
    _rows(call("search_logs", query='container:"lightningd"', since="1h", limit=10))
    assert fake_client.queries == [('_time:1h container:"lightningd"', 10)]


def test_search_logs_accepts_an_rfc3339_instant(call, fake_client) -> None:
    call("search_logs", query="priority:err", since="2026-11-02T10:00:00Z")
    assert fake_client.queries[0][0] == "_time:2026-11-02T10:00:00Z priority:err"


def test_search_logs_refuses_a_window_it_cannot_parse(error_call, fake_client) -> None:
    text = error_call("search_logs", query="x", since="yesterday")
    assert "since must be a duration" in text
    assert fake_client.queries == []


def test_search_logs_refuses_an_empty_query(error_call) -> None:
    # The schema's min_length catches this before the tool body runs.
    text = error_call("search_logs", query="")
    assert "query" in text.lower()


def test_a_long_message_is_truncated(call, fake_client) -> None:
    fake_client.rows = [{"_time": "2026-11-02T10:00:00Z", "_msg": "x" * 20_000}]
    payload = _rows(call("search_logs", query="x"))
    message = payload["entries"][0]["message"]
    assert "truncated" in message
    assert len(message) < 20_000


def test_only_the_core_fields_are_returned(call, fake_client) -> None:
    fake_client.rows = [
        {
            "_time": "2026-11-02T10:00:00Z",
            "_msg": "hello",
            "container": "bitcoind",
            "priority": "err",
            "http.url": "/health",
        }
    ]
    payload = _rows(call("search_logs", query="x"))
    assert payload["entries"][0] == {
        "_time": "2026-11-02T10:00:00Z",
        "container": "bitcoind",
        "priority": "err",
        "message": "hello",
    }


def test_the_response_budget_stops_a_huge_answer(call, fake_client, monkeypatch) -> None:
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 120)
    fake_client.rows = [{"_msg": "y" * 100} for _ in range(10)]
    payload = _rows(call("search_logs", query="x", limit=10))
    assert payload["truncated"] is True
    assert payload["returned"] == len(payload["entries"]) < 10


def test_tail_logs_sends_only_the_window(call, fake_client) -> None:
    call("tail_logs", since="5m", limit=3)
    assert fake_client.queries == [("_time:5m", 3)]


def test_top_sources_counts_by_container(call, fake_client) -> None:
    call("top_sources", since="24h", limit=5)
    query, limit = fake_client.queries[0]
    assert query == (
        "_time:24h | stats by (container) count() as n"
        " | sort by (n desc) | limit 5"
    )
    assert limit is None


def test_error_summary_selects_error_and_worse(call, fake_client) -> None:
    call("error_summary", since="2h", limit=7)
    query = fake_client.queries[0][0]
    assert "priority:in(emerg,alert,crit,err)" in query
    assert "stats by (container, unit)" in query
    assert query.endswith("limit 7")


def test_list_field_values_asks_the_store_for_values(call, fake_client) -> None:
    fake_client.values = [{"value": "bitcoind", "hits": 3}]
    payload = _rows(call("list_field_values", field="container", since="24h", limit=5))
    assert fake_client.field_calls == [("container", "_time:24h")]
    assert payload["values"] == [{"value": "bitcoind", "hits": 3}]


def test_list_field_values_slices_the_frequent_first_list(call, fake_client) -> None:
    # The store returns every value sorted by hits; the tool caps the answer.
    fake_client.values = [{"value": f"c{i}", "hits": 10 - i} for i in range(5)]
    payload = _rows(call("list_field_values", field="container", limit=2))
    assert [value["value"] for value in payload["values"]] == ["c0", "c1"]


def test_list_field_values_refuses_a_field_that_is_not_a_name(
    error_call, fake_client
) -> None:
    text = error_call("list_field_values", field='container") or (')
    assert "field must be a field name" in text
    assert fake_client.field_calls == []


def _stamp(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_log_health_is_ok_when_entries_are_recent(call, fake_client) -> None:
    fake_client.answers = {
        "_time:15m | stats": [{"n": "42"}],
        "_time:14d": [{"_time": _stamp(timedelta(seconds=30))}],
    }
    payload = _rows(call("get_log_health"))
    assert payload["status"] == "ok"
    assert payload["entries_in_window"] == 42
    assert payload["age_seconds"] <= 60


def test_log_health_is_stale_when_the_newest_entry_is_old(call, fake_client) -> None:
    fake_client.answers = {
        "_time:15m | stats": [{"n": "0"}],
        "_time:14d": [{"_time": _stamp(timedelta(hours=2))}],
    }
    payload = _rows(call("get_log_health"))
    assert payload["status"] == "stale"
    assert payload["age_seconds"] >= 7200


def test_log_health_is_empty_when_the_store_holds_nothing(call, fake_client) -> None:
    fake_client.answers = {"_time:15m | stats": [{"n": "0"}], "_time:14d": []}
    payload = _rows(call("get_log_health"))
    assert payload == {
        "status": "empty",
        "newest": None,
        "age_seconds": None,
        "entries_in_window": 0,
        "window": "15m",
        "max_age_seconds": 900,
    }


def test_context_builds_a_window_and_escapes_the_container(call, fake_client) -> None:
    payload = _rows(
        call(
            "get_context",
            at="2026-11-02T10:00:00Z",
            container='a"b',
            before=60,
            after=30,
            limit=10,
        )
    )
    assert payload["window"] == {
        "start": "2026-11-02T09:59:00Z",
        "end": "2026-11-02T10:00:30Z",
    }
    assert payload["query"] == (
        '_time:[2026-11-02T09:59:00Z, 2026-11-02T10:00:30Z]'
        ' container:"a\\"b" | sort by (_time)'
    )
    assert fake_client.queries[0][1] == 10


def test_context_defaults_to_now(call, fake_client) -> None:
    payload = _rows(call("get_context"))
    assert payload["query"].startswith("_time:[")
    assert payload["query"].endswith(" | sort by (_time)")


def test_context_refuses_an_instant_it_cannot_parse(error_call) -> None:
    text = error_call("get_context", at="yesterday")
    assert "at must be an RFC3339 instant" in text


def test_a_store_failure_is_a_tool_error(error_call, fake_client) -> None:
    fake_client.answers = {"_time:15m": LogsError("VictoriaLogs refused the query (400)")}
    text = error_call("search_logs", query="x")
    assert "VictoriaLogs refused the query (400)" in text


@pytest.mark.parametrize("limit", [0, 1001])
def test_the_schema_rejects_an_out_of_range_limit(error_call, limit) -> None:
    text = error_call("search_logs", query="x", limit=limit)
    assert "limit" in text.lower()
