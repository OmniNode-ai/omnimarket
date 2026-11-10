# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the sprint-ticket enumerate operation (OMN-23250).

The sprint project's tickets, the defect tickets, and the PRs the watcher holds red or stalled,
read in four stages (project-issues, defects, pr-assignments, triage-fences) each under its own
budget floor. A cut or unreadable stage marks only itself; the candidates the other stages read
are returned.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")

SPRINT_STAGES: tuple[str, ...] = (
    "project-issues",
    "defects",
    "pr-assignments",
    "triage-fences",
)
SprintStageState = Literal["ok", "empty", "stale", "unreadable"]


class ModelLabFillSprintTicketsRequest(BaseModel):
    """One sprint-ticket read. Every bound and list arrives from the caller's overlay."""

    model_config = _FROZEN

    project_id: str = Field(min_length=1)
    ticket_state_types: tuple[str, ...] = Field(
        min_length=1, description="Linear state types a sprint ticket may be in."
    )
    defect_state_types: tuple[str, ...] = Field(
        description="State types of the defect read."
    )
    defect_labels: tuple[str, ...] = ()
    defect_terms: tuple[str, ...] = ()
    pr_repos: tuple[str, ...] = Field(
        description="Repositories whose PRs count as red or stalled candidates."
    )
    stalled_hours: float = Field(gt=0)
    budget_s: float = Field(gt=0)
    stage_floor_s: float = Field(ge=0)


class ModelLabFillSprintStage(BaseModel):
    """One stage's outcome."""

    model_config = _FROZEN

    stage: str
    state: SprintStageState
    seen: int = 0
    detail: str = ""
    budget_s: float
    elapsed_s: float


class ModelLabFillSprintCandidate(BaseModel):
    """A sprint ticket, defect, red PR or stalled PR the selection judges."""

    model_config = _FROZEN

    kind: str
    ticket: str
    title: str
    pr: str = ""
    repo: str = ""
    head_ref: str = ""
    labels: tuple[str, ...] = ()
    milestone: str = ""
    under_parent: bool = False
    updated_at: str = ""
    project_id: str = ""
    assignee_id: str = ""
    hold_reason: str = ""
    ticket_prs: tuple[str, ...] = ()
    merged_implementations: tuple[str, ...] = ()
    ticket_readable: bool = True
    declared_repo: str = ""
    self_contained_code: bool = False


class ModelLabFillSprintTicketsResult(BaseModel):
    """Candidates, per-kind read diagnostics, the fenced tickets, and the stages that were cut or unreadable."""

    model_config = _FROZEN

    stages: tuple[ModelLabFillSprintStage, ...]
    candidates: tuple[ModelLabFillSprintCandidate, ...]
    diagnostics: dict[str, str]
    fenced_tickets: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()
    elapsed_s: float
