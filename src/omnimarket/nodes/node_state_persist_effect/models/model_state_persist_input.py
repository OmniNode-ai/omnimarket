# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Input model for node_state_persist_effect.

Carries the fields from ModelPersistStateIntent in a flat, handler-ready form.
The handler reconstructs ModelStateEnvelope from the nested ``envelope`` dict.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from omnibase_core.models.state.model_state_envelope import ModelStateEnvelope
from pydantic import BaseModel, ConfigDict, Field


class ModelStatePersistInput(BaseModel):
    """Input received by HandlerStatePersistEffect.

    Mirrors the fields of ModelPersistStateIntent so the handler can be called
    directly in tests without going through the full event bus pipeline.

    Attributes:
        correlation_id: Distributed tracing ID from the originating request.
        intent_id: Unique ID for this specific persist request.
        envelope: The state snapshot to write to ProtocolStateStore.
        emitted_at: Timezone-aware timestamp from the emitting reducer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: UUID = Field(..., description="Correlation ID for tracing.")
    intent_id: UUID = Field(..., description="Unique ID for this persist request.")
    envelope: ModelStateEnvelope = Field(..., description="State snapshot to persist.")
    emitted_at: datetime = Field(
        ..., description="Timezone-aware timestamp from the emitting reducer."
    )
