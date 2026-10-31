# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_database_sweep.handlers.handler_database_sweep import (
    NodeDatabaseSweep,
)


def build_node_database_sweep() -> NodeDatabaseSweep:
    """Build the handler with its constructor defaults."""
    return NodeDatabaseSweep()
