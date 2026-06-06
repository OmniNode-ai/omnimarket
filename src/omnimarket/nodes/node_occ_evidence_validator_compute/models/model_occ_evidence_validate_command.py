"""Command consumed by node_occ_evidence_validator_compute (OMN-12580, Phase 5).

The validator is a pure deterministic compute node. It receives a model-drafted
``ModelOccEvidenceDraft`` (always PROVISIONAL) together with the authoritative
deployment proof and the expected promotion pins, and decides — without any I/O —
whether the draft is acceptable.

The command carries every value the validation checks compare against so the
validator never reads external state:

- ``draft`` — the provisional OCC draft to validate.
- ``runtime_deployment_proof`` — the authoritative per-lane deployment proof
  (source SHA, image digest, topology freshness, runtime addresses).
- ``expected_repository`` / ``expected_source_sha`` / ``expected_image_digest`` —
  the authoritative pins the draft must match.
- ``required_receipt_commands`` — receipt commands the draft must include.
- ``allowed_receipt_commands`` — policy allow-list; every required command must be
  a member.
- ``receipt_gate_fixture_passes`` — whether the local Receipt Gate fixture passed
  (collected by an upstream effect, not by this pure node).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.occ_evidence import (
    EnumRuntimeLane,
    ModelOccEvidenceDraft,
    ModelRuntimeDeploymentProof,
)


class ModelOccEvidenceValidateCommand(BaseModel):
    """Deterministic validation command for a provisional OCC evidence draft."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Deployment run correlation ID.")
    deployment_id: UUID = Field(
        ..., description="Stable deployment attempt identifier."
    )
    draft: ModelOccEvidenceDraft = Field(
        ..., description="Provisional OCC draft to validate."
    )
    runtime_deployment_proof: ModelRuntimeDeploymentProof = Field(
        ..., description="Authoritative per-lane deployment proof."
    )
    runtime_lane: EnumRuntimeLane = Field(
        ..., description="Lane the draft and proof target."
    )
    expected_repository: str = Field(
        ..., min_length=1, description="Repository the OCC evidence must declare."
    )
    expected_source_sha: str = Field(
        ..., min_length=1, description="Authoritative source SHA the draft must match."
    )
    expected_image_digest: str = Field(
        ...,
        min_length=1,
        description="Authoritative image digest the draft must match.",
    )
    validated_at: datetime = Field(
        ..., description="ISO-8601 validation timestamp injected by the caller."
    )
    expected_promotion_batch_id: str | None = Field(
        default=None, description="Promotion batch the draft must match, if any."
    )
    required_receipt_commands: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Receipt commands the draft must include.",
    )
    allowed_receipt_commands: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Policy allow-list of receipt commands.",
    )
    topology_freshness_max_age_seconds: int = Field(
        default=3600,
        gt=0,
        description="Maximum age of the topology manifest proof before it is stale.",
    )
    receipt_gate_fixture_passes: bool = Field(
        default=True,
        description="Whether the upstream local Receipt Gate fixture passed.",
    )


__all__: list[str] = ["ModelOccEvidenceValidateCommand"]
