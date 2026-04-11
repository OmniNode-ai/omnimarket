# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_linear_triage."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLinearTriageStartCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = ""
    threshold_days: int = Field(default=14, ge=1)
    dry_run: bool = False
    team: str = "Omninode"


class ModelLinearTicket(BaseModel):
    """Minimal representation of a Linear ticket for triage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    identifier: str
    title: str
    state: str
    updated_at: str
    branch_name: str = ""
    parent_id: str = ""
    labels: list[str] = Field(default_factory=list)


class EnumTriageAction(str):
    """Triage action constants."""

    MARK_DONE = "marked_done"
    MARK_DONE_SUPERSEDED = "marked_done_superseded"
    MARK_DONE_EPIC = "marked_done_epic"
    FLAG_STALE = "flag_stale"
    NO_CHANGE = "no_change"
    WOULD_MARK_DONE = "would_mark_done"
    WOULD_MARK_DONE_SUPERSEDED = "would_mark_done_superseded"
    WOULD_MARK_DONE_EPIC = "would_mark_done_epic"


class ModelTriageAction(BaseModel):
    """A single triage decision record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    ticket_title: str
    action: str
    evidence: str = ""
    stale_recommendation: str = ""


class ModelLinearTriageResult(BaseModel):
    """Result of a linear triage run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "completed"
    dry_run: bool = False
    total_scanned: int = 0
    recent_count: int = 0
    stale_count: int = 0
    marked_done: int = 0
    marked_done_superseded: int = 0
    epics_closed: int = 0
    stale_flagged: int = 0
    orphaned: int = 0
    actions: list[ModelTriageAction] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)


class ModelLinearTriageCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "completed"
    correlation_id: str = ""
    total_scanned: int = 0
    recent_count: int = 0
    stale_count: int = 0
    marked_done: int = 0
    marked_done_superseded: int = 0
    epics_closed: int = 0
    stale_flagged: int = 0
    orphaned: int = 0
    dry_run: bool = False
