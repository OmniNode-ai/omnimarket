"""Models for node_design_to_plan — FSM state."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumDesignToPlanPhase(StrEnum):
    """FSM phases for the design-to-plan workflow."""

    IDLE = "idle"
    BRAINSTORM = "brainstorm"
    STRUCTURE = "structure"
    REVIEW = "review"
    FINALIZE = "finalize"
    DONE = "done"
    FAILED = "failed"


_PHASE_SEQUENCE: tuple[EnumDesignToPlanPhase, ...] = (
    EnumDesignToPlanPhase.BRAINSTORM,
    EnumDesignToPlanPhase.STRUCTURE,
    EnumDesignToPlanPhase.REVIEW,
    EnumDesignToPlanPhase.FINALIZE,
    EnumDesignToPlanPhase.DONE,
)

TERMINAL_PHASES: frozenset[EnumDesignToPlanPhase] = frozenset(
    {EnumDesignToPlanPhase.DONE, EnumDesignToPlanPhase.FAILED}
)


def next_phase(current: EnumDesignToPlanPhase) -> EnumDesignToPlanPhase:
    """Return the next phase. Raises ValueError for terminal phases."""
    if current in TERMINAL_PHASES:
        msg = f"Cannot advance from terminal phase: {current}"
        raise ValueError(msg)
    if current == EnumDesignToPlanPhase.IDLE:
        return _PHASE_SEQUENCE[0]
    idx = _PHASE_SEQUENCE.index(current)
    return _PHASE_SEQUENCE[idx + 1]


class ModelDesignToPlanState(BaseModel):
    """Mutable FSM state for the design-to-plan workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Run correlation ID.")
    current_phase: EnumDesignToPlanPhase = Field(default=EnumDesignToPlanPhase.IDLE)
    topic: str = Field(default="")
    plan_path: str | None = Field(default=None)
    dry_run: bool = Field(default=False)
    no_launch: bool = Field(default=False)
    consecutive_failures: int = Field(default=0, ge=0)
    max_consecutive_failures: int = Field(default=3, ge=1)
    error_message: str | None = Field(default=None)
    review_rounds: int = Field(default=0, ge=0)


class ModelDesignToPlanPhaseEvent(BaseModel):
    """Emitted on each FSM phase transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    from_phase: EnumDesignToPlanPhase = Field(...)
    to_phase: EnumDesignToPlanPhase = Field(...)
    success: bool = Field(...)
    error_message: str | None = Field(default=None)


class ModelDesignToPlanCompletedEvent(BaseModel):
    """Emitted when the design-to-plan workflow finishes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    final_phase: EnumDesignToPlanPhase = Field(...)
    plan_path: str | None = Field(default=None)
    error_message: str | None = Field(default=None)


# Legacy aliases kept for backward compat with old handler imports
ModelDesignToPlanStartCommand = ModelDesignToPlanState


__all__: list[str] = [
    "TERMINAL_PHASES",
    "EnumDesignToPlanPhase",
    "ModelDesignToPlanCompletedEvent",
    "ModelDesignToPlanPhaseEvent",
    "ModelDesignToPlanStartCommand",
    "ModelDesignToPlanState",
    "next_phase",
]
