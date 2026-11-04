# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler factory for node_network_graph_compute."""

from __future__ import annotations

from omnimarket.nodes.node_network_graph_compute.handlers.handler_network_graph_compute import (
    HandlerNetworkGraphCompute,
)

__all__ = ["create_handler_network_graph_compute"]


def create_handler_network_graph_compute() -> HandlerNetworkGraphCompute:
    """Build the handler; the compute node has no collaborator to inject."""
    return HandlerNetworkGraphCompute()
