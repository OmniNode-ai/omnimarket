# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point; the contract names the handler and bus topics."""

from omnibase_core.nodes.node_orchestrator import NodeOrchestrator


class NodeMergeSweepDispatchOrchestrator(NodeOrchestrator):
    """Send each admitted planned lane through the canonical lane dispatch node."""
