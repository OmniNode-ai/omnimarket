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


def topic_degraded_reason(missing_topics: tuple[str, ...]) -> str:
    """The reason a binding reports when a required topic is unknown, naming each missing topic."""
    return f"required topic missing: {', '.join(missing_topics)}"


def reduce_binding_state(
    prior: EnumBusBindingState,
    *,
    durable_healthy: bool,
    pending_outbox: int = 0,
    missing_topics: tuple[str, ...] = (),
) -> tuple[EnumBusBindingState, EnumBusBinding]:
    """The next state and the binding that state serves on.

    A durable bus that answers its health read is still not healthy while appends wait in the outbox and a
    required topic is unknown: the replay could not be answered, so the read counts as failed (OMN-21959).
    """
    stuck = pending_outbox > 0 and bool(missing_topics)
    trigger = (
        TRIGGER_DURABLE_OK if durable_healthy and not stuck else TRIGGER_DURABLE_FAILED
    )
    state = TRANSITIONS[(prior, trigger)]
    binding = (
        EnumBusBinding.INMEMORY
        if state is EnumBusBindingState.DEGRADED
        else EnumBusBinding.DURABLE
    )
    return state, binding
