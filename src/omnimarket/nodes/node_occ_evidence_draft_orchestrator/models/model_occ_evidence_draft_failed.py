"""Failure event for the OCC evidence draft orchestrator (OMN-12580, Phase 5).

Published on ``onex.evt.omnimarket.occ-evidence-draft-failed.v1`` when the local
model delegation fails or returns content that cannot be parsed into the required
OCC draft artifacts. This is a delegation/parse failure, distinct from a draft
that parses but fails deterministic validation (which the validator rejects on
``onex.evt.omnimarket.occ-evidence-draft-rejected.v1``).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumOccDraftFailureReason(StrEnum):
    """Why local-model OCC drafting failed."""

    DELEGATION_ERROR = "delegation_error"
    EMPTY_RESPONSE = "empty_response"
    UNPARSEABLE_RESPONSE = "unparseable_response"
    MISSING_ARTIFACTS = "missing_artifacts"


class ModelOccEvidenceDraftFailed(BaseModel):
    """Terminal failure event for an OCC draft delegation attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Deployment run correlation ID.")
    deployment_id: UUID = Field(
        ..., description="Stable deployment attempt identifier."
    )
    ticket_id: str = Field(
        ..., min_length=1, description="Ticket the OCC evidence covers."
    )
    failure_reason: EnumOccDraftFailureReason = Field(
        ..., description="Classified failure reason."
    )
    detail: str = Field(..., min_length=1, description="Human-readable failure detail.")
    model_identity: str = Field(
        ..., min_length=1, description="Identity of the model that was delegated to."
    )
    failed_at: datetime = Field(..., description="When the failure was recorded.")


__all__: list[str] = ["EnumOccDraftFailureReason", "ModelOccEvidenceDraftFailed"]
