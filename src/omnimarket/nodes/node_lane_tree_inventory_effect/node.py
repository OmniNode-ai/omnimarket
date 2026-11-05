# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative effect shell; the contract owns the routing and the handler owns the logic."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeLaneTreeInventoryEffect(NodeEffect):
    """Read-only lane inventory: one line per tree under a lane directory (repository, branch, HEAD, clean, unpushed, upstream)."""
