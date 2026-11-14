# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the bus ledger projection into the lab-fill selection (OMN-19998).

The real query node handler in process, through the real reader handler, into the bus adapter and
the select step with the real selection handler: the query node's rows reach the selection, a
seeded outage or a not-moved reader is a typed not-ok result, a changed row shape on either side
stops the handed-off read, and a row older than the window is not selected on.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_select import (
    HandlerLabFillSelect,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.bus_lab_fill_ledger_rows import (
    BusLabFillLedgerRows,
)
from omnimarket.nodes.node_lab_fill_selection_compute.handlers.handler_lab_fill_selection import (
    HandlerLabFillSelection,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import InMemoryEventSink
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    ModelPointAnswer,
    ModelPointQuery,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_rows_window import (
    OUTSIDE_ROW,
    SEAM_NOW,
    STATUS_ROWS,
    TOPICS,
    QueryNodePointRead,
    overlay,
)

from .test_lab_fill_select import HANDOFF_ROW, select_request

pytestmark = pytest.mark.unit


def _query_node(rows: tuple[str, ...]) -> HandlerWorkLedgerQuery:
    reader = InMemoryWorkLedgerQueryReader(now=lambda: SEAM_NOW)
    for number, raw in enumerate(rows):
        assert reader.ingest({"row_id": f"row-{number}", "raw_row": raw})
    return HandlerWorkLedgerQuery(reader=reader, now=lambda: SEAM_NOW)


def _select_over(
    point_read: object, moved_readers: tuple[str, ...] = ("lab-fill-selection",)
) -> HandlerLabFillSelect:
    reader = HandlerWorkLedgerReader(
        overlay(moved_readers=moved_readers, page_size=2),
        point_read,
        InMemoryEventSink(),
        clock=lambda: SEAM_NOW,
        topics=TOPICS,
    )
    return HandlerLabFillSelect(
        BusLabFillLedgerRows(reader), selector=HandlerLabFillSelection()
    )


def _reasons(result: object) -> dict[str, str | None]:
    return {decision.ticket: decision.reason for decision in result.selection.decisions}


def test_the_query_nodes_rows_reach_the_selection() -> None:
    rows = (*STATUS_ROWS, HANDOFF_ROW)
    point_read = QueryNodePointRead(_query_node(rows))
    result = _select_over(point_read).handle(select_request())
    assert result.ledger_status == "ok"
    assert result.ledger_rows == 6
    assert _reasons(result) == {"OMN-20677": "handed-off", "OMN-9999": None}
    assert point_read.requests[0].query == "rows"


def test_outage_is_unreadable_with_no_selection() -> None:
    class OutagePointRead:
        def read(self, query: ModelPointQuery) -> ModelPointAnswer:
            return ModelPointAnswer(error="bus-outage")

    result = _select_over(OutagePointRead()).handle(select_request())
    assert result.ledger_status == "unreadable"
    assert result.selection is None


def test_reader_not_moved_is_not_moved_with_no_selection() -> None:
    result = _select_over(
        QueryNodePointRead(_query_node(STATUS_ROWS)), moved_readers=("drain-map",)
    ).handle(select_request())
    assert result.ledger_status == "not-moved"
    assert result.selection is None


def test_row_shape_drift_stops_the_handed_off_read() -> None:
    drifted = HANDOFF_ROW.replace(" | pr=repo_a#235", "")
    result = _select_over(
        QueryNodePointRead(_query_node((*STATUS_ROWS, drifted)))
    ).handle(select_request())
    assert result.ledger_status == "ok"
    assert _reasons(result)["OMN-20677"] is None


def test_a_ledger_older_than_the_window_is_not_selected_on() -> None:
    old_handoff = "2026-11-08T10:25:00Z | MSG | from=fix-235 | to=landing-controller | ticket=OMN-20677 | pr=repo_a#235"
    result = _select_over(
        QueryNodePointRead(_query_node((*STATUS_ROWS, OUTSIDE_ROW, old_handoff)))
    ).handle(select_request())
    assert result.ledger_status == "ok"
    assert result.ledger_rows == len(STATUS_ROWS)
    assert _reasons(result)["OMN-20677"] is None
