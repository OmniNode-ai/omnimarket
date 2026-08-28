# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One materialized consumer-flow row, derived (OMN-16777)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.enum_consumer_flow_state import EnumConsumerFlowState
from omnimarket.nodes.node_projection_consumer_flow.models.enum_upstream_evidence import (
    EnumUpstreamEvidence,
)


class ModelConsumerFlowRow(BaseModel):
    """A row of ``consumer_flow_windows``, before it reaches a database.

    The counters are ``int | None`` and that is the whole point: ``None`` marks
    a window that was never observed (``flow_state = UNKNOWN``), which is a
    different fact from a window observed to have carried nothing. Collapsing
    the two is the false-green this ticket exists to close (AC5).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer_group: str = Field(..., min_length=1)
    topic: str = Field(..., min_length=1)
    window_start: datetime
    window_end: datetime
    node_id: UUID
    ingest_sequence: int = Field(..., ge=0)

    messages_in: int | None = None
    messages_out: int | None = None
    messages_dlq: int | None = None
    handler_errors: int | None = None

    upstream_produced: int | None = None
    upstream_evidence: EnumUpstreamEvidence
    flow_state: EnumConsumerFlowState

    # Event time, never a wall clock: the row is a statement ABOUT the window,
    # so replaying the window reproduces it byte-identically (AC6).
    evaluated_at: datetime


__all__ = ["ModelConsumerFlowRow"]
