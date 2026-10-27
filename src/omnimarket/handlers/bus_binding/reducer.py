# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The binding FSM as a pure function (OMN-20275).

node_bus_binding_reducer/contract.yaml declares the same states and transitions; a test keeps the two equal.
"""

from __future__ import annotations

from omnimarket.models.bus_binding import EnumBusBinding, EnumBusBindingState

TRIGGER_DURABLE_OK = "durable_health_ok"
TRIGGER_DURABLE_FAILED = "durable_health_failed"

TRANSITIONS: dict[tuple[EnumBusBindingState, str], EnumBusBindingState] = {
    (EnumBusBindingState.HEALTHY, TRIGGER_DURABLE_OK): EnumBusBindingState.HEALTHY,
    (EnumBusBindingState.HEALTHY, TRIGGER_DURABLE_FAILED): EnumBusBindingState.DEGRADED,
    (
        EnumBusBindingState.DEGRADED,
        TRIGGER_DURABLE_FAILED,
    ): EnumBusBindingState.DEGRADED,
    (EnumBusBindingState.DEGRADED, TRIGGER_DURABLE_OK): EnumBusBindingState.RECOVERING,
    (EnumBusBindingState.RECOVERING, TRIGGER_DURABLE_OK): EnumBusBindingState.HEALTHY,
    (
        EnumBusBindingState.RECOVERING,
        TRIGGER_DURABLE_FAILED,
    ): EnumBusBindingState.DEGRADED,
}


def reduce_binding_state(
    prior: EnumBusBindingState, *, durable_healthy: bool
) -> tuple[EnumBusBindingState, EnumBusBinding]:
    """The next state and the binding that state serves on."""
    trigger = TRIGGER_DURABLE_OK if durable_healthy else TRIGGER_DURABLE_FAILED
    state = TRANSITIONS[(prior, trigger)]
    binding = (
        EnumBusBinding.INMEMORY
        if state is EnumBusBindingState.DEGRADED
        else EnumBusBinding.DURABLE
    )
    return state, binding
