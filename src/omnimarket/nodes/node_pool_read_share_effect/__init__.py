# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local ONEX wrapper for node_pool_read_share_effect."""

from omnimarket.nodes.node_pool_read_share_effect.handlers.handler_pool_read_share import (
    HandlerPoolReadShare,
)


class NodePoolReadShareEffect(HandlerPoolReadShare):
    """ONEX entry-point wrapper for HandlerPoolReadShare."""


__all__ = ["HandlerPoolReadShare", "NodePoolReadShareEffect"]
