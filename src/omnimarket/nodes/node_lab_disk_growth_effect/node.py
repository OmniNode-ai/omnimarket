# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative lab disk growth effect: all behaviour is in the contract and its handlers."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeLabDiskGrowthEffect(NodeEffect):
    """Evict old plugin snapshots and colocate the uv cache with the worktrees."""
