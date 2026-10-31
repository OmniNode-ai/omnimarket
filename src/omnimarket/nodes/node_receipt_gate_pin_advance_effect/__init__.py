# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_receipt_gate_pin_advance_effect - open one receipt-gate pin pull request per stale caller (OMN-20134)."""

from omnimarket.nodes.node_receipt_gate_pin_advance_effect.handlers.handler_receipt_gate_pin_advance_effect import (
    HandlerReceiptGatePinAdvanceEffect,
)


class NodeReceiptGatePinAdvanceEffect(HandlerReceiptGatePinAdvanceEffect):
    """ONEX entry-point wrapper for HandlerReceiptGatePinAdvanceEffect (OMN-20134)."""


__all__ = ["HandlerReceiptGatePinAdvanceEffect", "NodeReceiptGatePinAdvanceEffect"]
