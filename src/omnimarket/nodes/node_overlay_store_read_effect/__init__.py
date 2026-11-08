# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one reader of loaded overlays: the store through its protocol, and a stamped local snapshot."""

from omnimarket.nodes.node_overlay_store_read_effect.handlers import (
    HandlerOverlayStoreRead,
)


class NodeOverlayStoreReadEffect(HandlerOverlayStoreRead):
    """ONEX entry point."""


__all__ = ["HandlerOverlayStoreRead", "NodeOverlayStoreReadEffect"]
