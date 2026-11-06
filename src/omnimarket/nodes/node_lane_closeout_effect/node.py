# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative effect shell; the contract owns the routing and the handler owns the logic."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeLaneCloseoutEffect(NodeEffect):
    """Guard one lane tree (verdict ok, wip or keep), push unpushed commits to a WIP branch, and remove a tree nothing would be lost from; returns the TERMINAL worktree cell."""
