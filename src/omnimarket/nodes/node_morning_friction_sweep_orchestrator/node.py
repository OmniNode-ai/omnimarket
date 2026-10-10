# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative orchestrator: all behavior is in the contract and its handler."""

from omnibase_core.nodes.node_orchestrator import NodeOrchestrator


class NodeMorningFrictionSweepOrchestrator(NodeOrchestrator):
    """Guarded morning friction sweep: precheck, sources, synthesis, report."""
