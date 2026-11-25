# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative host thermal guard effect: all behaviour is in the contract and its handler."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeHostThermalGuardEffect(NodeEffect):
    """Hold a hot host through the declared hold file and record each hold and release on the ledger."""
