# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Throughput goal check compute node (OMN-23734)."""

from omnimarket.nodes.node_throughput_goal_check_compute.handlers.handler_goal_check_scheduled_fire import (
    HandlerGoalCheckScheduledFire,
)
from omnimarket.nodes.node_throughput_goal_check_compute.handlers.handler_goal_scope_stale import (
    HandlerGoalScopeStale,
)
from omnimarket.nodes.node_throughput_goal_check_compute.handlers.handler_throughput_goal_check import (
    HandlerThroughputGoalCheck,
)


class NodeThroughputGoalCheckCompute(HandlerThroughputGoalCheck):
    """ONEX entrypoint for the hourly check's goal check."""


__all__ = [
    "HandlerGoalCheckScheduledFire",
    "HandlerGoalScopeStale",
    "HandlerThroughputGoalCheck",
    "NodeThroughputGoalCheckCompute",
]
