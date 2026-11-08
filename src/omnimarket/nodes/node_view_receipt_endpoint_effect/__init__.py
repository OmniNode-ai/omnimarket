# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_view_receipt_endpoint_effect: INSUFFICIENT scaffold of the publish effect's receipt endpoint."""

from omnimarket.nodes.node_view_receipt_endpoint_effect.handlers.handler_view_receipt_endpoint_effect import (
    HandlerViewReceiptEndpointEffect,
)

__all__ = ["HandlerViewReceiptEndpointEffect", "NodeViewReceiptEndpointEffect"]


class NodeViewReceiptEndpointEffect(HandlerViewReceiptEndpointEffect):
    """ONEX entry-point wrapper for HandlerViewReceiptEndpointEffect."""
