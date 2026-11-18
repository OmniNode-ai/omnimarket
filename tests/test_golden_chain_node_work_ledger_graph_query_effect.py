# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: ledger rows -> node_work_ledger_graph_compute -> graph file ->
node_work_ledger_graph_query_effect, for each of the five queries, plus the
error leg (an unreadable graph answers status=error, never an empty answer)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.work_ledger_graph.model_work_ledger_graph import (
    EnumWorkLedgerGraphQuery,
    EnumWorkLedgerGraphQueryStatus,
    ModelLedgerGraphRow,
    ModelWorkLedgerGraphComputeRequest,
    ModelWorkLedgerGraphQueryRequest,
)
from omnimarket.nodes.node_work_ledger_graph_compute.handlers.handler_work_ledger_graph import (
    HandlerWorkLedgerGraph,
)
from omnimarket.nodes.node_work_ledger_graph_query_effect.handlers.handler_work_ledger_graph_query import (
    HandlerWorkLedgerGraphQuery,
)

pytestmark = pytest.mark.unit

LINES = (
    "2026-11-17T10:00:00Z | CLAIM | lane=a | ticket=OMN-1 | x",
    "2026-11-17T10:01:00Z | STATUS | lane=a | ticket=OMN-1 | pr=omnimarket#1 | head=abc123abc123 | installed=h201:abc | observed=ran | x",
    "2026-11-17T10:02:00Z | TERMINAL | lane=a | closes-CLAIM=2026-11-17T10:00:00Z | outcome=partial | x",
    "2026-11-17T10:03:00Z | STATUS | lane=r1 | verdict=CHANGES-NEEDED | head=abc123abc123 | x",
    "2026-11-17T10:04:00Z | STATUS | lane=r2 | verdict=APPROVED | head=abc123abc123 | x",
    "2026-11-17T10:05:00Z | CLAIM | lane=lc | check=Lint | prs=omnimarket#1 | x",
    "2026-11-17T10:06:00Z | RULING | lane=o | kind=process | mechanism-ticket=OMN-9 | x",
)
EXPECTED = {
    EnumWorkLedgerGraphQuery.OPEN_PRS_NO_LIVE_OWNER: [],  # lane lc holds a live CLAIM naming pr 1
    EnumWorkLedgerGraphQuery.HEADS_REVIEWED_MORE_THAN_ONCE: ["head:abc123abc123"],
    EnumWorkLedgerGraphQuery.RED_CHECK_HOLDING_MOST_OPEN_PRS: ["check:Lint"],
    EnumWorkLedgerGraphQuery.PROCESS_RULINGS_NO_MECHANISM: [
        "ruling:2026-11-17T10:06:00Z"
    ],
    EnumWorkLedgerGraphQuery.SEEN_RUNNING_TICKET_OPEN: ["ticket:OMN-1"],
}


@pytest.mark.parametrize("query", list(EnumWorkLedgerGraphQuery))
def test_golden_chain(query: EnumWorkLedgerGraphQuery, tmp_path: Path) -> None:
    rows = tuple(
        ModelLedgerGraphRow(
            row_id=f"r{i}",
            row_ts=line.split(" | ")[0],
            row_type=line.split(" | ")[1],
            raw_row=line,
        )
        for i, line in enumerate(LINES, 1)
    )
    graph = HandlerWorkLedgerGraph().handle(
        ModelWorkLedgerGraphComputeRequest(rows=rows)
    )
    path = tmp_path / "graph.json"
    path.write_text(graph.model_dump_json(), encoding="utf-8")
    result = HandlerWorkLedgerGraphQuery().handle(
        ModelWorkLedgerGraphQueryRequest(query=query, graph_path=str(path))
    )
    assert result.status is EnumWorkLedgerGraphQueryStatus.OK, result.error
    assert result.graph_sha256 == graph.content_sha256
    assert [a.subject for a in result.answers] == EXPECTED[query]


@pytest.mark.parametrize("query", list(EnumWorkLedgerGraphQuery))
def test_golden_chain_error_leg(
    query: EnumWorkLedgerGraphQuery, tmp_path: Path
) -> None:
    path = tmp_path / "graph.json"
    path.write_text("[]", encoding="utf-8")
    result = HandlerWorkLedgerGraphQuery().handle(
        ModelWorkLedgerGraphQueryRequest(query=query, graph_path=str(path))
    )
    assert result.status is EnumWorkLedgerGraphQueryStatus.ERROR
    assert result.answers == ()
