"""Models for node_redeploy — FSM state."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumRedeployPhase(StrEnum):
    """FSM phases for the redeploy workflow."""

    IDLE = "idle"
    SYNC_CLONES = "sync_clones"
    UPDATE_PINS = "update_pins"
    REBUILD = "rebuild"
    SEED_INFISICAL = "seed_infisical"
    VERIFY_HEALTH = "verify_health"
    DONE = "done"
    FAILED = "failed"


_PHASE_SEQUENCE: tuple[EnumRedeployPhase, ...] = (
    EnumRedeployPhase.SYNC_CLONES,
    EnumRedeployPhase.UPDATE_PINS,
    EnumRedeployPhase.REBUILD,
    EnumRedeployPhase.SEED_INFISICAL,
    EnumRedeployPhase.VERIFY_HEALTH,
    EnumRedeployPhase.DONE,
)

TERMINAL_PHASES: frozenset[EnumRedeployPhase] = frozenset(
    {EnumRedeployPhase.DONE, EnumRedeployPhase.FAILED}
)


def next_phase(current: EnumRedeployPhase) -> EnumRedeployPhase:
    """Return the next phase. Raises ValueError for terminal phases."""
    if current in TERMINAL_PHASES:
        msg = f"Cannot advance from terminal phase: {current}"
        raise ValueError(msg)
    if current == EnumRedeployPhase.IDLE:
        return _PHASE_SEQUENCE[0]
    idx = _PHASE_SEQUENCE.index(current)
    return _PHASE_SEQUENCE[idx + 1]


class ModelRedeployState(BaseModel):
    """Mutable FSM state for the redeploy workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    current_phase: EnumRedeployPhase = Field(default=EnumRedeployPhase.IDLE)
    versions: dict[str, str] = Field(default_factory=dict)
    skip_sync: bool = Field(default=False)
    verify_only: bool = Field(default=False)
    dry_run: bool = Field(default=False)
    consecutive_failures: int = Field(default=0, ge=0)
    max_consecutive_failures: int = Field(default=3, ge=1)
    phases_completed: int = Field(default=0, ge=0)
    error_message: str | None = Field(default=None)


class ModelRedeployPhaseEvent(BaseModel):
    """Emitted on each FSM phase transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    from_phase: EnumRedeployPhase = Field(...)
    to_phase: EnumRedeployPhase = Field(...)
    success: bool = Field(...)
    error_message: str | None = Field(default=None)


class ModelRedeployCompletedEvent(BaseModel):
    """Emitted when the redeploy workflow finishes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    final_phase: EnumRedeployPhase = Field(...)
    phases_completed: int = Field(default=0, ge=0)
    error_message: str | None = Field(default=None)


# Legacy aliases kept for old handler imports
class ModelRedeployStartCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = ""
    versions: str = ""
    skip_sync: bool = False
    skip_dockerfile_update: bool = False
    skip_infisical: bool = False
    verify_only: bool = False
    dry_run: bool = False
    resume: str = ""


__all__: list[str] = [
    "TERMINAL_PHASES",
    "EnumRedeployPhase",
    "ModelRedeployCompletedEvent",
    "ModelRedeployPhaseEvent",
    "ModelRedeployStartCommand",
    "ModelRedeployState",
    "next_phase",
]
