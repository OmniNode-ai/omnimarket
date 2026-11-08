# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The source mappers build the projection's rows from the sources' own shapes: a plan file's text, the Linear
snapshot and the PR watcher state. Any plan file and any project is read by the overlay's patterns, with no code
change; nothing here reads the floor record."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_work_state_projection_compute.handlers.work_state_source_mapping import (
    plan_tasks_from_text,
    pull_requests_from_watcher,
    scope_from_declaration,
    tickets_from_linear_snapshot,
)

pytestmark = pytest.mark.unit

PLAN = """# A brand new plan

## 4. Tasks

### T1. First node
- **Ticket:** ZEP-50
- **Status:** merged

### T2. Second node
- **Ticket:** ZEP-51
- **Depends on:** T1, ZEP-49, omnimarket#9, reading:M0=PASS

### T3. Third node
"""


def test_plan_tasks_from_any_plan_text(rules) -> None:
    tasks = {t.task_id: t for t in plan_tasks_from_text("plans/new.md", PLAN, rules)}
    assert sorted(tasks) == ["T1", "T2", "T3"]
    assert tasks["T1"].done is True
    assert tasks["T1"].ticket == "ZEP-50"
    assert tasks["T2"].requires == (
        "plan:plans/new.md#T1",
        "ticket:ZEP-49",
        "pr:omnimarket#9",
        "reading:M0=PASS",
    )
    assert tasks["T3"].ticket is None
    assert tasks["T3"].done is False


def test_tickets_from_linear_snapshot(rules) -> None:
    snapshot = {
        "operator": "user-1",
        "issues": [
            {
                "id": "ZEP-60",
                "title": "t",
                "status": "Backlog",
                "statusType": "backlog",
                "assignee": "user-1",
                "project": "Zephyr",
                "updatedAt": "2026-11-07T00:00:00Z",
                "description": "Why.\n\nStart criterion: ZEP-59, reading:M0=PASS\n",
            }
        ],
    }
    (ticket,) = tickets_from_linear_snapshot(snapshot, rules)
    assert ticket.ticket_id == "ZEP-60"
    assert ticket.project == "Zephyr"
    assert ticket.state_type == "backlog"
    assert ticket.requires == ("ticket:ZEP-59", "reading:M0=PASS")


def test_pull_requests_from_watcher_keep_required_reds_only(rules) -> None:
    state = {
        "prs": {
            "omnimarket#9": {
                "cls": "red",
                "reason": "red: Tests Gate",
                "ci": {"red": ["Tests Gate", "lint (shadow)"]},
                "facts": {
                    "repo": "omnimarket",
                    "number": 9,
                    "state": "OPEN",
                    "draft": False,
                    "head_sha": "abc",
                    "title": "fix(ZEP-61): a thing",
                },
            }
        },
        "settings": {"omnimarket": {"branches": {"dev": {"contexts": ["Tests Gate"]}}}},
    }
    (pr,) = pull_requests_from_watcher(state, rules, base_by_repo={"omnimarket": "dev"})
    assert pr.ticket == "ZEP-61"
    assert pr.red_required_checks == ("Tests Gate",)
    assert pr.controller_decision == "red"
    assert pr.state == "OPEN"


def test_scope_from_declaration_reads_only_the_keys_it_knows() -> None:
    scope = scope_from_declaration(
        {
            "in_scope_tickets": ["ZEP-1"],
            "in_scope_projects": ["Zephyr"],
            "repo_work_kinds": ["dev-red"],
        }
    )
    assert scope.in_scope_tickets == ("ZEP-1",)
    assert scope.in_scope_projects == ("Zephyr",)


def test_scope_takes_the_ticket_rule_roots_and_the_pull_request_lists() -> None:
    scope = scope_from_declaration(
        {
            "in_scope_prs": ["omnimarket#9"],
            "paused_prs": ["omnimarket#8"],
            "in_scope_repos": ["web"],
        },
        {"ticket_roots": ["ZEP-100"], "endpoint": "ignored"},
    )
    assert scope.in_scope_tickets == ("ZEP-100",)
    assert scope.in_scope_prs == ("omnimarket#9",)
    assert scope.paused_prs == ("omnimarket#8",)
