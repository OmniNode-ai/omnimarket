# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus session read route: every page of a window inside one reusable connection (OMN-23977).

The reader's command route spawns one ``onex ledger query --stdin`` process per page. Each page
paid the process start and the Kafka connect again, so a 25 hour window (11,600 rows) took 532 s
on the dev bus and ended unreadable at the page cap. The session route enters the infra query
client once and reads every page from it. The tests charge the measured costs on a virtual clock
(no sleep) and hold the window to the queue-status step budget; the control is the per-page
reading the fix replaces. The seam test runs the real query node in process so a request key the
node does not know, or an answer shape it changes, fails here.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Self

import pytest

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    BusSessionPointRead,
    InMemoryEventSink,
    answer_from_json,
    build_point_read,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderEventKind,
    EnumReaderStatus,
    ModelBusSessionReadRoute,
    ModelPointQuery,
)
from omnimarket.nodes.node_work_ledger_reader_effect.overlay import load_overlay
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_lines_only import _node
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_rows_window import (
    EXAMPLE,
    MSG_ROW,
    SEAM_NOW,
    STATUS_ROWS,
    TOPICS,
    overlay,
    reader,
    rows_request,
)

pytestmark = pytest.mark.unit

# The queue-status step budget: one window read must finish inside this many seconds.
STEP_BUDGET_S = 20
# Measured on the dev bus 2026-11-12: 3.7 s process start plus 5 s Kafka connect per session.
CONNECT_S = 8.7
# Measured 0.3 to 1.7 s of node time per page; the virtual clock charges 1.0.
PAGE_S = 1.0
# The query node's max_answer_bytes: a page over it is refused.
BOUND = 917_516
# A 25 hour window on the dev bus: 11,600 rows; a lines-only row is about 600 bytes, a row that
# carries its typed record as well about 2,200.
ROWS_25H = 11_600
LINE_BYTES = 600
ROW_BYTES = 2_200


class FakeBusClient:
    """The infra client on a virtual clock: entering costs CONNECT_S, each query PAGE_S.

    It serves ROWS_25H synthetic lines page by page (the page index is the cursor) and, like the
    node, refuses a page whose bytes exceed BOUND. ``fail_on_query`` makes that query raise.
    """

    def __init__(self, clock: list[float], *, fail_on_query: int | None = None) -> None:
        self._clock = clock
        self._fail_on_query = fail_on_query
        self.entered = 0
        self.queries = 0
        self.closed = False

    async def __aenter__(self) -> Self:
        self.entered += 1
        self._clock.append(CONNECT_S)
        return self

    async def __aexit__(
        self, exc_type: object, exc_value: object, traceback: object
    ) -> None:
        self.closed = True

    async def query(
        self, request: dict[str, Any], *, timeout_s: float
    ) -> dict[str, Any]:
        self.queries += 1
        self._clock.append(PAGE_S)
        if self.queries == self._fail_on_query:
            raise RuntimeError("boom")
        page_size = int(request["page_size"])
        if page_size * (LINE_BYTES if request.get("lines_only") else ROW_BYTES) > BOUND:
            raise RuntimeError("answer over the bound")
        page = int(request.get("cursor") or 0)
        start = page * page_size
        stop = min(start + page_size, ROWS_25H)
        return {
            "correlation_id": request["correlation_id"],
            "status": "ok",
            "lines": [f"row-{i}" for i in range(start, stop)],
            "matched": ROWS_25H,
            "watermark": SEAM_NOW.isoformat().replace("+00:00", "Z"),
            "next_cursor": str(page + 1) if stop < ROWS_25H else None,
            "page_size": page_size,
        }


class PerPageSessionRead:
    """The reading the fix replaces: a fresh session, and its connect cost, for every page."""

    def __init__(self, clock: list[float]) -> None:
        self._clock = clock

    def read(self, query: ModelPointQuery):
        one_page = BusSessionPointRead(lambda: FakeBusClient(self._clock), 60)
        try:
            return one_page.read(query)
        finally:
            one_page.close()


class NodeBusClient:
    """The real query node in process behind the client's shape: validate, handle, JSON."""

    def __init__(self, node: Any) -> None:
        self._node = node
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: object, exc_value: object, traceback: object
    ) -> None:
        self.closed = True

    async def query(
        self, request: dict[str, Any], *, timeout_s: float
    ) -> dict[str, Any]:
        self.requests.append(dict(request))
        result = self._node.handle(ModelWorkLedgerQueryRequest.model_validate(request))
        return json.loads(result.model_dump_json())


class UnopenableBusClient:
    async def __aenter__(self) -> Self:
        raise OSError("cannot open the bus session")

    async def __aexit__(
        self, exc_type: object, exc_value: object, traceback: object
    ) -> None:
        return None

    async def query(
        self, request: dict[str, Any], *, timeout_s: float
    ) -> dict[str, Any]:
        raise AssertionError("never opened")


def test_golden_a_25_hour_window_reads_in_one_session_under_the_step_budget() -> None:
    clock: list[float] = []
    client = FakeBusClient(clock)
    read = BusSessionPointRead(lambda: client, 60)
    result = reader(
        read, clock=SEAM_NOW, page_size=1200, max_pages=50, rows_lines_only=True
    ).handle(rows_request(window_hours=25))
    assert result.status is EnumReaderStatus.OK, result.reason
    assert result.source == "node"
    assert len(result.lines) == ROWS_25H
    assert read.sessions_opened == 1
    assert client.entered == 1
    assert client.queries == 10
    assert client.closed is True
    assert sum(clock) < STEP_BUDGET_S


def test_control_one_process_per_page_is_over_the_step_budget() -> None:
    clock: list[float] = []
    result = reader(
        PerPageSessionRead(clock),
        clock=SEAM_NOW,
        page_size=200,
        max_pages=58,
        rows_lines_only=False,
    ).handle(rows_request(window_hours=25))
    assert result.status is EnumReaderStatus.OK, result.reason
    assert len(result.lines) == ROWS_25H
    assert sum(clock) > 10 * STEP_BUDGET_S


def test_control_a_full_row_page_over_the_bound_is_refused_not_cut_short() -> None:
    clock: list[float] = []
    read = BusSessionPointRead(lambda: FakeBusClient(clock), 60)
    result = reader(
        read, clock=SEAM_NOW, page_size=1200, max_pages=50, rows_lines_only=False
    ).handle(rows_request(window_hours=25))
    assert result.status is EnumReaderStatus.UNREADABLE
    assert "answer-over-the-bound" in result.reason
    assert result.lines == ()


def test_the_window_never_cuts_short_at_the_page_cap() -> None:
    read = BusSessionPointRead(lambda: FakeBusClient([]), 60)
    result = reader(
        read, clock=SEAM_NOW, page_size=1200, max_pages=3, rows_lines_only=True
    ).handle(rows_request(window_hours=25))
    assert result.status is EnumReaderStatus.UNREADABLE
    assert "open-set-longer-than-3-pages" in result.reason
    assert result.lines == ()


def test_reader_seam_the_session_route_reads_the_real_query_node_through_one_session() -> (
    None
):
    node = _node((*STATUS_ROWS, MSG_ROW))
    client = NodeBusClient(node)
    read = BusSessionPointRead(lambda: client, 60)
    result = reader(read, clock=SEAM_NOW, page_size=2, rows_lines_only=True).handle(
        rows_request(window_hours=27)
    )
    assert result.status is EnumReaderStatus.OK, result.reason
    direct = node.handle(
        ModelWorkLedgerQueryRequest(
            correlation_id=uuid.uuid4(),
            query=EnumWorkLedgerQueryKind.ROWS,
            since=SEAM_NOW - timedelta(hours=27),
            page_size=100,
        )
    )
    assert result.lines == tuple(direct.lines)
    assert len(result.lines) == 6
    assert read.sessions_opened == 1
    assert len(client.requests) == 3
    assert all(r["lines_only"] is True for r in client.requests)
    assert client.closed is True


def test_error_chain_a_failed_page_is_unreadable_and_the_session_is_closed() -> None:
    clients: list[FakeBusClient] = []

    def factory() -> FakeBusClient:
        clients.append(FakeBusClient([], fail_on_query=2 if not clients else None))
        return clients[-1]

    read = BusSessionPointRead(factory, 60)
    sink = InMemoryEventSink()
    handler = HandlerWorkLedgerReader(
        overlay(
            moved_readers=("rows",), page_size=1200, max_pages=50, rows_lines_only=True
        ),
        read,
        sink,
        clock=lambda: SEAM_NOW,
        topics=TOPICS,
    )
    result = handler.handle(rows_request(window_hours=25))
    assert result.status is EnumReaderStatus.UNREADABLE
    assert "bus-session-failed" in result.reason
    assert result.lines == ()
    assert clients[0].closed is True
    assert [e.kind for e in sink.events] == [EnumReaderEventKind.REFUSED]
    # The next read opens a fresh session and reads the whole window.
    again = handler.handle(rows_request(window_hours=25))
    assert again.status is EnumReaderStatus.OK, again.reason
    assert len(again.lines) == ROWS_25H
    assert read.sessions_opened == 2


def test_error_chain_a_session_that_cannot_open_is_unreadable_never_empty() -> None:
    read = BusSessionPointRead(UnopenableBusClient, 60)
    result = reader(
        read, clock=SEAM_NOW, page_size=1200, max_pages=50, rows_lines_only=True
    ).handle(rows_request(window_hours=25))
    assert result.status is EnumReaderStatus.UNREADABLE
    assert "bus-session-failed" in result.reason
    assert result.lines == ()
    assert read.sessions_opened == 0


def test_close_is_safe_when_never_opened_and_when_called_twice() -> None:
    read = BusSessionPointRead(lambda: FakeBusClient([]), 60)
    read.close()
    read.close()
    assert read.sessions_opened == 0


def test_a_page_answer_for_another_request_is_an_error_not_rows() -> None:
    answer = answer_from_json({"correlation_id": "other", "lines": ["x"]}, "mine")
    assert answer.error == "answer-for-another-request"


def test_the_overlay_names_the_bus_session_route_and_the_builder_makes_the_adapter(
    tmp_path: Path,
) -> None:
    document = re.sub(
        r"route:\n  kind: inmemory",
        "route: {kind: bus-session, bus_lane: dev, timeout_seconds: 120}",
        EXAMPLE.read_text(encoding="utf-8"),
    )
    assert "bus-session" in document
    path = tmp_path / "overlay.yaml"
    path.write_text(document, encoding="utf-8")
    loaded = load_overlay({"ONEX_LEDGER_READER_OVERLAY": str(path)})
    assert isinstance(loaded.route, ModelBusSessionReadRoute)
    assert (loaded.route.bus_lane, loaded.route.timeout_seconds) == ("dev", 120)
    infra_cli_loaded_before = "omnibase_infra.cli.cli_ledger" in sys.modules
    built = build_point_read(loaded.route)
    assert isinstance(built, BusSessionPointRead)
    # Building the route opens nothing and imports nothing from infra until the first page.
    assert built.sessions_opened == 0
    assert infra_cli_loaded_before or "omnibase_infra.cli.cli_ledger" not in sys.modules
