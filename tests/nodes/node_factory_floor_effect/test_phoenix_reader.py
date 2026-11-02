# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's Phoenix trace row: count and newest span age from the Phoenix GraphQL API (OMN-22097)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from omnimarket.models.model_floor_phoenix_traces import ModelFloorPhoenixTraces
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_effect.phoenix_reader import (
    load_phoenix_traces,
)
from tests.nodes.node_factory_floor_compute.support import request

pytestmark = pytest.mark.unit

NEWEST = datetime(2026, 11, 2, 13, 0, 0, tzinfo=UTC)


def _projects(*nodes: dict[str, object]) -> dict[str, object]:
    return {"data": {"projects": {"edges": [{"node": node} for node in nodes]}}}


class _Phoenix:
    """A local stand-in for Phoenix's GraphQL endpoint serving one canned document."""

    def __init__(self, document: dict[str, object], status: int = 200) -> None:
        outer = self
        self.posts: list[tuple[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                outer.posts.append((self.path, self.rfile.read(length).decode()))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(document).encode())

            def log_message(self, format: str, *args: object) -> None:
                return None

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> _Phoenix:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def phoenix_with_traces() -> Iterator[_Phoenix]:
    document = _projects(
        {"name": "default", "traceCount": 7, "endTime": "2026-11-02T13:00:00+00:00"},
        {"name": "other", "traceCount": 5, "endTime": "2026-11-02T12:40:00+00:00"},
    )
    with _Phoenix(document) as phoenix:
        yield phoenix


def test_no_url_is_not_read() -> None:
    assert load_phoenix_traces("", NEWEST) is None


def test_count_sums_projects_and_age_is_from_the_newest_end_time(
    phoenix_with_traces: _Phoenix,
) -> None:
    reading = load_phoenix_traces(
        phoenix_with_traces.url, NEWEST + timedelta(seconds=90)
    )

    assert reading == ModelFloorPhoenixTraces(
        state="read",
        trace_count=12,
        newest_span_at="2026-11-02T13:00:00Z",
        newest_span_age_seconds=90,
        read_at="2026-11-02T13:01:30Z",
    )
    (path, body), *_ = phoenix_with_traces.posts
    assert path == "/graphql"
    assert "traceCount" in body


def test_age_grows_while_no_span_arrives(phoenix_with_traces: _Phoenix) -> None:
    first = load_phoenix_traces(phoenix_with_traces.url, NEWEST + timedelta(minutes=1))
    later = load_phoenix_traces(phoenix_with_traces.url, NEWEST + timedelta(minutes=6))

    assert first is not None
    assert later is not None
    assert first.trace_count == later.trace_count
    assert first.newest_span_age_seconds == 60
    assert later.newest_span_age_seconds == 360


def test_a_phoenix_with_no_trace_reads_zero_without_an_age() -> None:
    document = _projects({"name": "default", "traceCount": 0, "endTime": None})
    with _Phoenix(document) as phoenix:
        reading = load_phoenix_traces(phoenix.url, NEWEST)

    assert reading is not None
    assert reading.state == "read"
    assert reading.trace_count == 0
    assert reading.newest_span_age_seconds is None


def test_an_unreachable_phoenix_is_unread_and_names_no_address() -> None:
    with _Phoenix(_projects()) as phoenix:
        url = phoenix.url
    reading = load_phoenix_traces(url, NEWEST)

    assert reading is not None
    assert reading.state == "unread"
    assert reading.trace_count == 0
    assert url not in reading.detail
    assert "127.0.0.1" not in reading.detail
    assert reading.detail.startswith("the Phoenix read failed")


def test_a_malformed_document_is_unread() -> None:
    with _Phoenix({"errors": [{"message": "no"}]}) as phoenix:
        reading = load_phoenix_traces(phoenix.url, NEWEST)

    assert reading is not None
    assert reading.state == "unread"


def test_a_non_http_url_is_refused_without_a_read() -> None:
    reading = load_phoenix_traces("file:///etc/passwd", NEWEST)

    assert reading is not None
    assert reading.state == "unread"
    assert "passwd" not in reading.detail


def test_the_floor_carries_the_reading_for_the_panel() -> None:
    reading = ModelFloorPhoenixTraces(
        state="read",
        trace_count=12,
        newest_span_at="2026-11-02T13:00:00Z",
        newest_span_age_seconds=90,
        read_at="2026-11-02T13:01:30Z",
    )

    assert (
        compute_factory_floor(request(phoenix_traces=reading)).floor.phoenix_traces
        == reading
    )


def test_the_floor_draws_an_unsupplied_reading_unread() -> None:
    shown = compute_factory_floor(request()).floor.phoenix_traces

    assert shown.state == "unread"
    assert shown.trace_count == 0
