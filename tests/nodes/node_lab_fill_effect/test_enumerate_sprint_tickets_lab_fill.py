# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Sprint-ticket enumerate chains (OMN-23250): project issues, defects, red and stalled PRs, fences, per-stage floors.

The clock is scripted: each fake read advances it by its planted cost, or by the budget it was given
and then raises LabFillSourceCutError when the cost is more.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pytest

from omnimarket.nodes.node_lab_fill_effect.handlers import HandlerLabFillSprintTickets
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillSprintTicketsRequest,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import (
    LabFillIssueNode,
    LabFillPortError,
    LabFillSourceCutError,
    LabFillWatcherPr,
)

from .fakes import FakeClock


def _issue(n: int, state: str = "started", **over: object) -> LabFillIssueNode:
    base = {
        "ticket": f"OMN-{n}",
        "title": f"title {n}",
        "state_type": state,
        "updated_at": "2026-11-09T00:00:00Z",
        "labels": ("lab",),
        "milestone": "M1",
        "project_id": "proj",
        "assignee_id": "",
        "has_children": False,
        "readable": True,
        "description_repo": "omnimarket",
        "prs": (),
        "merged_implementations": (),
    }
    base.update(over)
    return LabFillIssueNode(**base)  # type: ignore[arg-type]


def _pr(n: int, ticket: int, **over: object) -> LabFillWatcherPr:
    base = {
        "repo": "omnimarket",
        "number": n,
        "title": f"fix(OMN-{ticket}): x",
        "head_ref": f"omn-{ticket}-x",
        "labels": (),
        "updated_at": "2026-11-09T00:00:00Z",
        "idle_hours": 1.0,
        "head_read": True,
        "ci_head_matches": True,
        "ci_verdict": "GREEN",
    }
    base.update(over)
    return LabFillWatcherPr(**base)  # type: ignore[arg-type]


def _request(**over: object) -> ModelLabFillSprintTicketsRequest:
    base: dict[str, object] = {
        "project_id": "proj",
        "ticket_state_types": ("backlog", "unstarted", "started"),
        "defect_state_types": ("unstarted", "started"),
        "defect_labels": ("defect",),
        "defect_terms": (),
        "pr_repos": ("omnimarket",),
        "stalled_hours": 24.0,
        "budget_s": 80.0,
        "stage_floor_s": 10.0,
    }
    base.update(over)
    return ModelLabFillSprintTicketsRequest(**base)  # type: ignore[arg-type]


@dataclass
class World:
    clock: FakeClock
    project: Sequence[LabFillIssueNode] = ()
    project_cost: float = 1.0
    defects: Sequence[LabFillIssueNode] = ()
    defects_cost: float = 1.0
    assignments: Mapping[str, LabFillIssueNode] = field(default_factory=dict)
    prs: Sequence[LabFillWatcherPr] = ()
    prs_error: str = ""
    fences: tuple[str, ...] = ()
    fences_error: str = ""
    budgets: dict[str, float] = field(default_factory=dict)
    assignment_asks: list[Sequence[str]] = field(default_factory=list)

    def _spend(self, stage: str, cost: float, budget: float) -> None:
        self.budgets[stage] = budget
        if cost > budget:
            self.clock.t += budget
            raise LabFillSourceCutError(f"{stage} cut after {budget} s")
        self.clock.t += cost

    # ProtocolLabFillSprintIssues
    def project_issues(
        self, project_id: str, state_types: Sequence[str], budget_s: float
    ) -> Sequence[LabFillIssueNode]:
        self._spend("project-issues", self.project_cost, budget_s)
        return self.project

    def defect_issues(
        self, labels: Sequence[str], terms: Sequence[str], budget_s: float
    ) -> Sequence[LabFillIssueNode]:
        self._spend("defects", self.defects_cost, budget_s)
        return self.defects

    def ticket_issues(
        self, tickets: Sequence[str], budget_s: float
    ) -> Mapping[str, LabFillIssueNode]:
        self.assignment_asks.append(tuple(tickets))
        self._spend("pr-assignments", 1.0, budget_s)
        return self.assignments

    def watcher(self, repos: Sequence[str]) -> Sequence[LabFillWatcherPr]:
        if self.prs_error:
            raise LabFillPortError(self.prs_error)
        return self.prs

    def read_fences(self, budget_s: float) -> tuple[str, ...]:
        if self.fences_error:
            raise LabFillPortError(self.fences_error)
        return self.fences

    def handler(self) -> HandlerLabFillSprintTickets:
        issues = _Issues(self)
        return HandlerLabFillSprintTickets(
            issues=issues,
            watcher=self.watcher,
            fences=self.read_fences,
            clock=self.clock,
        )


class _Issues:
    def __init__(self, world: World) -> None:
        self.project = world.project_issues
        self.defects = world.defect_issues
        self.tickets = world.ticket_issues


@pytest.fixture
def world() -> World:
    return World(clock=FakeClock())


def test_golden_chain_tickets_defects_prs_and_fences(world: World) -> None:
    world.project = [_issue(1), _issue(2, "completed"), _issue(3, has_children=True)]
    world.defects = [_issue(4, labels=("defect",))]
    world.prs = [
        _pr(10, 1, ci_verdict="RED"),
        _pr(11, 5, idle_hours=30.0, labels=("hold:ci", "hold:auto-merge")),
        _pr(12, 6, idle_hours=2.0),  # green and fresh: neither
        _pr(13, 7, ci_head_matches=False, ci_verdict="RED"),  # no verdict for this head
    ]
    world.assignments = {"OMN-5": _issue(5, assignee_id="person")}
    world.fences = ("OMN-9",)
    result = world.handler().handle(_request())
    by = {(c.kind, c.ticket): c for c in result.candidates}
    assert set(by) == {
        ("ticket", "OMN-1"),
        ("defect", "OMN-4"),
        ("pr-red", "OMN-1"),
        ("pr-stalled", "OMN-5"),
    }
    assert by[("pr-red", "OMN-1")].pr == "omnimarket#10"
    assert by[("pr-red", "OMN-1")].under_parent is True
    assert by[("pr-red", "OMN-1")].milestone == "M1"
    assert by[("pr-stalled", "OMN-5")].assignee_id == "person"
    assert by[("pr-stalled", "OMN-5")].hold_reason == "hold:ci"
    assert by[("defect", "OMN-4")].under_parent is False
    assert world.assignment_asks == [("OMN-5",)]
    assert result.fenced_tickets == ("OMN-9",)
    assert result.stale == ()
    assert result.unreadable == ()
    assert set(result.diagnostics.values()) == {"ok"}


def test_ac1_slow_project_read_cannot_starve_the_later_stages(world: World) -> None:
    world.project = [_issue(1)]
    world.project_cost = 45.0
    world.defects = [_issue(4)]
    world.fences = ("OMN-9",)
    result = world.handler().handle(_request(budget_s=80.0, stage_floor_s=10.0))
    assert result.stale == ()
    assert world.budgets["defects"] >= 10.0
    assert [c.ticket for c in result.candidates] == ["OMN-1", "OMN-4"]
    assert result.fenced_tickets == ("OMN-9",)


def test_ac1_failing_control_floor_zero_is_the_shared_budget(world: World) -> None:
    """With the floor at 0 a slow earlier stage leaves the next one under 10 s; with it set it has 10 s."""
    world.project = [_issue(1)]
    world.project_cost = 79.0
    world.handler().handle(_request(budget_s=80.0, stage_floor_s=0.0))
    floorless = world.budgets["defects"]
    other = World(clock=FakeClock(), project=[_issue(1)], project_cost=69.0)
    other.handler().handle(_request(budget_s=80.0, stage_floor_s=10.0))
    assert floorless < 10.0 <= other.budgets["defects"]


def test_ac2_cut_stage_keeps_the_others_and_marks_only_itself(world: World) -> None:
    world.project = [_issue(1)]
    world.defects = [_issue(4)]
    world.defects_cost = 1000.0
    world.fences = ("OMN-9",)
    result = world.handler().handle(_request())
    assert result.stale == ("defects",)
    assert result.unreadable == ()
    assert result.diagnostics["defect"] == "stale:defects"
    assert [c.ticket for c in result.candidates] == ["OMN-1"]
    assert result.fenced_tickets == ("OMN-9",)


def test_error_chain_project_read_unreadable_still_reads_prs_and_fences(
    world: World,
) -> None:
    world.project_cost = 0.0
    world.prs = [_pr(10, 1, ci_verdict="RED")]
    world.assignments = {"OMN-1": _issue(1)}
    world.fences = ("OMN-9",)

    class Down(_Issues):
        def __init__(self, w: World) -> None:
            super().__init__(w)
            self.project = lambda *_a: (_ for _ in ()).throw(
                LabFillPortError("Linear unreachable")
            )

    handler = HandlerLabFillSprintTickets(
        issues=Down(world),
        watcher=world.watcher,
        fences=world.read_fences,
        clock=world.clock,
    )
    result = handler.handle(_request())
    assert result.unreadable == ("project-issues",)
    assert result.diagnostics["ticket"] == "unreadable:project-issues"
    assert [(c.kind, c.ticket) for c in result.candidates] == [("pr-red", "OMN-1")]
    assert result.fenced_tickets == ("OMN-9",)


def test_error_chain_watcher_unreadable_marks_both_pr_kinds(world: World) -> None:
    world.project = [_issue(1)]
    world.prs_error = "unreadable-from-snapshot: watcher freshness/schema"
    result = world.handler().handle(_request())
    assert (
        result.diagnostics["pr-red"]
        == result.diagnostics["pr-stalled"]
        == "unreadable-from-snapshot"
    )
    assert [c.kind for c in result.candidates] == ["ticket"]


def test_error_chain_unread_head_and_unread_ticket_are_partial_not_dropped_silently(
    world: World,
) -> None:
    world.prs = [_pr(10, 1, head_read=False), _pr(11, 8, ci_verdict="RED")]
    world.assignments = {}  # OMN-8 is outside the project and unreadable
    result = world.handler().handle(_request())
    assert result.diagnostics["pr-red"] == "partial:ticket-unreadable"
    assert result.diagnostics["pr-stalled"] == "partial:unreadable-head"
    assert [c.kind for c in result.candidates] == []


def test_error_chain_fences_unreadable_is_named(world: World) -> None:
    world.fences_error = "triage-fences exit 1"
    result = world.handler().handle(_request())
    assert result.unreadable == ("triage-fences",)
    assert result.fenced_tickets == ()


def test_no_defect_declaration_reads_no_defects(world: World) -> None:
    result = world.handler().handle(_request(defect_labels=(), defect_terms=()))
    assert [s.state for s in result.stages if s.stage == "defects"] == ["empty"]


def test_unwired_host_refuses_by_name() -> None:
    with pytest.raises(LabFillPortError, match="enumerate_lab_fill_sprint_tickets"):
        HandlerLabFillSprintTickets().handle(_request())
