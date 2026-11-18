# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the goal-trace fold (OMN-20034, GC.11)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_projection_goal_trace.models.model_goal_trace_event import (
    ModelGoalTraceEvent,
)


class ModelGoalTraceFoldRequest(BaseModel):
    """Every event known for the goals in question, in any order, redelivered or not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    events: tuple[ModelGoalTraceEvent, ...]


__all__ = ["ModelGoalTraceFoldRequest"]
