# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_projection_receipt_gate_pin_friction - fold pin pull request observations into rows (OMN-20134)."""

from omnimarket.nodes.node_projection_receipt_gate_pin_friction.handlers.handler_pin_friction_reducer import (
    HandlerReceiptGatePinFrictionReducer,
)


class NodeProjectionReceiptGatePinFriction(HandlerReceiptGatePinFrictionReducer):
    """ONEX entry-point wrapper for HandlerReceiptGatePinFrictionReducer (OMN-20134)."""


__all__ = [
    "HandlerReceiptGatePinFrictionReducer",
    "NodeProjectionReceiptGatePinFriction",
]
