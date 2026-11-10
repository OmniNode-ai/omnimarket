# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The order writer: advances a test order from the gate store's piece records, fenced.

``HandlerOrderAdvance.handle(ModelOrderAdvanceRequest) -> ModelOrderAdvanceResult``; the gate
store and the piece records are injected and the ``order_advance`` overlay sets the backend.
"""

from omnimarket.nodes.node_order_advance_effect.handlers.handler_order_advance import (
    HandlerOrderAdvance,
)

__all__ = ["HandlerOrderAdvance"]
