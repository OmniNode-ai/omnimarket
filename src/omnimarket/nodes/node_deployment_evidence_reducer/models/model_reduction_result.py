"""Reduction result model for the deployment evidence reducer."""

from __future__ import annotations

from omnibase_compat.contracts.evidence_pipeline.wire.types import ReadinessState
from pydantic import BaseModel, ConfigDict, Field


class ModelDeploymentEvidenceReductionResult(BaseModel):
    """Summary of reducer-owned projection writes for one evidence event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    deployment_id: str
    correlation_id: str
    validation_run_id: str
    readiness_state: ReadinessState
    rows_upserted: int = Field(default=0, ge=0)
    tables: tuple[str, ...] = Field(default_factory=tuple)
