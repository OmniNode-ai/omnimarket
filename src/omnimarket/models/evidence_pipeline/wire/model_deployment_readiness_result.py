# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT


"""ModelDeploymentReadinessResult — deployment eligibility decision."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.evidence_pipeline.wire.types import ReadinessState


class ModelDeploymentReadinessResult(BaseModel):
    """Machine-readable readiness gate result for a deployment set."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    correlation_id: str = Field(..., min_length=1)
    validation_run_id: str = Field(..., min_length=1)
    deployment_id: str = Field(..., min_length=1)
    readiness_state: ReadinessState
    scored_at: str = Field(..., min_length=1)
    validator_version: str = Field(..., min_length=1)
    gap_report_hash: str = Field(..., min_length=1)
    topology_affecting: bool = False
    blocking_reason_codes: tuple[str, ...] = Field(default_factory=tuple)
    required_evidence_refs: tuple[str, ...] = Field(default_factory=tuple)
    missing_contracts: tuple[str, ...] = Field(default_factory=tuple)
    superseded_artifacts: tuple[str, ...] = Field(default_factory=tuple)
    stale_validation_windows: tuple[str, ...] = Field(default_factory=tuple)
    unresolved_runtime_gaps: tuple[str, ...] = Field(default_factory=tuple)
    topology_metadata: Mapping[str, str] = Field(default_factory=dict)
    deployment_artifact_hashes: Mapping[str, str] = Field(default_factory=dict)
    receipt_refs: tuple[str, ...] = Field(default_factory=tuple)


__all__: list[str] = ["ModelDeploymentReadinessResult"]
