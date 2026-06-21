# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Start command for the redeploy orchestrator.

The orchestrator's bus entrypoint. Carries the deploy request plus the
deterministic prod-promotion facts the gate consults, so the orchestrator can
emit the gate command without rehydrating state. Shared deploy domain types live
in ``omnimarket.events.runtime_deployment``.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.runtime_deployment import (
    DEFAULT_PREVIOUS_IMAGE,
    EnumBuildSource,
    EnumOccGateState,
    EnumRedeployScope,
    EnumRuntimeLane,
    ModelProdPromotionGrant,
    ModelReadinessProjectionFact,
)


class ModelRedeployStartCommand(BaseModel):
    """Start a post-release runtime redeploy via the orchestrator.

    Optional lane/digest/promotion fields keep dev-lane triggers valid; prod pins
    ``image_digest`` and ``promotion_batch_id`` to the stability-proven artifact.
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
    promotion_batch_id: str | None = Field(
        default=None,
        description="Promotion batch shared with OCC evidence; required for prod.",
    )
    requested_by: str = Field(
        default="node_redeploy_orchestrator",
        description="Identity label emitted in the command.",
    )
    smoke_test: bool = Field(
        default=False,
        description="Run a post-deploy smoke probe (fails closed -> rollback).",
    )
    previous_image: str = Field(
        default=DEFAULT_PREVIOUS_IMAGE,
        description="Previous known-good image to restore on rollback.",
    )
    # Prod promotion facts (Phase 6) consulted by the prod gate before any deploy.
    readiness_projection: ModelReadinessProjectionFact | None = Field(
        default=None,
        description="Reducer-owned stability readiness projection for a prod request.",
    )
    occ_gate_state: EnumOccGateState = Field(
        default=EnumOccGateState.PENDING,
        description="Whether OCC evidence is merged / Receipt-Gate-PASS for prod.",
    )
    rollback_target: str | None = Field(
        default=None,
        description="Known previous-good digest for the prod rollback path.",
    )
    promotion_grant: ModelProdPromotionGrant | None = Field(
        default=None,
        description=(
            "Caller-attached grant, if any. The orchestrator DROPS this: a "
            "promotion grant is resolved out-of-band (Phase-2b resolver), never "
            "authored by the request it authorizes. Present here only so an "
            "attempted self-grant is observably discarded."
        ),
    )


__all__: list[str] = ["ModelRedeployStartCommand"]
