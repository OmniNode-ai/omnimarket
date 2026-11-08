# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane is attributed to items from its CLAIM's ticket, cause, owns and key cells only (OMN-22935 AC2).

No owner is typed into any overlay: the overlay names which CLAIM cells attribute, never which lane owns which item.
`test_injected_break_owns_cell_dropped_unattributes_the_lane` is the failing control: the same rows with the `owns` cell
removed from the overlay's attribution cells leave the pull request with no lane.
"""

from __future__ import annotations

import pytest

from omnimarket.models.work_state_projection import (
    EnumWorkItemState,
    ModelPlanTaskSource,
    ModelPullRequestSource,
    ModelTicketSource,
    ModelWorkScope,
    ModelWorkStateRequest,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)

pytestmark = pytest.mark.unit


def _items(rules, now, rows, **sources):
    result = HandlerWorkStateProjection().handle(
        ModelWorkStateRequest(
            now=now,
            rules=rules,
            scope=ModelWorkScope(),
            ledger_rows=tuple(rows),
            **sources,
        )
    )
    return {i.key: i for i in result.items}


PR = ModelPullRequestSource(
    repo="omnimarket", number=42, state="OPEN", red_required_checks=("Lint",)
)


def test_owns_cell_attributes_the_lane_to_the_pull_request_and_its_reds(
    rules, now, row
) -> None:
    items = _items(
        rules,
        now,
        [row("CLAIM", "lane=pr-fix", "owns=OmniNode-ai/omnimarket#42", minutes_ago=5)],
        pull_requests=(PR,),
    )
    assert items["pr:omnimarket#42"].lanes == ("pr-fix",)
    assert items["pr:omnimarket#42"].live_lane == "pr-fix"
    assert items["check:omnimarket#42:Lint"].state is EnumWorkItemState.IN_PROGRESS


def test_injected_break_owns_cell_dropped_unattributes_the_lane(
    rules, now, row
) -> None:
    cells = {k: v for k, v in rules.attribution_cells.items() if k != "owns"}
    broken = rules.model_copy(update={"attribution_cells": cells})
    items = _items(
        broken,
        now,
        [row("CLAIM", "lane=pr-fix", "owns=OmniNode-ai/omnimarket#42", minutes_ago=5)],
        pull_requests=(PR,),
    )
    assert items["pr:omnimarket#42"].lanes == ()
    assert items["check:omnimarket#42:Lint"].state is EnumWorkItemState.AVAILABLE


def test_every_owns_cell_of_a_claim_counts(rules, now, row) -> None:
    second = ModelPullRequestSource(repo="omnibase_core", number=7, state="OPEN")
    items = _items(
        rules,
        now,
        [
            row(
                "CLAIM",
                "lane=two-prs",
                "owns=OmniNode-ai/omnimarket#42",
                "owns=omnibase_core#7",
                minutes_ago=5,
            )
        ],
        pull_requests=(PR, second),
    )
    assert items["pr:omnimarket#42"].lanes == ("two-prs",)
    assert items["pr:omnibase_core#7"].lanes == ("two-prs",)


def test_key_cell_attributes_the_lane_to_a_plan_task(rules, now, row) -> None:
    items = _items(
        rules,
        now,
        [row("CLAIM", "lane=floor-t2", "key=plan:p.md#T2", minutes_ago=5)],
        plan_tasks=(ModelPlanTaskSource(plan="p.md", task_id="T2"),),
    )
    assert items["plan:p.md#T2"].live_lane == "floor-t2"


def test_ticket_cell_attributes_a_plan_task_carrying_that_ticket(
    rules, now, row
) -> None:
    items = _items(
        rules,
        now,
        [row("CLAIM", "lane=work", "ticket=ZEP-20", minutes_ago=5)],
        plan_tasks=(ModelPlanTaskSource(plan="p.md", task_id="T3", ticket="ZEP-20"),),
        tickets=(ModelTicketSource(ticket_id="ZEP-20", state_type="started"),),
    )
    assert items["plan:p.md#T3"].live_lane == "work"
    assert items["ticket:ZEP-20"].live_lane == "work"


def test_a_later_child_claim_adds_its_ticket(rules, now, row) -> None:
    items = _items(
        rules,
        now,
        [
            row("CLAIM", "lane=carrier", "ticket=ZEP-30", minutes_ago=40),
            row(
                "CLAIM",
                "lane=carrier",
                "ticket=ZEP-31",
                "parent_ticket=ZEP-30",
                minutes_ago=10,
            ),
        ],
        tickets=(
            ModelTicketSource(ticket_id="ZEP-30", state_type="started"),
            ModelTicketSource(ticket_id="ZEP-31", state_type="started"),
        ),
    )
    assert items["ticket:ZEP-30"].live_lane == "carrier"
    assert items["ticket:ZEP-31"].live_lane == "carrier"


def test_a_lane_cell_in_a_status_row_does_not_attribute(rules, now, row) -> None:
    """Only CLAIM rows attribute: a STATUS row naming a ticket is not an owner."""
    items = _items(
        rules,
        now,
        [row("STATUS", "lane=bystander", "ticket=ZEP-40", minutes_ago=5)],
        tickets=(ModelTicketSource(ticket_id="ZEP-40", state_type="started"),),
    )
    assert items["ticket:ZEP-40"].lanes == ()
    assert items["ticket:ZEP-40"].state is EnumWorkItemState.AVAILABLE
