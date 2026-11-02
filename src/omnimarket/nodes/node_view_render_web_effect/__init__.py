# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_view_render_web_effect: INSUFFICIENT scaffold of the web renderer."""

from omnimarket.nodes.node_view_render_web_effect.handlers.handler_view_render_web_effect import (
    HandlerViewRenderWebEffect,
)

__all__ = ["HandlerViewRenderWebEffect", "NodeViewRenderWebEffect"]


class NodeViewRenderWebEffect(HandlerViewRenderWebEffect):
    """ONEX entry-point wrapper for HandlerViewRenderWebEffect."""
