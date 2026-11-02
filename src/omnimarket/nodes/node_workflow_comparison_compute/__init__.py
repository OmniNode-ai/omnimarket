# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_workflow_comparison_compute: scaffold from the weekly_retrospective design."""

from omnimarket.nodes.node_workflow_comparison_compute.handlers.handler_workflow_comparison_compute import (
    HandlerWorkflowComparisonCompute,
)

__all__ = ["HandlerWorkflowComparisonCompute", "NodeWorkflowComparisonCompute"]


class NodeWorkflowComparisonCompute(HandlerWorkflowComparisonCompute):
    """ONEX entry-point wrapper for HandlerWorkflowComparisonCompute."""
