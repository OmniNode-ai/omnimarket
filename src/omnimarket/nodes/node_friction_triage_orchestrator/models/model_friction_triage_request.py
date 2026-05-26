# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input/output models for node_friction_triage_orchestrator [OMN-12205].

Contains:
- ModelFrictionTriageRequest: input to the orchestrator
- ModelFrictionTriageResult: output from the orchestrator
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelFrictionTriageRequest(BaseModel):
    """Input to the friction triage orchestrator.

    Specifies the friction registry path, rolling window, threshold overrides,
    and dry-run flag.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    friction_registry_path: str = Field(
        description="Absolute path to the friction registry NDJSON file.",
    )
    window_days: int = Field(
        default=30,
        ge=1,
        description="Rolling window in days for event aggregation.",
    )
    threshold_count: int = Field(
        default=3,
        ge=1,
        description="Minimum event count to cross threshold (count >= threshold_count).",
    )
    threshold_score: int = Field(
        default=9,
        ge=1,
        description="Minimum severity score to cross threshold (score >= threshold_score).",
    )
    dry_run: bool = Field(
        default=False,
        description="When true, preview what would be created without creating tickets.",
    )
    linear_team: str = Field(
        default="Omninode",
        description="Linear team name to create tickets in.",
    )
    linear_project: str = Field(
        default="Active Sprint",
        description="Linear project name to assign new tickets to.",
    )


class ModelFrictionTriageResult(BaseModel):
    """Output from the friction triage orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surfaces_tracked: int = Field(
        description="Total number of friction surfaces aggregated in the window.",
    )
    threshold_crossings: int = Field(
        description="Number of surfaces that crossed the threshold.",
    )
    tickets_created: int = Field(
        description="Number of new Linear tickets created.",
    )
    tickets_skipped: int = Field(
        description="Number of surfaces skipped due to existing open ticket.",
    )
    created_ticket_ids: tuple[str, ...] = Field(
        default=(),
        description="IDs of Linear tickets created in this run.",
    )
    dry_run: bool = Field(
        description="Whether this was a dry run (no tickets actually created).",
    )
    summary: str = Field(
        default="",
        description="Human-readable triage summary.",
    )
