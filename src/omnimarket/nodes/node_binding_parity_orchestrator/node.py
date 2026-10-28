# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative orchestrator: all behavior is in the contract and its handler."""

from omnibase_core.nodes.node_orchestrator import NodeOrchestrator


class NodeBindingParityOrchestrator(NodeOrchestrator):
    """Run one scenario under every profile of the overlay and compare the runs."""
