# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input/output models for node_adversarial_pipeline_orchestrator [OMN-12215].

Contains:
- ModelAdversarialPipelineRequest: input to the orchestrator
- ModelAdversarialPipelineResult: output from the orchestrator
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAdversarialPipelineRequest(BaseModel):
    """Input to the adversarial pipeline orchestrator.

    Drives a three-stage pipeline: design_to_plan → hostile_reviewer gate → plan_to_tickets.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(
        description="Design topic or problem statement to feed into Stage 1 (design_to_plan).",
    )
    plan_path: str | None = Field(
        default=None,
        description=(
            "Optional path to an already-generated plan file. "
            "When set, Stage 1 (design_to_plan) is skipped and this path is used directly."
        ),
    )
    min_findings_gate: int = Field(
        default=3,
        ge=1,
        description=(
            "Minimum number of findings the hostile_reviewer must report "
            "before Stage 3 (plan_to_tickets) is allowed to proceed. "
            "If fewer findings are found, the pipeline halts and surfaces the finding "
            "summary without creating tickets."
        ),
    )
    linear_project: str | None = Field(
        default=None,
        description="Linear project name to assign created tickets to.",
    )
    no_launch: bool = Field(
        default=False,
        description="When true, pass --no-launch to design_to_plan (skip browser launch).",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When true, run all stages but skip final ticket creation in Stage 3. "
            "Equivalent to passing --dry-run to plan_to_tickets."
        ),
    )


class ModelAdversarialPipelineResult(BaseModel):
    """Output from the adversarial pipeline orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path: str | None = Field(
        default=None,
        description="Path to the plan file produced or used by Stage 1.",
    )
    findings_count: int = Field(
        default=0,
        ge=0,
        description="Number of findings reported by the hostile_reviewer in Stage 2.",
    )
    findings_summary: str = Field(
        default="",
        description="Human-readable summary of Stage 2 findings.",
    )
    gate_passed: bool = Field(
        description=(
            "True if Stage 2 found >= min_findings_gate issues and Stage 3 was allowed to run."
        ),
    )
    created_ticket_ids: tuple[str, ...] = Field(
        default=(),
        description="IDs of Linear tickets created in Stage 3.",
    )
    tickets_created: int = Field(
        default=0,
        ge=0,
        description="Number of Linear tickets created in Stage 3.",
    )
    epic_url: str | None = Field(
        default=None,
        description="URL of the Linear epic created in Stage 3, if any.",
    )
    dry_run: bool = Field(
        description="Whether this was a dry run (no tickets actually created).",
    )
    stage_reached: int = Field(
        default=1,
        ge=1,
        le=3,
        description="Highest pipeline stage reached (1=design, 2=review, 3=tickets).",
    )
