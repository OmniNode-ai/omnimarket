# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_work_ledger_graph_query_effect: a golden and an error case per query."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.work_ledger_graph.model_work_ledger_graph import (
    EnumWorkLedgerGraphQuery,
    EnumWorkLedgerGraphQueryStatus,
    ModelLedgerGraphRow,
    ModelWorkLedgerGraph,
    ModelWorkLedgerGraphComputeRequest,
    ModelWorkLedgerGraphQueryRequest,
    ModelWorkLedgerGraphQueryResult,
)
from omnimarket.nodes.node_work_ledger_graph_compute.handlers.handler_work_ledger_graph import (
    HandlerWorkLedgerGraph,
)
from omnimarket.nodes.node_work_ledger_graph_query_effect.handlers.handler_work_ledger_graph_query import (
    HandlerWorkLedgerGraphQuery,
)

pytestmark = pytest.mark.unit

Q = EnumWorkLedgerGraphQuery
LINES = [
    # pr 1: open, its lane's claim is closed, no handoff -> no live owner
    "2026-11-17T10:00:00Z | CLAIM | lane=a | ticket=OMN-1 | x",
    "2026-11-17T10:01:00Z | STATUS | lane=a | pr=omnimarket#1 | head=111111111111aaaa | x",
    "2026-11-17T10:02:00Z | TERMINAL | lane=a | closes-CLAIM=2026-11-17T10:00:00Z | outcome=partial | x",
    # pr 2: open, owned by a live claim
    "2026-11-17T10:03:00Z | CLAIM | lane=b | ticket=OMN-2 | x",
    "2026-11-17T10:04:00Z | STATUS | lane=b | pr=omnimarket#2 | x",
    # pr 3: handed off to the landing controller
    "2026-11-17T10:05:00Z | MSG | from=c | to=landing-controller | pr=omnimarket#3 | head=333 | x",
    # pr 4: merged
    "2026-11-17T10:06:00Z | STATUS | lane=d | pr=omnimarket#4 | outcome=merged | x",
    # reviews: head 1111 twice, head 2222 once
    "2026-11-17T10:07:00Z | STATUS | lane=r1 | verdict=CHANGES-NEEDED | head=111111111111 | pr=omnimarket#1 | x",
    "2026-11-17T10:08:00Z | STATUS | lane=r2 | verdict=APPROVED head=111111111111aaaa | x",
    "2026-11-17T10:09:00Z | STATUS | lane=r3 | verdict=CHANGES_NEEDED | head=222222222222 | x",
    "2026-11-17T10:09:30Z | STATUS | lane=r4 | verdict=WORKING | head=222222222222 | x",
    # red checks: Lint holds pr 1 and pr 2 (through a cause), Tests holds pr 4 (merged)
    "2026-11-17T10:10:00Z | CLAIM | lane=lc1 | cause=cause:k | check=Lint | x",
    "2026-11-17T10:10:30Z | TERMINAL | lane=lc1 | cause=cause:k | prs=omnimarket#1,omnimarket#2 | closes-CLAIM=2026-11-17T10:10:00Z | x",
    "2026-11-17T10:11:00Z | STATUS | lane=lc2 | check=Tests,Lint | pr=omnimarket#4 | x",
    # rulings: R1 mechanism OMN-10 built, R2 mechanism OMN-11 not built, R3 none named, R4 kind=decision
    "2026-11-17T10:12:00Z | RULING | lane=o | kind=process | mechanism-ticket=OMN-10 | x",
    "2026-11-17T10:12:10Z | RULING | lane=o | kind=process | mechanism-ticket=OMN-11 | x",
    "2026-11-17T10:12:20Z | RULING | lane=o | kind=process | x",
    "2026-11-17T10:12:30Z | RULING | lane=o | kind=decision | mechanism-ticket=OMN-11 | x",
    "2026-11-17T10:13:00Z | STATUS | lane=m | ticket=OMN-10 | installed=h201:abc | observed=ran | x",
    # seen running: OMN-10 open, OMN-20 closed by a done TERMINAL
    "2026-11-17T10:14:00Z | STATUS | lane=s | ticket=OMN-20 | installed=h201:def | observed=ran | x",
    "2026-11-17T10:15:00Z | TERMINAL | lane=s | ticket=OMN-20 | outcome=done | x",
]


@pytest.fixture
def graph() -> ModelWorkLedgerGraph:
    rows = tuple(
        ModelLedgerGraphRow(
            row_id=f"r{i}",
            row_ts=line.split(" | ")[0],
            row_type=line.split(" | ")[1],
            raw_row=line,
        )
        for i, line in enumerate(LINES, 1)
    )
    return HandlerWorkLedgerGraph().handle(
        ModelWorkLedgerGraphComputeRequest(rows=rows)
    )


def _ask(
    query: Q, graph: ModelWorkLedgerGraph | None = None, path: str | None = None
) -> ModelWorkLedgerGraphQueryResult:
    return HandlerWorkLedgerGraphQuery().handle(
        ModelWorkLedgerGraphQueryRequest(query=query, graph=graph, graph_path=path)
    )


def _subjects(result: ModelWorkLedgerGraphQueryResult) -> list[str]:
    assert result.status is EnumWorkLedgerGraphQueryStatus.OK, result.error
    assert result.model_tokens == 0
    return [a.subject for a in result.answers]


GOLDEN = {
    Q.OPEN_PRS_NO_LIVE_OWNER: ["pr:omnimarket#1"],
    Q.HEADS_REVIEWED_MORE_THAN_ONCE: ["head:111111111111"],
    Q.RED_CHECK_HOLDING_MOST_OPEN_PRS: ["check:Lint"],
    Q.PROCESS_RULINGS_NO_MECHANISM: [
        "ruling:2026-11-17T10:12:10Z",
        "ruling:2026-11-17T10:12:20Z",
    ],
    Q.SEEN_RUNNING_TICKET_OPEN: ["ticket:OMN-10"],
}


@pytest.mark.parametrize("query", list(Q))
def test_golden(query: Q, graph: ModelWorkLedgerGraph) -> None:
    assert _subjects(_ask(query, graph)) == GOLDEN[query]


def test_red_check_ranks_by_open_prs(graph: ModelWorkLedgerGraph) -> None:
    answer = _ask(Q.RED_CHECK_HOLDING_MOST_OPEN_PRS, graph).answers[0]
    assert {f.key: f.value for f in answer.facts} == {
        "open_prs": "2",
        "prs": "pr:omnimarket#1,pr:omnimarket#2",
    }


def test_graph_file_round_trip(graph: ModelWorkLedgerGraph, tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    path.write_text(graph.model_dump_json(), encoding="utf-8")
    assert (
        _subjects(_ask(Q.OPEN_PRS_NO_LIVE_OWNER, path=str(path)))
        == GOLDEN[Q.OPEN_PRS_NO_LIVE_OWNER]
    )


def _error(result: ModelWorkLedgerGraphQueryResult) -> str:
    assert result.status is EnumWorkLedgerGraphQueryStatus.ERROR
    assert result.answers == ()
    assert result.error
    return result.error


@pytest.mark.parametrize("query", list(Q))
def test_error_missing_file(query: Q, tmp_path: Path) -> None:
    assert "unreadable" in _error(_ask(query, path=str(tmp_path / "absent.json")))


@pytest.mark.parametrize("query", list(Q))
def test_error_corrupt_file(query: Q, tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    path.write_text("{not json", encoding="utf-8")
    assert "does not validate" in _error(_ask(query, path=str(path)))


@pytest.mark.parametrize("query", list(Q))
def test_error_tampered_graph(query: Q, graph: ModelWorkLedgerGraph) -> None:
    tampered = graph.model_copy(
        update={"rows": graph.rows[:-1], "row_count": graph.row_count - 1}
    )
    assert "content_sha256" in _error(_ask(query, tampered))


@pytest.mark.parametrize("query", list(Q))
def test_error_empty_graph_is_never_an_empty_answer(query: Q) -> None:
    empty = HandlerWorkLedgerGraph().handle(ModelWorkLedgerGraphComputeRequest(rows=()))
    assert "no rows" in _error(_ask(query, empty))


def test_error_no_graph_named() -> None:
    assert "no graph" in _error(_ask(Q.OPEN_PRS_NO_LIVE_OWNER))
