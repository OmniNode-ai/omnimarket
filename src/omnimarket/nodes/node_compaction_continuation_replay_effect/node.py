# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative node; handler ports and topics are declared in contract.yaml."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeCompactionContinuationReplayEffect(NodeEffect):
    """Run sandboxed continuations through the container's fixed model and CM.3 scorer."""
