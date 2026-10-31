# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT


"""ModelDashboardEvent — normalized projection-write dashboard event."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from omnimarket.models.evidence_pipeline.wire.types import (
    DashboardStage,
    EvidenceLifecycleState,
)


class ModelDashboardEvent(BaseModel):
    """Normalized event persisted into reducer-owned dashboard projections."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    event_id: str = Field(..., min_length=1)
    causation_id: str | None = Field(default=None, min_length=1)
    source_event_hash: str = Field(..., min_length=1)
    projection_cursor: str = Field(..., min_length=1)
    ingest_sequence: int = Field(..., ge=0)
    correlation_id: str = Field(..., min_length=1)
    ticket_id: str | None = Field(default=None, min_length=1)
    topic: str = Field(..., min_length=1)
    stage: DashboardStage
    timestamp: str = Field(..., min_length=1)
    payload_summary: Mapping[str, str] = Field(default_factory=dict)
    evidence_lifecycle_state: EvidenceLifecycleState

    @model_validator(mode="after")
    def _freeze_payload_summary_mapping(self) -> ModelDashboardEvent:
        object.__setattr__(
            self,
            "payload_summary",
            MappingProxyType(dict(self.payload_summary)),
        )
        return self

    @field_serializer("payload_summary")
    def _serialize_payload_summary(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)


__all__: list[str] = ["ModelDashboardEvent"]
