# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_coverage_sweep.handlers.handler_coverage_sweep import (
    NodeCoverageSweep,
)


def build_node_coverage_sweep() -> NodeCoverageSweep:
    """Build the handler with its constructor defaults."""
    return NodeCoverageSweep()
