# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ticket whose description was never read is not a ticket with no criteria."""

from collections.abc import Mapping

import pytest

from omnimarket.handlers.handler_lab_fill_live_tickets import Post
from omnimarket.handlers.rules_lab_fill_tickets import drop_line, plan_ticket_backlog
from omnimarket.models.lab_fill import (
    EnumLabFillTicketDrop,
    ModelLabFillTicketFacts,
)

SPRINT = "Sprint 2026-10-26 → 2026-11-01: M4 End-to-End Part 4"
LIVE_TEXT = "Acceptance criteria\n- [ ] AC1: x\n- [ ] AC2: y"


def _issue(**changes: object) -> dict[str, object]:
    return {
        "id": "OMN-100",
        "title": "Pillar work",
        "status": "Todo",
        "project": SPRINT,
        "repo": "omnimarket",
        **changes,
    }


def _facts(*issues: dict[str, object], **changes: object) -> ModelLabFillTicketFacts:
    return ModelLabFillTicketFacts.model_validate(
        {"today": "2026-10-27", "issues": issues, **changes}
    )


@pytest.mark.unit
def test_criteria_unread_snapshot_issue_is_admitted_from_live_criteria() -> None:
    plan = plan_ticket_backlog(
        _facts(
            _issue(),
            live_descriptions=[{"id": "OMN-100", "description": LIVE_TEXT}],
        )
    )
    assert not plan.drops
    assert [item.criteria for item in plan.items] == [("AC1", "AC2")]


@pytest.mark.unit
def test_absent_description_without_live_text_is_criteria_unread() -> None:
    plan = plan_ticket_backlog(_facts(_issue()))
    assert not plan.items
    assert plan.drops[0].reason is EnumLabFillTicketDrop("criteria-unread")
    assert plan.drop_counts() == {"criteria-unread": 1}
    assert drop_line(plan) == "tickets=0 dropped: criteria-unread=1"


@pytest.mark.unit
def test_empty_description_is_no_criteria_not_unread() -> None:
    plan = plan_ticket_backlog(_facts(_issue(description="")))
    assert plan.drops[0].reason is EnumLabFillTicketDrop.NO_CRITERIA
    assert plan.drop_counts() == {"no-criteria": 1}


@pytest.mark.unit
def test_live_text_never_overrides_a_description_that_was_read() -> None:
    plan = plan_ticket_backlog(
        _facts(
            _issue(description=""),
            live_descriptions=[{"id": "OMN-100", "description": LIVE_TEXT}],
        )
    )
    assert plan.drops[0].reason is EnumLabFillTicketDrop.NO_CRITERIA


def _post(nodes: list[dict[str, object]]) -> Post:
    def post(query: str, variables: Mapping[str, object]) -> Mapping[str, object]:
        return {"issues": {"nodes": nodes}}

    return post


@pytest.mark.unit
def test_live_reader_returns_text_and_reads_a_blank_description_as_empty() -> None:
    from omnimarket.models.lab_fill import ModelLabFillCriteriaReadRequest
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        read_criteria,
    )

    result = read_criteria(
        ModelLabFillCriteriaReadRequest(tickets=("OMN-1", "OMN-2", "OMN-3")),
        post=_post(
            [
                {"identifier": "OMN-1", "description": LIVE_TEXT},
                {"identifier": "OMN-2", "description": None},
            ]
        ),
    )
    assert [(d.id, d.description) for d in result.descriptions] == [
        ("OMN-1", LIVE_TEXT),
        ("OMN-2", ""),
    ]
    assert result.error == ""


@pytest.mark.unit
def test_unreadable_linear_leaves_the_ticket_unread() -> None:
    from omnimarket.handlers.handler_lab_fill_live_tickets import LabFillLiveReadError
    from omnimarket.models.lab_fill import ModelLabFillCriteriaReadRequest
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        read_criteria,
    )

    def post(query: str, variables: Mapping[str, object]) -> Mapping[str, object]:
        raise LabFillLiveReadError("LINEAR_API_KEY is missing")

    result = read_criteria(
        ModelLabFillCriteriaReadRequest(tickets=("OMN-1",)), post=post
    )
    assert not result.descriptions
    assert result.error == "LINEAR_API_KEY is missing"


@pytest.mark.unit
def test_criteria_unread_fill_rule_reads_live_then_replans() -> None:
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        omnimarket_ticket_rule,
    )

    facts = _facts(_issue(), _issue(id="OMN-101")).model_dump(mode="json")
    post = _post([{"identifier": "OMN-100", "description": LIVE_TEXT}])
    plan = omnimarket_ticket_rule(post)(facts)
    assert [item["id"] for item in plan["items"]] == ["OMN-100"]
    assert [(d["id"], d["reason"]) for d in plan["drops"]] == [
        ("OMN-101", "criteria-unread")
    ]


@pytest.mark.unit
def test_typed_contract_carries_unread_and_live_text() -> None:
    from omnimarket.models.lab_fill import ModelLabFillTicketIssue

    assert ModelLabFillTicketIssue(id="OMN-1").description is None
    assert ModelLabFillTicketIssue(id="OMN-1", description="").description == ""
    assert "live_descriptions" in ModelLabFillTicketFacts.model_fields


@pytest.mark.unit
def test_dry_read_of_snapshot_shaped_issues_counts_the_unread() -> None:
    issues = [_issue(id=f"OMN-{9000 + n}") for n in range(31)]
    plan = plan_ticket_backlog(_facts(*issues))
    assert drop_line(plan) == "tickets=0 dropped: criteria-unread=31"
