# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the in-memory event bus a contract-seams run drives when none is injected (OMN-21695).

The handler never constructs the bus itself: it takes the one this module binds,
in the environment the node contract's ``in_memory_bindings`` section declares and
the consumer group the caller's run configuration names.
"""

from __future__ import annotations

from typing import cast

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)

_NODE = "node_contract_seams_orchestrator"


def bind_event_bus(*, group: str) -> EventBusInmemory:
    """Bind the in-memory bus for ``group`` in the contract's declared environment."""
    declared = cast(
        dict[str, str], load_packaged_contract_section(_NODE, "in_memory_bindings")
    )
    return EventBusInmemory(environment=declared["event_bus_environment"], group=group)
