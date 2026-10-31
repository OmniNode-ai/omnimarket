# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ticket whose description was never read is not a ticket with no criteria."""

import pytest

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
def test_snapshot_issue_without_description_is_admitted_from_live_criteria() -> None:
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
