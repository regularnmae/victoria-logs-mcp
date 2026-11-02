"""The stdlib LogsQL client: transport, NDJSON parsing and error mapping.

`urllib.request.urlopen` is monkeypatched, so the contract under test is what
this module sends and what it does with the answer -- no store is involved.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

import victorialogs
from victorialogs import LogsClient, LogsError, escape_ql, quote_ql


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def read(self, size: int = -1) -> bytes:
        return self.body if size is None or size < 0 else self.body[:size]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _capture(monkeypatch: pytest.MonkeyPatch, response: object) -> list[str]:
    """Answer every urlopen with `response`, recording the URL asked for."""
    urls: list[str] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float | None = None):
        urls.append(request.full_url)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return urls


def test_escape_ql_escapes_the_two_significant_characters() -> None:
    assert escape_ql(r'a"b\c') == r'a\"b\\c'
    assert quote_ql('container"x') == '"container\\"x"'


def test_query_parses_one_object_per_line(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b'{"_time":"2026-11-02T10:00:00Z","_msg":"a"}\n\n{"_msg":"b"}\n'
    urls = _capture(monkeypatch, _Response(body))
    rows = LogsClient("http://store:9428").query('_time:15m', limit=5)
    assert [row["_msg"] for row in rows] == ["a", "b"]
    parsed = urllib.parse.urlparse(urls[0])
    assert parsed.path == "/select/logsql/query"
    params = urllib.parse.parse_qs(parsed.query)
    assert params == {"query": ["_time:15m"], "limit": ["5"]}


def test_query_without_a_limit_sends_no_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    urls = _capture(monkeypatch, _Response(b""))
    assert LogsClient("http://store:9428").query("_time:15m") == []
    params = urllib.parse.parse_qs(urllib.parse.urlparse(urls[0]).query)
    assert "limit" not in params


def test_a_non_json_line_is_a_protocol_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, _Response(b"not json\n"))
    with pytest.raises(LogsError, match="not JSON"):
        LogsClient("http://store:9428").query("_time:15m")


def test_a_row_that_is_not_an_object_is_a_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(monkeypatch, _Response(b"[1,2,3]\n"))
    with pytest.raises(LogsError, match="not an object"):
        LogsClient("http://store:9428").query("_time:15m")


def test_field_values_reads_the_values_array(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"values": [{"value": "bitcoind", "hits": 4}]}).encode()
    urls = _capture(monkeypatch, _Response(body))
    values = LogsClient("http://store:9428").field_values("container", "_time:1h")
    assert values == [{"value": "bitcoind", "hits": 4}]
    parsed = urllib.parse.urlparse(urls[0])
    assert parsed.path == "/select/logsql/field_values"
    params = urllib.parse.parse_qs(parsed.query)
    assert params == {"field": ["container"], "query": ["_time:1h"]}


def test_field_values_without_an_array_is_a_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(monkeypatch, _Response(b'{"nope":true}'))
    with pytest.raises(LogsError, match="unexpected field_values body"):
        LogsClient("http://store:9428").field_values("container", "_time:1h")


def test_health_returns_the_store_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, _Response(b"OK\n"))
    assert LogsClient("http://store:9428").health() == "OK"


def test_an_http_error_carries_the_store_text(monkeypatch: pytest.MonkeyPatch) -> None:
    error = urllib.error.HTTPError(
        "http://store:9428/select/logsql/query",
        400,
        "Bad Request",
        email.message.Message(),
        io.BytesIO(b'cannot parse `query` arg'),
    )
    _capture(monkeypatch, error)
    with pytest.raises(LogsError) as raised:
        LogsClient("http://store:9428").query("badsyntax(")
    assert raised.value.status == 400
    assert "cannot parse" in raised.value.message


def test_an_unreachable_store_is_reported_with_its_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(monkeypatch, urllib.error.URLError("connection refused"))
    with pytest.raises(LogsError, match="unreachable at http://store:9428"):
        LogsClient("http://store:9428").query("_time:15m")


def test_a_response_over_the_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(victorialogs, "MAX_RESPONSE_BYTES", 8)
    _capture(monkeypatch, _Response(b'{"_msg":"way too long for the cap"}\n'))
    with pytest.raises(LogsError, match="returned more than"):
        LogsClient("http://store:9428").query("_time:15m")
