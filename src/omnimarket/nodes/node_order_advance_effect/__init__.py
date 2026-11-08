# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The order writer effect: advances the gate store's order one position per tick.

``HandlerOrderAdvance.handle(ModelOrderAdvanceRequest) -> ModelOrderAdvanceResult``; the gate
store and the readiness read are injected and the ``order_writer`` overlay selects them.
"""

from omnimarket.nodes.node_order_advance_effect.handlers.handler_order_advance import (
    HandlerOrderAdvance,
)

__all__ = ["HandlerOrderAdvance"]
