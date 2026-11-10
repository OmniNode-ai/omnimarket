# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the board's rows query and the live ledger read node (OMN-23421).

Two pieces share one request and one answer: the readers (``node_floor_delegation_effect``'s lab-node source and
``node_delegation_scenario_days_effect``) send ``ModelPointQuery`` through the bus read command's adapter
(``point_read_adapters._request``), and the ledger read node (``node_work_ledger_query_effect``) validates it as
``ModelWorkLedgerQueryRequest`` and answers ``lines`` and ``next_cursor``, which ``answer_from_json`` reads back. Before OMN-23421
the node refused every such request ("page_size applies to the point queries only"), so the board read each lab-node scenario as
unread. Each test fails when either side changes shape: a request cell the node's model refuses, an answer cell the adapter no
longer reads, a cursor the node no longer follows.

The ledger below is the real node over the in-memory reader, with the rows written by the real scenario handlers; the same request
is read on the real broker by the command named in the PR (``ONEX_LEDGER_BUS_READ_COMMAND``).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.handlers.work_ledger_judge_acceptance import (
    InMemoryJudgeAcceptanceStore,
)
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    ModelScenarioResult,
)
from omnimarket.models.model_point_query import ModelPointQuery
from omnimarket.models.work_ledger_append.model_work_ledger_judge_acceptance import (
    ModelWorkLedgerJudgeAcceptance,
)
from omnimarket.models.work_ledger_query import ModelWorkLedgerQueryRequest
from omnimarket.nodes.node_event_emit_effect.handlers.inmemory_bus_emitter import (
    InMemoryBusEmitter,
)
from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_sources import (
    read_lab_node_scenario,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
    HandlerWorkLedgerEmit,
)
from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_request import (
    ModelWorkLedgerEmitRequest,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.point_read_adapters import (
    _request,
    answer_from_json,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import ModelPointAnswer
from tests.nodes.node_delegation_scenario_r2_effect.test_golden_chain_scenario_r2_effect import (
    NOW as R2_RUN_AT,
)

from .support import LAB_NODE_RT as RT
from .test_seam_scenario_nodes import decl, run_r2

pytestmark = pytest.mark.unit

READ_AT = R2_RUN_AT + timedelta(minutes=10)
LANE = next(d.lane for d in RT.scenarios if d.id == "R2")


class NodeLedger:
    """The read port wired as production wires it: the adapter's request, the node's handler, the adapter's answer reader."""

    def __init__(self, reader: InMemoryWorkLedgerQueryReader) -> None:
        self.node = HandlerWorkLedgerQuery(reader=reader, now=lambda: READ_AT)
        self.requests: list[dict[str, object]] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        correlation_id = str(uuid4())
        wire = _request(query, correlation_id)
        self.requests.append(wire)
        request = ModelWorkLedgerQueryRequest.model_validate(wire)
        answer = self.node.handle(request)
        return answer_from_json(answer.model_dump(mode="json"), correlation_id)


def ledger_with(*rows: str) -> NodeLedger:
    reader = InMemoryWorkLedgerQueryReader(now=lambda: READ_AT)
    for number, raw in enumerate(rows):
        assert reader.ingest({"row_id": f"seam-row-{number}", "raw_row": raw})
    return NodeLedger(reader)


def scheduled_row(slot_minute: int) -> str:
    result = ModelScenarioResult(
        scenario_id="R2",
        slot_start=R2_RUN_AT.replace(minute=slot_minute, second=0)
        - timedelta(hours=slot_minute + 1),
        trigger=EnumScenarioTrigger.SCHEDULE,
        conclusion=EnumScenarioConclusion.SUCCESS,
        recorded_at=R2_RUN_AT - timedelta(minutes=slot_minute),
        assertions=("+a",),
    )
    return result.to_status_row(lane=LANE, ticket="OMN-23421", actor="script:test")


def test_golden_seam_the_boards_rows_query_is_answered_by_the_node(
    tmp_path: Path,
) -> None:
    lane, row, _ = run_r2(tmp_path)
    ledger = ledger_with(row)
    read = read_lab_node_scenario(decl("R2", lane), ledger, RT, READ_AT)
    assert read.read == "read", read.reason
    assert [r.conclusion for r in read.runs] == ["success"]
    sent = ledger.requests[0]
    assert sent["query"] == "rows"
    assert "page_size" in sent


def test_golden_seam_pages_are_followed_until_the_cursor_ends() -> None:
    ledger = ledger_with(*(scheduled_row(minute) for minute in (1, 2, 3, 4, 5)))
    pager = RT.model_copy(update={"lab_node_rows_page_size": 2})
    read = read_lab_node_scenario(decl("R2", LANE), ledger, pager, READ_AT)
    assert read.read == "read", read.reason
    assert len(read.runs) == 5
    assert len(ledger.requests) == 3
    assert "cursor" not in ledger.requests[0]
    assert "cursor" in ledger.requests[1]


def test_error_seam_a_page_the_node_refuses_is_unread_never_a_pass() -> None:
    class Refusing(NodeLedger):
        def read(self, query: ModelPointQuery) -> ModelPointAnswer:
            return super().read(query.model_copy(update={"cursor": "not-a-cursor"}))

    ledger = Refusing(InMemoryWorkLedgerQueryReader(now=lambda: READ_AT))
    read = read_lab_node_scenario(decl("R2", LANE), ledger, RT, READ_AT)
    assert read.read == "unread"
    assert read.runs == ()


def test_control_seam_a_query_shape_the_node_does_not_accept_is_found() -> None:
    """The injected break: the shape the node refused before OMN-23421 (page_size on a query kind that is not paged)."""
    wire = _request(ModelPointQuery(query="rows", page_size=5), str(uuid4()))
    wire["query"] = "newest-per-lane"
    with pytest.raises(ValueError, match="rows only"):
        ModelWorkLedgerQueryRequest.model_validate(wire)


def test_seam_the_days_reader_query_is_a_request_the_node_accepts() -> None:
    wire = _request(
        ModelPointQuery(
            query="rows",
            lanes=("lane-a", "lane-b"),
            since=READ_AT - timedelta(days=8),
            kinds=("STATUS",),
            page_size=200,
            cursor=None,
        ),
        str(uuid4()),
    )
    request = ModelWorkLedgerQueryRequest.model_validate(wire)
    assert request.lanes == ("lane-a", "lane-b")
    assert request.page_size == 200


async def test_golden_seam_on_the_in_memory_bus_a_row_written_by_the_emit_node_reads_on_the_board(
    tmp_path: Path,
) -> None:
    bus = EventBusInmemory()
    await bus.start()
    reader = InMemoryWorkLedgerQueryReader(now=lambda: READ_AT)
    await reader.attach(bus)
    lane, row, _ = run_r2(tmp_path)
    store = InMemoryJudgeAcceptanceStore()
    store.record(
        ModelWorkLedgerJudgeAcceptance(
            row_id=work_ledger_row_id(row), ledger_seq=1, request_id=uuid4()
        )
    )
    emitted = await asyncio.to_thread(
        HandlerWorkLedgerEmit(
            emitter=InMemoryBusEmitter(bus), acceptance_reader=store
        ).handle,
        ModelWorkLedgerEmitRequest(row=row, ledger_seq=1),
    )
    assert emitted.accepted
    assert emitted.published
    assert len(reader) == 1
    read = read_lab_node_scenario(decl("R2", lane), NodeLedger(reader), RT, READ_AT)
    assert read.read == "read", read.reason
    assert [r.conclusion for r in read.runs] == ["success"]
