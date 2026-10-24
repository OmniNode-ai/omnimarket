# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative delegation effect; the contract and handlers own behavior."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeLlmDelegationCallEffect(NodeEffect):
    """Execute model calls and coding sessions through target adapters."""
