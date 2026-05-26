# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelObservabilitySinkInput — command payload for the observability sink effect."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelActionEvent(BaseModel):
    """A single agent action event to be persisted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID = Field(..., description="Unique event identifier.")
    agent_name: str = Field(
        ..., description="Name of the agent that emitted the event."
    )
    action_type: str = Field(
        ...,
        description="Event category: tool_call, decision, error, success, routing, detection, transformation, performance.",
    )
    action_name: str = Field(
        ..., description="Specific action name within the category."
    )
    action_details: dict[str, object] = Field(
        default_factory=dict,
        description="Structured action metadata (arbitrary key-value pairs).",
    )
    duration_ms: int = Field(
        default=0, ge=0, description="Action duration in milliseconds."
    )
    emitted_at: datetime = Field(
        ..., description="Timestamp when the event was emitted."
    )


class ModelObservabilitySinkInput(BaseModel):
    """Batch of action events and session context to persist to observability backends."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Correlation ID for the session or run."
    )
    session_id: UUID = Field(..., description="Session identifier.")
    events: tuple[ModelActionEvent, ...] = Field(
        ..., description="Ordered batch of action events to persist."
    )
    sink_kafka: bool = Field(
        default=True, description="Publish events to the Kafka topic."
    )
    sink_postgres: bool = Field(
        default=True,
        description="Persist events to the PostgreSQL agent_actions table.",
    )
    submitted_at: datetime = Field(
        ..., description="Timestamp when the sink command was submitted."
    )


__all__: list[str] = ["ModelActionEvent", "ModelObservabilitySinkInput"]
