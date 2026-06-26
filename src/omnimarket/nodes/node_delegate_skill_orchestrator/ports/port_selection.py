# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transport-aware delegation dispatch-port selection.

The ports package — not the domain handler — owns the knowledge of which
transport a runtime bus represents. The handler asks this factory for a port
given the bus it was injected with; the factory returns the dispatch port whose
execution model matches that bus.

Two execution models exist (OMN-13601):

* In-memory single-process runtime (``onex delegate --bus inmemory``): the
  orchestrator is the only node booted; there is no co-deployed downstream
  delegation consumer of the runtime command topic. Publishing a runtime command
  and awaiting a terminal event would time out at the orchestrator's wait ceiling
  with no evidence row. The local in-process port instead resolves routing, runs
  the canonical LLM effect, applies the quality gate, and writes the sqlite
  evidence row — all in-process — so the bus-less CLI path is end-to-end
  functional.

* External broker runtime (deployed lanes): the full multi-node runtime,
  including the downstream delegation consumer, is co-deployed. The orchestrator
  publishes the runtime command and awaits the correlated terminal over the bus.
"""

from __future__ import annotations

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    ProtocolDelegationDispatchPort,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
    ProtocolDelegationEventBus,
    RuntimeDelegationDispatchPort,
)


def select_delegation_dispatch_port(
    event_bus: ProtocolDelegationEventBus | None,
) -> ProtocolDelegationDispatchPort:
    """Return the dispatch port whose execution model matches ``event_bus``.

    * ``None`` or an in-memory bus → :class:`LocalDelegationDispatchPort`
      (in-process effect + quality gate + sqlite evidence row).
    * Any other (external broker) bus → :class:`RuntimeDelegationDispatchPort`
      (publish runtime command, await terminal over the bus).
    """
    if event_bus is None or isinstance(event_bus, EventBusInmemory):
        # Imported lazily so compile-only / payload-building paths and unit tests
        # do not require the local effect + projection stack.
        from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
            LocalDelegationDispatchPort,
        )

        return LocalDelegationDispatchPort()
    return RuntimeDelegationDispatchPort(event_bus=event_bus)


__all__ = ["select_delegation_dispatch_port"]
