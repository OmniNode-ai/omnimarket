# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_view_render_text_effect: INSUFFICIENT scaffold of the terminal renderer."""

from omnimarket.nodes.node_view_render_text_effect.handlers.handler_view_render_text_effect import (
    HandlerViewRenderTextEffect,
)

__all__ = ["HandlerViewRenderTextEffect", "NodeViewRenderTextEffect"]


class NodeViewRenderTextEffect(HandlerViewRenderTextEffect):
    """ONEX entry-point wrapper for HandlerViewRenderTextEffect."""
