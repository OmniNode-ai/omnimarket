# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_dependency_health_sweep.handlers.handler_dep_health_sweep import (
    HandlerDepHealthSweep,
)


def build_handler_dep_health_sweep() -> HandlerDepHealthSweep:
    """Build the handler with its constructor defaults."""
    return HandlerDepHealthSweep()
