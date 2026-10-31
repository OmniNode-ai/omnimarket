# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the in-memory transports omnimarket entry points run on (OMN-21623).

A caller never constructs an in-memory event bus or database adapter itself, not
even as a fallback: it takes the one this module binds. The contracts-and-DI check
admits construction in ``container_*.py`` files only.
"""

from __future__ import annotations

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.event_bus_inmemory import (
    EventBusInmemory as InfraEventBusInmemory,
)

from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter


def bind_core_event_bus(*, environment: str, group: str) -> EventBusInmemory:
    """Bind core's in-memory event bus; ``environment`` and ``group`` name its identity."""
    return EventBusInmemory(environment=environment, group=group)


def bind_infra_event_bus(*, environment: str, group: str) -> InfraEventBusInmemory:
    """Bind the infrastructure package's in-memory event bus."""
    return InfraEventBusInmemory(environment=environment, group=group)


def bind_database_adapter() -> InmemoryDatabaseAdapter:
    """Bind the in-memory database adapter a projection runs on when no real one is injected."""
    return InmemoryDatabaseAdapter()
