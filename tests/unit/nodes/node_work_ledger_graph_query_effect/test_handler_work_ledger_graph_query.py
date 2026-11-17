# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each of the five typed questions has a golden case and an error case."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.handlers.work_ledger_graph_derive import derive_graph
from omnimarket.models.work_ledger_graph import (
    EnumWorkLedgerGraphQuery,
    ModelWorkLedgerGraphQueryRequest,
    ModelWorkLedgerGraphRow,
)
from omnimarket.nodes.node_work_ledger_graph_query_effect import (
    NodeWorkLedgerGraphQueryEffect,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 17, 23, 0, tzinfo=UTC)


def _row(ts: str, kind: str, cells: str) -> ModelWorkLedgerGraphRow:
    return ModelWorkLedgerGraphRow(
        row_id=f"{ts}-{kind}",
        row_ts=datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC),
        row_type=kind,
        raw_row=f"{ts} | {kind} | {cells} | text",
    )


ROWS = [
    # omnimarket#1: lane a holds a live CLAIM and names it -> owned.
    _row("2026-11-17T10:00:00Z", "CLAIM", "lane=a | ticket=OMN-1"),
    _row("2026-11-17T10:01:00Z", "STATUS", "lane=a | ticket=OMN-1 | pr=omnimarket#1"),
    # omnimarket#2: lane b's CLAIM was closed, never handed off -> no live owner.
    _row("2026-11-17T10:02:00Z", "CLAIM", "lane=b | ticket=OMN-2"),
    _row(
        "2026-11-17T10:03:00Z",
        "STATUS",
        "lane=b | ticket=OMN-2 | pr=OmniNode-ai/omnimarket#2 | check=CI Summary",
    ),
    _row(
        "2026-11-17T10:04:00Z",
        "TERMINAL",
        "lane=b | ticket=OMN-2 | closes-CLAIM=2026-11-17T10:02:00Z | outcome=partial",
    ),
    # omnimarket#3: handed off by MSG to landing-controller -> excluded.
    _row(
        "2026-11-17T10:05:00Z",
        "MSG",
        "from=c | to=landing-controller | pr=omnimarket#3 | head=cccccccccccc",
    ),
    # omnimarket#4: merged -> not open.
    _row(
        "2026-11-17T10:06:00Z",
        "TERMINAL",
        "lane=d | ticket=OMN-4 | pr=omnimarket#4 | merged=8ddb9680 | check=Lint",
    ),
    # omnimarket#6: a second PR held by CI Summary, and by Lint.
    _row(
        "2026-11-17T10:07:00Z",
        "STATUS",
        "lane=e | ticket=OMN-6 | pr=omnimarket#6 | check=CI Summary,Lint",
    ),
    # Reviews: head 5555... reviewed twice; a probe verdict twice is not a review.
    _row(
        "2026-11-17T11:00:00Z",
        "STATUS",
        "lane=fable-1 | verdict=CHANGES-NEEDED | head=555555555555aaaa | pr=omnimarket#5",
    ),
    _row(
        "2026-11-17T11:30:00Z",
        "STATUS",
        "lane=fable-2 | verdict=CHANGES_NEEDED | head=555555555555 | pr=omnimarket#5",
    ),
    _row(
        "2026-11-17T11:31:00Z",
        "STATUS",
        "lane=probe | verdict=WORKING | head=999999999999",
    ),
    _row(
        "2026-11-17T11:32:00Z",
        "STATUS",
        "lane=probe | verdict=NOT_WORKING | head=999999999999",
    ),
    # Rulings: OMN-10 has no wired mechanism, OMN-11 is installed and observed, one has no mechanism cell.
    _row(
        "2026-11-17T12:00:00Z",
        "RULING",
        "lane=orch | kind=process | mechanism-ticket=OMN-10",
    ),
    _row(
        "2026-11-17T12:01:00Z",
        "RULING",
        "lane=orch | kind=process | mechanism-ticket=OMN-11",
    ),
    _row("2026-11-17T12:02:00Z", "RULING", "lane=orch | kind=process"),
    _row("2026-11-17T12:03:00Z", "RULING", "lane=orch | kind=decision"),
    _row(
        "2026-11-17T12:04:00Z",
        "STATUS",
        "lane=m | ticket=OMN-11 | installed=h201:abcdefabcdef00 | observed=handled one run",
    ),
    # Seen running: omnimarket#7 installed and observed; omnimarket#8 installed=none.
    _row(
        "2026-11-17T13:00:00Z",
        "STATUS",
        "lane=r | ticket=OMN-20 | pr=omnimarket#7 | installed=h201:1234567890ab | observed=handled",
    ),
    _row(
        "2026-11-17T13:01:00Z",
        "STATUS",
        "lane=r | ticket=OMN-21 | pr=omnimarket#8 | installed=none | observed=none",
    ),
]


class _Reader:
    def __init__(
        self, rows: list[ModelWorkLedgerGraphRow] | None = None, fail: bool = False
    ) -> None:
        self.rows = rows if rows is not None else ROWS
        self.fail = fail

    def read_rows(
        self, since: datetime, until: datetime
    ) -> list[ModelWorkLedgerGraphRow]:
        if self.fail:
            raise ConnectionError("database refused the connection")
        return [r for r in self.rows if since <= r.row_ts < until]


def _ask(query: EnumWorkLedgerGraphQuery, reader: _Reader | None = None, **kw: object):
    node = NodeWorkLedgerGraphQueryEffect(reader=reader or _Reader(), now=lambda: _NOW)
    return node.handle(ModelWorkLedgerGraphQueryRequest(query=query, **kw))  # type: ignore[arg-type]


def test_open_prs_without_owner_golden() -> None:
    result = _ask(EnumWorkLedgerGraphQuery.OPEN_PRS_WITHOUT_OWNER)
    assert result.status == "ok"
    subjects = [i.subject for i in result.items]
    assert "omnimarket#2" in subjects
    assert "omnimarket#1" not in subjects  # live owner
    assert "omnimarket#3" not in subjects  # handed off
    assert "omnimarket#4" not in subjects  # merged
    narrowed = _ask(
        EnumWorkLedgerGraphQuery.OPEN_PRS_WITHOUT_OWNER,
        open_prs=("OmniNode-ai/omnimarket#6",),
    )
    assert [i.subject for i in narrowed.items] == ["omnimarket#6"]
    assert narrowed.open_rule.startswith("request:")


def test_heads_reviewed_more_than_once_golden() -> None:
    result = _ask(EnumWorkLedgerGraphQuery.HEADS_REVIEWED_MORE_THAN_ONCE)
    assert result.status == "ok"
    assert [(i.subject, i.count, i.prs) for i in result.items] == [
        ("555555555555", 2, ("omnimarket#5",))
    ]


def test_red_check_holding_most_prs_golden() -> None:
    result = _ask(EnumWorkLedgerGraphQuery.RED_CHECK_HOLDING_MOST_PRS)
    assert result.status == "ok"
    top = result.items[0]
    assert (top.subject, top.count, top.prs) == (
        "CI Summary",
        2,
        ("omnimarket#2", "omnimarket#6"),
    )
    lint = next(i for i in result.items if i.subject == "Lint")
    assert lint.prs == ("omnimarket#6",)  # omnimarket#4 merged


def test_process_rulings_without_mechanism_golden() -> None:
    result = _ask(EnumWorkLedgerGraphQuery.PROCESS_RULINGS_WITHOUT_MECHANISM)
    assert result.status == "ok"
    assert [(i.subject, i.tickets) for i in result.items] == [
        ("2026-11-17T12:00:00Z", ("OMN-10",)),
        ("2026-11-17T12:02:00Z", ()),
    ]


def test_seen_running_with_open_tickets_golden() -> None:
    result = _ask(EnumWorkLedgerGraphQuery.SEEN_RUNNING_WITH_OPEN_TICKETS)
    assert result.status == "ok"
    assert [(i.subject, i.tickets) for i in result.items] == [
        ("omnimarket#7", ("OMN-20",))
    ]
    closed = _ask(
        EnumWorkLedgerGraphQuery.SEEN_RUNNING_WITH_OPEN_TICKETS,
        closed_tickets=("OMN-20",),
    )
    assert closed.status == "ok"
    assert closed.items == ()


@pytest.mark.parametrize("query", list(EnumWorkLedgerGraphQuery))
def test_each_query_answers_an_error_when_the_rows_cannot_be_read(
    query: EnumWorkLedgerGraphQuery,
) -> None:
    result = _ask(query, reader=_Reader(fail=True))
    assert result.status == "error"
    assert result.error is not None
    assert "graph unreadable" in result.error
    assert result.items == ()


@pytest.mark.parametrize("query", list(EnumWorkLedgerGraphQuery))
def test_each_query_answers_an_error_on_an_empty_window_never_an_empty_answer(
    query: EnumWorkLedgerGraphQuery,
) -> None:
    result = _ask(query, reader=_Reader(rows=[]))
    assert result.status == "error"
    assert result.error == "graph unreadable: no rows in the window"


@pytest.mark.parametrize("query", list(EnumWorkLedgerGraphQuery))
def test_each_query_answers_an_error_on_a_tampered_graph(
    query: EnumWorkLedgerGraphQuery,
) -> None:
    graph = derive_graph(ROWS)
    tampered = graph.model_copy(update={"edges": graph.edges[1:]})
    node = NodeWorkLedgerGraphQueryEffect(reader=_Reader(), now=lambda: _NOW)
    result = node.answer_graph(ModelWorkLedgerGraphQueryRequest(query=query), tampered)
    assert result.status == "error"
    assert result.error == "graph unreadable: digest does not match its content"
