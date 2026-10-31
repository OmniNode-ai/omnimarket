# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT


"""ModelEvidenceValidationResult — per-ticket deterministic validation result."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.evidence_pipeline.wire.types import (
    EvidenceLifecycleState,
    ValidationState,
)


class ModelEvidenceValidationResult(BaseModel):
    """Contract/evidence alignment result produced by the matcher compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    correlation_id: str = Field(..., min_length=1)
    validation_run_id: str = Field(..., min_length=1)
    ticket_id: str = Field(..., min_length=1)
    repository: str = Field(..., min_length=1)
    contract_hash: str = Field(..., min_length=1)
    evidence_bundle_hash: str = Field(..., min_length=1)
    verifier_identity: str = Field(..., min_length=1)
    validator_version: str = Field(..., min_length=1)
    validated_at: str = Field(..., min_length=1)
    validation_state: ValidationState
    evidence_lifecycle_state: EvidenceLifecycleState
    topology_affecting: bool
    requirement_results: Mapping[str, str] = Field(default_factory=dict)
    missing_dod_items: tuple[str, ...] = Field(default_factory=tuple)
    scope_drift_detected: bool = False
    blocking_reason_codes: tuple[str, ...] = Field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple)


__all__: list[str] = ["ModelEvidenceValidationResult"]
