# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Phase transition event for the ticket pipeline FSM."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_state import (
    EnumPipelinePhase,
)


class ModelPipelinePhaseEvent(BaseModel):
    """Emitted on every phase transition (success or failure)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Pipeline run correlation ID.")
    ticket_id: str = Field(..., description="Linear ticket ID.")
    from_phase: EnumPipelinePhase = Field(...)
    to_phase: EnumPipelinePhase = Field(...)
    success: bool = Field(...)
    timestamp: datetime = Field(...)
    error_message: str | None = Field(default=None)


__all__: list[str] = ["ModelPipelinePhaseEvent"]
