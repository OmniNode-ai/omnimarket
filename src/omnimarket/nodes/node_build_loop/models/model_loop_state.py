"""ModelLoopState — mutable FSM state for the build loop."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumBuildLoopPhase(StrEnum):
    """FSM phases for the autonomous build loop.

    Phase Transitions (mode-dependent):
        IDLE -> first phase per mode
        ...sequence per mode...
        last phase -> COMPLETE
        Any -> FAILED: Circuit breaker tripped or unrecoverable error
    """

    IDLE = "idle"
    CLOSING_OUT = "closing_out"
    VERIFYING = "verifying"
    FILLING = "filling"
    CLASSIFYING = "classifying"
    BUILDING = "building"
    RELEASING = "releasing"
    DEPLOYING = "deploying"
    POST_VERIFY = "post_verify"
    COMPLETE = "complete"
    FAILED = "failed"


class EnumBuildLoopMode(StrEnum):
    """Execution modes for the build loop.

    Each mode defines a different subset and ordering of phases.
    """

    BUILD = "build"
    CLOSE_OUT = "close_out"
    FULL = "full"
    OBSERVE = "observe"


# Phase sequences by mode (excluding IDLE and COMPLETE which are control states)
_MODE_PHASE_SEQUENCES: dict[EnumBuildLoopMode, tuple[EnumBuildLoopPhase, ...]] = {
    EnumBuildLoopMode.BUILD: (
        EnumBuildLoopPhase.CLOSING_OUT,
        EnumBuildLoopPhase.VERIFYING,
        EnumBuildLoopPhase.FILLING,
        EnumBuildLoopPhase.CLASSIFYING,
        EnumBuildLoopPhase.BUILDING,
    ),
    EnumBuildLoopMode.CLOSE_OUT: (
        EnumBuildLoopPhase.CLOSING_OUT,
        EnumBuildLoopPhase.VERIFYING,
        EnumBuildLoopPhase.RELEASING,
        EnumBuildLoopPhase.DEPLOYING,
        EnumBuildLoopPhase.POST_VERIFY,
    ),
    EnumBuildLoopMode.FULL: (
        EnumBuildLoopPhase.CLOSING_OUT,
        EnumBuildLoopPhase.VERIFYING,
        EnumBuildLoopPhase.FILLING,
        EnumBuildLoopPhase.CLASSIFYING,
        EnumBuildLoopPhase.BUILDING,
        EnumBuildLoopPhase.RELEASING,
        EnumBuildLoopPhase.DEPLOYING,
        EnumBuildLoopPhase.POST_VERIFY,
    ),
    EnumBuildLoopMode.OBSERVE: (EnumBuildLoopPhase.VERIFYING,),
}

TERMINAL_PHASES: frozenset[EnumBuildLoopPhase] = frozenset(
    {EnumBuildLoopPhase.COMPLETE, EnumBuildLoopPhase.FAILED}
)


def next_phase(
    current: EnumBuildLoopPhase,
    skip_closeout: bool = False,
    mode: EnumBuildLoopMode = EnumBuildLoopMode.BUILD,
) -> EnumBuildLoopPhase:
    """Return the next phase in the build loop progression.

    Uses mode-aware phase sequences. Returns COMPLETE after the last phase
    in the mode's sequence. Raises ValueError for terminal phases.
    """
    if current in TERMINAL_PHASES:
        msg = f"No next phase from terminal state: {current}"
        raise ValueError(msg)

    sequence = _MODE_PHASE_SEQUENCES[mode]

    if current == EnumBuildLoopPhase.IDLE:
        first = sequence[0]
        if skip_closeout and first == EnumBuildLoopPhase.CLOSING_OUT:
            return sequence[1]
        return first

    idx = sequence.index(current)
    next_idx = idx + 1

    if next_idx >= len(sequence):
        return EnumBuildLoopPhase.COMPLETE

    next_candidate = sequence[next_idx]
    if skip_closeout and next_candidate == EnumBuildLoopPhase.CLOSING_OUT:
        next_idx += 1
        if next_idx >= len(sequence):
            return EnumBuildLoopPhase.COMPLETE
        return sequence[next_idx]

    return next_candidate


class ModelLoopState(BaseModel):
    """Mutable FSM state for the build loop.

    Tracks the current phase, cycle count, consecutive failure count,
    and per-phase metrics. Immutable (frozen) — state transitions produce
    new instances.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Root correlation ID.")
    current_phase: EnumBuildLoopPhase = Field(
        default=EnumBuildLoopPhase.IDLE, description="Current FSM phase."
    )
    mode: EnumBuildLoopMode = Field(
        default=EnumBuildLoopMode.BUILD, description="Execution mode."
    )
    cycle_count: int = Field(default=0, ge=0, description="Completed cycles.")
    consecutive_failures: int = Field(
        default=0, ge=0, description="Consecutive phase failures."
    )
    max_consecutive_failures: int = Field(
        default=3, ge=1, description="Circuit breaker threshold."
    )
    skip_closeout: bool = Field(default=False, description="Skip CLOSING_OUT phase.")
    dry_run: bool = Field(default=False, description="No side effects.")
    tickets_filled: int = Field(default=0, ge=0)
    tickets_classified: int = Field(default=0, ge=0)
    tickets_dispatched: int = Field(default=0, ge=0)
    error_message: str | None = Field(default=None)
    verification_snapshot: dict[str, object] | None = Field(
        default=None,
        description="Captured during VERIFYING phase — platform readiness, "
        "golden chain, and data flow results.",
    )


__all__: list[str] = [
    "TERMINAL_PHASES",
    "EnumBuildLoopMode",
    "EnumBuildLoopPhase",
    "ModelLoopState",
    "next_phase",
]
