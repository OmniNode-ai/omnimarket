"""ModelOrchestratorStartCommand — command to start the build loop orchestrator."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EnumOrchestratorMode(StrEnum):
    """Orchestration modes controlling which phase sequences run.

    - BUILD: FILLING -> CLASSIFYING -> BUILDING
    - CLOSE_OUT: CLOSING_OUT -> VERIFYING
    - FULL: CLOSING_OUT -> VERIFYING -> FILLING -> CLASSIFYING -> BUILDING
    - OBSERVE: VERIFYING only (read-only health check)
    """

    BUILD = "build"
    CLOSE_OUT = "close_out"
    FULL = "full"
    OBSERVE = "observe"


class ModelOrchestratorStartCommand(BaseModel):
    """Command to start the build loop orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Unique orchestration run ID.")
    mode: EnumOrchestratorMode = Field(
        default=EnumOrchestratorMode.FULL,
        description="Orchestration mode: build, close_out, full, or observe.",
    )
    max_cycles: int = Field(default=1, ge=1, description="Max build loop cycles.")
    skip_closeout: bool = Field(
        default=False,
        description="Skip the CLOSING_OUT phase.",
    )
    max_tickets: int = Field(default=5, ge=1, description="Max tickets per fill cycle.")
    dry_run: bool = Field(default=False, description="No side effects if true.")
    requested_at: datetime = Field(..., description="When the command was issued.")

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: object) -> object:
        if value == "close-out":
            return "close_out"
        return value


__all__: list[str] = ["EnumOrchestratorMode", "ModelOrchestratorStartCommand"]
