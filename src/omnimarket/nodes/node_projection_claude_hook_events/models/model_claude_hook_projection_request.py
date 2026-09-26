# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input to the pure claude-hook-events fold (OMN-19513)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_projection_claude_hook_events.models.model_claude_hook_event_wire import (
    ModelClaudeHookEventWire,
)


class ModelKnownParentToolCall(BaseModel):
    """A ``PreToolUse`` already materialized: which agent made that tool call.

    ``agent_id`` null means the main thread made it. The writer reads these
    from the table it owns and hands them in, which is what keeps the fold free
    of I/O while still resolving a parent that arrived in an earlier message.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    tool_use_id: str = Field(min_length=1)
    agent_id: str | None


class ModelClaudeHookProjectionRequest(BaseModel):
    """One hook event plus the parent tool calls the writer already holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: ModelClaudeHookEventWire
    known_parent_calls: tuple[ModelKnownParentToolCall, ...] = ()
    source_topic: str | None = Field(
        default=None,
        description=(
            "The canonical topic the event was consumed from. None means the "
            "contract's own subscribe topic."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_the_bare_event(cls, data: Any) -> Any:
        """The bus message IS the event; the wrapper is this model's own.

        The runtime hands a handler the bare event dict plus underscore-prefixed
        injections. The same lesson as the runner-fleet request (OMN-18880):
        a request model that only accepts the nested form cannot be built from
        a live message at all. The nested form stays accepted because the
        writer builds it deliberately.
        """
        if not isinstance(data, dict) or "event" in data:
            return data
        return {"event": data}


__all__ = ["ModelClaudeHookProjectionRequest", "ModelKnownParentToolCall"]
