# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_data_flow_sweep.handlers.handler_data_flow_sweep import (
    NodeDataFlowSweep,
)


def build_node_data_flow_sweep() -> NodeDataFlowSweep:
    """Build the handler with its constructor defaults."""
    return NodeDataFlowSweep()
