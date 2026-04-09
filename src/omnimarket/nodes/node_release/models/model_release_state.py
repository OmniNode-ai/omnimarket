"""Models for node_release — FSM state."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumReleasePhase(StrEnum):
    """FSM phases for the release workflow."""

    IDLE = "idle"
    BUMP_VERSIONS = "bump_versions"
    PIN_CROSS_REPO = "pin_cross_repo"
    CREATE_PRS = "create_prs"
    MERGE = "merge"
    TAG = "tag"
    PUBLISH = "publish"
    DONE = "done"
    FAILED = "failed"


_PHASE_SEQUENCE: tuple[EnumReleasePhase, ...] = (
    EnumReleasePhase.BUMP_VERSIONS,
    EnumReleasePhase.PIN_CROSS_REPO,
    EnumReleasePhase.CREATE_PRS,
    EnumReleasePhase.MERGE,
    EnumReleasePhase.TAG,
    EnumReleasePhase.PUBLISH,
    EnumReleasePhase.DONE,
)

TERMINAL_PHASES: frozenset[EnumReleasePhase] = frozenset(
    {EnumReleasePhase.DONE, EnumReleasePhase.FAILED}
)


def next_phase(current: EnumReleasePhase) -> EnumReleasePhase:
    """Return the next phase. Raises ValueError for terminal phases."""
    if current in TERMINAL_PHASES:
        msg = f"Cannot advance from terminal phase: {current}"
        raise ValueError(msg)
    if current == EnumReleasePhase.IDLE:
        return _PHASE_SEQUENCE[0]
    idx = _PHASE_SEQUENCE.index(current)
    return _PHASE_SEQUENCE[idx + 1]


class ModelReleaseState(BaseModel):
    """Mutable FSM state for the release workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    current_phase: EnumReleasePhase = Field(default=EnumReleasePhase.IDLE)
    repos: list[str] = Field(default_factory=list)
    bump: str | None = Field(default=None)
    dry_run: bool = Field(default=False)
    consecutive_failures: int = Field(default=0, ge=0)
    max_consecutive_failures: int = Field(default=3, ge=1)
    repos_succeeded: int = Field(default=0, ge=0)
    repos_failed: int = Field(default=0, ge=0)
    repos_skipped: int = Field(default=0, ge=0)
    error_message: str | None = Field(default=None)


class ModelReleasePhaseEvent(BaseModel):
    """Emitted on each FSM phase transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    from_phase: EnumReleasePhase = Field(...)
    to_phase: EnumReleasePhase = Field(...)
    success: bool = Field(...)
    error_message: str | None = Field(default=None)


class ModelReleaseCompletedEvent(BaseModel):
    """Emitted when the release workflow finishes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    final_phase: EnumReleasePhase = Field(...)
    repos_succeeded: int = Field(default=0, ge=0)
    repos_failed: int = Field(default=0, ge=0)
    repos_skipped: int = Field(default=0, ge=0)
    error_message: str | None = Field(default=None)


# Legacy alias
class ModelReleaseStartCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = ""
    repos: list[str] = Field(default_factory=list)
    bump: str = ""
    dry_run: bool = False
    resume: str = ""
    skip_pypi_wait: bool = False
    autonomous: bool = False
    gate_attestation: str = ""


__all__: list[str] = [
    "TERMINAL_PHASES",
    "EnumReleasePhase",
    "ModelReleaseCompletedEvent",
    "ModelReleasePhaseEvent",
    "ModelReleaseStartCommand",
    "ModelReleaseState",
    "next_phase",
]
