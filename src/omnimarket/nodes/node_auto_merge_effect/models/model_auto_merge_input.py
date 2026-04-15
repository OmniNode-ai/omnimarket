# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelAutoMergeInput -- input contract for node_auto_merge_effect."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelAutoMergeInput(BaseModel):
    """Input to the auto-merge effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Correlation ID flowing through the pipeline."
    )
    pr_number: int = Field(..., description="GitHub PR number to merge.", gt=0)
    repo: str = Field(..., description="GitHub repo slug (org/repo).")
    strategy: str = Field(
        default="squash", description="Merge strategy: squash | merge | rebase."
    )
    delete_branch: bool = Field(
        default=True, description="Delete source branch after merge."
    )
    ticket_id: str | None = Field(
        default=None, description="Linear ticket ID to close after merge."
    )
    gate_timeout_hours: float = Field(
        default=24.0,
        description="Wall-clock budget in hours for CI readiness polling.",
        gt=0,
    )


__all__: list[str] = ["ModelAutoMergeInput"]
