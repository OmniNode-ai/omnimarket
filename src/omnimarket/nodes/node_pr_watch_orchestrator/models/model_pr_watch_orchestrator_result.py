from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_watch_orchestrator.models.model_pr_check_status import (
    ModelPrCheckStatus,
)


class EnumPrWatchStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class EnumPrWatchConclusion(StrEnum):
    GREEN = "green"
    RED = "red"
    TIMEOUT = "timeout"


class ModelPrWatchOrchestratorResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Runtime correlation ID.")
    repo: str = Field(..., description="GitHub repository in OWNER/REPO form.")
    pr_number: int = Field(..., gt=0, description="Pull request number watched.")
    status: EnumPrWatchStatus = Field(..., description="Runtime terminal status.")
    conclusion: EnumPrWatchConclusion = Field(
        ..., description="PR watch domain conclusion."
    )
    terminal_event: str = Field(..., description="Contract-selected terminal topic.")
    checks: tuple[ModelPrCheckStatus, ...] = Field(
        default_factory=tuple,
        description="Most recent gh pr checks snapshot.",
    )
    attempts: int = Field(..., ge=1, description="Number of polling attempts.")
    elapsed_seconds: float = Field(..., ge=0.0, description="Elapsed watch time.")
    failed_checks: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Check names in fail/cancel terminal buckets.",
    )
    pending_checks: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Check names still pending when the watch timed out.",
    )
    observed_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=UTC),
        description="When the terminal result was observed.",
    )
    error_message: str = Field(default="", description="Failure or timeout detail.")


__all__ = [
    "EnumPrWatchConclusion",
    "EnumPrWatchStatus",
    "ModelPrWatchOrchestratorResult",
]
