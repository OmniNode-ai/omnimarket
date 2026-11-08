# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The old fill cannot pick a minted ticket (plan T11, criterion 6).

``plan_ticket_backlog`` is the one Linear-ticket rule of the generated source of both copies of the old fill. It is run
here on the facts of a minted ticket, and a change to it that admits Backlog tickets fails the first test and names it.
"""

from __future__ import annotations

import pytest

from omnimarket.handlers.rules_lab_fill_tickets import plan_ticket_backlog
from omnimarket.models.lab_fill import (
    EnumLabFillTicketDrop,
    EnumLabFillTicketSource,
    ModelLabFillTicketFacts,
    ModelLabFillTicketIssue,
)
from omnimarket.nodes.node_floor_task_ticket_payload_compute.handlers.handler_floor_task_ticket_payload import (
    HandlerFloorTaskTicketPayload,
)
from omnimarket.nodes.node_floor_task_ticket_payload_compute.models import (
    ModelFloorTaskTicketPayload,
)
from omnimarket.nodes.node_floor_task_ticket_payload_compute.tests.support import (
    LINE,
    PLAN,
    request,
)

pytestmark = pytest.mark.unit

TODAY = "2026-11-04"
CURRENT_SPRINT = "Sprint 2026-11-02 → 2026-11-08"
MINTED = "OMN-91001"
STAMP = "created-by-lane: t11-gate-test\n"


def minted_payload() -> ModelFloorTaskTicketPayload:
    payload = HandlerFloorTaskTicketPayload().handle(request("T1", PLAN)).payload
    assert payload is not None
    return payload


def facts_of_a_minted_ticket(**issue_fields: object) -> ModelLabFillTicketFacts:
    """A minted ticket as the plugin passes it: Backlog, the lane stamp and the payload as its description."""
    fields: dict[str, object] = {
        "id": MINTED,
        "title": minted_payload().title,
        "status": "Backlog",
        "status_type": "backlog",
        "labels": ("lane-created",),
        "project": "",
        "pillar": None,
        "description": STAMP + minted_payload().description,
    }
    fields.update(issue_fields)
    return ModelLabFillTicketFacts(
        today=TODAY,
        issues=(ModelLabFillTicketIssue.model_validate(fields),),
    )


def test_floor_ticket_dropped_by_old_rule() -> None:
    facts = facts_of_a_minted_ticket()
    assert f"Floor key: floor:{LINE}:T1" in (facts.issues[0].description or "")
    plan = plan_ticket_backlog(facts)
    assert plan.items == (), "plan_ticket_backlog admitted a minted Backlog ticket"
    [drop] = plan.drops
    assert (drop.id, drop.reason) == (MINTED, EnumLabFillTicketDrop.NOT_PILLAR), (
        "plan_ticket_backlog no longer drops a minted Backlog ticket as not-pillar"
    )
    assert plan.drop_counts() == {"not-pillar": 1}


def test_floor_ticket_control_admitted_with_a_delegation_label() -> None:
    plan = plan_ticket_backlog(
        facts_of_a_minted_ticket(labels=("lane-created", "delegation"))
    )
    [item] = plan.items
    assert (item.id, item.source) == (MINTED, EnumLabFillTicketSource.DELEGATION)
    assert plan.drops == ()


def test_floor_ticket_control_admitted_with_a_current_sprint_project() -> None:
    plan = plan_ticket_backlog(facts_of_a_minted_ticket(project=CURRENT_SPRINT))
    [item] = plan.items
    assert (item.id, item.source) == (MINTED, EnumLabFillTicketSource.SPRINT_CURRENT)
    # the payload's own criteria are what the sprint source reads: the control reaches the criteria gate
    assert item.criteria == ("AC1", "AC2")
    assert plan.drops == ()


def test_floor_ticket_control_admitted_with_a_delegation_pillar() -> None:
    plan = plan_ticket_backlog(facts_of_a_minted_ticket(pillar="delegation"))
    [item] = plan.items
    assert item.source is EnumLabFillTicketSource.DELEGATION


def test_floor_ticket_dropped_by_old_rule_whatever_the_description_says() -> None:
    for description in (None, "", STAMP + minted_payload().description):
        plan = plan_ticket_backlog(facts_of_a_minted_ticket(description=description))
        assert plan.items == ()
        assert [d.reason for d in plan.drops] == [EnumLabFillTicketDrop.NOT_PILLAR]


def test_payload_has_no_source_fields() -> None:
    declared = set(ModelFloorTaskTicketPayload.model_fields)
    assert declared.isdisjoint({"project", "projectId", "label", "labels", "pillar"})
    assert set(minted_payload().model_dump()) == {
        "floor_key",
        "title",
        "description",
        "parent",
    }
