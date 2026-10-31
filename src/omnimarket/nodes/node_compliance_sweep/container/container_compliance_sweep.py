# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_compliance_sweep.handlers.handler_compliance_sweep import (
    NodeComplianceSweep,
)


def build_node_compliance_sweep() -> NodeComplianceSweep:
    """Build the handler with its constructor defaults."""
    return NodeComplianceSweep()
