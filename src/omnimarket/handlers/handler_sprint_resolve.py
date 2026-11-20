# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the sprint a date falls in: its project, milestone and due date, by date from Linear.

``node_create_ticket`` places a new ticket in the current sprint and ``node_ticket_query`` reads
the sprint's open tickets; both import this one resolver (a shared handler, so neither node imports
the other's private package). Nothing here holds a sprint id, milestone id or date: the listing
comes from Linear and the date from the clock of the call.

A sprint is a project whose name starts with ``Sprint <start> → <end>`` (its own start and target
dates) and whose start date is on or before the date and target date on or after it. Its milestone
is the one whose target date equals the project's target date, else the project's only milestone.
A date inside no sprint returns the typed ``no_sprint`` result, never a guess.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from omnimarket.models.sprint_resolution.model_sprint_resolution import (
    EnumSprintResolutionStatus,
    ModelSprintMilestone,
    ModelSprintProjectListing,
    ModelSprintResolution,
    ModelSprintResolveRequest,
)

SPRINT_NAME_PREFIX = "Sprint"
ARROW = "→"
MAX_PAGES = 20

SPRINT_PROJECTS_QUERY = """
query SprintProjects($after: String) {
  projects(first: 100, after: $after, filter: { name: { startsWith: "Sprint" } }) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id name startDate targetDate completedAt
      projectMilestones { nodes { id name targetDate } }
    }
  }
}
"""


def sprint_name_prefix(start: str, end: str) -> str:
    """The name a sprint project starts with for its own dates."""
    return f"{SPRINT_NAME_PREFIX} {start} {ARROW} {end}"


def _is_sprint_window(project: ModelSprintProjectListing) -> bool:
    if not (project.start_date and project.target_date):
        return False
    return project.name.startswith(
        sprint_name_prefix(project.start_date, project.target_date)
    )


def _milestone_for(
    project: ModelSprintProjectListing,
) -> tuple[ModelSprintMilestone | None, str]:
    """The project's milestone for the sprint, and a note when none could be picked."""
    same_end = [m for m in project.milestones if m.target_date == project.target_date]
    if len(same_end) == 1:
        return same_end[0], ""
    if len(same_end) > 1:
        return (
            None,
            f"{len(same_end)} milestones end {project.target_date}; none picked",
        )
    if len(project.milestones) == 1:
        return project.milestones[0], ""
    if not project.milestones:
        return None, "the sprint project has no milestone"
    return None, (
        f"{len(project.milestones)} milestones, none ends {project.target_date}; none picked"
    )


def resolve_sprint_for_date(
    request: ModelSprintResolveRequest,
) -> ModelSprintResolution:
    """The one sprint holding ``request.date``, or the typed reason there is none."""
    day = request.date
    inside = [
        p
        for p in request.projects
        if _is_sprint_window(p)
        and p.start_date is not None
        and p.target_date is not None
        and p.start_date <= day <= p.target_date
    ]
    if len(inside) > 1:
        live = [p for p in inside if not p.completed_at]
        inside = live if live else inside
    if not inside:
        return ModelSprintResolution(
            status=EnumSprintResolutionStatus.NO_SPRINT,
            date=day,
            detail=f"no sprint project holds {day}; the ticket stays in the Backlog with no project",
        )
    if len(inside) > 1:
        names = "; ".join(p.name for p in inside)
        return ModelSprintResolution(
            status=EnumSprintResolutionStatus.AMBIGUOUS,
            date=day,
            detail=f"{len(inside)} sprint projects hold {day} ({names}); none picked",
        )
    project = inside[0]
    milestone, note = _milestone_for(project)
    return ModelSprintResolution(
        status=EnumSprintResolutionStatus.RESOLVED,
        date=day,
        project_id=project.id,
        project_name=project.name,
        milestone_id=milestone.id if milestone else "",
        milestone_name=milestone.name if milestone else "",
        due_date=project.target_date or "",
        detail=note,
    )


def projects_from_nodes(nodes: list[Any]) -> list[ModelSprintProjectListing]:
    """The project listings of one page of the ``SPRINT_PROJECTS_QUERY`` answer."""
    found: list[ModelSprintProjectListing] = []
    for node in nodes:
        milestones = (node.get("projectMilestones") or {}).get("nodes") or []
        found.append(
            ModelSprintProjectListing(
                id=str(node["id"]),
                name=str(node.get("name") or ""),
                start_date=node.get("startDate"),
                target_date=node.get("targetDate"),
                completed_at=node.get("completedAt"),
                milestones=tuple(
                    ModelSprintMilestone(
                        id=str(m["id"]),
                        name=str(m.get("name") or ""),
                        target_date=m.get("targetDate"),
                    )
                    for m in milestones
                ),
            )
        )
    return found


def list_sprint_projects(
    post: Callable[[str, dict[str, Any]], Any],
) -> tuple[ModelSprintProjectListing, ...]:
    """Every project whose name starts with ``Sprint``, with its milestones, every page.

    ``post(query, variables)`` returns the parsed GraphQL body.
    """
    found: list[ModelSprintProjectListing] = []
    after: str | None = None
    for _ in range(MAX_PAGES):
        body = post(SPRINT_PROJECTS_QUERY, {"after": after})
        projects = (body.get("data") or {}).get("projects") or {}
        found += projects_from_nodes(projects.get("nodes") or [])
        page = projects.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return tuple(found)
        after = page.get("endCursor")
    raise RuntimeError(
        f"Linear sprint project listing did not end within {MAX_PAGES} pages"
    )


def utc_today(clock: Callable[[], datetime] | None = None) -> str:
    """The UTC day of the call's clock (``datetime.now(UTC)`` unless a clock is given)."""
    now = clock() if clock is not None else datetime.now(UTC)
    return now.astimezone(UTC).date().isoformat()


def resolve_current_sprint(
    post: Callable[[str, dict[str, Any]], Any],
    clock: Callable[[], datetime] | None = None,
) -> ModelSprintResolution:
    """The sprint holding today (the clock's UTC day), read from Linear now."""
    return resolve_sprint_for_date(
        ModelSprintResolveRequest(
            date=utc_today(clock), projects=list_sprint_projects(post)
        )
    )


class HandlerSprintResolve:
    """``handle(request: ModelSprintResolveRequest) -> ModelSprintResolution``: the pure resolver."""

    def handle(self, request: ModelSprintResolveRequest) -> ModelSprintResolution:
        return resolve_sprint_for_date(request)


__all__ = [
    "MAX_PAGES",
    "SPRINT_PROJECTS_QUERY",
    "HandlerSprintResolve",
    "list_sprint_projects",
    "projects_from_nodes",
    "resolve_current_sprint",
    "resolve_sprint_for_date",
    "sprint_name_prefix",
    "utc_today",
]
