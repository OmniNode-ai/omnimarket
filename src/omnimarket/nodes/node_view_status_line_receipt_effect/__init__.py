# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_view_status_line_receipt_effect: INSUFFICIENT scaffold of the status line consumer's receipt."""

from omnimarket.nodes.node_view_status_line_receipt_effect.handlers.handler_view_status_line_receipt_effect import (
    HandlerViewStatusLineReceiptEffect,
)

__all__ = ["HandlerViewStatusLineReceiptEffect", "NodeViewStatusLineReceiptEffect"]


class NodeViewStatusLineReceiptEffect(HandlerViewStatusLineReceiptEffect):
    """ONEX entry-point wrapper for HandlerViewStatusLineReceiptEffect."""
