# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Claude token pool store adopt compute node (OMN-23887)."""

from omnimarket.nodes.node_claude_pool_store_adopt_compute.handlers.handler_store_adopt import (
    HandlerStoreAdopt,
)


class NodeClaudePoolStoreAdoptCompute(HandlerStoreAdopt):
    """ONEX entrypoint for the pure store adopt decision."""


__all__ = ["HandlerStoreAdopt", "NodeClaudePoolStoreAdoptCompute"]
