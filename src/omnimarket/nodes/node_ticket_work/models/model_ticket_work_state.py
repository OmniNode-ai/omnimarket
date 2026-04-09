"""Models for node_ticket_work — FSM state."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumTicketWorkPhase(StrEnum):
    """FSM phases for the ticket-work workflow."""

    IDLE = "idle"
    INTAKE = "intake"
    RESEARCH = "research"
    QUESTIONS = "questions"
    SPEC = "spec"
    IMPLEMENT = "implement"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"


_PHASE_SEQUENCE: tuple[EnumTicketWorkPhase, ...] = (
    EnumTicketWorkPhase.INTAKE,
    EnumTicketWorkPhase.RESEARCH,
    EnumTicketWorkPhase.QUESTIONS,
    EnumTicketWorkPhase.SPEC,
    EnumTicketWorkPhase.IMPLEMENT,
    EnumTicketWorkPhase.REVIEW,
    EnumTicketWorkPhase.DONE,
)

TERMINAL_PHASES: frozenset[EnumTicketWorkPhase] = frozenset(
    {EnumTicketWorkPhase.DONE, EnumTicketWorkPhase.FAILED}
)


def next_phase(current: EnumTicketWorkPhase) -> EnumTicketWorkPhase:
    """Return the next phase. Raises ValueError for terminal phases."""
    if current in TERMINAL_PHASES:
        msg = f"Cannot advance from terminal phase: {current}"
        raise ValueError(msg)
    if current == EnumTicketWorkPhase.IDLE:
        return _PHASE_SEQUENCE[0]
    idx = _PHASE_SEQUENCE.index(current)
    return _PHASE_SEQUENCE[idx + 1]


class ModelTicketWorkState(BaseModel):
    """Mutable FSM state for the ticket-work workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    current_phase: EnumTicketWorkPhase = Field(default=EnumTicketWorkPhase.IDLE)
    ticket_id: str = Field(default="")
    autonomous: bool = Field(default=False)
    dry_run: bool = Field(default=False)
    consecutive_failures: int = Field(default=0, ge=0)
    max_consecutive_failures: int = Field(default=3, ge=1)
    pr_url: str | None = Field(default=None)
    commits: list[str] = Field(default_factory=list)
    error_message: str | None = Field(default=None)


class ModelTicketWorkPhaseEvent(BaseModel):
    """Emitted on each FSM phase transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    from_phase: EnumTicketWorkPhase = Field(...)
    to_phase: EnumTicketWorkPhase = Field(...)
    success: bool = Field(...)
    error_message: str | None = Field(default=None)


class ModelTicketWorkCompletedEvent(BaseModel):
    """Emitted when the ticket-work workflow finishes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    final_phase: EnumTicketWorkPhase = Field(...)
    ticket_id: str = Field(default="")
    pr_url: str | None = Field(default=None)
    error_message: str | None = Field(default=None)


# Legacy aliases
class ModelTicketWorkStartCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = ""
    ticket_id: str
    autonomous: bool = False
    skip_to: str = ""


__all__: list[str] = [
    "TERMINAL_PHASES",
    "EnumTicketWorkPhase",
    "ModelTicketWorkCompletedEvent",
    "ModelTicketWorkPhaseEvent",
    "ModelTicketWorkStartCommand",
    "ModelTicketWorkState",
    "next_phase",
]
