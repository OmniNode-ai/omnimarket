# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure transition of the bus binding FSM (OMN-20275)."""

from __future__ import annotations

from omnimarket.handlers.bus_binding.reducer import reduce_binding_state
from omnimarket.models.bus_binding import EnumBusBindingState
from omnimarket.nodes.node_bus_binding_reducer.models.model_bus_binding import (
    ModelBusBindingRequest,
    ModelBusBindingResult,
)


class HandlerBusBindingReducer:
    """The next state, the binding it serves on, whether to alarm and whether to replay the outbox."""

    def handle(self, request: ModelBusBindingRequest) -> ModelBusBindingResult:
        state, binding = reduce_binding_state(
            request.prior_state, durable_healthy=request.durable_healthy
        )
        return ModelBusBindingResult(
            state=state,
            binding=binding,
            alarm=state is EnumBusBindingState.DEGRADED
            and request.prior_state is not EnumBusBindingState.DEGRADED,
            replay_outbox=state is EnumBusBindingState.RECOVERING
            or (
                state is EnumBusBindingState.HEALTHY
                and request.prior_state is EnumBusBindingState.RECOVERING
            ),
        )
