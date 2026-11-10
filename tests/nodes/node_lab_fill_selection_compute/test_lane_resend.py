# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane-resend backlog read from ledger rows (OMN-17427, moved by OMN-23250).

Parity with the tests the frozen repository ran against the same handler: a lane that stopped
partial, blocked or failed in the last 48 hours is resent, a lane with a live claim, a peer's claim
or finish, the umbrella ticket and a credential or operator-decision blocker are dropped, and the
backlog is newest first and capped. The error chains are the drop reasons.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_selection_compute.handlers import HandlerLaneResend
from omnimarket.nodes.node_lab_fill_selection_compute.models import (
    ModelLaneResendRequest,
)

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)


def _row(at: datetime, kind: str, **cells: str) -> str:
    body = " | ".join(f"{key}={value}" for key, value in cells.items())
    return f"{at.strftime('%Y-%m-%dT%H:%M:%SZ')} | {kind} | {body}"


def _terminal(
    hours_ago: float, lane: str, ticket: str, outcome: str, **cells: str
) -> str:
    return _row(
        NOW - timedelta(hours=hours_ago),
        "TERMINAL",
        lane=lane,
        ticket=ticket,
        outcome=outcome,
        **cells,
    )


def _resends(*lines: str, limit: int = 12) -> Any:
    return HandlerLaneResend().handle(
        ModelLaneResendRequest(
            now=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ledger_lines=tuple(lines),
            parent_ticket="OMN-17427",
            limit=limit,
        )
    )


def _tickets(result: Any) -> list[str]:
    return [candidate.ticket for candidate in result.candidates]


@pytest.mark.parametrize("outcome", ["partial", "blocked", "failed"])
def test_a_lane_that_stopped_short_in_the_last_48_hours_is_resent(outcome: str) -> None:
    result = _resends(
        _terminal(3, "lane-a", "OMN-100", outcome, blocker="hook budget spent")
    )
    assert _tickets(result) == ["OMN-100"]
    assert result.candidates[0].evidence == (
        f"lane lane-a ended {outcome} at 2026-10-18T09:00:00Z: hook budget spent"
    )


@pytest.mark.parametrize("outcome", ["done", "handed-off", "skipped-owned"])
def test_a_lane_that_finished_is_not_resent(outcome: str) -> None:
    assert _tickets(_resends(_terminal(3, "lane-a", "OMN-100", outcome))) == []


def test_only_the_newest_terminal_of_a_lane_counts() -> None:
    lines = (
        _terminal(30, "lane-a", "OMN-100", "blocked"),
        _terminal(2, "lane-a", "OMN-100", "done"),
    )
    assert _tickets(_resends(*lines)) == []


def test_a_terminal_older_than_48_hours_is_not_resent() -> None:
    lines = (
        _terminal(49, "lane-a", "OMN-100", "failed"),
        _terminal(47, "lane-b", "OMN-101", "failed"),
    )
    assert _tickets(_resends(*lines)) == ["OMN-101"]


@pytest.mark.parametrize(
    "blocker",
    [
        "the Claude token is expired",
        "needs an operator decision on scope",
        "awaiting operator ruling",
        "login required on h202",
        "403 permission denied",
        "no consent row for the rotation",
    ],
)
def test_a_credential_or_operator_decision_blocker_is_not_fixable_by_a_lane(
    blocker: str,
) -> None:
    result = _resends(_terminal(3, "lane-a", "OMN-100", "blocked", blocker=blocker))
    assert _tickets(result) == []
    assert dict(result.dropped) == {"credential-or-operator-decision": 1}


def test_a_ticket_with_a_live_lane_is_never_sent_a_second_copy() -> None:
    lines = (
        _terminal(3, "lane-a", "OMN-100", "partial"),
        _row(NOW - timedelta(minutes=20), "CLAIM", lane="lane-b", ticket="OMN-100"),
    )
    result = _resends(*lines)
    assert _tickets(result) == []
    assert dict(result.dropped) == {"running": 1}


def test_a_ticket_a_peer_already_re_claimed_or_finished_is_not_resent() -> None:
    old_claim = _row(NOW - timedelta(hours=2), "CLAIM", lane="lane-b", ticket="OMN-100")
    finished = _terminal(1, "lane-c", "OMN-101", "done")
    lines = (
        _terminal(3, "lane-a", "OMN-100", "partial"),
        old_claim,
        _terminal(3, "lane-d", "OMN-101", "failed"),
        finished,
    )
    result = _resends(*lines)
    assert _tickets(result) == []
    assert dict(result.dropped) == {"finished-by-peer": 1, "resent": 1}


def test_the_umbrella_ticket_and_rows_without_a_ticket_are_dropped() -> None:
    lines = (
        _terminal(3, "lane-a", "OMN-17427", "partial"),
        _row(NOW - timedelta(hours=3), "TERMINAL", lane="lane-b", outcome="failed"),
    )
    result = _resends(*lines)
    assert _tickets(result) == []
    assert dict(result.dropped) == {"no-ticket": 1, "umbrella": 1}


def test_two_lanes_on_one_ticket_give_one_resend_from_the_newest_terminal() -> None:
    lines = (
        _terminal(8, "lane-a", "OMN-100", "failed", blocker="old"),
        _terminal(2, "lane-b", "OMN-100", "partial", blocker="new"),
    )
    result = _resends(*lines)
    assert [(c.ticket, c.lane) for c in result.candidates] == [("OMN-100", "lane-b")]


def test_the_backlog_is_newest_first_and_capped_at_the_limit() -> None:
    lines = tuple(
        _terminal(10 - n, f"lane-{n}", f"OMN-{200 + n}", "partial") for n in range(5)
    )
    result = _resends(*lines, limit=3)
    assert _tickets(result) == ["OMN-204", "OMN-203", "OMN-202"]
    assert dict(result.dropped) == {"over-limit": 2}
