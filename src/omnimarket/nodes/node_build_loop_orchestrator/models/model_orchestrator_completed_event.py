"""ModelOrchestratorCompletedEvent — terminal event for the build loop orchestrator."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_build_loop_orchestrator.models.model_orchestrator_start_command import (
    EnumOrchestratorMode,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_orchestrator_state import (
    TERMINAL_ORCHESTRATOR_PHASES,
    EnumOrchestratorPhase,
)


class ModelOrchestratorCompletedEvent(BaseModel):
    """Terminal event emitted when the orchestrator finishes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Root correlation ID.")
    mode: EnumOrchestratorMode = Field(..., description="Mode that was executed.")
    final_phase: EnumOrchestratorPhase = Field(
        ..., description="Terminal orchestrator phase."
    )
    phases_completed: int = Field(default=0, ge=0)
    started_at: datetime = Field(..., description="Orchestration start time.")
    completed_at: datetime = Field(..., description="Orchestration completion time.")
    error_message: str | None = Field(default=None)

    @field_validator("final_phase")
    @classmethod
    def validate_terminal_final_phase(
        cls,
        value: EnumOrchestratorPhase,
    ) -> EnumOrchestratorPhase:
        if value not in TERMINAL_ORCHESTRATOR_PHASES:
            msg = f"final_phase must be a terminal phase (COMPLETE or FAILED), got {value}"
            raise ValueError(msg)
        return value


__all__: list[str] = ["ModelOrchestratorCompletedEvent"]
