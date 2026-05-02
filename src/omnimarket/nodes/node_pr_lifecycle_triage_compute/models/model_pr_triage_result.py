# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrTriageResult — triage classification for a single PR."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_pr_triage_category import (
    EnumPrTriageCategory,
)


class ModelPrTriageResult(BaseModel):
    """Triage classification result for a single PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int = Field(..., description="GitHub PR number.")
    repo: str = Field(..., description="Repository slug.")
    category: EnumPrTriageCategory = Field(
        ..., description="Triage category assigned to this PR."
    )
    ticket_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Canonical OMN ticket identifiers carried from inventory.",
    )
    failed_check_names: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Failed checks that informed this classification.",
    )
    reason: str = Field(
        ..., description="Human-readable explanation of the classification."
    )


__all__: list[str] = ["ModelPrTriageResult"]
