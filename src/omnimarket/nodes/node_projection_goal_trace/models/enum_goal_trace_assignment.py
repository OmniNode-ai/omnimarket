# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How an event came to belong to a goal's trace (OMN-20034, GC.11)."""

from __future__ import annotations

from enum import StrEnum


class EnumGoalTraceAssignment(StrEnum):
    """The join that attached an event to a goal, or why it was not attached."""

    OWN = "own"  # the event names its goal (claim, revision, close, ...)
    BINDING = "binding"  # the event's agent is bound to exactly one goal
    ANCESTRY = "ancestry"  # the agent is unbound; its nearest bound ancestor is
    UNASSIGNED = "unassigned"  # no binding on the agent or any ancestor
    AMBIGUOUS = "ambiguous"  # bindings to two goals; attached to neither


__all__ = ["EnumGoalTraceAssignment"]
