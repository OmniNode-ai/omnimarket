# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-state projection gives every item a state from the sources (OMN-22935 AC1).

Each test builds a request from source rows only (tickets, plan tasks, pull requests, ledger rows, receipts, readings);
no floor record and no floor page data file is read anywhere. The two failing controls of the brief are
`test_new_project_ticket_with_true_criterion_is_available` and `test_stopped_lane_with_pushed_branch_reads_stopped`;
each has an injected-break twin that turns the rule off through the overlay and shows the reading fail.
"""

from __future__ import annotations

import pytest

from omnimarket.models.work_state_projection import (
    EnumCriterionReading,
    EnumScopeStatus,
    EnumWorkItemKind,
    EnumWorkItemState,
    ModelLaneReceiptSource,
    ModelPlanTaskSource,
    ModelPullRequestSource,
    ModelReadingSource,
    ModelTicketSource,
    ModelWorkScope,
    ModelWorkStateRequest,
    ModelWorkStateResult,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)

pytestmark = pytest.mark.unit


def _project(**fields: object) -> ModelWorkStateResult:
    return HandlerWorkStateProjection().handle(
        ModelWorkStateRequest.model_validate(fields)
    )


def _item(result: ModelWorkStateResult, key: str):
    found = [i for i in result.items if i.key == key]
    assert len(found) == 1, (key, [i.key for i in result.items])
    return found[0]


def test_new_project_ticket_with_true_criterion_is_available(rules, now) -> None:
    """A ticket of a project no code names, whose prerequisite is done, reads available and in scope."""
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_projects=("Zephyr Brand New Project",)),
        tickets=[
            ModelTicketSource(
                ticket_id="ZEP-1",
                state_type="completed",
                project="Zephyr Brand New Project",
            ),
            ModelTicketSource(
                ticket_id="ZEP-2",
                title="second step",
                state_type="backlog",
                project="Zephyr Brand New Project",
                requires=("ZEP-1",),
            ),
        ],
    )
    item = _item(result, "ticket:ZEP-2")
    assert item.state is EnumWorkItemState.AVAILABLE
    assert item.criterion is EnumCriterionReading.TRUE
    assert item.scope is EnumScopeStatus.IN_SCOPE
    assert item in result.ready()
    assert _item(result, "ticket:ZEP-1").state is EnumWorkItemState.DONE


def test_injected_break_done_states_emptied_blocks_the_new_ticket(rules, now) -> None:
    """Injected break: an overlay whose done state types name nothing the tracker says turns the reading false."""
    broken = rules.model_copy(update={"ticket_done_state_types": ("no-such-state",)})
    result = _project(
        now=now,
        rules=broken,
        scope=ModelWorkScope(in_scope_projects=("Zephyr Brand New Project",)),
        tickets=[
            ModelTicketSource(
                ticket_id="ZEP-1",
                state_type="completed",
                project="Zephyr Brand New Project",
            ),
            ModelTicketSource(
                ticket_id="ZEP-2",
                state_type="backlog",
                project="Zephyr Brand New Project",
                requires=("ZEP-1",),
            ),
        ],
    )
    item = _item(result, "ticket:ZEP-2")
    assert item.state is EnumWorkItemState.BLOCKED
    assert item.blocking == ("ticket:ZEP-1",)


def test_ticket_outside_the_scope_is_projected_paused(rules, now) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        tickets=[
            ModelTicketSource(
                ticket_id="ZEP-9", state_type="backlog", project="Elsewhere"
            )
        ],
    )
    item = _item(result, "ticket:ZEP-9")
    assert item.state is EnumWorkItemState.AVAILABLE
    assert item.scope is EnumScopeStatus.PAUSED
    assert result.ready() == ()


def test_scope_reaches_a_child_through_its_parent(rules, now) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_tickets=("ZEP-100",)),
        tickets=[
            ModelTicketSource(ticket_id="ZEP-100", state_type="started"),
            ModelTicketSource(
                ticket_id="ZEP-101", state_type="backlog", parent="ZEP-100"
            ),
        ],
    )
    assert _item(result, "ticket:ZEP-101").scope is EnumScopeStatus.IN_SCOPE


def test_stopped_lane_with_pushed_branch_reads_stopped(rules, now, row) -> None:
    """A lane whose CLAIM names the ticket and whose TERMINAL left a pushed branch reads stopped with what is left."""
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_tickets=("ZEP-7",)),
        tickets=[ModelTicketSource(ticket_id="ZEP-7", state_type="started")],
        ledger_rows=[
            row("CLAIM", "lane=fix-7", "ticket=ZEP-7", minutes_ago=90),
            row(
                "TERMINAL",
                "lane=fix-7",
                "ticket=ZEP-7",
                "outcome=partial",
                "branch=zep-7-fix",
                "head=abc123",
                minutes_ago=20,
            ),
        ],
    )
    item = _item(result, "ticket:ZEP-7")
    assert item.state is EnumWorkItemState.STOPPED
    assert item.lanes == ("fix-7",)
    assert item.left is not None
    assert "branch=zep-7-fix" in item.left
    assert "head=abc123" in item.left
    assert "outcome=partial" in item.left
    lane = _item(result, "lane:fix-7")
    assert lane.kind is EnumWorkItemKind.LANE
    assert lane.state is EnumWorkItemState.STOPPED
    assert item in result.ready()


def test_injected_break_partial_read_as_done_hides_the_stopped_lane(
    rules, now, row
) -> None:
    """Injected break: an overlay that counts `partial` as done makes the stopped lane vanish from the item."""
    broken = rules.model_copy(
        update={"terminal_done_outcomes": (*rules.terminal_done_outcomes, "partial")}
    )
    result = _project(
        now=now,
        rules=broken,
        scope=ModelWorkScope(in_scope_tickets=("ZEP-7",)),
        tickets=[ModelTicketSource(ticket_id="ZEP-7", state_type="started")],
        ledger_rows=[
            row("CLAIM", "lane=fix-7", "ticket=ZEP-7", minutes_ago=90),
            row(
                "TERMINAL",
                "lane=fix-7",
                "outcome=partial",
                "branch=zep-7-fix",
                "head=abc123",
                minutes_ago=20,
            ),
        ],
    )
    assert _item(result, "ticket:ZEP-7").state is not EnumWorkItemState.STOPPED


def test_live_lane_marks_in_progress(rules, now, row) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        tickets=[ModelTicketSource(ticket_id="ZEP-8", state_type="started")],
        ledger_rows=[row("CLAIM", "lane=work-8", "ticket=ZEP-8", minutes_ago=30)],
    )
    item = _item(result, "ticket:ZEP-8")
    assert item.state is EnumWorkItemState.IN_PROGRESS
    assert item.live_lane == "work-8"
    assert _item(result, "lane:work-8").state is EnumWorkItemState.IN_PROGRESS


def test_lane_silent_past_the_live_window_reads_stopped(rules, now, row) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        tickets=[ModelTicketSource(ticket_id="ZEP-8", state_type="started")],
        ledger_rows=[row("CLAIM", "lane=work-8", "ticket=ZEP-8", minutes_ago=60 * 5)],
    )
    item = _item(result, "ticket:ZEP-8")
    assert item.state is EnumWorkItemState.STOPPED
    assert "no closing row" in item.reason


def test_receipt_ended_with_no_terminal_reads_stopped(rules, now, row) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        tickets=[ModelTicketSource(ticket_id="ZEP-8", state_type="started")],
        ledger_rows=[row("CLAIM", "lane=work-8", "ticket=ZEP-8", minutes_ago=10)],
        receipts=[ModelLaneReceiptSource(lane="work-8", status="exited", exit_code=1)],
    )
    assert _item(result, "lane:work-8").state is EnumWorkItemState.STOPPED


def test_done_lane_on_open_ticket_leaves_it_available(rules, now, row) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        tickets=[ModelTicketSource(ticket_id="ZEP-8", state_type="started")],
        ledger_rows=[
            row("CLAIM", "lane=work-8", "ticket=ZEP-8", minutes_ago=100),
            row("TERMINAL", "lane=work-8", "outcome=done", minutes_ago=50),
        ],
    )
    assert _item(result, "ticket:ZEP-8").state is EnumWorkItemState.AVAILABLE
    assert _item(result, "lane:work-8").state is EnumWorkItemState.DONE


def test_plan_task_waits_on_its_prerequisite_task(rules, now) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_plans=("plans/new-plan.md",)),
        plan_tasks=[
            ModelPlanTaskSource(plan="plans/new-plan.md", task_id="T1"),
            ModelPlanTaskSource(
                plan="plans/new-plan.md",
                task_id="T2",
                requires=("plan:plans/new-plan.md#T1",),
            ),
        ],
    )
    t1 = _item(result, "plan:plans/new-plan.md#T1")
    t2 = _item(result, "plan:plans/new-plan.md#T2")
    assert t1.state is EnumWorkItemState.AVAILABLE
    assert t1.scope is EnumScopeStatus.IN_SCOPE
    assert t2.state is EnumWorkItemState.BLOCKED
    assert t2.blocking == ("plan:plans/new-plan.md#T1",)


def test_plan_task_done_when_its_ticket_is_done(rules, now) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        tickets=[ModelTicketSource(ticket_id="ZEP-3", state_type="completed")],
        plan_tasks=[ModelPlanTaskSource(plan="p.md", task_id="T1", ticket="ZEP-3")],
    )
    assert _item(result, "plan:p.md#T1").state is EnumWorkItemState.DONE


def test_reading_criterion(rules, now) -> None:
    """A criterion reference to a reading reads true only when the reading has the named value, unread when absent."""
    tickets = [
        ModelTicketSource(
            ticket_id="ZEP-5", state_type="backlog", requires=("reading:M0=PASS",)
        )
    ]
    passing = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        tickets=tickets,
        readings=[ModelReadingSource(key="M0", value="PASS")],
    )
    failing = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        tickets=tickets,
        readings=[ModelReadingSource(key="M0", value="FAIL")],
    )
    absent = _project(now=now, rules=rules, scope=ModelWorkScope(), tickets=tickets)
    assert _item(passing, "ticket:ZEP-5").state is EnumWorkItemState.AVAILABLE
    assert _item(failing, "ticket:ZEP-5").state is EnumWorkItemState.BLOCKED
    assert _item(failing, "ticket:ZEP-5").blocking == ("reading:M0",)
    unread = _item(absent, "ticket:ZEP-5")
    assert unread.state is EnumWorkItemState.BLOCKED
    assert unread.criterion is EnumCriterionReading.UNREAD


def test_unknown_prerequisite_reads_unread_not_true(rules, now) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        tickets=[
            ModelTicketSource(
                ticket_id="ZEP-6", state_type="backlog", requires=("ZEP-404",)
            )
        ],
    )
    item = _item(result, "ticket:ZEP-6")
    assert item.criterion is EnumCriterionReading.UNREAD
    assert item.state is EnumWorkItemState.BLOCKED


def test_pull_request_and_its_red_checks(rules, now, row) -> None:
    pr = ModelPullRequestSource(
        repo="omnimarket",
        number=42,
        ticket="ZEP-4",
        head_sha="f00",
        state="OPEN",
        controller_decision="red",
        red_required_checks=("Tests Gate", "Lint"),
        cause_keys={"Tests Gate": "cause:OmniNode-ai/omnimarket:aa11"},
    )
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_repos=("omnimarket",)),
        pull_requests=[pr],
        ledger_rows=[
            row(
                "CLAIM",
                "lane=red-fix",
                "cause=cause:OmniNode-ai/omnimarket:aa11",
                minutes_ago=5,
            )
        ],
    )
    item = _item(result, "pr:omnimarket#42")
    assert item.state is EnumWorkItemState.BLOCKED
    assert item.blocking == (
        "check:omnimarket#42:Lint",
        "check:omnimarket#42:Tests Gate",
    )
    tests_gate = _item(result, "check:omnimarket#42:Tests Gate")
    assert tests_gate.state is EnumWorkItemState.IN_PROGRESS
    assert tests_gate.live_lane == "red-fix"
    lint = _item(result, "check:omnimarket#42:Lint")
    assert lint.state is EnumWorkItemState.AVAILABLE
    assert lint.scope is EnumScopeStatus.IN_SCOPE


def test_green_open_pull_request_is_in_landing_and_merged_is_done(rules, now) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        pull_requests=[
            ModelPullRequestSource(
                repo="r", number=1, state="OPEN", controller_decision="arm"
            ),
            ModelPullRequestSource(repo="r", number=2, state="MERGED"),
        ],
        tickets=[ModelTicketSource(ticket_id="ZEP-11", state_type="started")],
    )
    assert _item(result, "pr:r#1").state is EnumWorkItemState.IN_PROGRESS
    assert "arm" in _item(result, "pr:r#1").reason
    assert _item(result, "pr:r#2").state is EnumWorkItemState.DONE


def test_open_pull_request_carries_its_ticket_in_progress(rules, now) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(),
        pull_requests=[
            ModelPullRequestSource(repo="r", number=1, ticket="ZEP-11", state="OPEN")
        ],
        tickets=[ModelTicketSource(ticket_id="ZEP-11", state_type="started")],
    )
    item = _item(result, "ticket:ZEP-11")
    assert item.state is EnumWorkItemState.IN_PROGRESS
    assert "pr:r#1" in item.reason


def test_unread_sources_are_carried(rules, now) -> None:
    result = _project(
        now=now, rules=rules, scope=ModelWorkScope(), unread=("linear:snapshot absent",)
    )
    assert result.unread == ("linear:snapshot absent",)


def test_same_request_gives_identical_output(rules, now, row) -> None:
    fields: dict[str, object] = {
        "now": now,
        "rules": rules,
        "scope": ModelWorkScope(),
        "tickets": [ModelTicketSource(ticket_id="ZEP-8", state_type="started")],
        "ledger_rows": [row("CLAIM", "lane=work-8", "ticket=ZEP-8", minutes_ago=30)],
    }
    assert _project(**fields).model_dump_json() == _project(**fields).model_dump_json()


def test_pull_request_scope_lists(rules, now) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(
            in_scope_prs=("r#1",), paused_prs=("r#2",), in_scope_repos=("r",)
        ),
        pull_requests=[
            ModelPullRequestSource(repo="r", number=1, state="OPEN"),
            ModelPullRequestSource(repo="r", number=2, state="OPEN"),
        ],
    )
    assert _item(result, "pr:r#1").scope is EnumScopeStatus.IN_SCOPE
    assert _item(result, "pr:r#2").scope is EnumScopeStatus.PAUSED
