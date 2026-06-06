"""Runtime deployment request/result/proof models for node_redeploy (OMN-12576).

These are repo-private models in the node_redeploy model layer. They mirror the
OCC-owned wire schema (source of truth in onex_change_control); the wire DTOs are
also mirrored transiently through omnibase_compat. ``image_digest`` is the
prod-gate authority: production deploys only the digest proven READY in
stability-test, so it is a required field on the build result, deploy result,
and deployment proof.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_redeploy.models.model_redeploy_command import (
    EnumRuntimeLane,
)

type ProbeStatus = Literal["pass", "fail"]
type DeploymentResultStatus = Literal["success", "failed"]


class ModelRuntimeDeploymentRequest(BaseModel):
    """Lane/digest/promotion deployment request consumed by node_redeploy.

    ``image_digest`` and ``promotion_batch_id`` are optional (the dev lane builds
    from a ref); production pins both to the stability-test READY digest before
    publishing the request.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Deployment run correlation ID.")
    deployment_id: UUID = Field(
        ..., description="Stable deployment attempt identifier."
    )
    runtime_lane: EnumRuntimeLane = Field(..., description="Target runtime lane.")
    source_branch: str = Field(
        ..., min_length=1, description="Branch that triggered the deployment."
    )
    source_sha: str = Field(
        ..., min_length=1, description="Exact source commit SHA to deploy."
    )
    requested_by: str = Field(
        ...,
        min_length=1,
        description="Identity of the requesting workflow or operator.",
    )
    requested_at: datetime = Field(..., description="When the request was issued.")
    promotion_batch_id: str | None = Field(
        default=None,
        description="Promotion batch identifier shared with OCC evidence; required for prod.",
    )
    image_ref: str | None = Field(
        default=None,
        description="Mutable image reference. The digest is the authority, not the ref.",
    )
    image_digest: str | None = Field(
        default=None,
        description="Immutable image digest. Required for prod.",
    )
    deployment_reason: str | None = Field(
        default=None, description="Human-readable trigger reason."
    )
    requires_occ: bool = Field(
        default=False, description="Whether OCC evidence drafting is required."
    )
    requires_readiness_gate: bool = Field(
        default=False,
        description="Whether the readiness gate must pass before the lane is READY.",
    )

    @model_validator(mode="after")
    def validate_prod_pins(self) -> ModelRuntimeDeploymentRequest:
        """Production requests must carry the stability-proven artifact pins."""
        if self.runtime_lane is EnumRuntimeLane.PROD and (
            not self.image_digest or not self.promotion_batch_id
        ):
            raise ValueError(
                "prod deployment request requires image_digest and promotion_batch_id"
            )
        return self


class ModelRuntimeBuildResult(BaseModel):
    """Result of building or resolving the immutable runtime image for a lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Deployment run correlation ID.")
    deployment_id: UUID = Field(
        ..., description="Stable deployment attempt identifier."
    )
    runtime_lane: EnumRuntimeLane = Field(..., description="Lane the build targeted.")
    source_sha: str = Field(
        ..., min_length=1, description="Source SHA bound to the build."
    )
    image_digest: str = Field(
        ..., min_length=1, description="Immutable digest of the built/resolved image."
    )
    image_ref: str = Field(..., min_length=1, description="Mutable image reference.")
    build_source: str = Field(
        ...,
        min_length=1,
        description="Source of the artifact (built-from-ref | pulled-digest).",
    )
    build_started_at: datetime = Field(..., description="Build start timestamp.")
    build_completed_at: datetime = Field(..., description="Build completion timestamp.")
    status: DeploymentResultStatus = Field(..., description="Build status.")
    error_message: str | None = Field(
        default=None, description="Build error if failed."
    )


class ModelRuntimeDeployResult(BaseModel):
    """Result of deploying a resolved image digest to a runtime lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Deployment run correlation ID.")
    deployment_id: UUID = Field(
        ..., description="Stable deployment attempt identifier."
    )
    runtime_lane: EnumRuntimeLane = Field(..., description="Lane that was deployed.")
    source_sha: str = Field(
        ..., min_length=1, description="Source SHA bound to the deploy."
    )
    image_digest: str = Field(
        ..., min_length=1, description="Immutable digest of the deployed image."
    )
    compose_project: str = Field(
        ..., min_length=1, description="Compose project that owns the deployed lane."
    )
    compose_files: tuple[str, ...] = Field(
        default_factory=tuple, description="Compose overlay files applied for the lane."
    )
    services_restarted: tuple[str, ...] = Field(
        default_factory=tuple, description="Services restarted by the deploy."
    )
    deploy_started_at: datetime = Field(..., description="Deploy start timestamp.")
    deploy_completed_at: datetime = Field(
        ..., description="Deploy completion timestamp."
    )
    status: DeploymentResultStatus = Field(..., description="Deploy status.")
    error_message: str | None = Field(
        default=None, description="Deploy error if failed."
    )
    rollback_target: str | None = Field(
        default=None, description="Previous known-good digest for rollback, if any."
    )


class ModelRuntimeDeploymentProof(BaseModel):
    """Per-lane deployment proof assembled from probe + runtime attestation.

    ``image_digest`` is the prod-gate authority: production may deploy only the
    digest proven READY in stability-test, so it is a required proof field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Deployment run correlation ID.")
    deployment_id: UUID = Field(
        ..., description="Stable deployment attempt identifier."
    )
    runtime_lane: EnumRuntimeLane = Field(..., description="Lane that was probed.")
    source_sha: str = Field(
        ..., min_length=1, description="Source SHA bound to the deployed artifact."
    )
    image_digest: str = Field(
        ..., min_length=1, description="Digest of the running container image."
    )
    compose_project: str = Field(
        ..., min_length=1, description="Compose project that owns the deployed lane."
    )
    health_status: ProbeStatus = Field(
        ..., description="Per-lane /health probe result."
    )
    ready_status: ProbeStatus = Field(..., description="Per-lane /ready probe result.")
    probed_at: datetime = Field(..., description="When the probe completed.")
    status: DeploymentResultStatus = Field(..., description="Overall proof status.")
    promotion_batch_id: str | None = Field(
        default=None, description="Promotion batch identifier shared with OCC evidence."
    )
    runtime_addresses: tuple[str, ...] = Field(
        default_factory=tuple, description="Probed runtime addresses for the lane."
    )
    topology_manifest_sha256: str | None = Field(
        default=None,
        description="Topology manifest hash from the runtime manifest reducer.",
    )
    package_versions: dict[str, str] = Field(
        default_factory=dict,
        description="Deployed package versions from runtime attestation.",
    )
    runtime_source_hash: str | None = Field(
        default=None,
        description="Runtime source hash from the runtime source attestor.",
    )
    consumer_groups: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Active consumer groups observed on the lane.",
    )
    runtime_sweep_input_ref: str | None = Field(
        default=None,
        description="Reference to the runtime sweep input used for classification.",
    )


__all__: list[str] = [
    "DeploymentResultStatus",
    "ModelRuntimeBuildResult",
    "ModelRuntimeDeployResult",
    "ModelRuntimeDeploymentProof",
    "ModelRuntimeDeploymentRequest",
    "ProbeStatus",
]
