# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT


"""ModelDashboardProjectionEvent — normalized effect-to-reducer dashboard event."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.evidence_pipeline.wire.types import (
    DashboardSeverity,
    DashboardStage,
    DashboardStatus,
    EvidenceLifecycleState,
)


class ModelDashboardProjectionEvent(BaseModel):
    """Canonical normalized event emitted by the dashboard effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    event_id: str = Field(..., min_length=1)
    causation_id: str | None = Field(default=None, min_length=1)
    source_event_type: str = Field(..., min_length=1)
    normalized_stage: DashboardStage
    normalized_status: DashboardStatus
    severity: DashboardSeverity
    lifecycle_state: EvidenceLifecycleState
    source_event_hash: str = Field(..., min_length=1)
    projection_cursor: str = Field(..., min_length=1)
    ingest_sequence: int = Field(..., ge=0)
    correlation_id: str = Field(..., min_length=1)
    ticket_id: str | None = Field(default=None, min_length=1)
    topic: str = Field(..., min_length=1)
    observed_at: str = Field(..., min_length=1)


__all__: list[str] = ["ModelDashboardProjectionEvent"]
