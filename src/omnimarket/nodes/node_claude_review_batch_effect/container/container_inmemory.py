# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the in-memory event bus the batch entry point runs on (OMN-21695).

The entry point never constructs the bus itself: it takes the one this module binds,
named by the node contract's ``in_memory_bindings`` section.
"""

from __future__ import annotations

from typing import cast

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)

_NODE = "node_claude_review_batch_effect"


def bind_event_bus() -> EventBusInmemory:
    """Bind the in-memory bus under the environment and group the contract declares."""
    declared = cast(
        dict[str, str], load_packaged_contract_section(_NODE, "in_memory_bindings")
    )
    return EventBusInmemory(
        environment=declared["event_bus_environment"],
        group=declared["event_bus_group"],
    )
