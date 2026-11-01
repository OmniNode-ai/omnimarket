# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the in-memory event bus the DI profiles fall back to (OMN-21695).

The profile factories never construct the bus themselves: they take the one this
module binds, in the group the node contract's ``in_memory_bindings`` section
declares and the environment the caller's profile names.
"""

from __future__ import annotations

from typing import cast

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)

_NODE = "node_integration_test_runner"


def bind_local_event_bus() -> EventBusInmemory:
    """Bind the bus of the LOCAL profile, in the environment the contract declares for it."""
    declared = cast(
        dict[str, str], load_packaged_contract_section(_NODE, "in_memory_bindings")
    )
    return EventBusInmemory(
        environment=declared["local_profile_environment"],
        group=declared["event_bus_group"],
    )


def bind_fallback_event_bus(*, environment: str) -> EventBusInmemory:
    """Bind the bus a profile falls back to when Kafka is not reachable."""
    declared = cast(
        dict[str, str], load_packaged_contract_section(_NODE, "in_memory_bindings")
    )
    return EventBusInmemory(environment=environment, group=declared["event_bus_group"])
