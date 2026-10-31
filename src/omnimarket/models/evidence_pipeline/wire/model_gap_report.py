# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT


"""ModelGapReport — deployment-level evidence gap analysis."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.evidence_pipeline.wire.types import GapClassification


class ModelGapReport(BaseModel):
    """Typed classification of validation gaps across a deployment set."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    correlation_id: str = Field(..., min_length=1)
    validation_run_id: str = Field(..., min_length=1)
    deployment_id: str = Field(..., min_length=1)
    generated_at: str = Field(..., min_length=1)
    validator_version: str = Field(..., min_length=1)
    gap_classifications: Mapping[str, GapClassification] = Field(default_factory=dict)
    validation_result_refs: tuple[str, ...] = Field(default_factory=tuple)
    missing_evidence_refs: tuple[str, ...] = Field(default_factory=tuple)
    stale_evidence_refs: tuple[str, ...] = Field(default_factory=tuple)
    superseded_artifacts: tuple[str, ...] = Field(default_factory=tuple)
    hash_mismatch_refs: tuple[str, ...] = Field(default_factory=tuple)
    receipt_missing_refs: tuple[str, ...] = Field(default_factory=tuple)
    failed_validation_refs: tuple[str, ...] = Field(default_factory=tuple)
    unknown_refs: tuple[str, ...] = Field(default_factory=tuple)


__all__: list[str] = ["ModelGapReport"]
