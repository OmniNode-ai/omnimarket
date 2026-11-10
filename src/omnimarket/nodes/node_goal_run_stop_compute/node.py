# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical local entry point for the goal-run stop decision."""

from omnimarket.nodes.node_goal_run_stop_compute.handlers.handler_goal_run_stop import (
    HandlerGoalRunStop,
)


class NodeGoalRunStopCompute(HandlerGoalRunStop):
    """ONEX entrypoint: decide whether a goal-driven run continues, retries or stops."""
