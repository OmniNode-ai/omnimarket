# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_dashboard_sweep.handlers.handler_dashboard_sweep import (
    NodeDashboardSweep,
)


def build_node_dashboard_sweep() -> NodeDashboardSweep:
    """Build the handler with its constructor defaults."""
    return NodeDashboardSweep()
