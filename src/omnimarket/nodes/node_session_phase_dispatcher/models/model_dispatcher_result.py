# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for node_session_phase_dispatcher."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelSessionPhaseEvent(BaseModel):
    """A single published event payload (phase-state or budget-warning)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    event_type: str
    payload: dict[str, object]


class ModelSessionPhaseDispatcherResult(BaseModel):
    """Result of one dispatcher handle() call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    events: tuple[ModelSessionPhaseEvent, ...] = Field(
        default=(), description="Events to publish to the event bus."
    )
    workers_dispatched: int = Field(default=0, ge=0)
    budget_warnings_emitted: int = Field(default=0, ge=0)


__all__ = [
    "ModelSessionPhaseDispatcherResult",
    "ModelSessionPhaseEvent",
]
