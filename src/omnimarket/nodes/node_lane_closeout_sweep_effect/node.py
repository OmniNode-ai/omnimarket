# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative effect shell; the contract owns the routing and the handler owns the logic."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeLaneCloseoutSweepEffect(NodeEffect):
    """Close out every ended lane on this host: salvage dirty trees, remove trees and run directories, write one record per lane run."""
