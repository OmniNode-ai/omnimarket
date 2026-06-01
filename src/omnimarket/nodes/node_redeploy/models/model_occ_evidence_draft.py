"""OCC evidence draft models for node_redeploy (OMN-12576).

The OCC creation path is deliberately split into model generation and
deterministic acceptance. A local model drafts the OCC contract/receipt/PR body
(``ModelOccEvidenceDraft``, always PROVISIONAL); a deterministic validator
decides acceptance.

Blocker B4: the PASS path must publish the EXISTING
``omnibase_compat ... ModelEvidenceValidationResult`` on
``onex.evt.omnimarket.evidence-validated.v1`` so the existing OCC PR writer
fires. ``ModelOccEvidenceDraftValidationResult`` here is the INTERNAL
reject/audit shape ONLY — it is never published on ``evidence-validated.v1``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_redeploy.models.model_redeploy_command import (
    EnumRuntimeLane,
)
from omnimarket.nodes.node_redeploy.models.model_runtime_deployment import (
    ModelRuntimeDeploymentProof,
)

type ValidationCheckStatus = Literal["pass", "fail", "skipped"]
type FreshnessStatus = Literal["current", "stale", "degraded"]
type DraftValidationState = Literal["PASSED", "FAILED"]


class EnumEvidenceLifecycleState(StrEnum):
    """Lifecycle state of an OCC evidence draft.

    Values mirror the compat ``EvidenceLifecycleState`` literal. A model-drafted
    OCC artifact is always ``PROVISIONAL`` until the deterministic validator
    accepts it; the model may never mark its own draft authoritative.
    """

    PROVISIONAL = "PROVISIONAL"
    VALIDATED = "VALIDATED"
    FINALIZED = "FINALIZED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


class ModelOccEvidenceDraftRequest(BaseModel):
    """Request for local-model OCC drafting, built by the draft orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Deployment run correlation ID.")
    deployment_id: UUID = Field(
        ..., description="Stable deployment attempt identifier."
    )
    ticket_id: str = Field(
        ..., min_length=1, description="Ticket the OCC evidence covers."
    )
    runtime_lane: EnumRuntimeLane = Field(
        ..., description="Lane the deployment targeted."
    )
    target_occ_repo: str = Field(
        ..., min_length=1, description="Repository the OCC PR will be written into."
    )
    model_profile: str = Field(
        ...,
        min_length=1,
        description="Delegation model profile used to draft the evidence.",
    )
    requested_at: datetime = Field(
        ..., description="When the draft request was issued."
    )
    promotion_batch_id: str | None = Field(
        default=None, description="Promotion batch identifier shared with OCC evidence."
    )
    runtime_deployment_proof: ModelRuntimeDeploymentProof | None = Field(
        default=None, description="Deployment proof the draft must reference."
    )
    required_receipts: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Receipt command names the draft must include.",
    )


class ModelOccEvidenceDraft(BaseModel):
    """Provisional OCC evidence draft produced by a local model.

    ``evidence_lifecycle_state`` is fixed to PROVISIONAL: the draft cannot be
    marked authoritative by the model. Acceptance is decided by the deterministic
    validator, which publishes the existing compat
    ``ModelEvidenceValidationResult`` on the PASS path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Deployment run correlation ID.")
    deployment_id: UUID = Field(
        ..., description="Stable deployment attempt identifier."
    )
    ticket_id: str = Field(
        ..., min_length=1, description="Ticket the OCC evidence covers."
    )
    draft_hash: str = Field(
        ..., min_length=1, description="Content hash of the generated draft."
    )
    contract_yaml: str = Field(
        ..., min_length=1, description="Model-generated OCC contract YAML."
    )
    pr_body: str = Field(..., min_length=1, description="Model-generated OCC PR body.")
    model_identity: str = Field(
        ..., min_length=1, description="Identity of the model that produced the draft."
    )
    generated_at: datetime = Field(..., description="When the draft was generated.")
    receipt_yamls: tuple[str, ...] = Field(
        default_factory=tuple, description="Model-generated DoD receipt YAML documents."
    )
    evidence_lifecycle_state: Literal[EnumEvidenceLifecycleState.PROVISIONAL] = Field(
        default=EnumEvidenceLifecycleState.PROVISIONAL,
        description="Always PROVISIONAL — the model cannot mark its own draft authoritative.",
    )


class ModelOccEvidenceDraftValidationResult(BaseModel):
    """INTERNAL deterministic reject/audit result for an OCC draft.

    This is NOT published on ``onex.evt.omnimarket.evidence-validated.v1`` — the
    PASS path emits the existing compat ``ModelEvidenceValidationResult`` so the
    existing OCC PR writer fires (blocker B4). This model records the per-check
    audit trail used to build that PASS result or to reject the draft.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Deployment run correlation ID.")
    deployment_id: UUID = Field(
        ..., description="Stable deployment attempt identifier."
    )
    ticket_id: str = Field(
        ..., min_length=1, description="Ticket the OCC evidence covers."
    )
    draft_hash: str = Field(
        ..., min_length=1, description="Content hash of the validated draft."
    )
    validation_state: DraftValidationState = Field(
        ..., description="Deterministic verdict for the draft."
    )
    schema_status: ValidationCheckStatus = Field(
        ..., description="OCC YAML schema validity check."
    )
    sha_match_status: ValidationCheckStatus = Field(
        ..., description="Source SHA match check."
    )
    image_digest_match_status: ValidationCheckStatus = Field(
        ..., description="Image digest match check."
    )
    receipt_probe_status: ValidationCheckStatus = Field(
        ..., description="Required receipt commands present and policy-allowed."
    )
    topology_freshness_status: FreshnessStatus = Field(
        ..., description="Topology proof freshness check."
    )
    validated_at: datetime = Field(..., description="ISO-8601 validation timestamp.")
    promotion_batch_id: str | None = Field(
        default=None, description="Promotion batch identifier shared with OCC evidence."
    )
    blocking_reason_codes: tuple[str, ...] = Field(
        default_factory=tuple, description="Reason codes for a FAILED verdict."
    )


__all__: list[str] = [
    "DraftValidationState",
    "EnumEvidenceLifecycleState",
    "FreshnessStatus",
    "ModelOccEvidenceDraft",
    "ModelOccEvidenceDraftRequest",
    "ModelOccEvidenceDraftValidationResult",
    "ValidationCheckStatus",
]
