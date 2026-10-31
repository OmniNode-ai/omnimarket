# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_receipt_gate_pin_advance_compute - plan the receipt-gate caller pin advances (OMN-20134)."""

from omnimarket.nodes.node_receipt_gate_pin_advance_compute.handlers.handler_receipt_gate_pin_advance import (
    HandlerReceiptGatePinAdvance,
)


class NodeReceiptGatePinAdvanceCompute(HandlerReceiptGatePinAdvance):
    """ONEX entry-point wrapper for HandlerReceiptGatePinAdvance (OMN-20134)."""


__all__ = ["HandlerReceiptGatePinAdvance", "NodeReceiptGatePinAdvanceCompute"]
