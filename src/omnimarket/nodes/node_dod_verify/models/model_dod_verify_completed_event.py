"""ModelDodVerifyCompletedEvent — emitted when DoD verification finishes.

Schema changelog:
  1.0.0 — initial schema
  1.1.0 — added optional ``receipt_path`` field carrying the on-disk location
           of the written dod_report.json (None when no --output-path was given
           or when the write was skipped).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    ModelEvidenceCheckResult,
)


class ModelDodVerifyCompletedEvent(BaseModel):
    """Final event when DoD verification finishes (schema v1.1.0)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="1.1.0")
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
    receipt_path: Path | None = Field(
        default=None,
        description=(
            "Absolute path of the on-disk dod_report.json written by ReceiptWriter, "
            "or None if no output path was requested."
        ),
    )


__all__: list[str] = ["ModelDodVerifyCompletedEvent"]
