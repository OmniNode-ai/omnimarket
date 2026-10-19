# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative host thermal observe effect: all behaviour is in the contract and its handler."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeHostThermalObserveEffect(NodeEffect):
    """Append one thermal reading of the host it runs on to that host's series."""
