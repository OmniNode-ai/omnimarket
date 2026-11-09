# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection reads scope along the Linear parent chain, and flags pull requests that cite no ticket (OMN-23184).

The fixtures are the cases that fell out of the floor's work list: omnimarket#5389 and #5390 (a child with no project
under a goal root, four hops up), a child with no project under a ticket of the Factory project, and omnimarket#5445
(a ticket with no project and no parent, which stays out). One function decides scope for tickets, plan tasks and pull
requests; the hop bound and the author exclusions are the rules overlay's.
"""

from __future__ import annotations

import pytest

from omnimarket.models.work_state_projection import (
    EnumScopeStatus,
    ModelPlanTaskSource,
    ModelPullRequestSource,
    ModelTicketSource,
    ModelWorkScope,
    ModelWorkStateRequest,
    ModelWorkStateResult,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.work_state_source_mapping import (
    pull_requests_from_watcher,
    tickets_from_linear_snapshot,
)

pytestmark = pytest.mark.unit

FACTORY_ID = "ae87f221-ad60-49fe-aeae-b4417960ff90"
ROOT = "OMN-22478"


def _project(**fields: object) -> ModelWorkStateResult:
    return HandlerWorkStateProjection().handle(
        ModelWorkStateRequest.model_validate(fields)
    )


def _scope_of(result: ModelWorkStateResult, key: str) -> EnumScopeStatus:
    (found,) = [i for i in result.items if i.key == key]
    return found.scope


def _issue(
    ident: str, parent_id: str | None = None, project: str = "", **extra: object
):
    """One issue as the snapshot writer spells it."""
    return {
        "id": ident,
        "title": f"title of {ident}",
        "status": "In Progress",
        "statusType": "started",
        "assignee": "user-1",
        "project": project,
        "updatedAt": "2026-11-08T00:00:00Z",
        "description": "",
        **({"parentId": parent_id} if parent_id is not None else {}),
        **extra,
    }


def _pr(number: int, ticket: str | None, **fields: object) -> ModelPullRequestSource:
    return ModelPullRequestSource(
        repo="omnimarket",
        number=number,
        title=f"change {number}",
        ticket=ticket,
        state="OPEN",
        **fields,
    )


def test_a_child_with_no_project_four_hops_under_a_goal_root_is_in_scope(
    rules, now
) -> None:
    """omnimarket#5389/#5390: OMN-23229 -> OMN-23127 -> OMN-22880 -> OMN-22478 (root), none of them in a project."""
    snapshot = {
        "operator": "user-1",
        "issues": [
            _issue("OMN-23229", "OMN-23127"),
            _issue("OMN-23127", "OMN-22880"),
            _issue("OMN-22880", ROOT),
            _issue(ROOT, None),
        ],
    }
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_tickets=(ROOT,)),
        tickets=tickets_from_linear_snapshot(snapshot, rules),
        pull_requests=[_pr(5389, "OMN-23229"), _pr(5390, "OMN-23229")],
        plan_tasks=[ModelPlanTaskSource(plan="p.md", task_id="T1", ticket="OMN-23229")],
    )
    assert _scope_of(result, "ticket:OMN-23229") is EnumScopeStatus.IN_SCOPE
    assert _scope_of(result, "pr:omnimarket#5389") is EnumScopeStatus.IN_SCOPE
    assert _scope_of(result, "pr:omnimarket#5390") is EnumScopeStatus.IN_SCOPE
    assert _scope_of(result, "plan:p.md#T1") is EnumScopeStatus.IN_SCOPE


def test_a_child_with_no_project_under_a_factory_project_ticket_is_in_scope(
    rules, now
) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_projects=("Factory",)),
        tickets=[
            ModelTicketSource(
                ticket_id="OMN-100", state_type="started", project="Factory"
            ),
            ModelTicketSource(
                ticket_id="OMN-101", state_type="started", parent="OMN-100"
            ),
            ModelTicketSource(
                ticket_id="OMN-102", state_type="started", parent="OMN-101"
            ),
        ],
        pull_requests=[_pr(7, "OMN-102")],
    )
    assert _scope_of(result, "ticket:OMN-100") is EnumScopeStatus.IN_SCOPE
    assert _scope_of(result, "ticket:OMN-101") is EnumScopeStatus.IN_SCOPE
    assert _scope_of(result, "ticket:OMN-102") is EnumScopeStatus.IN_SCOPE
    assert _scope_of(result, "pr:omnimarket#7") is EnumScopeStatus.IN_SCOPE


def test_a_project_is_matched_by_name_or_by_id(rules, now) -> None:
    snapshot = {
        "operator": "user-1",
        "issues": [
            _issue("OMN-200", None, project="Factory", projectId=FACTORY_ID),
            _issue("OMN-201", "OMN-200"),
            _issue("OMN-300", None, projectId=FACTORY_ID),
            _issue("OMN-301", "OMN-300"),
        ],
    }
    by_name = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_projects=("Factory",)),
        tickets=tickets_from_linear_snapshot(snapshot, rules),
    )
    assert _scope_of(by_name, "ticket:OMN-201") is EnumScopeStatus.IN_SCOPE
    by_id = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_projects=(FACTORY_ID,)),
        tickets=tickets_from_linear_snapshot(snapshot, rules),
    )
    assert _scope_of(by_id, "ticket:OMN-201") is EnumScopeStatus.IN_SCOPE
    assert _scope_of(by_id, "ticket:OMN-301") is EnumScopeStatus.IN_SCOPE


def test_omnimarket_5445_a_ticket_with_no_project_and_no_parent_stays_out(
    rules, now
) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_tickets=(ROOT,), in_scope_projects=("Factory",)),
        tickets=tickets_from_linear_snapshot(
            {"operator": "user-1", "issues": [_issue("OMN-14172", None)]}, rules
        ),
        pull_requests=[_pr(5445, "OMN-14172")],
    )
    assert _scope_of(result, "ticket:OMN-14172") is EnumScopeStatus.PAUSED
    assert _scope_of(result, "pr:omnimarket#5445") is EnumScopeStatus.PAUSED


def test_a_chain_reaching_no_root_and_no_scoped_project_stays_out(rules, now) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_tickets=(ROOT,), in_scope_projects=("Factory",)),
        tickets=[
            ModelTicketSource(ticket_id="OMN-1", state_type="started", project="Other"),
            ModelTicketSource(ticket_id="OMN-2", state_type="started", parent="OMN-1"),
            ModelTicketSource(ticket_id="OMN-3", state_type="started", parent="OMN-2"),
        ],
    )
    assert _scope_of(result, "ticket:OMN-3") is EnumScopeStatus.PAUSED


def test_a_paused_ticket_on_the_chain_wins_over_the_root_above_it(rules, now) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_tickets=(ROOT,), paused_tickets=("OMN-2",)),
        tickets=[
            ModelTicketSource(ticket_id="OMN-1", state_type="started", parent=ROOT),
            ModelTicketSource(ticket_id="OMN-2", state_type="started", parent="OMN-1"),
            ModelTicketSource(ticket_id="OMN-3", state_type="started", parent="OMN-2"),
        ],
        pull_requests=[_pr(3, "OMN-3")],
    )
    assert _scope_of(result, "ticket:OMN-1") is EnumScopeStatus.IN_SCOPE
    assert _scope_of(result, "ticket:OMN-2") is EnumScopeStatus.PAUSED
    assert _scope_of(result, "ticket:OMN-3") is EnumScopeStatus.PAUSED
    assert _scope_of(result, "pr:omnimarket#3") is EnumScopeStatus.PAUSED


def test_a_paused_ticket_wins_over_a_scoped_project_of_the_same_chain(
    rules, now
) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_projects=("Factory",), paused_tickets=("OMN-2",)),
        tickets=[
            ModelTicketSource(
                ticket_id="OMN-1", state_type="started", project="Factory"
            ),
            ModelTicketSource(ticket_id="OMN-2", state_type="started", parent="OMN-1"),
            ModelTicketSource(ticket_id="OMN-3", state_type="started", parent="OMN-2"),
        ],
    )
    assert _scope_of(result, "ticket:OMN-3") is EnumScopeStatus.PAUSED


def test_a_paused_pr_stays_out_though_its_ticket_chain_reaches_the_root(
    rules, now
) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_tickets=(ROOT,), paused_prs=("omnimarket#9",)),
        tickets=[
            ModelTicketSource(ticket_id="OMN-1", state_type="started", parent=ROOT)
        ],
        pull_requests=[_pr(9, "OMN-1"), _pr(10, "OMN-1")],
    )
    assert _scope_of(result, "pr:omnimarket#9") is EnumScopeStatus.PAUSED
    assert _scope_of(result, "pr:omnimarket#10") is EnumScopeStatus.IN_SCOPE


def test_the_hop_bound_is_the_rules_and_a_longer_chain_stays_out(rules, now) -> None:
    tickets = [
        ModelTicketSource(
            ticket_id=f"OMN-{n}", state_type="started", parent=f"OMN-{n + 1}"
        )
        for n in range(1, 9)
    ] + [ModelTicketSource(ticket_id="OMN-9", state_type="started")]
    scope = ModelWorkScope(in_scope_tickets=("OMN-9",))
    short = _project(
        now=now,
        rules=rules.model_copy(update={"scope_chain_max_hops": 3}),
        scope=scope,
        tickets=tickets,
    )
    assert _scope_of(short, "ticket:OMN-7") is EnumScopeStatus.IN_SCOPE
    assert _scope_of(short, "ticket:OMN-6") is EnumScopeStatus.IN_SCOPE
    assert _scope_of(short, "ticket:OMN-5") is EnumScopeStatus.PAUSED
    wide = _project(
        now=now,
        rules=rules.model_copy(update={"scope_chain_max_hops": 8}),
        scope=scope,
        tickets=tickets,
    )
    assert _scope_of(wide, "ticket:OMN-1") is EnumScopeStatus.IN_SCOPE


def test_a_parent_cycle_ends_the_walk(rules, now) -> None:
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_tickets=(ROOT,)),
        tickets=[
            ModelTicketSource(ticket_id="OMN-1", state_type="started", parent="OMN-2"),
            ModelTicketSource(ticket_id="OMN-2", state_type="started", parent="OMN-1"),
        ],
    )
    assert _scope_of(result, "ticket:OMN-1") is EnumScopeStatus.PAUSED


def test_an_old_snapshot_without_the_new_keys_reads_as_before(rules) -> None:
    (ticket,) = tickets_from_linear_snapshot(
        {"operator": "user-1", "issues": [_issue("OMN-5", None, project="Zephyr")]},
        rules,
    )
    assert ticket.parent is None
    assert ticket.project == "Zephyr"
    assert ticket.project_id == ""


def test_the_snapshot_parent_id_becomes_the_ticket_parent(rules) -> None:
    (ticket,) = tickets_from_linear_snapshot(
        {
            "operator": "user-1",
            "issues": [_issue("OMN-23229", "OMN-23127", projectId=FACTORY_ID)],
        },
        rules,
    )
    assert ticket.parent == "OMN-23127"
    assert ticket.project_id == FACTORY_ID


# --- pull requests that cite no ticket ------------------------------------------------------------------------------


def _watcher_state(*records: dict) -> dict:
    return {
        "prs": {
            f"omnimarket#{r['number']}": {
                "cls": "green",
                "facts": {"repo": "omnimarket", "state": "OPEN", "draft": False, **r},
            }
            for r in records
        }
    }


def _flagged(result: ModelWorkStateResult) -> list[str]:
    return sorted(i.key for i in result.items if i.needs_ticket)


def test_an_open_pr_that_cites_no_ticket_is_flagged_and_a_bot_pr_is_not(
    rules, now
) -> None:
    state = _watcher_state(
        {
            "number": 1,
            "title": "no ticket here",
            "author": "alice",
            "body": "Why.",
        },
        {
            "number": 2,
            "title": "fix(OMN-5): has one",
            "author": "alice",
            "body": "",
        },
        {
            "number": 3,
            "title": "no title ticket",
            "author": "alice",
            "body": "x\nTicket: OMN-77\n",
        },
        {"number": 4, "title": "Bump httpx", "author": "dependabot[bot]", "body": ""},
        {
            "number": 5,
            "title": "other bot",
            "author": "onexbot-occ-writer[bot]",
            "body": "",
        },
        {
            "number": 6,
            "title": "draft work",
            "author": "alice",
            "body": "",
            "draft": True,
        },
        {
            "number": 7,
            "title": "already merged",
            "author": "alice",
            "body": "",
            "state": "MERGED",
        },
    )
    prs = pull_requests_from_watcher(state, rules)
    result = _project(now=now, rules=rules, scope=ModelWorkScope(), pull_requests=prs)
    assert _flagged(result) == ["pr:omnimarket#1"]


def test_the_author_exclusions_are_the_rules_not_a_constant(rules, now) -> None:
    state = _watcher_state(
        {"number": 4, "title": "Bump httpx", "author": "dependabot[bot]", "body": ""},
        {"number": 8, "title": "by a robot", "author": "ci-robot", "body": ""},
    )
    prs = pull_requests_from_watcher(state, rules)
    assert _flagged(
        _project(now=now, rules=rules, scope=ModelWorkScope(), pull_requests=prs)
    ) == ["pr:omnimarket#8"]
    none_exempt = rules.model_copy(update={"ticketless_pr_exempt_author_patterns": ()})
    assert _flagged(
        _project(now=now, rules=none_exempt, scope=ModelWorkScope(), pull_requests=prs)
    ) == [
        "pr:omnimarket#4",
        "pr:omnimarket#8",
    ]
    robots = rules.model_copy(
        update={"ticketless_pr_exempt_author_patterns": ("^ci-robot$",)}
    )
    assert _flagged(
        _project(now=now, rules=robots, scope=ModelWorkScope(), pull_requests=prs)
    ) == ["pr:omnimarket#4"]


def test_a_paused_ticket_on_the_chain_leaves_a_sibling_branch_four_hops_down_in_scope(
    rules, now
) -> None:
    """The paused branch stays out while the unpaused sibling four hops under the root reads in scope."""
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(in_scope_tickets=(ROOT,), paused_tickets=("OMN-2",)),
        tickets=[
            ModelTicketSource(ticket_id="OMN-1", state_type="started", parent=ROOT),
            ModelTicketSource(ticket_id="OMN-2", state_type="started", parent="OMN-1"),
            ModelTicketSource(ticket_id="OMN-3", state_type="started", parent="OMN-2"),
            ModelTicketSource(ticket_id="OMN-4", state_type="started", parent="OMN-1"),
            ModelTicketSource(ticket_id="OMN-5", state_type="started", parent="OMN-4"),
            ModelTicketSource(ticket_id="OMN-6", state_type="started", parent="OMN-5"),
        ],
    )
    assert _scope_of(result, "ticket:OMN-3") is EnumScopeStatus.PAUSED
    assert _scope_of(result, "ticket:OMN-6") is EnumScopeStatus.IN_SCOPE


def test_a_paused_ticket_wins_over_a_project_matched_by_id_above_it(rules, now) -> None:
    """The project above the paused ticket is matched by id; the ticket below it stays paused, its sibling is in scope."""
    result = _project(
        now=now,
        rules=rules,
        scope=ModelWorkScope(
            in_scope_projects=(FACTORY_ID,), paused_tickets=("OMN-2",)
        ),
        tickets=tickets_from_linear_snapshot(
            {
                "operator": "user-1",
                "issues": [
                    _issue("OMN-1", None, projectId=FACTORY_ID),
                    _issue("OMN-2", "OMN-1"),
                    _issue("OMN-3", "OMN-2"),
                    _issue("OMN-4", "OMN-1"),
                ],
            },
            rules,
        ),
    )
    assert _scope_of(result, "ticket:OMN-3") is EnumScopeStatus.PAUSED
    assert _scope_of(result, "ticket:OMN-4") is EnumScopeStatus.IN_SCOPE
