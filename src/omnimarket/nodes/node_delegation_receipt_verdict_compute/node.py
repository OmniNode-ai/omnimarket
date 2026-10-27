# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical compute entry point; all decisions are owned by the handler."""

from omnimarket.nodes.node_delegation_receipt_verdict_compute.handlers.handler_receipt_verdict import (
    HandlerReceiptVerdict,
)


class NodeDelegationReceiptVerdictCompute(HandlerReceiptVerdict):
    """Decode correlated receipts and apply the delegation verdict table."""
