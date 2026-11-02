# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus binding settle effect: resolve open bus-binding exception rows in code (OMN-21959).

``HandlerBusBindingSettle.handle(ModelBusBindingSettleRequest) -> ModelBusBindingSettleResult``
"""

from omnimarket.nodes.node_bus_binding_settle_effect.handlers.handler_bus_binding_settle import (
    HandlerBusBindingSettle,
)


class NodeBusBindingSettleEffect(HandlerBusBindingSettle):
    """ONEX entry-point wrapper for HandlerBusBindingSettle."""


__all__ = ["HandlerBusBindingSettle", "NodeBusBindingSettleEffect"]
