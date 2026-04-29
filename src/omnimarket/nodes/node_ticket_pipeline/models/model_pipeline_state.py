"""ModelPipelineState and EnumPipelinePhase for the ticket pipeline FSM."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumPipelinePhase(StrEnum):
    """FSM phases for the ticket pipeline.

    Phase Transitions:
        IDLE -> PRE_FLIGHT -> IMPLEMENT -> LOCAL_REVIEW -> CREATE_PR
        -> TEST_ITERATE -> CI_WATCH -> PR_REVIEW -> AUTO_MERGE -> DONE
        Any -> FAILED: Unrecoverable error or circuit breaker tripped
        Any -> BLOCKED: Waiting on human gate
    """

    IDLE = "idle"
    PRE_FLIGHT = "pre_flight"
    IMPLEMENT = "implement"
    LOCAL_REVIEW = "local_review"
    CREATE_PR = "create_pr"
    TEST_ITERATE = "test_iterate"
    CI_WATCH = "ci_watch"
    PR_REVIEW = "pr_review"
    AUTO_MERGE = "auto_merge"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


_PHASE_ORDER: tuple[EnumPipelinePhase, ...] = (
    EnumPipelinePhase.PRE_FLIGHT,
    EnumPipelinePhase.IMPLEMENT,
    EnumPipelinePhase.LOCAL_REVIEW,
    EnumPipelinePhase.CREATE_PR,
    EnumPipelinePhase.TEST_ITERATE,
    EnumPipelinePhase.CI_WATCH,
    EnumPipelinePhase.PR_REVIEW,
    EnumPipelinePhase.AUTO_MERGE,
)

EXECUTABLE_PHASE_ORDER: tuple[EnumPipelinePhase, ...] = _PHASE_ORDER
EXECUTABLE_PHASES: frozenset[EnumPipelinePhase] = frozenset(EXECUTABLE_PHASE_ORDER)

TERMINAL_PHASES: frozenset[EnumPipelinePhase] = frozenset(
    {EnumPipelinePhase.DONE, EnumPipelinePhase.FAILED, EnumPipelinePhase.BLOCKED}
)


def next_phase(
    current: EnumPipelinePhase,
    skip_test_iterate: bool = False,
) -> EnumPipelinePhase:
    """Return the next phase in the pipeline progression."""
    if current == EnumPipelinePhase.IDLE:
        return EnumPipelinePhase.PRE_FLIGHT
    if current == EnumPipelinePhase.AUTO_MERGE:
        return EnumPipelinePhase.DONE
    if current in TERMINAL_PHASES:
        msg = f"No next phase from terminal state: {current}"
        raise ValueError(msg)

    idx = _PHASE_ORDER.index(current)
    next_idx = idx + 1
    if skip_test_iterate and _PHASE_ORDER[next_idx] == EnumPipelinePhase.TEST_ITERATE:
        next_idx += 1
    return _PHASE_ORDER[next_idx]


class ModelPipelineState(BaseModel):
    """Immutable FSM state for the ticket pipeline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Pipeline run correlation ID.")
    ticket_id: str = Field(..., description="Linear ticket ID (e.g. OMN-1234).")
    current_phase: EnumPipelinePhase = Field(
        default=EnumPipelinePhase.IDLE, description="Current FSM phase."
    )
    consecutive_failures: int = Field(default=0, ge=0)
    max_consecutive_failures: int = Field(default=3, ge=1)
    skip_test_iterate: bool = Field(default=False)
    dry_run: bool = Field(default=False)
    pr_number: int | None = Field(default=None)
    error_message: str | None = Field(default=None)


__all__: list[str] = [
    "EXECUTABLE_PHASES",
    "EXECUTABLE_PHASE_ORDER",
    "TERMINAL_PHASES",
    "EnumPipelinePhase",
    "ModelPipelineState",
    "next_phase",
]
