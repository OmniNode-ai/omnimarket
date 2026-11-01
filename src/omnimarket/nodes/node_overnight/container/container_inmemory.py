# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the in-memory event bus the overnight handler drives the build loop on (OMN-21695).

A caller never constructs the bus itself: it takes the one this module binds.
"""

from __future__ import annotations

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory


def bind_event_bus() -> EventBusInmemory:
    """Bind a fresh in-memory event bus."""
    return EventBusInmemory()
