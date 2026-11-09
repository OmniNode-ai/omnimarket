# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The folded trace of one goal (OMN-20034, GC.11)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_projection_goal_trace.models.model_goal_trace_event_row import (
    ModelGoalTraceEventRow,
)


class ModelGoalTrace(BaseModel):
    """One goal's ordered trace, its revision chain, its verdict and its checksum."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal_id: UUID
    parent_goal_ids: tuple[UUID, ...]
    revisions: tuple[str, ...]
    # revision -> the revision it replaces (a claim replaces nothing)
    revision_edges: tuple[tuple[str, str], ...]
    # revisions that two or more revisions replace: a fork, every child kept
    forked_revisions: tuple[str, ...]
    closed_revision: str | None
    competing_closes: tuple[str, ...]
    attempt_ids: tuple[str, ...]
    current_attempt_id: str | None
    verdict: str
    delegation_ids: tuple[str, ...]
    receipt_ids: tuple[str, ...]
    assigned_event_count: int
    conflicting_event_ids: tuple[str, ...]
    checksum: str
    events: tuple[ModelGoalTraceEventRow, ...]


__all__ = ["ModelGoalTrace"]
