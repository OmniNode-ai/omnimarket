# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""list_unowned_sprint_tickets: the current sprint's open tickets with no live CLAIM, over a ledger fixture."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.models.sprint_resolution.model_sprint_resolution import (
    EnumSprintResolutionStatus,
)
from omnimarket.models.ticket_query.model_unowned_sprint_tickets import (
    ModelUnownedSprintTicketsInput,
)
from omnimarket.nodes.node_ticket_query.handlers.handler_unowned_sprint_tickets import (
    HandlerUnownedSprintTickets,
    LedgerUnreadableError,
    ModelClaimRows,
    live_claimed_tickets,
)

ARROW = "→"
# One live CLAIM (OMN-101), one CLAIM closed by a TERMINAL naming the ticket (OMN-102), one closed by a
# TERMINAL naming only its lane (OMN-103), and a CLAIM whose ticket a later CLAIM of another lane reopens
# (OMN-104).
LEDGER = (
    "2031-05-06T01:00:00Z | CLAIM | lane=lane-a | ticket=OMN-101 | actor=x | worktree=none | est ~1 lane-hours; displaces none; (OMN-101)",
    "2031-05-06T01:05:00Z | CLAIM | lane=lane-b | ticket=OMN-102 | actor=x | worktree=none | est ~1 lane-hours; displaces none; (OMN-102)",
    "2031-05-06T01:10:00Z | CLAIM | lane=lane-c | ticket=OMN-103 | actor=x | worktree=none | est ~1 lane-hours; displaces none; (OMN-103)",
    "2031-05-06T01:15:00Z | TERMINAL | lane=lane-b | ticket=OMN-102 | outcome=done | friction=none",
    "2031-05-06T01:20:00Z | TERMINAL | lane=lane-c | outcome=done | friction=none",
    "2031-05-06T01:25:00Z | CLAIM | lane=lane-d | ticket=OMN-104 | actor=x | worktree=none | est ~1 lane-hours; displaces none; (OMN-104)",
    "2031-05-06T01:26:00Z | TERMINAL | lane=lane-d | ticket=OMN-104 | outcome=done | friction=none",
    "2031-05-06T01:27:00Z | CLAIM | lane=lane-e | ticket=OMN-104 | actor=x | worktree=none | est ~1 lane-hours; displaces none; (OMN-104)",
)


class _Response:
    status_code = 200

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def json(self) -> dict[str, Any]:
        return {"data": self._data}


class _Tracker:
    """Serves the sprint listing and the sprint project's open issues."""

    def __init__(
        self,
        tickets: list[str],
        *,
        sprint_window: tuple[str, str] = ("2031-05-05", "2031-05-11"),
    ) -> None:
        self.tickets = tickets
        self.window = sprint_window
        self.issue_queries: list[dict[str, object]] = []

    async def post_graphql(
        self, query: str, variables: dict[str, object] | None = None
    ) -> _Response:
        if "SprintProjects" in query:
            start, end = self.window
            return _Response(
                {
                    "projects": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {
                                "id": "fixture-project",
                                "name": f"Sprint {start} {ARROW} {end}: Fixture",
                                "startDate": start,
                                "targetDate": end,
                                "completedAt": None,
                                "projectMilestones": {"nodes": []},
                            }
                        ],
                    }
                }
            )
        assert variables is not None
        self.issue_queries.append(variables)
        return _Response(
            {
                "issues": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "identifier": t,
                            "title": f"title {t}",
                            "state": {"name": "Backlog", "type": "backlog"},
                            "assignee": None,
                            "labels": {"nodes": [{"name": "lane-created"}]},
                        }
                        for t in self.tickets
                    ],
                }
            }
        )


class _Rows:
    def __init__(self, status: str = "ok", lines: tuple[str, ...] = LEDGER) -> None:
        self.status = status
        self.lines = lines
        self.windows: list[int] = []

    def read_claim_rows(self, window_hours: int) -> ModelClaimRows:
        self.windows.append(window_hours)
        return ModelClaimRows(status=self.status, lines=self.lines, reason="fixture")


def _run(tracker: _Tracker, rows: _Rows, day: datetime) -> Any:
    async def provider() -> _Tracker:
        return tracker

    handler = HandlerUnownedSprintTickets(provider, rows, clock=lambda: day)
    return asyncio.run(handler.handle(ModelUnownedSprintTicketsInput()))


INSIDE = datetime(2031, 5, 8, 12, 0, tzinfo=UTC)


def test_the_live_claim_holds_its_ticket_and_every_other_open_sprint_ticket_is_returned() -> (
    None
):
    tracker = _Tracker(["OMN-101", "OMN-102", "OMN-103", "OMN-104", "OMN-105"])
    out = _run(tracker, _Rows(), INSIDE)
    assert out.sprint.status is EnumSprintResolutionStatus.RESOLVED
    assert [t.identifier for t in out.tickets] == ["OMN-102", "OMN-103", "OMN-105"]
    assert out.claimed == ("OMN-101", "OMN-104")
    assert out.open_in_sprint == 5
    assert tracker.issue_queries == [{"project": "fixture-project", "after": None}]


def test_a_terminal_naming_the_ticket_or_only_the_lane_closes_the_claim() -> None:
    assert live_claimed_tickets(LEDGER) == frozenset({"OMN-101", "OMN-104"})


def test_a_terminal_before_its_claim_closes_nothing() -> None:
    lines = (
        LEDGER[3],
        LEDGER[0],
    )  # TERMINAL for OMN-102 first, then the CLAIM of OMN-101
    assert live_claimed_tickets(lines) == frozenset({"OMN-101"})


def test_the_ledger_is_read_for_the_requested_window_only() -> None:
    rows = _Rows()
    _run(_Tracker(["OMN-101"]), rows, INSIDE)
    assert rows.windows == [168]


def test_a_date_in_no_sprint_returns_the_typed_result_and_no_tickets() -> None:
    tracker = _Tracker(["OMN-105"])
    out = _run(tracker, _Rows(), datetime(2031, 5, 12, 0, 0, tzinfo=UTC))
    assert out.sprint.status is EnumSprintResolutionStatus.NO_SPRINT
    assert out.tickets == ()
    assert tracker.issue_queries == []


def test_the_last_day_of_the_sprint_is_inside_it() -> None:
    out = _run(
        _Tracker(["OMN-105"]), _Rows(), datetime(2031, 5, 11, 23, 59, tzinfo=UTC)
    )
    assert [t.identifier for t in out.tickets] == ["OMN-105"]


@pytest.mark.parametrize("status", ["unreadable", "not-moved"])
def test_an_unreadable_ledger_raises_instead_of_reading_every_ticket_as_unowned(
    status: str,
) -> None:
    with pytest.raises(LedgerUnreadableError, match="unowned"):
        _run(_Tracker(["OMN-105"]), _Rows(status=status, lines=()), INSIDE)


def test_a_stale_ledger_answer_is_used_and_reported() -> None:
    out = _run(_Tracker(["OMN-101", "OMN-105"]), _Rows(status="stale"), INSIDE)
    assert out.ledger_status == "stale"
    assert [t.identifier for t in out.tickets] == ["OMN-105"]
