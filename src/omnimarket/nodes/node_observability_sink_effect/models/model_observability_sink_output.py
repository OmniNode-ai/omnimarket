# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelObservabilitySinkOutput — persistence acknowledgement for the observability sink effect."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelObservabilitySinkOutput(BaseModel):
    """Persistence acknowledgement returned after sinking observability events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Correlation ID echoed from the input."
    )
    session_id: UUID = Field(..., description="Session ID echoed from the input.")
    persisted_event_count: int = Field(
        ..., ge=0, description="Number of events successfully persisted."
    )
    kafka_trace_ids: tuple[str, ...] = Field(
        default=(),
        description="Kafka message IDs (offset references) for published events.",
    )
    postgres_row_ids: tuple[UUID, ...] = Field(
        default=(),
        description="Primary-key UUIDs of rows inserted into agent_actions.",
    )
    persisted_at: datetime = Field(
        ..., description="Timestamp when persistence completed."
    )
    error: str = Field(
        default="",
        description="Non-empty when persistence partially or fully failed.",
    )


__all__: list[str] = ["ModelObservabilitySinkOutput"]
