# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared runtime-deployment event surfaces and domain models.

Canonical owner (OMN-13211 / B3) of the deployment domain primitives shared by
the four canonical redeploy nodes — orchestrator, deploy publish-monitor effect,
prod-promotion-gate compute, and FSM reducer. The bespoke ``node_redeploy``
WorkflowPackage that previously owned these models is deleted in B3; co-locating
them here keeps a single source of truth and avoids one node importing another
node's private package (the dependency-elimination invariant).

What lives here:

* ``RuntimeLaneLike`` / ``ModelRuntimeDeploymentProof`` (Protocol) — the
  structural deployment-proof shape consumed by ``evidence_pipeline_native``.
* The FSM phase enum (``EnumRedeployPhase``), sequence helpers, and the
  ``ModelRedeployState`` + transition-event models.
* The redeploy start command (``ModelRedeployCommand``).
* Lane policy + same-digest gate (``lane_target`` / ``evaluate_prod_digest_gate``).
* The full prod promotion gate (``evaluate_prod_promotion_gate``).
* The deploy-agent Kafka wire DTOs (``ModelDeployRebuildCommand`` /
  ``ModelDeployRebuildCompleted`` / ``ModelRedeployResult``) round-tripped with
  the external ``.201`` deploy agent.

``EnumRuntimeLane`` is the canonical core wire enum re-exported here for
convenience (its authority lives in ``omnibase_core.models.runtime_deployment``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

from omnibase_compat.contracts.evidence_pipeline.wire.types import ReadinessState
from omnibase_core.models.runtime_deployment.wire import EnumRuntimeLane
from pydantic import BaseModel, ConfigDict, Field, model_validator


class RuntimeLaneLike(Protocol):
    """Runtime lane enum shape shared across deployment-proof consumers."""

    @property
    def value(self) -> str:
        """Wire value for the runtime lane."""


class ModelRuntimeDeploymentProof(Protocol):
    """Shared deployment-proof shape consumed outside the redeploy node."""

    correlation_id: UUID
    deployment_id: UUID
    runtime_lane: RuntimeLaneLike
    source_sha: str
    image_digest: str
    compose_project: str
    health_status: str
    ready_status: str
    probed_at: datetime
    status: str
    promotion_batch_id: str | None
    runtime_addresses: Sequence[str]
    topology_manifest_sha256: str | None
    package_versions: Mapping[str, str]
    runtime_source_hash: str | None
    consumer_groups: Sequence[str]
    runtime_sweep_input_ref: str | None


# Previous known-good runtime image restored on rollback when the deployment did
# not carry an explicit prior digest. The documented last-good runtime tag; a real
# deployment overrides it via ``ModelRedeployState.previous_image``.
DEFAULT_PREVIOUS_IMAGE = "omninode-runtime:v2.3.1"


# ---------------------------------------------------------------------------
# FSM phases + transition state
# ---------------------------------------------------------------------------


class EnumRedeployPhase(StrEnum):
    """FSM phases for the redeploy workflow.

    The base deploy segment (``IDLE`` … ``VERIFY_HEALTH`` … ``DONE``) is the
    original deploy-agent lifecycle. OMN-12577 adds a post-deploy *verification*
    segment after ``VERIFY_HEALTH`` — per-lane probe, runtime sweep, evidence
    reduction, OCC draft/validate, readiness scoring — that terminates in
    ``READY`` or ``BLOCKED``, plus a ``ROLLING_BACK`` → ``ROLLED_BACK`` rollback
    segment for failed post-deploy health.
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

# OMN-12577 verification segment, walked after VERIFY_HEALTH completes.
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
    segment terminates on ``READINESS_SCORING`` whose successor is decided by the
    readiness gate (``READY`` or ``BLOCKED``).
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


class ModelRedeployCommand(BaseModel):
    """Command to start a post-release runtime redeploy.

    OMN-12576 evolves this command in place (rather than forking a parallel
    deployment-request contract) with the lane/digest/promotion fields the
    node-based deployment design needs. The new fields are optional so existing
    callers and dev-lane triggers stay valid; production pins ``image_digest``
    and ``promotion_batch_id`` to the artifact proven READY in stability-test.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Redeploy run correlation ID.")
    versions: dict[str, str] = Field(
        default_factory=dict,
        description="Plugin version pins (pkg -> version).",
    )
    skip_sync: bool = Field(default=False, description="Skip SYNC_CLONES phase.")
    verify_only: bool = Field(default=False, description="Skip to VERIFY_HEALTH only.")
    dry_run: bool = Field(default=False, description="Print without executing.")
    requested_at: datetime = Field(..., description="When the command was issued.")
    runtime_lane: EnumRuntimeLane = Field(
        default=EnumRuntimeLane.DEV,
        description="Target runtime lane for this deployment.",
    )
    image_ref: str | None = Field(
        default=None,
        description="Mutable image reference. The digest is the authority, not the ref.",
    )
    image_digest: str | None = Field(
        default=None,
        description=(
            "Immutable image digest. Required for prod; pinned to the "
            "stability-test READY digest."
        ),
    )
    promotion_batch_id: str | None = Field(
        default=None,
        description="Promotion batch identifier shared with OCC evidence; required for prod.",
    )

    @model_validator(mode="after")
    def validate_prod_pins(self) -> ModelRedeployCommand:
        """Production redeploys must pin the proven stability artifact."""
        if self.runtime_lane is EnumRuntimeLane.PROD and (
            not self.image_digest or not self.promotion_batch_id
        ):
            raise ValueError(
                "prod redeploy requires image_digest and promotion_batch_id"
            )
        return self


class ModelRedeployState(BaseModel):
    """Immutable FSM state for the redeploy workflow."""

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
    deploy effect restores ``restored_image`` (the previous known-good image) and
    ``failure_reason`` records why the rollback fired (smoke/health/timeout).
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


# ---------------------------------------------------------------------------
# Lane policy + same-digest gate
# ---------------------------------------------------------------------------


class ModelLaneDeployTarget(BaseModel):
    """Per-lane compose project, overlay files, and runtime health targets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_lane: EnumRuntimeLane = Field(..., description="Runtime lane.")
    compose_project: str = Field(
        ..., min_length=1, description="Compose project name that owns the lane."
    )
    compose_files: tuple[str, ...] = Field(
        ..., description="Compose overlay files applied for the lane (base first)."
    )
    health_targets: tuple[str, ...] = Field(
        ..., description="Per-lane runtime health endpoints (main, effects)."
    )
    rebuilds_from_source: bool = Field(
        ...,
        description=(
            "True if the lane builds from a ref; False if it deploys a pinned "
            "digest only (prod). Production never rebuilds."
        ),
    )


_BASE_COMPOSE = "docker-compose.infra.yml"

_LANE_TARGETS: dict[EnumRuntimeLane, ModelLaneDeployTarget] = {
    EnumRuntimeLane.DEV: ModelLaneDeployTarget(
        runtime_lane=EnumRuntimeLane.DEV,
        compose_project="omnibase-infra",
        compose_files=(_BASE_COMPOSE,),
        health_targets=(
            "http://omninode-runtime:8085/health",
            "http://runtime-effects:8086/health",
        ),
        rebuilds_from_source=True,
    ),
    EnumRuntimeLane.STABILITY_TEST: ModelLaneDeployTarget(
        runtime_lane=EnumRuntimeLane.STABILITY_TEST,
        compose_project="omnibase-infra-stability-test",
        compose_files=(_BASE_COMPOSE, "docker-compose.stability-test.yml"),
        health_targets=(
            "http://omninode-runtime:18085/health",
            "http://runtime-effects:18086/health",
        ),
        rebuilds_from_source=True,
    ),
    EnumRuntimeLane.PROD: ModelLaneDeployTarget(
        runtime_lane=EnumRuntimeLane.PROD,
        compose_project="omnibase-infra-prod",
        compose_files=(_BASE_COMPOSE, "docker-compose.prod.yml"),
        health_targets=(
            "http://omninode-runtime:28085/health",
            "http://runtime-effects:28086/health",
        ),
        rebuilds_from_source=False,
    ),
}


def lane_target(runtime_lane: EnumRuntimeLane) -> ModelLaneDeployTarget:
    """Return the deploy target (project / overlays / health) for a lane."""
    return _LANE_TARGETS[runtime_lane]


class ModelStabilityReadiness(BaseModel):
    """A stability-test readiness fact consulted by the prod same-digest gate.

    A prod deploy is gated on a ``stability-test`` lane that reached ``READY``
    for a specific image digest. ``ready`` records whether stability passed;
    failed stability readiness blocks prod even when the digest matches.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_lane: EnumRuntimeLane = Field(
        default=EnumRuntimeLane.STABILITY_TEST,
        description="Lane this readiness fact came from (stability-test).",
    )
    image_digest: str = Field(
        ..., min_length=1, description="Digest proven (or attempted) in stability."
    )
    promotion_batch_id: str | None = Field(
        default=None, description="Promotion batch the digest belongs to."
    )
    ready: bool = Field(
        ..., description="True only when stability-test reached READY for the digest."
    )


class ModelProdGateDecision(BaseModel):
    """Result of the production deploy eligibility (same-digest) gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool = Field(..., description="True if prod may enter REBUILD/deploy.")
    image_digest: str | None = Field(
        default=None,
        description="The stability-proven digest prod must reuse (no rebuild).",
    )
    reason: str = Field(
        ..., min_length=1, description="Human-readable gate decision reason."
    )


def evaluate_prod_digest_gate(
    requested_digest: str | None,
    stability_readiness: ModelStabilityReadiness | None,
) -> ModelProdGateDecision:
    """Decide whether a prod deploy may proceed, before any deploy effect.

    Rules (plan "Prod Deploy Eligibility"):
      - a matching ``stability-test`` deployment must be ``READY``;
      - the prod request digest must equal the stability ``READY`` digest;
      - prod reuses that exact digest (it does not rebuild).

    Returns a decision rather than raising so the FSM can route to BLOCKED with a
    reason instead of crashing the workflow.
    """
    if requested_digest is None or not requested_digest.strip():
        return ModelProdGateDecision(
            allowed=False,
            image_digest=None,
            reason="prod deploy requires a pinned image_digest",
        )
    if stability_readiness is None:
        return ModelProdGateDecision(
            allowed=False,
            image_digest=None,
            reason=(
                "no stability-test readiness event exists for the requested "
                "digest; prod is blocked"
            ),
        )
    if not stability_readiness.ready:
        return ModelProdGateDecision(
            allowed=False,
            image_digest=None,
            reason="stability-test readiness failed for the digest; prod is blocked",
        )
    if stability_readiness.image_digest != requested_digest:
        return ModelProdGateDecision(
            allowed=False,
            image_digest=None,
            reason=(
                "prod digest does not match the latest stability-test READY "
                f"digest ({stability_readiness.image_digest!r} != "
                f"{requested_digest!r})"
            ),
        )
    return ModelProdGateDecision(
        allowed=True,
        image_digest=stability_readiness.image_digest,
        reason="stability-test READY for matching digest; prod reuses it (no rebuild)",
    )


# ---------------------------------------------------------------------------
# Full prod promotion gate (Phase 6)
# ---------------------------------------------------------------------------


class EnumOccGateState(StrEnum):
    """Whether OCC evidence satisfies the prod promotion gate.

    Production may promote only when its OCC evidence is durable: either the OCC
    PR is merged on the governance branch, or the Receipt Gate has PASS evidence
    for the contract. ``PENDING`` / ``BLOCKED`` block promotion.
    """

    MERGED = "merged"
    RECEIPT_GATE_PASS = "receipt-gate-pass"
    PENDING = "pending"
    BLOCKED = "blocked"


_OCC_SATISFIED: frozenset[EnumOccGateState] = frozenset(
    {EnumOccGateState.MERGED, EnumOccGateState.RECEIPT_GATE_PASS}
)


class EnumProdGrantReason(StrEnum):
    """Typed prod-promotion authorization-grant failure reasons (OMN-13436).

    Emitted verbatim in the gate decision reason so consumers can branch on the
    exact authorization failure mode without parsing free text.
    """

    MISSING_PROMOTION_GRANT = "missing_promotion_grant"
    EXPIRED_PROMOTION_GRANT = "expired_promotion_grant"
    GRANT_LANE_MISMATCH = "grant_lane_mismatch"
    GRANT_DIGEST_MISMATCH = "grant_digest_mismatch"
    GRANT_BATCH_MISMATCH = "grant_batch_mismatch"
    SELF_GRANTED = "self_granted"


class ModelReadinessProjectionFact(BaseModel):
    """A stability-test readiness fact read from the reducer-owned projection.

    The Phase-6 input that closes the OMN-12577 gap: the prod gate resolves
    stability readiness from the ``deployment_readiness_projection`` (reducer-
    owned truth), not from a stub that always returned ``None``. The
    ``readiness_state`` is the projected ``ReadinessState``; only ``READY``
    satisfies the gate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_lane: EnumRuntimeLane = Field(
        default=EnumRuntimeLane.STABILITY_TEST,
        description="Lane the readiness fact came from (stability-test).",
    )
    readiness_state: ReadinessState = Field(
        ..., description="Projected readiness state for the stability deployment."
    )
    image_digest: str = Field(
        ...,
        min_length=1,
        description="Digest proven READY in stability for this promotion batch.",
    )
    promotion_batch_id: str = Field(
        ...,
        min_length=1,
        description="Promotion batch the projected readiness belongs to.",
    )

    def as_stability_readiness(self) -> ModelStabilityReadiness:
        """Project this fact onto the OMN-12577 same-digest gate input."""
        return ModelStabilityReadiness(
            runtime_lane=self.runtime_lane,
            image_digest=self.image_digest,
            promotion_batch_id=self.promotion_batch_id,
            ready=self.readiness_state == "READY",
        )


class ModelProdPromotionGrant(BaseModel):
    """An approver-issued authorization grant for one prod promotion (OMN-13436).

    The keystone fact of the prod-promotion authorization gate. A grant is issued
    out-of-band by an approver (resolved by the Phase-2b grant resolver EFFECT,
    never authored by the redeploy request it authorizes) and pins the exact lane,
    image digest, and promotion batch it authorizes. ``expires_at`` is an
    approver-set ABSOLUTE timestamp (not a 30-minute relative TTL): the prod gate
    fails closed once the deterministic ``evaluated_at`` passes it.

    Known follow-on gap (NOT closed by this model): the requester identity threaded
    into the gate is forgeable, so ``approved_by != requested_by`` is an
    anti-self-grant check, NOT cryptographic two-person integrity.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: str = Field(
        ..., min_length=1, description="Stable identifier for the authorization grant."
    )
    approved_lane: EnumRuntimeLane = Field(
        ..., description="Lane the grant authorizes; must be PROD for a prod gate."
    )
    approved_image_digest: str = Field(
        ...,
        min_length=1,
        description="Exact image digest the grant authorizes for promotion.",
    )
    approved_promotion_batch_id: str = Field(
        ...,
        min_length=1,
        description="Promotion batch the grant authorizes.",
    )
    approved_by: str = Field(
        ...,
        min_length=1,
        description="Approver identity; must differ from the requester (anti-self-grant).",
    )
    created_at: datetime = Field(..., description="When the approver issued the grant.")
    expires_at: datetime = Field(
        ...,
        description="Approver-set ABSOLUTE expiry; the gate fails closed once past it.",
    )


class ModelDeployRefusedEvent(BaseModel):
    """Emitted when the deploy EFFECT refuses a prod command at its own boundary.

    Defense-in-depth refusal (OMN-13440 Phase 3): the deploy publish-monitor
    EFFECT independently verifies that a prod ``ModelDeployPublishCommand`` carries
    a grant whose ``(approved_lane==PROD, approved_image_digest,
    approved_promotion_batch_id)`` MATCH the command's ``(runtime_lane,
    image_digest, promotion_batch_id)`` (target binding, not mere presence). When
    that target-binding check fails, the EFFECT refuses to publish the deploy-agent
    rebuild command and emits this durable fact on
    ``onex.evt.omnimarket.redeploy-deploy-refused.v1`` instead — closing the
    deploy-agent off-gate path even if the upstream gate were bypassed.

    ``reason`` is a typed ``EnumProdGrantReason`` so consumers branch on the exact
    authorization failure mode without parsing free text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Redeploy run correlation ID.")
    runtime_lane: EnumRuntimeLane = Field(
        ..., description="Lane the refused command targeted (PROD)."
    )
    requested_image_digest: str | None = Field(
        default=None,
        description="Digest the refused command wanted to deploy.",
    )
    promotion_batch_id: str | None = Field(
        default=None,
        description="Promotion batch the refused command belonged to.",
    )
    grant_id: str | None = Field(
        default=None,
        description="grant_id carried by the command, if any; None when absent.",
    )
    reason: EnumProdGrantReason = Field(
        ..., description="Typed prod-grant authorization failure mode."
    )
    detail: str = Field(..., min_length=1, description="Human-readable refusal detail.")


def verify_prod_deploy_grant_binding(
    *,
    runtime_lane: EnumRuntimeLane,
    image_digest: str | None,
    promotion_batch_id: str | None,
    grant: ModelProdPromotionGrant | None,
    evaluated_at: datetime | None,
) -> tuple[EnumProdGrantReason, str] | None:
    """Verify a prod deploy command is target-bound to a verified grant (OMN-13440).

    Pure, deterministic, side-effect-free. Returns ``None`` when the deploy may
    proceed; otherwise a ``(typed reason, detail)`` describing why the deploy EFFECT
    must refuse.

    Non-prod lanes always pass (``None``) — the prod-promotion authorization gate is
    a prod-only concern and dev/stability deploys are byte-for-byte unchanged.

    For ``runtime_lane == PROD`` the deploy refuses unless the grant satisfies ALL
    of (target binding, NOT mere presence):

    * a grant is present at all (``missing_promotion_grant``);
    * ``approved_lane == PROD`` (``grant_lane_mismatch``);
    * ``approved_image_digest == image_digest`` — a grant for digest A must never
      authorize a deploy of digest B (``grant_digest_mismatch``);
    * ``approved_promotion_batch_id == promotion_batch_id`` — likewise for batch
      (``grant_batch_mismatch``);
    * ``evaluated_at <= expires_at`` (``expired_promotion_grant``); the boundary is
      inclusive and ``evaluated_at`` must be present (a prod deploy with no
      evaluation timestamp is treated as expired/unverifiable).

    This mirrors ``_evaluate_promotion_grant`` in the prod gate COMPUTE but runs at
    the deploy EFFECT boundary so the deploy-agent off-gate path is closed even if
    the upstream gate were bypassed. ``approved_by != requested_by`` (anti-self-
    grant) is enforced upstream at the gate; the deploy EFFECT does not re-thread a
    forgeable requester identity, so it does not duplicate that check here.
    """
    if runtime_lane is not EnumRuntimeLane.PROD:
        return None

    if grant is None:
        return (
            EnumProdGrantReason.MISSING_PROMOTION_GRANT,
            "prod deploy refused: no verified promotion grant on the command",
        )
    if grant.approved_lane is not EnumRuntimeLane.PROD:
        return (
            EnumProdGrantReason.GRANT_LANE_MISMATCH,
            f"prod deploy refused: grant lane {grant.approved_lane.value!r} != prod",
        )
    if grant.approved_image_digest != image_digest:
        return (
            EnumProdGrantReason.GRANT_DIGEST_MISMATCH,
            "prod deploy refused: grant digest "
            f"{grant.approved_image_digest!r} != command digest {image_digest!r}",
        )
    if grant.approved_promotion_batch_id != promotion_batch_id:
        return (
            EnumProdGrantReason.GRANT_BATCH_MISMATCH,
            "prod deploy refused: grant batch "
            f"{grant.approved_promotion_batch_id!r} != command batch "
            f"{promotion_batch_id!r}",
        )
    if evaluated_at is None or evaluated_at > grant.expires_at:
        return (
            EnumProdGrantReason.EXPIRED_PROMOTION_GRANT,
            "prod deploy refused: grant expired "
            f"(evaluated_at={evaluated_at}, expires_at={grant.expires_at})",
        )
    return None


class ModelProdPromotionInputs(BaseModel):
    """All deterministic facts the prod promotion gate consults."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_image_digest: str | None = Field(
        default=None,
        description="Digest the prod request wants to deploy (must equal stability READY).",
    )
    promotion_batch_id: str | None = Field(
        default=None,
        description="Promotion batch the prod request belongs to.",
    )
    readiness_projection: ModelReadinessProjectionFact | None = Field(
        default=None,
        description="Reducer-owned stability readiness projection; None means absent.",
    )
    occ_gate_state: EnumOccGateState = Field(
        default=EnumOccGateState.PENDING,
        description="Whether OCC evidence is merged / Receipt-Gate-PASS.",
    )
    rollback_target: str | None = Field(
        default=None,
        description="Known previous-good digest restored on failed post-deploy health.",
    )
    requested_by: str = Field(
        default="node_redeploy_orchestrator",
        description="Identity that requested the promotion; gated against the grant approver.",
    )
    promotion_grant: ModelProdPromotionGrant | None = Field(
        default=None,
        description="Approver-issued authorization grant; None means the prod gate fails closed.",
    )
    evaluated_at: datetime = Field(
        ...,
        description=(
            "Deterministic evaluation timestamp threaded in by the orchestrator/"
            "runtime; compared against the grant's absolute expiry. NEVER "
            "datetime.now() inside the compute."
        ),
    )


class ModelProdPromotionGateDecision(BaseModel):
    """Result of the production promotion gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool = Field(..., description="True if prod promotion may proceed.")
    image_digest: str | None = Field(
        default=None,
        description="The stability-proven digest prod must reuse (no rebuild).",
    )
    rollback_target: str | None = Field(
        default=None,
        description="Known rollback target carried into the deploy for the rollback path.",
    )
    reason: str = Field(
        ..., min_length=1, description="Human-readable gate decision reason."
    )


def evaluate_prod_promotion_gate(
    inputs: ModelProdPromotionInputs,
) -> ModelProdPromotionGateDecision:
    """Decide whether a prod promotion may proceed, before any deploy effect.

    Rules (plan "Prod Deploy Eligibility" + Phase 6):
      - a reducer-owned readiness projection must exist;
      - the projection's promotion batch must match the request;
      - the stability-test lane must be READY in that projection;
      - the prod request digest must equal the stability READY digest
        (delegated to the OMN-12577 ``evaluate_prod_digest_gate``);
      - OCC evidence must be merged or Receipt-Gate-PASS;
      - a known rollback target must exist.

    Returns a decision rather than raising so the FSM can route to BLOCKED with a
    reason instead of crashing the workflow.
    """
    projection = inputs.readiness_projection
    if projection is None:
        return ModelProdPromotionGateDecision(
            allowed=False,
            image_digest=None,
            rollback_target=inputs.rollback_target,
            reason=(
                "no reducer-owned readiness projection exists for the prod "
                "request; promotion is blocked"
            ),
        )

    if projection.promotion_batch_id != inputs.promotion_batch_id:
        return ModelProdPromotionGateDecision(
            allowed=False,
            image_digest=None,
            rollback_target=inputs.rollback_target,
            reason=(
                "readiness projection promotion batch does not match the prod "
                f"request ({projection.promotion_batch_id!r} != "
                f"{inputs.promotion_batch_id!r})"
            ),
        )

    if projection.readiness_state != "READY":
        return ModelProdPromotionGateDecision(
            allowed=False,
            image_digest=None,
            rollback_target=inputs.rollback_target,
            reason=(
                "stability-test readiness projection is not READY "
                f"({projection.readiness_state!r}); prod promotion is blocked"
            ),
        )

    # Exact-digest enforcement is delegated to the OMN-12577 same-digest gate so
    # there is a single source of truth for the prod-vs-stability drift guard.
    digest_gate = evaluate_prod_digest_gate(
        requested_digest=inputs.requested_image_digest,
        stability_readiness=projection.as_stability_readiness(),
    )
    if not digest_gate.allowed:
        return ModelProdPromotionGateDecision(
            allowed=False,
            image_digest=None,
            rollback_target=inputs.rollback_target,
            reason=digest_gate.reason,
        )

    if inputs.occ_gate_state not in _OCC_SATISFIED:
        return ModelProdPromotionGateDecision(
            allowed=False,
            image_digest=None,
            rollback_target=inputs.rollback_target,
            reason=(
                "OCC evidence is not durable "
                f"({inputs.occ_gate_state.value}); prod promotion requires a "
                "merged OCC PR or Receipt Gate PASS"
            ),
        )

    if inputs.rollback_target is None or not inputs.rollback_target.strip():
        return ModelProdPromotionGateDecision(
            allowed=False,
            image_digest=None,
            rollback_target=None,
            reason="prod promotion requires a known rollback target",
        )

    # OMN-13436: prod-promotion authorization gate. After the technical checks
    # pass, prod additionally requires an approver-issued promotion grant. The
    # gate fails CLOSED — no grant, or a grant that does not satisfy every field,
    # blocks promotion. dev/stability never reach this function (the compute node
    # short-circuits non-prod lanes), so their behavior is byte-for-byte unchanged.
    grant_block = _evaluate_promotion_grant(inputs)
    if grant_block is not None:
        return grant_block

    return ModelProdPromotionGateDecision(
        allowed=True,
        image_digest=digest_gate.image_digest,
        rollback_target=inputs.rollback_target,
        reason=(
            "readiness projection READY for matching digest and batch, OCC "
            "evidence durable, rollback target known, promotion grant authorized; "
            "prod reuses the stability digest (no rebuild)"
        ),
    )


def _evaluate_promotion_grant(
    inputs: ModelProdPromotionInputs,
) -> ModelProdPromotionGateDecision | None:
    """Authorize a prod promotion against its approver-issued grant.

    Returns a BLOCKED decision (with a typed ``EnumProdGrantReason``) when the
    grant is absent or fails any condition; returns ``None`` when the grant fully
    authorizes the request so the caller proceeds to allow.

    The grant must satisfy ALL of: ``approved_lane == PROD``, digest match, batch
    match, ``approved_by != requested_by`` (anti-self-grant), and
    ``evaluated_at <= expires_at`` (absolute expiry, inclusive boundary).
    """
    grant = inputs.promotion_grant
    if grant is None:
        return _grant_blocked(
            inputs,
            EnumProdGrantReason.MISSING_PROMOTION_GRANT,
            "prod promotion requires an approver-issued promotion grant; none present",
        )

    if grant.approved_lane is not EnumRuntimeLane.PROD:
        return _grant_blocked(
            inputs,
            EnumProdGrantReason.GRANT_LANE_MISMATCH,
            (
                "promotion grant authorizes a different lane "
                f"({grant.approved_lane.value!r} != prod)"
            ),
        )

    if grant.approved_image_digest != inputs.requested_image_digest:
        return _grant_blocked(
            inputs,
            EnumProdGrantReason.GRANT_DIGEST_MISMATCH,
            (
                "promotion grant digest does not match the request "
                f"({grant.approved_image_digest!r} != {inputs.requested_image_digest!r})"
            ),
        )

    if grant.approved_promotion_batch_id != inputs.promotion_batch_id:
        return _grant_blocked(
            inputs,
            EnumProdGrantReason.GRANT_BATCH_MISMATCH,
            (
                "promotion grant batch does not match the request "
                f"({grant.approved_promotion_batch_id!r} != {inputs.promotion_batch_id!r})"
            ),
        )

    if grant.approved_by == inputs.requested_by:
        return _grant_blocked(
            inputs,
            EnumProdGrantReason.SELF_GRANTED,
            (
                "promotion grant approver equals the requester "
                f"({grant.approved_by!r}); self-granted promotions are blocked"
            ),
        )

    if inputs.evaluated_at > grant.expires_at:
        return _grant_blocked(
            inputs,
            EnumProdGrantReason.EXPIRED_PROMOTION_GRANT,
            (
                "promotion grant has expired "
                f"(evaluated_at={inputs.evaluated_at.isoformat()} > "
                f"expires_at={grant.expires_at.isoformat()})"
            ),
        )

    return None


def _grant_blocked(
    inputs: ModelProdPromotionInputs,
    reason: EnumProdGrantReason,
    detail: str,
) -> ModelProdPromotionGateDecision:
    """Build a BLOCKED decision carrying a typed grant-failure reason verbatim."""
    return ModelProdPromotionGateDecision(
        allowed=False,
        image_digest=None,
        rollback_target=inputs.rollback_target,
        reason=f"{reason.value}: {detail}",
    )


# ---------------------------------------------------------------------------
# Deploy-agent Kafka wire DTOs (round-tripped with the external .201 agent)
# ---------------------------------------------------------------------------


class EnumRedeployScope(StrEnum):
    """Scope of a runtime rebuild command."""

    FULL = "full"
    RUNTIME = "runtime"
    CORE = "core"


class EnumBuildSource(StrEnum):
    """Artifact source accepted by the deploy-agent wire contract."""

    WORKSPACE = "workspace"
    RELEASE = "release"


class EnumPhaseResult(StrEnum):
    """Per-phase result in rebuild-completed event."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    PENDING = "pending"


class EnumRedeployStatus(StrEnum):
    """Top-level rebuild status."""

    SUCCESS = "success"
    FAILED = "failed"


class ModelHealthCheck(BaseModel):
    """Single service health check result."""

    model_config = ConfigDict(frozen=True, extra="ignore", from_attributes=True)

    service: str = Field(..., description="Service name.")
    endpoint: str = Field(..., description="Endpoint checked.")
    status: Literal["pass", "fail"] = Field(..., description="Check result.")
    latency_ms: int = Field(default=0, description="Latency in milliseconds.")


class ModelDeployRebuildCommand(BaseModel):
    """Command to trigger a deploy agent rebuild.

    Published to: onex.cmd.deploy.rebuild-requested.v1
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., description="UUID correlation ID for tracking.")
    requested_by: str = Field(
        default="node_redeploy_orchestrator",
        description="Identity of the requester.",
    )
    scope: EnumRedeployScope = Field(
        default=EnumRedeployScope.FULL,
        description="Scope of rebuild.",
    )
    runtime_lane: EnumRuntimeLane = Field(
        ...,
        description="Target runtime lane consumed by the deploy agent.",
    )
    build_source: EnumBuildSource = Field(
        default=EnumBuildSource.RELEASE,
        description="Whether the agent builds workspace code or release-promoted code.",
    )
    services: list[str] = Field(
        default_factory=list,
        description="Optional service filter. Empty = scope default.",
    )
    git_ref: str = Field(
        default="origin/main",
        description="Git ref to deploy.",
    )
    image_ref: str | None = Field(
        default=None,
        description="Mutable image reference. The digest is authoritative when present.",
    )
    image_digest: str | None = Field(
        default=None,
        description="Pinned image digest. Required for prod deployments.",
    )

    @model_validator(mode="after")
    def validate_prod_digest(self) -> ModelDeployRebuildCommand:
        """Production deploy-agent commands must pin the proven artifact."""
        if self.runtime_lane is EnumRuntimeLane.PROD and not self.image_digest:
            raise ValueError(
                "prod runtime_lane requires image_digest: production deploys the "
                "exact stability-proven digest"
            )
        return self


class ModelDeployPhaseResults(BaseModel):
    """Phase-level results from the deploy agent."""

    model_config = ConfigDict(frozen=True, extra="ignore", from_attributes=True)

    git: EnumPhaseResult = Field(default=EnumPhaseResult.SKIPPED)
    core: EnumPhaseResult = Field(default=EnumPhaseResult.SKIPPED)
    runtime: EnumPhaseResult = Field(default=EnumPhaseResult.SKIPPED)
    verification: EnumPhaseResult = Field(default=EnumPhaseResult.SKIPPED)
    publish: EnumPhaseResult = Field(default=EnumPhaseResult.PENDING)


class ModelDeployRebuildCompleted(BaseModel):
    """Completion event from the deploy agent.

    Received from: onex.evt.deploy.rebuild-completed.v1
    """

    model_config = ConfigDict(frozen=True, extra="ignore", from_attributes=True)

    correlation_id: str = Field(
        ..., description="Must match the command correlation_id."
    )
    requested_git_ref: str = Field(default="", description="Echo of git_ref input.")
    git_sha: str = Field(default="", description="Git SHA after pull.")
    started_at: datetime | None = Field(default=None, description="Rebuild start time.")
    completed_at: datetime | None = Field(default=None, description="Rebuild end time.")
    duration_seconds: float = Field(default=0.0, description="Total duration.")
    scope: str = Field(default="full", description="Scope that was rebuilt.")
    runtime_lane: EnumRuntimeLane | None = Field(
        default=None,
        description="Lane reported by the deploy agent.",
    )
    image_ref: str | None = Field(
        default=None, description="Image ref reported by the agent."
    )
    image_digest: str | None = Field(
        default=None,
        description="Image digest reported by the agent.",
    )
    services_restarted: list[str] = Field(
        default_factory=list,
        description="Services that were restarted.",
    )
    phase_results: ModelDeployPhaseResults = Field(
        default_factory=ModelDeployPhaseResults,
        description="Per-phase outcomes.",
    )
    status: EnumRedeployStatus = Field(
        ...,
        description="success | failed",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Per-phase error messages. Empty on success.",
    )
    health_checks: list[ModelHealthCheck] = Field(
        default_factory=list,
        description="Service health check results.",
    )


class ModelRedeployResult(BaseModel):
    """Structured result returned by the deploy publish-monitor effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., description="The correlation ID used.")
    success: bool = Field(..., description="True if deploy agent reported success.")
    status: EnumRedeployStatus = Field(..., description="success | failed")
    duration_seconds: float = Field(default=0.0, description="Total wall-clock time.")
    git_sha: str = Field(default="", description="Git SHA deployed.")
    runtime_lane: EnumRuntimeLane | None = Field(
        default=None,
        description="Lane reported by the deploy agent.",
    )
    image_ref: str | None = Field(
        default=None, description="Image ref reported by the agent."
    )
    image_digest: str | None = Field(
        default=None,
        description="Image digest reported by the agent.",
    )
    services_restarted: list[str] = Field(
        default_factory=list,
        description="Services restarted by the deploy agent.",
    )
    phase_results: dict[str, str] = Field(
        default_factory=dict,
        description="Phase name -> result string.",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Errors from deploy agent.",
    )
    timed_out: bool = Field(
        default=False,
        description="True if polling timed out before completion event arrived.",
    )
    health_checks: list[ModelHealthCheck] = Field(
        default_factory=list,
        description="Service health check results carried through from the agent.",
    )


# ---------------------------------------------------------------------------
# Cross-node command models (orchestrator builds; compute/effect consume)
# ---------------------------------------------------------------------------


class ModelProdPromotionGateCommand(BaseModel):
    """Deterministic facts the prod promotion gate consults for one request.

    For non-prod lanes the gate is a no-op (allowed); the compute node decides
    based on ``runtime_lane``. For prod the full promotion-fact set is evaluated:
    reducer-owned readiness projection, OCC evidence state, and rollback target.

    The redeploy ORCHESTRATOR builds this command and the prod-promotion-gate
    COMPUTE node consumes it, so it lives in this shared owner module (not inside
    either node's package).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Redeploy run correlation ID.")
    runtime_lane: EnumRuntimeLane = Field(
        default=EnumRuntimeLane.DEV,
        description="Target runtime lane the gate evaluates.",
    )
    requested_image_digest: str | None = Field(
        default=None,
        description="Digest the request wants to deploy (must equal stability READY).",
    )
    promotion_batch_id: str | None = Field(
        default=None,
        description="Promotion batch the request belongs to.",
    )
    readiness_projection: ModelReadinessProjectionFact | None = Field(
        default=None,
        description="Reducer-owned stability readiness projection; None means absent.",
    )
    occ_gate_state: EnumOccGateState = Field(
        default=EnumOccGateState.PENDING,
        description="Whether OCC evidence is merged / Receipt-Gate-PASS.",
    )
    rollback_target: str | None = Field(
        default=None,
        description="Known previous-good digest restored on failed post-deploy health.",
    )
    previous_image: str | None = Field(
        default=None,
        description="Previous known-good image used as the rollback target fallback.",
    )
    requested_by: str = Field(
        default="node_redeploy_orchestrator",
        description="Identity that requested the promotion; gated against the grant approver.",
    )
    promotion_grant: ModelProdPromotionGrant | None = Field(
        default=None,
        description=(
            "Approver-issued authorization grant resolved out-of-band (Phase-2b "
            "resolver). None means the prod gate fails closed."
        ),
    )
    evaluated_at: datetime | None = Field(
        default=None,
        description=(
            "Deterministic evaluation timestamp stamped by the orchestrator/"
            "runtime; threaded into the gate so the compute never calls "
            "datetime.now(). Required for the prod grant expiry check."
        ),
    )


class ModelDeployPublishCommand(BaseModel):
    """Inputs for one deploy publish-monitor (+rollback) effect invocation.

    The redeploy ORCHESTRATOR builds this command and the deploy publish-monitor
    EFFECT consumes it, so it lives in this shared owner module.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Redeploy run correlation ID.")
    scope: EnumRedeployScope = Field(
        default=EnumRedeployScope.FULL, description="Rebuild scope."
    )
    git_ref: str = Field(
        default="origin/main", description="Git ref the deploy agent pulls."
    )
    runtime_lane: EnumRuntimeLane = Field(
        default=EnumRuntimeLane.DEV, description="Target runtime lane."
    )
    promotion_batch_id: str | None = Field(
        default=None,
        description=(
            "Promotion batch the deploy belongs to. For prod the carried grant's "
            "approved_promotion_batch_id must match this (target binding, OMN-13440)."
        ),
    )
    promotion_grant: ModelProdPromotionGrant | None = Field(
        default=None,
        description=(
            "Verified prod-promotion grant the orchestrator resolved out-of-band and "
            "threaded through the gate. For prod the deploy EFFECT refuses to publish "
            "the rebuild command unless this grant is target-bound to (lane, digest, "
            "batch); None means the prod deploy is refused (fail closed, OMN-13440)."
        ),
    )
    evaluated_at: datetime | None = Field(
        default=None,
        description=(
            "Deterministic evaluation timestamp threaded by the orchestrator; the "
            "deploy EFFECT compares it against the grant's absolute expiry. Required "
            "(non-None) for a prod deploy to be authorized."
        ),
    )
    build_source: EnumBuildSource = Field(
        default=EnumBuildSource.RELEASE, description="Artifact source for the agent."
    )
    services: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Optional service filter. Empty = scope default.",
    )
    image_ref: str | None = Field(default=None, description="Mutable image reference.")
    image_digest: str | None = Field(
        default=None, description="Pinned image digest. Required for prod."
    )
    requested_by: str = Field(
        default="node_redeploy_orchestrator",
        description="Identity label emitted in the command.",
    )
    smoke_test: bool = Field(
        default=False,
        description=(
            "Run a post-deploy smoke probe. When the deploy reports success but "
            "no live runtime proof is available, the smoke probe fails closed and "
            "triggers rollback (OMN-9579)."
        ),
    )
    rollback_target: str = Field(
        default=DEFAULT_PREVIOUS_IMAGE,
        description="Previous known-good image to restore on rollback.",
    )


# ---------------------------------------------------------------------------
# Prod-promotion grant resolution (Phase 2b resolver EFFECT, OMN-13439)
# ---------------------------------------------------------------------------


# Canonical durable anchor the resolver reads the grant from. The grant is
# fetched from onex_change_control@main (NOT the PR branch) so a redeploy request
# cannot author the authorization that approves it (anti-self-approval, OMN-10971;
# mirrors reject-deploy-gate-skip.yml's `?ref=main` fetch). The file path inside
# that repo is fixed by the OMN-13437 schema.
GRANT_REPO = "OmniNode-ai/onex_change_control"
GRANT_FILE_PATH = "grants/prod_promotion_grants.yaml"
GRANT_FETCH_REF = "main"


class EnumGrantResolution(StrEnum):
    """Outcome of resolving a prod-promotion grant from the durable anchor.

    Only ``RESOLVED`` materializes a grant into the gate command. Every other
    outcome leaves the grant ``None`` so the prod gate fails closed — there is no
    silent default and no replay of a consumed grant.
    """

    RESOLVED = "resolved"
    ABSENT = "absent"
    EXPIRED = "expired"
    CONSUMED = "consumed"
    SELF_GRANTED = "self_granted"


class ModelProdPromotionGrantResolveCommand(BaseModel):
    """Command the orchestrator emits to the grant resolver EFFECT.

    Carries the request key ``(promotion_batch_id, image_digest, lane=prod)`` plus
    the requester identity and the deterministic ``evaluated_at`` the resolver
    stamps so the gate compute never calls ``datetime.now()``. The resolver reads
    the grant from ``onex_change_control@main`` — the command does NOT carry a
    caller-supplied grant (a request cannot author its own authorization).

    The redeploy ORCHESTRATOR builds this command and the grant resolver EFFECT
    consumes it, so it lives in this shared owner module.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Redeploy run correlation ID.")
    runtime_lane: Literal[EnumRuntimeLane.PROD] = Field(
        default=EnumRuntimeLane.PROD,
        description="Lane the grant must authorize; resolver keys on lane=prod.",
    )
    requested_image_digest: str | None = Field(
        default=None,
        description="Digest the request wants to promote; the grant must match it.",
    )
    promotion_batch_id: str | None = Field(
        default=None,
        description="Promotion batch the request belongs to; the grant must match it.",
    )
    requested_by: str = Field(
        default="node_redeploy_orchestrator",
        description="Requester identity; a grant whose approver equals it is rejected.",
    )
    evaluated_at: datetime = Field(
        ...,
        description=(
            "Deterministic evaluation timestamp stamped by the orchestrator; the "
            "resolver compares it against the grant's absolute expiry and threads "
            "it into the gate command so the compute never calls datetime.now()."
        ),
    )


class ModelGrantProvenance(BaseModel):
    """Durable audit provenance for one grant-resolution attempt (OMN-13439).

    Emitted on the resolver EFFECT's audit-evidence event — NEVER on the pure
    ``ModelProdPromotionGrant`` DTO (which is approver-authored truth, not
    resolver-observed metadata). Records exactly which durable bytes the resolver
    read so a promotion decision is reproducible from the anchor: the
    ``onex_change_control@main`` source commit, the grant file path, the matched
    ``grant_id``, the sha256 of the fetched file content, and whether the grant
    file is CODEOWNERS-protected (the un-forgeable trust property).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_commit_sha: str = Field(
        ...,
        min_length=1,
        description="onex_change_control@main commit the grant file was read at.",
    )
    grant_file_path: str = Field(
        default=GRANT_FILE_PATH,
        min_length=1,
        description="Path of the grant file inside onex_change_control.",
    )
    grant_id: str | None = Field(
        default=None,
        description="grant_id of the matched entry; None when no entry matched.",
    )
    file_sha256: str = Field(
        ...,
        min_length=64,
        max_length=64,
        description="sha256 hex of the exact grant-file bytes the resolver parsed.",
    )
    codeowners_match: bool = Field(
        ...,
        description=(
            "Whether the grant file is CODEOWNERS-protected on the source ref "
            "(the un-forgeable trust property; the dedicated platform-leads rule)."
        ),
    )


class ModelProdPromotionGrantResolvedEvent(BaseModel):
    """The resolver EFFECT's emitted fact: resolved grant (or None) + provenance.

    The orchestrator consumes this to stamp ``promotion_grant`` + ``evaluated_at``
    onto the gate command. ``grant`` is ``None`` for every non-``RESOLVED``
    outcome so the prod gate fails closed; ``resolution`` carries the typed
    reason. ``provenance`` is always populated — even when no grant matched — so
    the audit trail records what durable bytes were inspected.

    The grant resolver EFFECT emits this and the redeploy ORCHESTRATOR consumes
    it, so it lives in this shared owner module.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Redeploy run correlation ID.")
    resolution: EnumGrantResolution = Field(
        ..., description="Typed resolution outcome (only RESOLVED yields a grant)."
    )
    grant: ModelProdPromotionGrant | None = Field(
        default=None,
        description="Materialized grant when RESOLVED; None otherwise (fail closed).",
    )
    evaluated_at: datetime = Field(
        ...,
        description="Deterministic evaluation timestamp threaded into the gate.",
    )
    provenance: ModelGrantProvenance = Field(
        ...,
        description="Durable audit provenance of the bytes the resolver inspected.",
    )


__all__ = [
    "DEFAULT_PREVIOUS_IMAGE",
    "GRANT_FETCH_REF",
    "GRANT_FILE_PATH",
    "GRANT_REPO",
    "ROLLBACK_ELIGIBLE_PHASES",
    "TERMINAL_PHASES",
    "EnumBuildSource",
    "EnumGrantResolution",
    "EnumOccGateState",
    "EnumPhaseResult",
    "EnumProdGrantReason",
    "EnumRedeployPhase",
    "EnumRedeployScope",
    "EnumRedeployStatus",
    "EnumRuntimeLane",
    "ModelDeployPhaseResults",
    "ModelDeployPublishCommand",
    "ModelDeployRebuildCommand",
    "ModelDeployRebuildCompleted",
    "ModelDeployRefusedEvent",
    "ModelGrantProvenance",
    "ModelHealthCheck",
    "ModelLaneDeployTarget",
    "ModelProdGateDecision",
    "ModelProdPromotionGateCommand",
    "ModelProdPromotionGateDecision",
    "ModelProdPromotionGrant",
    "ModelProdPromotionGrantResolveCommand",
    "ModelProdPromotionGrantResolvedEvent",
    "ModelProdPromotionInputs",
    "ModelReadinessProjectionFact",
    "ModelRedeployCommand",
    "ModelRedeployCompletedEvent",
    "ModelRedeployPhaseEvent",
    "ModelRedeployResult",
    "ModelRedeployRolledBackEvent",
    "ModelRedeployState",
    "ModelRuntimeDeploymentProof",
    "ModelStabilityReadiness",
    "RuntimeLaneLike",
    "evaluate_prod_digest_gate",
    "evaluate_prod_promotion_gate",
    "lane_target",
    "next_phase",
    "next_verification_phase",
    "verify_prod_deploy_grant_binding",
]
