# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay resolver compute node: a skill's structured overlay from the overlay roots, validated against its schema."""

from omnimarket.nodes.node_overlay_resolver_effect.handlers.handler_overlay_resolver import (
    HandlerOverlayResolver,
)


class NodeOverlayResolverEffect(HandlerOverlayResolver):
    """ONEX entrypoint for the overlay resolver."""


__all__ = ["HandlerOverlayResolver", "NodeOverlayResolverEffect"]
