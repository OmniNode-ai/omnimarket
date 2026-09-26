# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the pure claude-hook-events fold (OMN-19513)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from omnimarket.nodes.node_projection_claude_hook_events.models.enum_claude_hook_event_name import (
    EnumClaudeHookEventName,
)
from omnimarket.nodes.node_projection_claude_hook_events.models.enum_parent_resolution import (
    EnumParentResolution,
)


class ModelClaudeHookEventRow(BaseModel):
    """One row of ``omninode_internal.claude_hook_events``.

    ``ingested_at`` and ``projection_cursor`` are absent on purpose: both are
    assigned by the database at write time, and a pure fold has no clock.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    session_id: str = Field(min_length=1)
    agent_id: str | None
    is_subagent: bool
    agent_type: str | None
    parent_tool_use_id: str | None
    parent_agent_id: str | None
    workflow_run_id: str | None
    spawn_depth: int | None
    hook_event_name: EnumClaudeHookEventName
    tool_use_id: str | None
    tool_name: str | None
    prompt_id: str | None
    turn_id: str | None
    correlation_id: UUID
    causation_id: UUID | None
    emitted_at: datetime
    payload: dict[str, JsonValue]
    content_ref_ids: tuple[str, ...]
    source_topic: str = Field(min_length=1)


class ModelClaudeAgentSpanUpdate(BaseModel):
    """What one event contributes to its agent's span.

    The writer merges it into ``claude_agent_spans`` in SQL: the earliest
    ``seen_at`` is the span's start, the latest ``stopped_at`` its stop, and a
    known parent replaces an ``unknown`` one but is never replaced by it.
    ``tool_call_count`` is not carried at all -- the writer recounts it from the
    event table, so a redelivered event cannot count twice.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    agent_type: str | None
    parent_tool_use_id: str | None
    parent_agent_id: str | None
    parent_resolution: EnumParentResolution
    workflow_run_id: str | None
    spawn_depth: int | None
    seen_at: datetime
    stopped_at: datetime | None


class ModelChildParentRepair(BaseModel):
    """A parent that arrived after its child: resolve the child's span in place.

    Emitted for every ``PreToolUse`` carrying a ``tool_use_id``. The writer
    applies it only to spans still ``unknown`` whose ``parent_tool_use_id`` is
    this call, so on the ordinary in-order path it touches nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    parent_tool_use_id: str = Field(min_length=1)
    parent_agent_id: str | None
    parent_resolution: EnumParentResolution


class ModelClaudeHookProjectionResult(BaseModel):
    """The rows one hook event derives."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_row: ModelClaudeHookEventRow
    span_update: ModelClaudeAgentSpanUpdate | None = Field(
        default=None,
        description="Present exactly when the event fired inside a subagent.",
    )
    resolves_children: ModelChildParentRepair | None = Field(
        default=None,
        description=(
            "Present when the event is a PreToolUse with a tool_use_id: any "
            "span already stored with this call as its parent_tool_use_id and "
            "an unknown parent is resolved by it. This is the out-of-order "
            "repair, for a child whose events were written before its parent's."
        ),
    )


__all__ = [
    "ModelChildParentRepair",
    "ModelClaudeAgentSpanUpdate",
    "ModelClaudeHookEventRow",
    "ModelClaudeHookProjectionResult",
]
