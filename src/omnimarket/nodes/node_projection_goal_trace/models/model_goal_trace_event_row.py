# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One ordered row of a goal's trace (OMN-20034, GC.11)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_goal_trace.models.enum_goal_trace_assignment import (
    EnumGoalTraceAssignment,
)
from omnimarket.nodes.node_projection_goal_trace.models.enum_goal_trace_event_kind import (
    EnumGoalTraceEventKind,
)


class ModelGoalTraceEventRow(BaseModel):
    """An event with its position in the canonical order and its assignment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal_id: UUID | None
    seq: int | None = Field(default=None, ge=0)
    event_id: str
    kind: EnumGoalTraceEventKind
    body_digest: str
    emitted_at: datetime | None
    session_id: str | None
    agent_key: str | None
    parent_agent_key: str | None
    revision: str | None
    replaces: str | None
    completed_at: datetime | None
    verdict: str | None
    item_hash: str | None
    assignment: EnumGoalTraceAssignment
    id_conflict: bool = False


__all__ = ["ModelGoalTraceEventRow"]
