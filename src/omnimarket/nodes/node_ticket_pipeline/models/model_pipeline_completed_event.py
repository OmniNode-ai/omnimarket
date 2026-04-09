# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Completed event for the ticket pipeline FSM."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_state import (
    EnumPipelinePhase,
)


class ModelPipelineCompletedEvent(BaseModel):
    """Emitted when the pipeline reaches a terminal phase."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Pipeline run correlation ID.")
    ticket_id: str = Field(..., description="Linear ticket ID.")
    final_phase: EnumPipelinePhase = Field(...)
    started_at: datetime = Field(...)
    completed_at: datetime = Field(...)
    pr_number: int | None = Field(default=None)
    error_message: str | None = Field(default=None)


__all__: list[str] = ["ModelPipelineCompletedEvent"]
