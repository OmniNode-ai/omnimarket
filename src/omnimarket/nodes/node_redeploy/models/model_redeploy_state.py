"""Models for node_redeploy — FSM state."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_redeploy.models.model_redeploy_command import (
    EnumRuntimeLane,
)


class EnumRedeployPhase(StrEnum):
    """FSM phases for the redeploy workflow.

    The base deploy segment (``IDLE`` … ``VERIFY_HEALTH`` … ``DONE``) is the
    original deploy-agent lifecycle and is unchanged. OMN-12577 adds a
    post-deploy *verification* segment after ``VERIFY_HEALTH`` — per-lane probe,
    runtime sweep, evidence reduction, OCC draft/validate, readiness scoring —
    that terminates in ``READY`` or ``BLOCKED``, plus a ``ROLLING_BACK`` →
    ``ROLLED_BACK`` rollback segment for failed post-deploy health.
    """

    IDLE = "idle"
    SYNC_CLONES = "sync_clones"
    UPDATE_PINS = "update_pins"
    REBUILD = "rebuild"
    SEED_INFISICAL = "seed_infisical"
    VERIFY_HEALTH = "verify_health"
    DONE = "done"
    FAILED = "failed"
    # OMN-12577 post-deploy verification segment.
    PROBING = "probing"
    SWEEPING = "sweeping"
    EVIDENCE_REDUCING = "evidence_reducing"
    OCC_DRAFTING = "occ_drafting"
    OCC_VALIDATING = "occ_validating"
    READINESS_SCORING = "readiness_scoring"
    READY = "ready"
    BLOCKED = "blocked"
    # OMN-12577 rollback segment.
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


_PHASE_SEQUENCE: tuple[EnumRedeployPhase, ...] = (
    EnumRedeployPhase.SYNC_CLONES,
    EnumRedeployPhase.UPDATE_PINS,
    EnumRedeployPhase.REBUILD,
    EnumRedeployPhase.SEED_INFISICAL,
    EnumRedeployPhase.VERIFY_HEALTH,
    EnumRedeployPhase.DONE,
)

# OMN-12577 verification segment, walked after VERIFY_HEALTH completes. It is a
# separate ordered tuple so the legacy ``_PHASE_SEQUENCE`` (and the existing
# golden-chain tests pinned to its 6-transition shape) stay byte-identical.
_VERIFICATION_SEQUENCE: tuple[EnumRedeployPhase, ...] = (
    EnumRedeployPhase.PROBING,
    EnumRedeployPhase.SWEEPING,
    EnumRedeployPhase.EVIDENCE_REDUCING,
    EnumRedeployPhase.OCC_DRAFTING,
    EnumRedeployPhase.OCC_VALIDATING,
    EnumRedeployPhase.READINESS_SCORING,
)

TERMINAL_PHASES: frozenset[EnumRedeployPhase] = frozenset(
    {
        EnumRedeployPhase.DONE,
        EnumRedeployPhase.FAILED,
        EnumRedeployPhase.READY,
        EnumRedeployPhase.BLOCKED,
        EnumRedeployPhase.ROLLED_BACK,
    }
)

# Phases from which a post-deploy health failure may begin a rollback.
ROLLBACK_ELIGIBLE_PHASES: frozenset[EnumRedeployPhase] = frozenset(
    {
        EnumRedeployPhase.VERIFY_HEALTH,
        EnumRedeployPhase.PROBING,
        EnumRedeployPhase.REBUILD,
    }
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


def next_verification_phase(current: EnumRedeployPhase) -> EnumRedeployPhase:
    """Return the next post-deploy verification phase.

    ``VERIFY_HEALTH`` is the entry edge into the verification segment; the
    segment terminates on ``READINESS_SCORING`` whose successor is decided by
    the readiness gate (``READY`` or ``BLOCKED``), so this helper does not
    advance past ``READINESS_SCORING``.
    """
    if current is EnumRedeployPhase.VERIFY_HEALTH:
        return _VERIFICATION_SEQUENCE[0]
    if current is EnumRedeployPhase.READINESS_SCORING:
        msg = "READINESS_SCORING transitions to READY or BLOCKED via the gate"
        raise ValueError(msg)
    if current not in _VERIFICATION_SEQUENCE:
        msg = f"Not a verification phase: {current}"
        raise ValueError(msg)
    idx = _VERIFICATION_SEQUENCE.index(current)
    return _VERIFICATION_SEQUENCE[idx + 1]


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
    # OMN-12577 lane / digest / rollback tracking.
    runtime_lane: EnumRuntimeLane = Field(
        default=EnumRuntimeLane.DEV,
        description="Target runtime lane for the deployment.",
    )
    image_digest: str | None = Field(
        default=None,
        description=(
            "Immutable image digest for the deployed artifact. For prod this is "
            "pinned to the stability-test READY digest."
        ),
    )
    promotion_batch_id: str | None = Field(
        default=None,
        description="Promotion batch identifier shared with OCC evidence.",
    )
    previous_image: str | None = Field(
        default=None,
        description="Previous known-good image, restored on rollback.",
    )
    rolled_back: bool = Field(
        default=False,
        description="True once a rollback transition has completed.",
    )


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


class ModelRedeployRolledBackEvent(BaseModel):
    """Emitted when a failed post-deploy health check triggers rollback.

    Published on ``onex.evt.omnimarket.redeploy-rolled-back.v1`` (OMN-9579). The
    deploy effect restores ``restored_image`` (the previous known-good image)
    and ``failure_reason`` records why the rollback fired (smoke/health/timeout).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Redeploy run correlation ID.")
    runtime_lane: EnumRuntimeLane = Field(
        default=EnumRuntimeLane.DEV,
        description="Lane the rolled-back deployment targeted.",
    )
    restored_image: str = Field(
        ..., min_length=1, description="Previous known-good image that was restored."
    )
    failure_reason: str = Field(
        ..., min_length=1, description="Why the rollback was triggered."
    )
    failed_phase: EnumRedeployPhase = Field(
        ..., description="Phase whose failure triggered the rollback."
    )


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
    "ROLLBACK_ELIGIBLE_PHASES",
    "TERMINAL_PHASES",
    "EnumRedeployPhase",
    "ModelRedeployCompletedEvent",
    "ModelRedeployPhaseEvent",
    "ModelRedeployRolledBackEvent",
    "ModelRedeployStartCommand",
    "ModelRedeployState",
    "next_phase",
    "next_verification_phase",
]
