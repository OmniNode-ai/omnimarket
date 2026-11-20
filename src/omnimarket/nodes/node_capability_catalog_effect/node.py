# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative capability catalog effect; routing is owned by its contract."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeCapabilityCatalogEffect(NodeEffect):
    """Collect and render the registry's declared capabilities."""
