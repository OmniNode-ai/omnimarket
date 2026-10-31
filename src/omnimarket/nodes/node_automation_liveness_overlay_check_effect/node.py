# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative overlay check run effect: all behaviour is in the contract and its handler."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeAutomationLivenessOverlayCheckEffect(NodeEffect):
    """Read the private overlay and the registry clones, then run the pure completeness check."""
