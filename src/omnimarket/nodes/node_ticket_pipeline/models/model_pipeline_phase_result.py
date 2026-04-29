"""Explicit phase execution result for the ticket pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_completed_event import (
    ModelPipelineCompletedEvent,
)
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_phase_event import (
    ModelPipelinePhaseEvent,
)
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_state import (
    EnumPipelinePhase,
    ModelPipelineState,
)


class EnumPipelinePhaseResultStatus(StrEnum):
    """Outcome status for one attempted phase execution."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    NOT_IMPLEMENTED = "not_implemented"


class ModelPipelinePhaseResult(BaseModel):
    """Deterministic result from executing one node-owned pipeline phase."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Pipeline run correlation ID.")
    ticket_id: str = Field(..., description="Linear ticket ID.")
    phase: EnumPipelinePhase = Field(...)
    status: EnumPipelinePhaseResultStatus = Field(...)
    dry_run: bool = Field(...)
    started_at: datetime = Field(...)
    completed_at: datetime = Field(...)
    message: str | None = Field(default=None)
    details: dict[str, object] = Field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.status == EnumPipelinePhaseResultStatus.SUCCEEDED


class ModelPipelineExecutionReport(BaseModel):
    """Parseable report for a bounded ticket-pipeline execution slice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelPipelineState = Field(..., description="Final pipeline state.")
    phase_results: list[ModelPipelinePhaseResult] = Field(default_factory=list)
    phase_events: list[ModelPipelinePhaseEvent] = Field(default_factory=list)
    completed: ModelPipelineCompletedEvent = Field(
        ..., description="Completion/stop event."
    )
    ran_phase: EnumPipelinePhase | None = Field(
        default=None, description="Last phase attempted by this invocation."
    )
    stopped_at: EnumPipelinePhase = Field(
        ..., description="State phase where this invocation stopped."
    )
    stop_reason: str = Field(..., description="Why execution stopped.")


__all__: list[str] = [
    "EnumPipelinePhaseResultStatus",
    "ModelPipelineExecutionReport",
    "ModelPipelinePhaseResult",
]
