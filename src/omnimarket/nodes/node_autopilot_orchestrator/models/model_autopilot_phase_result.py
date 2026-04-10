# SPDX-License-Identifier: MIT
"""Phase result models for node_autopilot_orchestrator."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumAutopilotPhaseStatus(StrEnum):
    """Result vocabulary for individual autopilot phases (canonical, stable)."""

    PASS = "pass"
    PASS_REPAIRED = "pass_repaired"
    WARN = "warn"
    FAIL = "fail"
    HALT = "halt"
    SKIPPED = "skipped"
    NOT_RUN = "not_run"


class EnumAutopilotCycleStatus(StrEnum):
    """Overall cycle status written to cycle record."""

    COMPLETE = "complete"
    HALTED = "halted"
    CIRCUIT_BREAKER = "circuit_breaker"
    FAILED = "failed"


class ModelAutopilotPhaseResult(BaseModel):
    """Result for a single autopilot pipeline phase."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase_id: str = Field(..., description="Canonical phase ID (e.g. A, B, C, D).")
    status: EnumAutopilotPhaseStatus = Field(
        default=EnumAutopilotPhaseStatus.NOT_RUN,
        description="Phase execution result.",
    )
    detail: str = Field(
        default="",
        description="Human-readable detail for warn/fail/halt outcomes.",
    )
    halt_reason: str = Field(
        default="",
        description="Non-empty only when status=halt.",
    )


class ModelAutopilotResult(BaseModel):
    """Final result returned by the autopilot orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    overall_status: EnumAutopilotCycleStatus = Field(
        default=EnumAutopilotCycleStatus.COMPLETE,
    )
    phase_a: ModelAutopilotPhaseResult = Field(
        default_factory=lambda: ModelAutopilotPhaseResult(phase_id="A"),
    )
    phase_b: ModelAutopilotPhaseResult = Field(
        default_factory=lambda: ModelAutopilotPhaseResult(phase_id="B"),
    )
    phase_c: ModelAutopilotPhaseResult = Field(
        default_factory=lambda: ModelAutopilotPhaseResult(phase_id="C"),
    )
    phase_d: ModelAutopilotPhaseResult = Field(
        default_factory=lambda: ModelAutopilotPhaseResult(phase_id="D"),
    )
    halt_reason: str = Field(
        default="",
        description="Phase/step that triggered halt, or empty on complete.",
    )
    phases_completed: int = Field(default=0, ge=0)
    phases_failed: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)


__all__: list[str] = [
    "EnumAutopilotCycleStatus",
    "EnumAutopilotPhaseStatus",
    "ModelAutopilotPhaseResult",
    "ModelAutopilotResult",
]
