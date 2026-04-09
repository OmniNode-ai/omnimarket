"""ModelPrLifecycleFixResult — result of a PR lifecycle fix action."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
)


class ModelPrLifecycleFixResult(BaseModel):
    """Result of a PR lifecycle fix dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Fix run correlation ID.")
    pr_number: int = Field(..., description="PR number that was remediated.")
    repo: str = Field(..., description="GitHub repo slug.")
    block_reason: EnumPrBlockReason = Field(
        ..., description="Block reason that was routed."
    )
    fix_applied: bool = Field(..., description="Whether a fix action was dispatched.")
    fix_action: str = Field(
        ..., description="Fix action taken or would be taken (dry_run)."
    )
    error: str | None = Field(default=None, description="Error message if fix failed.")
    completed_at: datetime = Field(..., description="When the fix completed.")


__all__: list[str] = ["ModelPrLifecycleFixResult"]
