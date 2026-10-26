# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab fill re-reads each ticket's live Linear state and assignee before a send.

The fill's Linear snapshot is read once a day, so a ticket closed or reassigned since
was still sent. The live gate decides from facts read just before the send: completed,
cancelled and other-owner tickets are skipped, and an unread ticket is never free.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping

import pytest

from omnimarket.handlers.handler_lab_fill_live_tickets import (
    HandlerLabFillLiveGate,
    LabFillLiveReadError,
    read_live_tickets,
)
from omnimarket.handlers.lab_fill_live_gate_cli import main
from omnimarket.handlers.rules_lab_fill_tickets import gate_live_tickets
from omnimarket.models.lab_fill import (
    EnumLabFillLiveSkip,
    ModelLabFillLiveFacts,
    ModelLabFillLiveGateRequest,
    ModelLabFillLiveTicket,
)

OPERATOR = "user-operator"


def _live(ticket: str, **changes: object) -> ModelLabFillLiveTicket:
    return ModelLabFillLiveTicket.model_validate(
        {"id": ticket, "state_type": "backlog", "state_name": "Backlog", **changes}
    )


def _facts(*live: ModelLabFillLiveTicket, **changes: object) -> ModelLabFillLiveFacts:
    return ModelLabFillLiveFacts.model_validate(
        {
            "tickets": tuple(t.id for t in live),
            "operators": (OPERATOR,),
            "live": live,
            **changes,
        }
    )


def _verdict(facts: ModelLabFillLiveFacts, ticket: str) -> tuple[bool, object]:
    plan = gate_live_tickets(facts)
    verdict = next(v for v in plan.verdicts if v.id == ticket)
    return verdict.send, verdict.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    ("state_type", "state_name"),
    [
        ("completed", "Done"),
        ("canceled", "Canceled"),
        ("cancelled", "Cancelled"),
        ("canceled", "Duplicate"),
        ("", "Done"),
    ],
)
def test_closed_ticket_is_not_sent(state_type: str, state_name: str) -> None:
    facts = _facts(_live("OMN-19131", state_type=state_type, state_name=state_name))
    assert _verdict(facts, "OMN-19131") == (False, EnumLabFillLiveSkip.CLOSED)


@pytest.mark.unit
def test_ticket_assigned_to_someone_else_is_not_sent() -> None:
    facts = _facts(_live("OMN-20820", assignee_id="user-jake", assignee_name="Jake"))
    plan = gate_live_tickets(facts)
    assert plan.verdicts[0].send is False
    assert plan.verdicts[0].reason is EnumLabFillLiveSkip.OTHER_OWNER
    assert "user-jake" in plan.verdicts[0].detail


@pytest.mark.unit
@pytest.mark.parametrize(
    "assignee", [{"assignee_id": OPERATOR}, {"assignee_name": OPERATOR}, {}]
)
def test_open_ticket_unassigned_or_the_operators_is_sent(
    assignee: Mapping[str, str],
) -> None:
    facts = _facts(_live("OMN-21279", state_type="started", **assignee))
    assert _verdict(facts, "OMN-21279") == (True, None)


@pytest.mark.unit
def test_assigned_ticket_with_no_known_operator_is_not_sent() -> None:
    facts = _facts(_live("OMN-21279", assignee_id="user-x"), operators=())
    assert _verdict(facts, "OMN-21279") == (False, EnumLabFillLiveSkip.OTHER_OWNER)


@pytest.mark.unit
def test_closed_wins_over_other_owner() -> None:
    facts = _facts(_live("OMN-1", state_type="completed", assignee_id="user-jake"))
    assert _verdict(facts, "OMN-1") == (False, EnumLabFillLiveSkip.CLOSED)


@pytest.mark.unit
def test_ticket_linear_did_not_return_is_not_sent() -> None:
    facts = ModelLabFillLiveFacts(tickets=("OMN-7",), operators=(OPERATOR,))
    assert _verdict(facts, "OMN-7") == (False, EnumLabFillLiveSkip.NOT_FOUND)


@pytest.mark.unit
def test_unread_live_state_sends_nothing() -> None:
    facts = _facts(_live("OMN-1"), _live("OMN-2"), error="Linear unreadable: URLError")
    plan = gate_live_tickets(facts)
    assert [(v.id, v.send, v.reason) for v in plan.verdicts] == [
        ("OMN-1", False, EnumLabFillLiveSkip.UNREADABLE),
        ("OMN-2", False, EnumLabFillLiveSkip.UNREADABLE),
    ]
    assert plan.send_ids() == ()


@pytest.mark.unit
def test_verdicts_keep_request_order_and_count_skips() -> None:
    facts = _facts(
        _live("OMN-3", state_type="completed"),
        _live("OMN-1"),
        _live("OMN-2", assignee_id="user-jake"),
    )
    plan = gate_live_tickets(facts)
    assert [v.id for v in plan.verdicts] == ["OMN-3", "OMN-1", "OMN-2"]
    assert plan.send_ids() == ("OMN-1",)
    assert plan.skip_counts() == {"closed": 1, "other-owner": 1}


class _Post:
    """A Linear GraphQL double: records the request, answers the given data."""

    def __init__(self, data: Mapping[str, object] | Exception) -> None:
        self.data = data
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    def __call__(
        self, query: str, variables: Mapping[str, object]
    ) -> Mapping[str, object]:
        self.calls.append((query, variables))
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


_ANSWER = {
    "viewer": {"id": OPERATOR},
    "issues": {
        "nodes": [
            {
                "identifier": "OMN-19131",
                "state": {"type": "completed", "name": "Done"},
                "assignee": {"id": "user-jake", "name": "Jake"},
            },
            {
                "identifier": "OMN-21279",
                "state": {"type": "backlog", "name": "Backlog"},
                "assignee": None,
            },
        ]
    },
}


@pytest.mark.unit
def test_reader_asks_once_for_every_ticket_and_the_viewer() -> None:
    post = _Post(_ANSWER)
    live, viewer = read_live_tickets(("OMN-19131", "OMN-21279"), post=post)
    assert len(post.calls) == 1
    numbers = post.calls[0][1]["numbers"]
    assert isinstance(numbers, list)
    assert sorted(numbers) == [19131, 21279]
    assert viewer == OPERATOR
    assert live == (
        ModelLabFillLiveTicket(
            id="OMN-19131",
            state_type="completed",
            state_name="Done",
            assignee_id="user-jake",
            assignee_name="Jake",
        ),
        ModelLabFillLiveTicket(
            id="OMN-21279", state_type="backlog", state_name="Backlog"
        ),
    )


@pytest.mark.unit
def test_reader_with_no_tickets_makes_no_call() -> None:
    post = _Post(_ANSWER)
    assert read_live_tickets((), post=post) == ((), "")
    assert post.calls == []


@pytest.mark.unit
def test_gate_counts_the_linear_viewer_as_the_operator() -> None:
    answer = {
        "viewer": {"id": "user-viewer"},
        "issues": {
            "nodes": [
                {
                    "identifier": "OMN-5",
                    "state": {"type": "started", "name": "In Progress"},
                    "assignee": {"id": "user-viewer", "name": "Operator"},
                }
            ]
        },
    }
    plan = HandlerLabFillLiveGate(post=_Post(answer)).handle(
        ModelLabFillLiveGateRequest(tickets=("OMN-5",))
    )
    assert plan.send_ids() == ("OMN-5",)


@pytest.mark.unit
def test_gate_skips_closed_and_reassigned_tickets_read_live() -> None:
    plan = HandlerLabFillLiveGate(post=_Post(_ANSWER)).handle(
        ModelLabFillLiveGateRequest(tickets=("OMN-19131", "OMN-21279"), operator="me")
    )
    assert plan.send_ids() == ("OMN-21279",)
    assert plan.verdicts[0].reason is EnumLabFillLiveSkip.CLOSED


@pytest.mark.unit
def test_gate_with_unreadable_linear_sends_nothing() -> None:
    post = _Post(LabFillLiveReadError("LINEAR_API_KEY is missing"))
    plan = HandlerLabFillLiveGate(post=post).handle(
        ModelLabFillLiveGateRequest(tickets=("OMN-1",))
    )
    assert plan.send_ids() == ()
    assert plan.verdicts[0].reason is EnumLabFillLiveSkip.UNREADABLE
    assert "LINEAR_API_KEY is missing" in plan.verdicts[0].detail


@pytest.mark.unit
def test_cli_reads_request_on_stdin_and_prints_the_plan(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"tickets": ["OMN-19131", "OMN-21279"]})),
    )
    assert main(post=_Post(_ANSWER)) == 0
    plan = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert [(v["id"], v["send"], v["reason"]) for v in plan["verdicts"]] == [
        ("OMN-19131", False, "closed"),
        ("OMN-21279", True, None),
    ]
