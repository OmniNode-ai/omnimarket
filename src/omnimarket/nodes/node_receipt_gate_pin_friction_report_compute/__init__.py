# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_receipt_gate_pin_friction_report_compute - the pin friction comparison per pin kind (OMN-20134)."""

from omnimarket.nodes.node_receipt_gate_pin_friction_report_compute.handlers.handler_pin_friction_report import (
    HandlerReceiptGatePinFrictionReport,
)


class NodeReceiptGatePinFrictionReportCompute(HandlerReceiptGatePinFrictionReport):
    """ONEX entry-point wrapper for HandlerReceiptGatePinFrictionReport (OMN-20134)."""


__all__ = [
    "HandlerReceiptGatePinFrictionReport",
    "NodeReceiptGatePinFrictionReportCompute",
]
