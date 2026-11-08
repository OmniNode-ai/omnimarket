# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative installation node; the contract owns routing and topics."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeAutomaticFixInstallEffect(NodeEffect):
    """Run the server-bound fix installation handler on each host tick."""
