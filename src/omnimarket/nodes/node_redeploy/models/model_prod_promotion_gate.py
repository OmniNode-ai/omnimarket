# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Production promotion gate for node_redeploy (OMN-12581, Phase 6).

The OMN-12577 same-digest gate (``model_lane_policy.evaluate_prod_digest_gate``)
already proves the *digest* is the one stability proved. Phase 6 makes the prod
*request* depend on the full set of deterministic promotion facts, resolved
BEFORE any deploy-agent invocation:

  1. the reducer-owned readiness projection
     (``node_deployment_evidence_reducer`` → ``deployment_readiness_projection``,
     surfaced as ``ModelReadinessAggregateProjection`` /
     ``ModelDeploymentReadinessResult``) shows the stability-test lane READY for
     the requested promotion batch and image digest;
  2. the OCC evidence PR is merged OR the Receipt Gate has PASS evidence;
  3. a known rollback target (previous good digest) exists.

The exact stability READY ``image_digest`` is enforced: a prod request whose
digest differs from the latest stability-test READY digest is blocked here, with
no deploy effect — the first-class regression guard for the observed live prod
0.36.1 vs stability 0.37.0 drift.

This module is import-only logic — no I/O, no subprocess, no Docker. The deploy
actuation stays behind ``handler_redeploy_kafka`` and the deploy agent.
"""

from __future__ import annotations

from enum import StrEnum

from omnibase_compat.contracts.evidence_pipeline.wire.types import ReadinessState
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_redeploy.models.model_lane_policy import (
    ModelStabilityReadiness,
    evaluate_prod_digest_gate,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_command import (
    EnumRuntimeLane,
)


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


# OCC states that satisfy the prod gate (plan "Prod Deploy Eligibility":
# "OCC PR is merged or Receipt Gate has PASS evidence").
_OCC_SATISFIED: frozenset[EnumOccGateState] = frozenset(
    {EnumOccGateState.MERGED, EnumOccGateState.RECEIPT_GATE_PASS}
)


class ModelReadinessProjectionFact(BaseModel):
    """A stability-test readiness fact read from the reducer-owned projection.

    This is the Phase-6 input that closes the OMN-12577 gap: the prod gate now
    resolves stability readiness from the ``deployment_readiness_projection``
    (reducer-owned truth), not from a stub that always returned ``None``. The
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

    return ModelProdPromotionGateDecision(
        allowed=True,
        image_digest=digest_gate.image_digest,
        rollback_target=inputs.rollback_target,
        reason=(
            "readiness projection READY for matching digest and batch, OCC "
            "evidence durable, rollback target known; prod reuses the "
            "stability digest (no rebuild)"
        ),
    )


__all__: list[str] = [
    "EnumOccGateState",
    "ModelProdPromotionGateDecision",
    "ModelProdPromotionInputs",
    "ModelReadinessProjectionFact",
    "evaluate_prod_promotion_gate",
]
