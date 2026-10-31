# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT


"""ModelReadinessAggregateProjection — reducer-owned deployment readiness projection."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from omnimarket.models.evidence_pipeline.wire.types import (
    DashboardStatus,
    FreshnessState,
    GapClassification,
    ReadinessState,
)


class ModelReadinessAggregateProjection(BaseModel):
    """Deployment readiness state kept separate from evidence pipeline completion."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    deployment_id: str = Field(..., min_length=1)
    projection_cursor: str = Field(..., min_length=1)
    last_event_id: str = Field(..., min_length=1)
    last_ingest_sequence: int = Field(..., ge=0)
    freshness_state: FreshnessState
    degraded_reason: str | None = Field(default=None, min_length=1)
    observed_at: str = Field(..., min_length=1)
    version: str = Field(default="1.0.0", min_length=1)
    readiness_state: ReadinessState
    evidence_pipeline_state: DashboardStatus
    gap_breakdown: Mapping[GapClassification, int] = Field(default_factory=dict)
    blocking_reason_codes: tuple[str, ...] = Field(default_factory=tuple)
    correlation_ids: tuple[str, ...] = Field(default_factory=tuple)
    ticket_ids: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _freeze_gap_breakdown_mapping(self) -> Self:
        object.__setattr__(
            self, "gap_breakdown", MappingProxyType(dict(self.gap_breakdown))
        )
        return self

    @field_serializer("gap_breakdown")
    def _serialize_gap_breakdown(
        self,
        value: Mapping[GapClassification, int],
    ) -> dict[GapClassification, int]:
        return dict(value)

    @model_validator(mode="after")
    def _projection_metadata_is_consistent(self) -> Self:
        if self.freshness_state == "DEGRADED" and self.degraded_reason is None:
            raise ValueError(
                "degraded_reason is required when freshness_state is DEGRADED"
            )
        invalid_gap_counts = [
            classification
            for classification, count in self.gap_breakdown.items()
            if count < 0
        ]
        if invalid_gap_counts:
            raise ValueError("gap_breakdown counts must be non-negative")
        return self


__all__: list[str] = ["ModelReadinessAggregateProjection"]
