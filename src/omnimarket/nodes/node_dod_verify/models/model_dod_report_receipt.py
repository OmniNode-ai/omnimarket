# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelDodReportReceipt — on-disk receipt written by node_dod_verify.

This model is the provenanced, schema-versioned artifact that the completion-guard
hook reads to validate DoD evidence.  It wraps ModelDodVerifyState fields with
first-class provenance fields so the hook can verify authenticity without trusting
the file's location or creation time alone.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    ModelEvidenceCheckResult,
)


class EnumReceiptGenerator(StrEnum):
    """Allowed values for the generated_by provenance field."""

    NODE_DOD_VERIFY = "node_dod_verify"


class ModelDodReportResult(BaseModel):
    """Summary counts from a DoD verification run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int = Field(..., ge=0)
    verified: int = Field(..., ge=0)
    failed: int = Field(..., ge=0)
    skipped: int = Field(..., ge=0)
    status: EnumDodVerifyStatus


class ModelDodReportReceipt(BaseModel):
    """On-disk receipt written by ReceiptWriter after node_dod_verify runs.

    Fields:
        schema_version: Semver of the receipt format (not the node version).
        timestamp: UTC datetime when the receipt was written.
        ticket_id: Linear ticket ID this receipt covers.
        generated_by: Always "node_dod_verify" — provenance field for hook validation.
        generator_version: Semver of the node that generated this receipt.
        node_correlation_id: UUID identifying this specific verification run.
        contract_path: Absolute path of the contract file used, or None if not found.
        result: Summary counts and overall status.
        checks: Full per-check results.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="1.0.0")
    timestamp: datetime = Field(...)
    ticket_id: str = Field(...)
    generated_by: EnumReceiptGenerator = Field(
        default=EnumReceiptGenerator.NODE_DOD_VERIFY
    )
    generator_version: str = Field(...)
    node_correlation_id: UUID = Field(...)
    contract_path: str | None = Field(default=None)
    result: ModelDodReportResult = Field(...)
    checks: list[ModelEvidenceCheckResult] = Field(default_factory=list)


__all__: list[str] = [
    "EnumReceiptGenerator",
    "ModelDodReportReceipt",
    "ModelDodReportResult",
]
