# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus binding reducer: the DEGRADED state of the runtime's bus binding (OMN-20275).

``HandlerBusBindingReducer.handle(ModelBusBindingRequest) -> ModelBusBindingResult``
"""

from omnimarket.nodes.node_bus_binding_reducer.handlers.handler_bus_binding_reducer import (
    HandlerBusBindingReducer,
)


class NodeBusBindingReducer(HandlerBusBindingReducer):
    """ONEX entry-point wrapper for HandlerBusBindingReducer."""


__all__ = ["HandlerBusBindingReducer", "NodeBusBindingReducer"]
