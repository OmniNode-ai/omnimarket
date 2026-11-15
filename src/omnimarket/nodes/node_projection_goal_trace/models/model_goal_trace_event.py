# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One content-free event as the goal-trace fold reads it (OMN-20034, GC.11)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_goal_trace.models.enum_goal_trace_event_kind import (
    EnumGoalTraceEventKind,
)


class ModelGoalTraceEvent(BaseModel):
    """A normalized, digest-only event. It carries identifiers and a body digest.

    The trace is content-free (plan section 12.6): no prompt, tool input or
    result text ever enters this model. ``body_digest`` is the digest of the
    source body, and it is what tells a redelivery (same id, same digest) from
    a conflict (same id, different digest).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    kind: EnumGoalTraceEventKind
    body_digest: str = Field(min_length=1)
    emitted_at: datetime | None = None
    # CLAIM, REVISE, CLOSE, DELEGATION, RECEIPT and ATTEMPT name their goal. A
    # BIND names its goal and the agent it binds. A HOOK carries the goal only
    # when it is the stamped binding event (GC.8); otherwise it joins by agent.
    goal_id: UUID | None = None
    parent_goal_id: UUID | None = None
    session_id: str | None = None
    agent_key: str | None = None
    parent_agent_key: str | None = None
    # REVISE: the revision it replaces. CLOSE, ATTEMPT, RECEIPT: the revision
    # the close named or the attempt or receipt was bound to.
    revision: str | None = None
    replaces: str | None = None
    # ATTEMPT: when the verifier completed, and what it concluded.
    completed_at: datetime | None = None
    verdict: str | None = None
    # RECEIPT: the hash of the contract item the receipt binds to.
    item_hash: str | None = None


__all__ = ["ModelGoalTraceEvent"]
