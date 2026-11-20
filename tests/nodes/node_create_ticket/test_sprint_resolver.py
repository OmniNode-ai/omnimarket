# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The current-sprint resolver: project, milestone and due date from a fixture listing and a fixed clock."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from omnimarket.handlers.handler_sprint_resolve import (
    list_sprint_projects,
    resolve_current_sprint,
    resolve_sprint_for_date,
)
from omnimarket.models.sprint_resolution.model_sprint_resolution import (
    EnumSprintResolutionStatus,
    ModelSprintMilestone,
    ModelSprintProjectListing,
    ModelSprintResolveRequest,
)

ARROW = "→"


def _sprint(
    pid: str,
    start: str,
    end: str,
    *,
    milestones: tuple[ModelSprintMilestone, ...] = (),
    suffix: str = ": Fixture Theme",
    completed: str | None = None,
) -> ModelSprintProjectListing:
    return ModelSprintProjectListing(
        id=pid,
        name=f"Sprint {start} {ARROW} {end}{suffix}",
        start_date=start,
        target_date=end,
        completed_at=completed,
        milestones=milestones,
    )


CURRENT = _sprint(
    "proj-current",
    "2026-11-16",
    "2026-11-22",
    milestones=(
        ModelSprintMilestone(id="ms-current", name="Theme", target_date="2026-11-22"),
    ),
)
NEXT = _sprint(
    "proj-next",
    "2026-11-23",
    "2026-11-29",
    milestones=(
        ModelSprintMilestone(id="ms-next", name="Theme", target_date="2026-11-29"),
    ),
)
NOT_A_SPRINT = ModelSprintProjectListing(
    id="proj-other",
    name="Sprint planning notes",
    start_date="2026-11-01",
    target_date="2026-12-31",
)


def _resolve(day: str, *projects: ModelSprintProjectListing):
    return resolve_sprint_for_date(
        ModelSprintResolveRequest(date=day, projects=projects)
    )


def test_date_inside_the_sprint_resolves_project_milestone_and_due_date() -> None:
    got = _resolve("2026-11-18", NEXT, CURRENT, NOT_A_SPRINT)
    assert got.status is EnumSprintResolutionStatus.RESOLVED
    assert (got.project_id, got.milestone_id, got.due_date) == (
        "proj-current",
        "ms-current",
        "2026-11-22",
    )


def test_last_day_of_the_sprint_is_inside_it() -> None:
    got = _resolve("2026-11-22", CURRENT, NEXT)
    assert got.project_id == "proj-current"
    assert got.due_date == "2026-11-22"


def test_first_day_of_the_next_sprint_is_the_next_sprint() -> None:
    got = _resolve("2026-11-23", CURRENT, NEXT)
    assert got.project_id == "proj-next"
    assert got.milestone_id == "ms-next"
    assert got.due_date == "2026-11-29"


def test_day_after_the_last_listed_sprint_is_no_sprint() -> None:
    got = _resolve("2026-11-23", CURRENT)
    assert got.status is EnumSprintResolutionStatus.NO_SPRINT
    assert (got.project_id, got.milestone_id, got.due_date) == ("", "", "")
    assert "Backlog" in got.detail


def test_date_in_no_sprint_is_no_sprint() -> None:
    assert _resolve("2027-06-01", CURRENT, NEXT, NOT_A_SPRINT).status is (
        EnumSprintResolutionStatus.NO_SPRINT
    )


def test_a_project_whose_name_dates_differ_from_its_window_is_no_sprint() -> None:
    renamed = CURRENT.model_copy(
        update={"name": f"Sprint 2026-11-01 {ARROW} 2026-11-07: x"}
    )
    assert (
        _resolve("2026-11-18", renamed).status is EnumSprintResolutionStatus.NO_SPRINT
    )


def test_milestone_is_the_one_ending_with_the_project_else_the_only_one() -> None:
    two = _sprint(
        "p",
        "2026-11-16",
        "2026-11-22",
        milestones=(
            ModelSprintMilestone(id="early", name="a", target_date="2026-11-18"),
            ModelSprintMilestone(id="late", name="b", target_date="2026-11-22"),
        ),
    )
    assert _resolve("2026-11-17", two).milestone_id == "late"
    only = _sprint(
        "p",
        "2026-11-16",
        "2026-11-22",
        milestones=(
            ModelSprintMilestone(id="solo", name="a", target_date="2026-11-30"),
        ),
    )
    assert _resolve("2026-11-17", only).milestone_id == "solo"


def test_no_milestone_is_picked_when_none_fits_and_several_exist() -> None:
    odd = _sprint(
        "p",
        "2026-11-16",
        "2026-11-22",
        milestones=(
            ModelSprintMilestone(id="m1", name="a", target_date="2026-11-18"),
            ModelSprintMilestone(id="m2", name="b", target_date="2026-11-19"),
        ),
    )
    got = _resolve("2026-11-17", odd)
    assert got.status is EnumSprintResolutionStatus.RESOLVED
    assert got.milestone_id == ""
    assert "none picked" in got.detail


def test_two_open_sprints_holding_the_date_are_ambiguous_not_picked() -> None:
    twin = CURRENT.model_copy(update={"id": "proj-twin"})
    got = _resolve("2026-11-18", CURRENT, twin)
    assert got.status is EnumSprintResolutionStatus.AMBIGUOUS
    assert got.project_id == ""


def test_a_completed_twin_loses_to_the_open_sprint() -> None:
    done = CURRENT.model_copy(
        update={"id": "proj-done", "completed_at": "2026-11-23T00:00:00Z"}
    )
    assert _resolve("2026-11-18", done, CURRENT).project_id == "proj-current"


def test_the_date_comes_from_the_clock_of_the_call() -> None:
    def post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {
            "data": {
                "projects": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "id": p.id,
                            "name": p.name,
                            "startDate": p.start_date,
                            "targetDate": p.target_date,
                            "completedAt": p.completed_at,
                            "projectMilestones": {
                                "nodes": [
                                    {
                                        "id": m.id,
                                        "name": m.name,
                                        "targetDate": m.target_date,
                                    }
                                    for m in p.milestones
                                ]
                            },
                        }
                        for p in (CURRENT, NEXT)
                    ],
                }
            }
        }

    inside = resolve_current_sprint(
        post, lambda: datetime(2026, 11, 22, 23, 59, tzinfo=UTC)
    )
    after = resolve_current_sprint(
        post, lambda: datetime(2026, 11, 23, 0, 0, tzinfo=UTC)
    )
    assert (inside.date, inside.project_id) == ("2026-11-22", "proj-current")
    assert (after.date, after.project_id) == ("2026-11-23", "proj-next")


def test_listing_reads_every_page() -> None:
    pages = iter(
        [
            {
                "data": {
                    "projects": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                        "nodes": [
                            {
                                "id": "a",
                                "name": "Sprint x",
                                "projectMilestones": {"nodes": []},
                            }
                        ],
                    }
                }
            },
            {
                "data": {
                    "projects": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "b",
                                "name": "Sprint y",
                                "projectMilestones": {"nodes": []},
                            }
                        ],
                    }
                }
            },
        ]
    )
    seen: list[dict[str, Any]] = []

    def post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        seen.append(variables)
        return next(pages)

    assert [p.id for p in list_sprint_projects(post)] == ["a", "b"]
    assert seen == [{"after": None}, {"after": "c1"}]
