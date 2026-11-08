# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The loader of deployment overlays: one overlay by its id onto the bus."""

from omnimarket.nodes.node_overlay_store_load_effect.handlers import (
    HandlerOverlayStoreLoad,
)


class NodeOverlayStoreLoadEffect(HandlerOverlayStoreLoad):
    """ONEX entry point."""


__all__ = ["HandlerOverlayStoreLoad", "NodeOverlayStoreLoadEffect"]
