# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which listed overlays have a loader assertion past the bound."""

from omnimarket.nodes.node_overlay_assertion_stale_compute.handlers.handler_overlay_assertion_stale import (
    HandlerOverlayAssertionStale,
)


class NodeOverlayAssertionStaleCompute(HandlerOverlayAssertionStale):
    """ONEX entry point."""


__all__ = ["HandlerOverlayAssertionStale", "NodeOverlayAssertionStaleCompute"]
