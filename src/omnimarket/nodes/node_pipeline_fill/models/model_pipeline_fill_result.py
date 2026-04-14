# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPipelineFillResult — output from one pipeline fill cycle.

Related:
    - OMN-8688: node_pipeline_fill
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelPipelineFillResult(BaseModel):
    """Result of one pipeline fill cycle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Cycle correlation ID.")
    candidates_found: int = Field(
        ..., ge=0, description="Total unstarted tickets found."
    )
    candidates_after_filter: int = Field(
        ..., ge=0, description="Candidates after filtering already-dispatched/blocked."
    )
    dispatched: tuple[str, ...] = Field(
        default_factory=tuple, description="Ticket IDs dispatched this cycle."
    )
    skipped: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Ticket IDs skipped (wave-cap, filtered, low score).",
    )
    skip_reason: str = Field(
        default="", description="Human-readable reason if no tickets were dispatched."
    )
    dry_run: bool = Field(
        default=False, description="True if no dispatch occurred due to dry_run."
    )


__all__: list[str] = ["ModelPipelineFillResult"]
