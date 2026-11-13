# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay root resolver effect: the single resolver of the overlay root list (OMN-24032).

``HandlerOverlayRootsResolve.handle(ModelOverlayRootsRequest) -> ModelOverlayRootsResult``
resolves the ordered overlay root list (store, canonical clones, declared) through an
injected filesystem probe. It has no bus surface: the caller invokes it in process.
"""

from omnimarket.nodes.node_overlay_roots_resolve_effect.handlers.handler_overlay_roots_resolve import (
    HandlerOverlayRootsResolve,
)


class NodeOverlayRootsResolveEffect(HandlerOverlayRootsResolve):
    """ONEX entry-point wrapper for HandlerOverlayRootsResolve."""


__all__ = [
    "HandlerOverlayRootsResolve",
    "NodeOverlayRootsResolveEffect",
]
