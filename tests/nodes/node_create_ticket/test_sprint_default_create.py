# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane-minted ticket is created in the current sprint; one declared not planned stays in the Backlog.

The sprint comes from the resolver (a fixed fixture here), never from a literal in the code under
test. ``clear_project`` and ``repair_project`` leave a ticket in the current sprint alone.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.handler_lane_intake import ModelLaneIntakePolicy
from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
)
from omnimarket.models.sprint_resolution.model_sprint_resolution import (
    EnumSprintResolutionStatus,
    ModelSprintResolution,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    LinearTicketHttpGateway,
    ModelTicketGuardDecision,
    TicketGuardRefusedError,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_ticket_project import (
    HandlerTicketProject,
    LinearTicketProjectGateway,
    TicketProjectNotSetError,
)

SPRINT = ModelSprintResolution(
    status=EnumSprintResolutionStatus.RESOLVED,
    date="2031-05-06",
    project_id="fixture-project-id",
    project_name="Sprint 2031-05-05 → 2031-05-11: Fixture",
    milestone_id="fixture-milestone-id",
    milestone_name="Fixture Theme",
    due_date="2031-05-11",
)
NO_SPRINT = ModelSprintResolution(
    status=EnumSprintResolutionStatus.NO_SPRINT,
    date="2031-06-01",
    detail="no sprint project holds 2031-06-01; the ticket stays in the Backlog with no project",
)
POLICY = ModelLaneIntakePolicy(label="lane-created", stamp_prefix="created-by-lane:")


class _Admit:
    def check(self, tool_input: dict[str, object]) -> ModelTicketGuardDecision:
        self.seen = dict(tool_input)
        return ModelTicketGuardDecision(admitted=True, guard_path="fixture-guard")


class _Linear:
    """A fake Linear. A created issue carries ``project`` (what Linear shows on a readback)."""

    def __init__(
        self, *, parent_project: str | None = None, sprint_applies: bool = True
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.parent_project = parent_project
        self.sprint_applies = sprint_applies
        self.project: str | None = None

    def post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((query, variables))
        if "GetTeamByName" in query:
            return {"data": {"teams": {"nodes": [{"id": "team-1"}]}}}
        if "GetBacklogState" in query:
            return {"data": {"workflowStates": {"nodes": [{"id": "state-1"}]}}}
        if "issueLabels" in query:
            return {"data": {"issueLabels": {"nodes": [{"id": "label-1"}]}}}
        if "GetViewer" in query:
            return {"data": {"viewer": {"id": "user-1"}}}
        if "GetIssueByIdentifier" in query:
            return {"data": {"issue": {"id": "parent-uuid"}}}
        if "CreateIssueInSprint" in query:
            self.project = (
                SPRINT.project_name if self.sprint_applies else self.parent_project
            )
            return self._created()
        if "CreateIssue" in query:
            self.project = self.parent_project  # Linear copies a parent's project
            return self._created()
        if "issueUpdate" in query:
            self.project = None
            return {"data": {"issueUpdate": {"success": True, "issue": {}}}}
        return {
            "data": {
                "issue": {
                    "identifier": "OMN-1",
                    "createdAt": "2031-05-06T01:00:00.000Z",
                    "labels": {"nodes": [{"name": "lane-created"}]},
                    "project": {"name": self.project} if self.project else None,
                }
            }
        }

    @staticmethod
    def _created() -> dict[str, Any]:
        return {
            "data": {
                "issueCreate": {"issue": {"identifier": "OMN-1", "url": "https://x"}}
            }
        }

    def of(self, text: str) -> list[tuple[str, dict[str, Any]]]:
        return [c for c in self.calls if text in c[0]]


def _handler(
    fake: _Linear, resolution: ModelSprintResolution | None
) -> tuple[HandlerCreateTicket, _Admit]:
    class _Offline(LinearTicketHttpGateway):
        def _post(self, query: str, variables: dict[str, Any]) -> Any:
            return fake.post(query, variables)

    guard = _Admit()
    handler = HandlerCreateTicket(
        linear_client=_Offline("fixture-key"),
        ticket_guard=guard,
        lane_intake_policy=POLICY,
        sprint_resolver=(lambda: resolution) if resolution is not None else None,
        lane_id="fixture-lane",
    )
    return handler, guard


def test_a_lane_minted_ticket_is_created_in_the_sprint_with_milestone_and_due_date() -> (
    None
):
    fake = _Linear()
    handler, _ = _handler(fake, SPRINT)
    result = handler.handle(ModelCreateTicketRequest(title="t", description="d"))
    ((query, variables),) = fake.of("CreateIssueInSprint")
    assert variables["projectId"] == SPRINT.project_id
    assert variables["projectMilestoneId"] == SPRINT.milestone_id
    assert variables["dueDate"] == SPRINT.due_date
    assert variables["stateId"] == "state-1"  # still the Backlog state
    assert "projectId: $projectId" in query
    assert fake.of("issueUpdate") == []
    assert result.status == "created"
    assert (
        result.sprint_status,
        result.sprint_project,
        result.sprint_milestone,
        result.due_date,
    ) == (
        "resolved",
        SPRINT.project_name,
        SPRINT.milestone_name,
        SPRINT.due_date,
    )


def test_a_sprint_without_a_milestone_sends_no_milestone_variable() -> None:
    fake = _Linear()
    handler, _ = _handler(
        fake, SPRINT.model_copy(update={"milestone_id": "", "milestone_name": ""})
    )
    handler.handle(ModelCreateTicketRequest(title="t"))
    ((_, variables),) = fake.of("CreateIssueInSprint")
    assert "projectMilestoneId" not in variables


def test_a_ticket_declared_not_planned_stays_in_the_backlog_with_no_project() -> None:
    fake = _Linear()
    resolver_calls: list[int] = []
    handler, _ = _handler(fake, SPRINT)
    handler._injectable_sprint_resolver = lambda: resolver_calls.append(1) or SPRINT  # type: ignore[assignment,func-returns-value]
    result = handler.handle(
        ModelCreateTicketRequest(title="t", not_planned_this_sprint=True)
    )
    ((query, variables),) = fake.of("CreateIssue")
    assert "projectId: null" in query
    assert "$projectId" not in query
    assert not {"projectId", "projectMilestoneId", "dueDate"} & set(variables)
    assert fake.of("CreateIssueInSprint") == []
    assert resolver_calls == []
    assert result.status == "created"
    assert (result.sprint_status, result.sprint_project, result.due_date) == (
        "",
        "",
        "",
    )


def test_a_not_planned_sub_issue_still_has_the_inherited_project_cleared() -> None:
    fake = _Linear(parent_project=SPRINT.project_name)
    handler, _ = _handler(fake, SPRINT)
    handler.handle(
        ModelCreateTicketRequest(
            title="t", parent="OMN-17427", not_planned_this_sprint=True
        )
    )
    assert len(fake.of("issueUpdate")) == 1
    assert fake.project is None


def test_a_date_in_no_sprint_falls_back_to_the_backlog_and_says_so() -> None:
    fake = _Linear()
    handler, _ = _handler(fake, NO_SPRINT)
    result = handler.handle(ModelCreateTicketRequest(title="t"))
    ((query, _),) = fake.of("CreateIssue")
    assert "projectId: null" in query
    assert result.status == "created"
    assert result.sprint_status == "no_sprint"
    assert "Backlog" in result.sprint_detail
    assert result.sprint_project == ""


def test_a_create_whose_readback_shows_another_project_is_not_reported_placed() -> None:
    fake = _Linear(parent_project="Some Other Project", sprint_applies=False)
    handler, _ = _handler(fake, SPRINT)
    with pytest.raises(TicketProjectNotSetError, match="Some Other Project"):
        handler.handle(ModelCreateTicketRequest(title="t", parent="OMN-17427"))
    assert (
        fake.of("issueUpdate") == []
    )  # the sprint project is never cleared by the create


def test_the_guard_still_judges_the_backlog_payload_with_no_project() -> None:
    fake = _Linear()
    handler, guard = _handler(fake, SPRINT)
    handler.handle(ModelCreateTicketRequest(title="t"))
    assert "project" not in guard.seen
    assert guard.seen["state"] == "Backlog"


@pytest.mark.parametrize("named", [SPRINT.project_name, SPRINT.project_id])
def test_a_project_naming_the_current_sprint_is_admitted(named: str) -> None:
    fake = _Linear()
    handler, guard = _handler(fake, SPRINT)
    result = handler.handle(ModelCreateTicketRequest(title="t", project=named))
    assert result.status == "created"
    assert "project" not in guard.seen
    assert len(fake.of("CreateIssueInSprint")) == 1


def test_any_other_project_is_still_refused_with_project_set() -> None:
    fake = _Linear()
    handler, _ = _handler(fake, SPRINT)
    with pytest.raises(TicketGuardRefusedError) as raised:
        handler.handle(
            ModelCreateTicketRequest(title="t", project="Some Other Project")
        )
    assert raised.value.finding_codes == ["project_set"]
    assert fake.calls == []


def test_the_sprint_project_is_refused_when_the_ticket_is_declared_not_planned() -> (
    None
):
    fake = _Linear()
    handler, _ = _handler(fake, SPRINT)
    with pytest.raises(TicketGuardRefusedError) as raised:
        handler.handle(
            ModelCreateTicketRequest(
                title="t", project=SPRINT.project_name, not_planned_this_sprint=True
            )
        )
    assert raised.value.finding_codes == ["project_set"]


def test_not_planned_is_a_create_field_only() -> None:
    with pytest.raises(ValidationError, match="only valid with operation=create"):
        ModelCreateTicketRequest(
            operation=EnumTicketOperation.CLEAR_PROJECT,
            ticket_id="OMN-1",
            not_planned_this_sprint=True,
        )


def test_dry_run_reports_the_sprint_it_would_use() -> None:
    fake = _Linear()
    handler, _ = _handler(fake, SPRINT)
    result = handler.handle(ModelCreateTicketRequest(title="t", dry_run=True))
    assert result.status == "dry_run"
    assert (result.sprint_status, result.due_date) == ("resolved", SPRINT.due_date)
    assert fake.of("CreateIssue") == []


# --- clear_project and repair_project leave the current sprint alone -------------------------


class _ProjectLinear:
    """Serves the project gateway's reads and records its clears."""

    def __init__(self, projects: dict[str, str | None]) -> None:
        self.projects = dict(projects)
        self.clears: list[str] = []

    def post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "issueUpdate" in query:
            self.clears.append(variables["id"])
            self.projects[variables["id"]] = None
            return {"data": {"issueUpdate": {"success": True, "issue": {}}}}
        if "ListIssuesInProject" in query:
            return {
                "data": {
                    "issues": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {"identifier": i}
                            for i, p in self.projects.items()
                            if p == variables["project"]
                        ],
                    }
                }
            }
        project = self.projects[variables["id"]]
        return {
            "data": {
                "issue": {
                    "identifier": variables["id"],
                    "createdAt": "2031-05-06T01:00:00.000Z",
                    "labels": {"nodes": [{"name": "lane-created"}]},
                    "project": {"name": project} if project else None,
                }
            }
        }


def test_clear_project_leaves_a_ticket_in_the_current_sprint_alone() -> None:
    linear = _ProjectLinear({"OMN-1": SPRINT.project_name})
    handler = HandlerTicketProject(
        LinearTicketProjectGateway(linear.post), sprint_resolver=lambda: SPRINT
    )
    result = handler.clear("OMN-1")
    assert result.status == "project_kept_current_sprint"
    assert result.project_cleared is False
    assert linear.clears == []
    assert linear.projects["OMN-1"] == SPRINT.project_name


def test_clear_project_still_clears_a_project_that_is_not_the_sprints() -> None:
    linear = _ProjectLinear({"OMN-1": "Ready"})
    handler = HandlerTicketProject(
        LinearTicketProjectGateway(linear.post), sprint_resolver=lambda: SPRINT
    )
    result = handler.clear("OMN-1")
    assert result.status == "project_cleared"
    assert linear.clears == ["OMN-1"]


def test_clear_project_protects_the_sprint_through_the_live_gateway_listing() -> None:
    """With no injected resolver the gateway's own sprint read protects the project."""
    linear = _ProjectLinear({"OMN-1": SPRINT.project_name})
    gateway = LinearTicketProjectGateway(linear.post)
    gateway.current_sprint = lambda: SPRINT  # type: ignore[method-assign]
    result = HandlerTicketProject(gateway).clear("OMN-1")
    assert result.status == "project_kept_current_sprint"
    assert linear.clears == []


def test_repair_project_leaves_a_ticket_in_the_current_sprint_alone() -> None:
    linear = _ProjectLinear({"OMN-1": SPRINT.project_name, "OMN-2": "Ready"})
    handler = HandlerTicketProject(
        LinearTicketProjectGateway(linear.post),
        "lane-created",
        sprint_resolver=lambda: SPRINT,
    )
    sprint_run = handler.repair(
        day="2031-05-06",
        from_project=SPRINT.project_name,
        label="lane-created",
        dry_run=False,
    )
    assert linear.clears == []
    assert sprint_run.cleared_ids == []
    assert "current sprint" in sprint_run.skipped[0]
    ready_run = handler.repair(
        day="2031-05-06", from_project="Ready", label="lane-created", dry_run=False
    )
    assert ready_run.cleared_ids == ["OMN-2"]
    assert linear.clears == ["OMN-2"]
    assert copy.deepcopy(linear.projects["OMN-1"]) == SPRINT.project_name
