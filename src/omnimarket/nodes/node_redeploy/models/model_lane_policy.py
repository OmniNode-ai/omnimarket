# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane policy and digest pinning for node_redeploy (OMN-12577).

Pure, deterministic policy for the three live runtime lanes (verified on
``.201`` 2026-06-01):

  - dev:            ports 8085/8086,   project ``omnibase-infra``
  - stability-test: ports 18085/18086, project ``omnibase-infra-stability-test``
  - prod:           ports 28085/28086, project ``omnibase-infra-prod``

It also owns the production same-digest gate: prod may not enter REBUILD/deploy
unless a ``stability-test`` readiness event exists for the SAME image digest, and
prod reuses that digest (no rebuild). Failed stability readiness blocks prod.

This module is import-only logic — no I/O, no subprocess, no Docker. The deploy
actuation stays behind ``handler_redeploy_kafka`` and the deploy agent.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_redeploy.models.model_redeploy_command import (
    EnumRuntimeLane,
)


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
    """Result of the production deploy eligibility gate."""

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


__all__: list[str] = [
    "ModelLaneDeployTarget",
    "ModelProdGateDecision",
    "ModelStabilityReadiness",
    "evaluate_prod_digest_gate",
    "lane_target",
]
