# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT


"""ModelCorrelationTraceProjection — reducer-owned trace projection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.evidence_pipeline.wire.model_correlation_trace import (
    ModelCorrelationTraceEvent,
)
from omnimarket.models.evidence_pipeline.wire.types import (
    DashboardStage,
    FreshnessState,
    MissingEventClassification,
)


class ModelCorrelationTraceProjection(BaseModel):
    """Materialized event chain for one correlation_id."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    correlation_id: str = Field(..., min_length=1)
    projection_cursor: str = Field(..., min_length=1)
    source_event_ids: tuple[str, ...] = Field(default_factory=tuple)
    last_event_id: str = Field(..., min_length=1)
    last_ingest_sequence: int = Field(..., ge=0)
    freshness_state: FreshnessState
    degraded_reason: str | None = Field(default=None, min_length=1)
    observed_at: str = Field(..., min_length=1)
    version: str = Field(default="1.0.0", min_length=1)
    events: tuple[ModelCorrelationTraceEvent, ...] = Field(default_factory=tuple)
    missing_event_classifications: Mapping[
        DashboardStage, MissingEventClassification
    ] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _projection_events_follow_ingest_sequence(self) -> Self:
        sequences = [event.ingest_sequence for event in self.events]
        if sequences != sorted(sequences):
            raise ValueError("events must be ordered by ingest_sequence")
        if sequences and self.last_ingest_sequence < sequences[-1]:
            raise ValueError("last_ingest_sequence must cover the last projected event")
        if self.freshness_state == "DEGRADED" and self.degraded_reason is None:
            raise ValueError(
                "degraded_reason is required when freshness_state is DEGRADED"
            )
        return self


__all__: list[str] = ["ModelCorrelationTraceProjection"]
