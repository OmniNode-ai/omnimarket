# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative compute node for the network graph structure; the contract routes to the handler."""

from __future__ import annotations

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.network_graph import (
    ModelNetworkGraphDocument,
    ModelNetworkGraphRequest,
)


class NodeNetworkGraphCompute(
    NodeCompute[ModelNetworkGraphRequest, ModelNetworkGraphDocument]
):
    """Thin shell: HandlerNetworkGraphCompute, bound in contract.yaml, owns the logic."""


__all__ = ["NodeNetworkGraphCompute"]
