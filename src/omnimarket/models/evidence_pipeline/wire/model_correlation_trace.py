# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT


"""ModelCorrelationTrace — ingest-sequence-ordered dashboard trace."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.evidence_pipeline.wire.types import (
    DashboardStage,
    DashboardStatus,
    EvidenceLifecycleState,
)


class ModelCorrelationTraceEvent(BaseModel):
    """Single event in a correlation trace, ordered by ingest sequence."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    event_id: str = Field(..., min_length=1)
    projection_cursor: str = Field(..., min_length=1)
    ingest_sequence: int = Field(..., ge=0)
    topic: str = Field(..., min_length=1)
    stage: DashboardStage
    status: DashboardStatus
    timestamp: str = Field(..., min_length=1)
    evidence_lifecycle_state: EvidenceLifecycleState
    latency_ms_since_previous: int | None = Field(default=None, ge=0)
    payload_summary: Mapping[str, str] = Field(default_factory=dict)


class ModelCorrelationTrace(BaseModel):
    """Full dashboard event chain for a correlation_id."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    correlation_id: str = Field(..., min_length=1)
    events: tuple[ModelCorrelationTraceEvent, ...] = Field(default_factory=tuple)
    ordered_by: Literal["ingest_sequence"] = "ingest_sequence"
    total_latency_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _events_follow_ingest_sequence(self) -> Self:
        sequences = [event.ingest_sequence for event in self.events]
        if sequences != sorted(sequences):
            raise ValueError("events must be ordered by ingest_sequence")
        return self


__all__: list[str] = ["ModelCorrelationTrace", "ModelCorrelationTraceEvent"]
