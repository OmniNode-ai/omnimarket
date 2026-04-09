# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrTriageOutput — output from the PR lifecycle triage compute node.

Related:
    - OMN-8083: pr_lifecycle_triage_compute
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_pr_triage_result import (
    ModelPrTriageResult,
)


class ModelPrTriageOutput(BaseModel):
    """Output from the PR triage compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Correlation ID from the input event."
    )
    results: tuple[ModelPrTriageResult, ...] = Field(
        ..., description="Triage results for each PR."
    )
    total_green: int = Field(
        default=0, ge=0, description="Count of GREEN (ready to merge) PRs."
    )
    total_red: int = Field(
        default=0, ge=0, description="Count of RED (CI failing) PRs."
    )
    total_conflicted: int = Field(
        default=0, ge=0, description="Count of CONFLICTED PRs."
    )
    total_needs_review: int = Field(
        default=0, ge=0, description="Count of NEEDS_REVIEW PRs."
    )


__all__: list[str] = ["ModelPrTriageOutput"]
