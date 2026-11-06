# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative delivery effect; all operations are contract routed."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeErrorRuleDeliveryEffect(NodeEffect):
    """Deliver error rule hints and measure both hook surfaces."""
