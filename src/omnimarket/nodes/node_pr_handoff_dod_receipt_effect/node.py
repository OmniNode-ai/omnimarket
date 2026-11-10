# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative effect shell; the contract owns routing and receipt policy."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodePrHandoffDodReceiptEffect(NodeEffect):
    """Read-only handoff prerequisite, executed by its contract-bound handler."""
