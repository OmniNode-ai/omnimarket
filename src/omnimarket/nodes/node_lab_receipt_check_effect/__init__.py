# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_receipt_check_effect: facts in, one call to the core lab_receipt validator (OMN-21826)."""

from omnimarket.nodes.node_lab_receipt_check_effect.handlers.handler_lab_receipt_check import (
    HandlerLabReceiptCheck,
)

__all__: list[str] = ["HandlerLabReceiptCheck"]
