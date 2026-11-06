# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative shell for standing-rule reads and audit effects."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeDispatchStandingRulesEffect(NodeEffect):
    """Typed effect with its handler and dependencies declared in the contract."""
