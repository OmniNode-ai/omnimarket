"""ModelDodVerifyCompletedEvent — emitted when DoD verification finishes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    ModelEvidenceCheckResult,
)


class ModelDodVerifyCompletedEvent(BaseModel):
    """Final event when DoD verification finishes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    ticket_id: str = Field(...)
    status: EnumDodVerifyStatus = Field(...)
    started_at: datetime = Field(...)
    completed_at: datetime = Field(...)
    checks: list[ModelEvidenceCheckResult] = Field(default_factory=list)
    total_checks: int = Field(default=0, ge=0)
    verified_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    error_message: str | None = Field(default=None)


__all__: list[str] = ["ModelDodVerifyCompletedEvent"]
