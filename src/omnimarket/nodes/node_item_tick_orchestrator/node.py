# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative item tick node; the contract routes work to its handler."""

from omnibase_core.nodes.node_orchestrator import NodeOrchestrator


class NodeItemTickOrchestrator(NodeOrchestrator):
    """The runtime injects the bus into the contract-bound HandlerItemTick."""
