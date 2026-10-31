# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_receipt_gate_pin_friction_ledger_effect - a pin friction event as a FRICTION ledger row (OMN-20134)."""

from omnimarket.nodes.node_receipt_gate_pin_friction_ledger_effect.handlers.handler_pin_friction_ledger import (
    HandlerReceiptGatePinFrictionLedger,
)


class NodeReceiptGatePinFrictionLedgerEffect(HandlerReceiptGatePinFrictionLedger):
    """ONEX entry-point wrapper for HandlerReceiptGatePinFrictionLedger (OMN-20134)."""


__all__ = [
    "HandlerReceiptGatePinFrictionLedger",
    "NodeReceiptGatePinFrictionLedgerEffect",
]
