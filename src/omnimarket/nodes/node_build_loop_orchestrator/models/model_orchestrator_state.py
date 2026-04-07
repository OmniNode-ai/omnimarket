"""ModelOrchestratorState — mutable FSM state for the build loop orchestrator."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_build_loop.models.model_loop_state import (
    EnumBuildLoopPhase,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_orchestrator_start_command import (
    EnumOrchestratorMode,
)


class EnumOrchestratorPhase(StrEnum):
    """High-level orchestrator phases."""

    IDLE = "idle"
    DRIVING = "driving"
    COMPLETE = "complete"
    FAILED = "failed"


TERMINAL_ORCHESTRATOR_PHASES: frozenset[EnumOrchestratorPhase] = frozenset(
    {EnumOrchestratorPhase.COMPLETE, EnumOrchestratorPhase.FAILED}
)


# Phase sequences for each mode
MODE_PHASE_SEQUENCES: dict[EnumOrchestratorMode, tuple[EnumBuildLoopPhase, ...]] = {
    EnumOrchestratorMode.BUILD: (
        EnumBuildLoopPhase.FILLING,
        EnumBuildLoopPhase.CLASSIFYING,
        EnumBuildLoopPhase.BUILDING,
    ),
    EnumOrchestratorMode.CLOSE_OUT: (
        EnumBuildLoopPhase.CLOSING_OUT,
        EnumBuildLoopPhase.VERIFYING,
    ),
    EnumOrchestratorMode.FULL: (
        EnumBuildLoopPhase.CLOSING_OUT,
        EnumBuildLoopPhase.VERIFYING,
        EnumBuildLoopPhase.FILLING,
        EnumBuildLoopPhase.CLASSIFYING,
        EnumBuildLoopPhase.BUILDING,
    ),
    EnumOrchestratorMode.OBSERVE: (EnumBuildLoopPhase.VERIFYING,),
}


class ModelOrchestratorState(BaseModel):
    """Mutable FSM state for the build loop orchestrator.

    Tracks the orchestrator-level phase, the underlying build loop FSM phase,
    mode, and failure tracking.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Root correlation ID.")
    mode: EnumOrchestratorMode = Field(..., description="Active orchestration mode.")
    orchestrator_phase: EnumOrchestratorPhase = Field(
        default=EnumOrchestratorPhase.IDLE,
        description="High-level orchestrator phase.",
    )
    current_build_phase: EnumBuildLoopPhase = Field(
        default=EnumBuildLoopPhase.IDLE,
        description="Current phase in the build loop FSM being driven.",
    )
    phase_index: int = Field(
        default=0, ge=0, description="Index into mode's phase sequence."
    )
    consecutive_failures: int = Field(
        default=0, ge=0, description="Consecutive phase failures."
    )
    max_consecutive_failures: int = Field(
        default=3, ge=1, description="Circuit breaker threshold."
    )
    dry_run: bool = Field(default=False, description="No side effects.")
    phases_completed: int = Field(default=0, ge=0, description="Phases completed.")
    error_message: str | None = Field(default=None)


__all__: list[str] = [
    "MODE_PHASE_SEQUENCES",
    "TERMINAL_ORCHESTRATOR_PHASES",
    "EnumOrchestratorPhase",
    "ModelOrchestratorState",
]
