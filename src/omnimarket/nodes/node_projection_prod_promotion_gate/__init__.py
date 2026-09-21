# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_projection_prod_promotion_gate — durable prod-gate decisions (OMN-18999)."""

from omnimarket.nodes.node_projection_prod_promotion_gate.handlers.handler_projection_prod_promotion_gate import (
    HandlerProjectionProdPromotionGate,
)

__all__ = [
    "HandlerProjectionProdPromotionGate",
    "NodeProjectionProdPromotionGate",
]


class NodeProjectionProdPromotionGate(HandlerProjectionProdPromotionGate):
    """ONEX entry-point wrapper for HandlerProjectionProdPromotionGate."""
