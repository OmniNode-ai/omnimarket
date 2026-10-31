# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT


"""ModelEvidenceDashboardProjection — reducer-owned pipeline stage projection."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.evidence_pipeline.wire.types import (
    DashboardStage,
    DashboardStatus,
    FreshnessState,
)


class ModelEvidenceDashboardStageProjection(BaseModel):
    """Materialized dashboard state for one evidence pipeline stage."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    stage: DashboardStage
    status: DashboardStatus
    event_count: int = Field(..., ge=0)
    stale_event_count: int = Field(default=0, ge=0)
    blocked_event_count: int = Field(default=0, ge=0)
    freshness_state: FreshnessState
    last_projection_update_at: str = Field(..., min_length=1)
    correlation_ids: tuple[str, ...] = Field(default_factory=tuple)


class ModelEvidenceDashboardProjection(BaseModel):
    """Aggregate pipeline stage counts and freshness state."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    projection_cursor: str = Field(..., min_length=1)
    last_event_id: str = Field(..., min_length=1)
    last_ingest_sequence: int = Field(..., ge=0)
    freshness_state: FreshnessState
    degraded_reason: str | None = Field(default=None, min_length=1)
    observed_at: str = Field(..., min_length=1)
    version: str = Field(default="1.0.0", min_length=1)
    stages: tuple[ModelEvidenceDashboardStageProjection, ...] = Field(
        default_factory=tuple
    )

    @model_validator(mode="after")
    def _degraded_projection_has_reason(self) -> Self:
        if self.freshness_state == "DEGRADED" and self.degraded_reason is None:
            raise ValueError(
                "degraded_reason is required when freshness_state is DEGRADED"
            )
        return self


__all__: list[str] = [
    "ModelEvidenceDashboardProjection",
    "ModelEvidenceDashboardStageProjection",
]
