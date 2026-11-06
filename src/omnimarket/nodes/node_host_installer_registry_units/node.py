# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative host installer effect; the contract routes to HandlerRegistryUnits."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeHostInstallerRegistryUnits(NodeEffect):
    """All installation work is owned by the contract-bound handler."""
