# SPDX-License-Identifier: MIT
"""Result and worker state models for the swarm supervisor."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumWorkerStatus(StrEnum):
    HEALTHY = "healthy"
    ZOMBIE = "zombie"
    CONTEXT_EXHAUSTED = "context_exhausted"
    FALSE_COMPLETION = "false_completion"
    RESPAWNED = "respawned"
    ABANDONED = "abandoned"
    TERMINAL = "terminal"


class EnumSupervisorStatus(StrEnum):
    COMPLETE = "complete"
    HALTED = "halted"
    FAILED = "failed"


class ModelWorkerState(BaseModel):
    """Snapshot of a single worker's observed state."""

    model_config = ConfigDict(frozen=False, extra="forbid")

    worker_id: str = Field(..., description="Unique worker task ID.")
    status: EnumWorkerStatus = Field(default=EnumWorkerStatus.HEALTHY)
    last_heartbeat_at: datetime | None = Field(default=None)
    context_usage_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    respawn_count: int = Field(default=0)
    respawn_reason: str = Field(default="")
    abandoned: bool = Field(default=False)


class ModelSwarmSupervisorResult(BaseModel):
    """Result returned when the swarm supervisor session ends."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Session ID from start command.")
    overall_status: EnumSupervisorStatus = Field(...)
    workers_supervised: int = Field(default=0)
    respawns_issued: int = Field(default=0)
    zombies_detected: int = Field(default=0)
    false_completions_detected: int = Field(default=0)
    halt_reason: str = Field(default="")


__all__: list[str] = [
    "EnumSupervisorStatus",
    "EnumWorkerStatus",
    "ModelSwarmSupervisorResult",
    "ModelWorkerState",
]
