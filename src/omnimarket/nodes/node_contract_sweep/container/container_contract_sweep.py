# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_contract_sweep.handlers.handler_contract_sweep import (
    NodeContractSweep,
)


def build_node_contract_sweep() -> NodeContractSweep:
    """Build the handler with its constructor defaults."""
    return NodeContractSweep()
