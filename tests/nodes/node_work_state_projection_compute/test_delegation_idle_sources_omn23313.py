# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation:idle row's tickets are read from the work-state projection, by overlay epics and assignees (OMN-23313).

No ticket id is named in code: the epics and the operator's assignee values come from the overlay, the assignee and the
parent from the tracker snapshot through the projection, and the start criterion from the projection's own reading.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.models.floor_17a.model_delegation_idle import ModelDelegationIdleOverlay
from omnimarket.models.work_state_projection import (
    ModelTicketSource,
    ModelWorkScope,
    ModelWorkStateRequest,
    ModelWorkStateResult,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    read_delegation_tickets,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.work_state_source_mapping import (
    tickets_from_linear_snapshot,
)

pytestmark = pytest.mark.unit

SPEC = ModelDelegationIdleOverlay(
    epics=("EPIC-1",), operator_assignees=("op-1",), title="idle"
)


def project(rules: object, tickets: list[ModelTicketSource]) -> ModelWorkStateResult:
    return HandlerWorkStateProjection().handle(
        ModelWorkStateRequest.model_validate(
            {
                "now": datetime(2026, 11, 9, 12, 0, tzinfo=UTC),
                "rules": rules,
                "scope": ModelWorkScope(in_scope_tickets=("EPIC-1",)),
                "tickets": tickets,
            }
        )
    )


def t(tid: str, **kw: object) -> ModelTicketSource:
    return ModelTicketSource.model_validate(
        {"ticket_id": tid, "state_type": "backlog", "assignee": "op-1", **kw}
    )


def test_delegation_idle_snapshot_carries_the_assignee_by_string_or_mapping(
    rules,
) -> None:
    snapshot = {
        "operator": "op-9",
        "issues": [
            {"id": "A-1", "statusType": "backlog", "assignee": "op-1"},
            {
                "id": "A-2",
                "statusType": "backlog",
                "assignee": {"id": "op-2", "name": "x"},
            },
            {"id": "A-3", "statusType": "backlog", "assigneeId": "op-3"},
            {"id": "A-4", "statusType": "backlog"},
            {"id": "A-5", "statusType": "backlog", "assignee": "op-9"},
        ],
    }
    got = {
        x.ticket_id: x.assignee for x in tickets_from_linear_snapshot(snapshot, rules)
    }
    assert got == {
        "A-1": "op-1",
        "A-2": "op-2",
        "A-3": "op-3",
        "A-4": None,
        "A-5": "me",
    }


def test_delegation_idle_reads_operator_tickets_under_an_epic_with_a_true_criterion(
    rules,
) -> None:
    result = project(
        rules,
        [
            t("EPIC-1", assignee="op-1"),
            t("D-1", parent="EPIC-1"),
            t("D-2", parent="D-1"),  # grandchild: the chain reaches the epic
            t("D-3", parent="EPIC-1", assignee="someone-else"),
            t("D-4", parent="OTHER"),
            t("D-5", parent="EPIC-1", state_type="completed"),
            t("D-6", parent="EPIC-1", requires=("D-7",)),
            t("D-7", parent="EPIC-1", assignee="someone-else"),
        ],
    )
    item = next(i for i in result.items if i.key == "ticket:D-2")
    assert (item.assignee, item.parent) == ("op-1", "D-1")
    got = read_delegation_tickets(result, SPEC)
    assert got is not None
    # D-6 waits for D-7 (not done), so its criterion is not true; the epic is not its own member.
    assert [x.ticket for x in got] == ["D-1", "D-2"]


def test_error_delegation_idle_a_snapshot_with_no_assignee_is_unread_not_empty(
    rules,
) -> None:
    result = project(rules, [t("D-1", parent="EPIC-1", assignee=None)])
    assert read_delegation_tickets(result, SPEC) is None
    assert read_delegation_tickets(None, SPEC) is None


def test_error_delegation_idle_a_parent_cycle_does_not_loop(rules) -> None:
    result = project(rules, [t("D-1", parent="D-2"), t("D-2", parent="D-1")])
    assert read_delegation_tickets(result, SPEC) == ()
