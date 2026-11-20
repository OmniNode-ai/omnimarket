# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ticket operation models shared by the ticket core and bus effect."""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

_TICKET_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class EnumTicketOperation(StrEnum):
    """What one ``node_create_ticket`` request does (OMN-20595)."""

    CREATE = "create"
    READ = "read"
    COMMENT = "comment"
    TRANSITION = "transition"
    CLEAR_PROJECT = "clear_project"
    REPAIR_PROJECT = "repair_project"
    LIST_OPEN = "list_open"


def _is_utc_day(value: str) -> bool:
    if not _DAY_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


class ModelCreateTicketRequest(BaseModel):
    """Request to create, read or comment on a Linear ticket.

    The contract's ``handler.input_model``: the payload the runtime validates is
    this model, the one ``handle()`` takes (OMN-20595 fixed the drift).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumTicketOperation = Field(
        default=EnumTicketOperation.CREATE,
        description="create (the default), read, comment, transition or list_open.",
    )
    title: str = Field(default="", description="Ticket title (create).")
    description: str = Field(
        default="",
        description="Ticket description (create), sent to Linear unchanged.",
    )
    ticket_id: str | None = Field(
        default=None, description="Ticket identifier, e.g. OMN-1234 (read, comment)."
    )
    body: str = Field(default="", description="Comment body (comment).")
    state: str = Field(
        default="",
        description="Target workflow state name, e.g. Canceled (transition).",
    )
    repo: str | None = Field(default=None, description="Primary repo for scoping.")
    parent: str | None = Field(default=None, description="Parent ticket ID (OMN-XXXX).")
    blocked_by: list[str] = Field(
        default_factory=list, description="Blocking ticket IDs."
    )
    dry_run: bool = Field(default=False)
    team: str = Field(default="Omninode")
    allow_arch_violation: bool = Field(
        default=False,
        description="Bypass architecture dependency validation (contract input).",
    )
    pillar: str | None = Field(
        default=None,
        description=(
            "Ticket pillar (dashboard, onboarding) that sets the assignee from the shared "
            "pillar owner map; none means the creator."
        ),
    )
    day: str = Field(
        default="",
        description="One UTC day, YYYY-MM-DD, whose lane-created tickets repair_project lists.",
    )
    from_project: str = Field(
        default="",
        description="The project repair_project clears from the tickets it lists (e.g. Ready).",
    )
    project: str = Field(
        default="",
        description=(
            "Admitted only when it names the current sprint's project (the node resolves that "
            "project by date and files it anyway); any other project is refused with the "
            "ticket-creation guard's project_set finding."
        ),
    )
    not_planned_this_sprint: bool = Field(
        default=False,
        description=(
            "create: the lane declares this ticket not planned this sprint. It is then filed in "
            "the Backlog with no project, milestone or due date. The default places it in the "
            "current sprint (project, milestone and due date resolved by date from Linear)."
        ),
    )

    @model_validator(mode="after")
    def _fields_for_operation(self) -> ModelCreateTicketRequest:
        op = self.operation
        if self.state.strip() and op is not EnumTicketOperation.TRANSITION:
            raise ValueError("state is only valid with operation=transition")
        if self.not_planned_this_sprint and op is not EnumTicketOperation.CREATE:
            raise ValueError(
                "not_planned_this_sprint is only valid with operation=create"
            )
        if op is EnumTicketOperation.REPAIR_PROJECT:
            if not _is_utc_day(self.day):
                raise ValueError(
                    f"operation=repair_project requires day as one UTC date YYYY-MM-DD, got {self.day!r}"
                )
            if not self.from_project.strip():
                raise ValueError("operation=repair_project requires from_project")
            return self
        if self.day or self.from_project:
            raise ValueError(
                "day and from_project are only valid with operation=repair_project"
            )
        if op is EnumTicketOperation.LIST_OPEN:
            if not self.project.strip():
                raise ValueError("operation=list_open requires project (a project id)")
            return self
        if op is EnumTicketOperation.CREATE:
            if not self.title.strip():
                raise ValueError("operation=create requires a title")
            return self
        if not self.ticket_id or not _TICKET_ID_RE.match(self.ticket_id):
            raise ValueError(
                f"operation={op.value} requires ticket_id like OMN-1234, "
                f"got {self.ticket_id!r}"
            )
        if op is EnumTicketOperation.COMMENT and not self.body.strip():
            raise ValueError("operation=comment requires a non-empty body")
        if op is EnumTicketOperation.TRANSITION and not self.state.strip():
            raise ValueError("operation=transition requires a non-empty state")
        return self


class ModelCreateTicketResult(BaseModel):
    """Result of a ticket create, read or comment request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="created")
    operation: EnumTicketOperation = Field(default=EnumTicketOperation.CREATE)
    ticket_id: str = Field(default="")
    ticket_url: str = Field(default="")
    title: str = Field(default="")
    team: str = Field(default="Omninode")
    ticket_status: str = Field(default="", description="Workflow state name (read).")
    assignee: str = Field(
        default="", description="Assignee name, empty when unassigned (read)."
    )
    comment_id: str = Field(default="", description="Created comment id (comment).")
    guard_path: str = Field(
        default="", description="The ticket-creation guard that admitted a create."
    )
    is_seam_ticket: bool = Field(default=False)
    interfaces_touched: list[str] = Field(default_factory=list)
    contract_completeness: str = Field(default="stub")
    validation_errors: list[str] = Field(default_factory=list)
    finding_codes: list[str] = Field(
        default_factory=list,
        description="The guard's own finding codes when it refused the request.",
    )
    description_body: str = Field(
        default="",
        description="The description sent to Linear (create) or read from it (read).",
    )
    dry_run: bool = Field(default=False)
    project_cleared: bool = Field(
        default=False,
        description="clear_project: the independent readback showed an explicit null project.",
    )
    cleared_ids: list[str] = Field(
        default_factory=list,
        description="repair_project: the tickets whose project was cleared and read back as null.",
    )
    candidate_ids: list[str] = Field(
        default_factory=list,
        description=(
            "repair_project: the tickets the listing found for the day, label and project; "
            "list_open: the identifiers of the project's tickets that are not completed or canceled."
        ),
    )
    skipped: list[str] = Field(
        default_factory=list,
        description="repair_project: '<id>: <reason>' for each listed ticket the recheck refused.",
    )
    sprint_status: str = Field(
        default="",
        description=(
            "create: resolved, no_sprint or ambiguous, the current-sprint resolution; empty when "
            "the ticket was declared not planned this sprint or no resolver was wired."
        ),
    )
    sprint_project: str = Field(
        default="", description="create: the sprint project's name."
    )
    sprint_milestone: str = Field(
        default="", description="create: the sprint milestone's name."
    )
    due_date: str = Field(
        default="", description="create: the due date set (the sprint's end)."
    )
    sprint_detail: str = Field(
        default="", description="create: why no sprint was set, when none was."
    )
