# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative orchestrator: all behaviour is in the contract and its handler."""

from omnibase_core.nodes.node_orchestrator import NodeOrchestrator


class NodeContextCompactionOrchestrator(NodeOrchestrator):
    """Compaction command in; package reply or native fallback, with receipts, out."""
