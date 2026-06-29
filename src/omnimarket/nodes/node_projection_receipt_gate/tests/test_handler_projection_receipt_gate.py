# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node-local coverage marker for handler_projection_receipt_gate."""

from __future__ import annotations

from omnimarket.nodes.node_projection_receipt_gate.handlers.handler_projection_receipt_gate import (
    HandlerProjectionReceiptGate,
)


def test_handler_projection_receipt_gate_imports() -> None:
    handler = HandlerProjectionReceiptGate()

    assert handler.handler_type == "NODE_HANDLER"
