"""ModelOrchestratorStartCommand — command to start the build loop orchestrator."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumOrchestratorMode(StrEnum):
    """Orchestration modes controlling which phase sequences run.

    - BUILD: FILLING -> CLASSIFYING -> BUILDING
    - CLOSE_OUT: CLOSING_OUT -> VERIFYING
    - FULL: CLOSING_OUT -> VERIFYING -> FILLING -> CLASSIFYING -> BUILDING
    - OBSERVE: VERIFYING only (read-only health check)
    """

    BUILD = "build"
    CLOSE_OUT = "close-out"
    FULL = "full"
    OBSERVE = "observe"


class ModelOrchestratorStartCommand(BaseModel):
    """Command to start the build loop orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Unique orchestration run ID.")
    mode: EnumOrchestratorMode = Field(
        default=EnumOrchestratorMode.FULL,
        description="Orchestration mode: build, close-out, full, or observe.",
    )
    max_cycles: int = Field(default=1, ge=1, description="Max build loop cycles.")
    dry_run: bool = Field(default=False, description="No side effects if true.")
    requested_at: datetime = Field(..., description="When the command was issued.")


__all__: list[str] = ["EnumOrchestratorMode", "ModelOrchestratorStartCommand"]
