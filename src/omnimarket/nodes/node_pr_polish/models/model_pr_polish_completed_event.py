"""ModelPrPolishCompletedEvent — emitted when PR polish finishes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_polish.models.model_pr_polish_state import (
    EnumPrPolishPhase,
)


class ModelPrPolishCompletedEvent(BaseModel):
    """Final event when PR polish finishes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    final_phase: EnumPrPolishPhase = Field(...)
    started_at: datetime = Field(...)
    completed_at: datetime = Field(...)
    pr_number: int | None = Field(default=None)
    conflicts_resolved: int = Field(default=0, ge=0)
    ci_fixes_applied: int = Field(default=0, ge=0)
    comments_addressed: int = Field(default=0, ge=0)
    run_dir: str | None = Field(default=None)
    dispatch_worker_spec_path: str | None = Field(default=None)
    dispatch_execution_result_path: str | None = Field(default=None)
    delegation_payloads_path: str | None = Field(default=None)
    dispatch_receipt_dir: str | None = Field(default=None)
    repair_worker_payloads_prepared: int = Field(default=0, ge=0)
    repair_workers_dispatched: int = Field(default=0, ge=0)
    repair_workers_skipped: int = Field(default=0, ge=0)
    delegation_publish_status: str | None = Field(default=None)
    error_message: str | None = Field(default=None)


__all__: list[str] = ["ModelPrPolishCompletedEvent"]
