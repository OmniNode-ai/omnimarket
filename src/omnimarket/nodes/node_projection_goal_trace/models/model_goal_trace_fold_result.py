# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the goal-trace fold (OMN-20034, GC.11)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_projection_goal_trace.models.model_goal_trace import (
    ModelGoalTrace,
)
from omnimarket.nodes.node_projection_goal_trace.models.model_goal_trace_event_row import (
    ModelGoalTraceEventRow,
)


class ModelGoalTraceFoldResult(BaseModel):
    """One trace per goal, plus the events that belong to no goal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    traces: tuple[ModelGoalTrace, ...]
    unassigned: tuple[ModelGoalTraceEventRow, ...]
    ambiguous: tuple[ModelGoalTraceEventRow, ...]
    unassigned_count: int
    ambiguous_count: int
    id_conflict_ids: tuple[str, ...]
    row_count: int


__all__ = ["ModelGoalTraceFoldResult"]
