# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output models for node_audit_trail_compactor."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelFailureMode(BaseModel):
    """A top failure mode with count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(..., description="Failure mode description / category.")
    count: int = Field(..., description="Number of occurrences in the period.")
    ticket_ids: list[str] = Field(
        default_factory=list,
        description="Ticket IDs affected.",
    )


class ModelRecurringTicket(BaseModel):
    """A ticket that appears multiple times in failure records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Linear ticket identifier.")
    failure_count: int = Field(..., description="Number of failure entries.")
    last_failure: str = Field(..., description="Description of most recent failure.")


class ModelStallHeatmapEntry(BaseModel):
    """A single entry in the stall heatmap."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(..., description="Agent or skill ID.")
    stall_count: int = Field(..., description="Number of stall events.")
    affected_tickets: list[str] = Field(
        default_factory=list,
        description="Tickets affected by stalls.",
    )


class ModelCompactorResult(BaseModel):
    """Result produced by HandlerAuditTrailCompactor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_entries: int = Field(..., description="Total audit entries processed.")
    failure_modes: list[ModelFailureMode] = Field(
        default_factory=list,
        description="Top failure modes sorted by count.",
    )
    recurring_tickets: list[ModelRecurringTicket] = Field(
        default_factory=list,
        description="Tickets with multiple failures.",
    )
    stall_heatmap: list[ModelStallHeatmapEntry] = Field(
        default_factory=list,
        description="Stall events grouped by agent.",
    )
    rollup_path: str | None = Field(
        default=None,
        description="Path to written rollup file, if any.",
    )
    dry_run: bool = Field(default=False, description="Whether this was a dry run.")
