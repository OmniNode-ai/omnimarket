# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_projection_goal_trace -- one content-free trace per goal (OMN-20034, GC.11).

The package re-exports the pure fold and its models, which is this node's wiring
evidence for the unimported-handler check. The writer is deliberately NOT
re-exported: it imports the Kafka projection-runner stack, and importing the
package for the pure fold must not drag that in.
"""

from omnimarket.nodes.node_projection_goal_trace.handlers.handler_projection_goal_trace import (
    HandlerProjectionGoalTrace,
    fold_goal_traces,
)
from omnimarket.nodes.node_projection_goal_trace.models import (
    EnumGoalTraceAssignment,
    EnumGoalTraceEventKind,
    ModelGoalTrace,
    ModelGoalTraceEvent,
    ModelGoalTraceFoldRequest,
    ModelGoalTraceFoldResult,
)

__all__: list[str] = [
    "EnumGoalTraceAssignment",
    "EnumGoalTraceEventKind",
    "HandlerProjectionGoalTrace",
    "ModelGoalTrace",
    "ModelGoalTraceEvent",
    "ModelGoalTraceFoldRequest",
    "ModelGoalTraceFoldResult",
    "NodeProjectionGoalTrace",
    "fold_goal_traces",
]


class NodeProjectionGoalTrace(HandlerProjectionGoalTrace):
    """ONEX entry-point wrapper for HandlerProjectionGoalTrace."""
