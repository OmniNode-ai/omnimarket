# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The flush node wires through the infra runtime's own auto-wiring and is not quarantined (OMN-24562).

The composed runtime boots with ``ONEX_WIRING_STRICT_MODE=1``: a handler the runtime cannot build with no arguments is
quarantined as ``unresolvable_handler`` and the kernel exits, then restarts, which reads as a crash loop. Measured on the
composed image at head 7d0b54eeb4, where ``HandlerTenantOutboxFlush`` required ``['store', 'publisher', 'topic']``.
This test wires only this node's contract through ``wire_from_manifest`` with a real container and an in-memory bus, and
fails on any failed or quarantined outcome for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.container import ModelONEXContainer
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)
from omnibase_core.protocols.event_bus.protocol_event_bus_subscriber import (
    ProtocolEventBusSubscriber,
)
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.auto_wiring.report import EnumWiringOutcome
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.topology import load_topology_profile

import omnimarket.nodes.node_tenant_outbox_flush_effect as node_package

pytestmark = pytest.mark.unit

_NODE = "node_tenant_outbox_flush_effect"
_PROFILE = "local"


@pytest.mark.asyncio
async def test_the_flush_node_wires_strict_without_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_path = Path(node_package.__file__).parent / "contract.yaml"
    discovered = discover_contracts_from_paths([contract_path])
    assert not discovered.errors, discovered.errors
    assert [c.name for c in discovered.contracts] == [_NODE]
    monkeypatch.setenv("ONEX_WIRING_STRICT_MODE", "1")
    monkeypatch.setenv("ONEX_DATABASE_TOPOLOGY_PROFILE", _PROFILE)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9")
    topology = load_topology_profile(_PROFILE)
    bus = EventBusInmemory()
    container = ModelONEXContainer()
    await container.service_registry.register_instance(ProtocolEventBusPublisher, bus)
    await container.service_registry.register_instance(ProtocolEventBusSubscriber, bus)
    try:
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(discovered.contracts)),
            MessageDispatchEngine(),
            event_bus=bus,
            environment=_PROFILE,
            container=container,
            topology=topology,
        )
    finally:
        await bus.close()
    outcomes = {r.contract_name: r.outcome for r in report.results}
    assert outcomes.get(_NODE) == EnumWiringOutcome.WIRED, outcomes
    assert not report.quarantined_handlers, [
        (q.handler_name, q.reason) for q in report.quarantined_handlers
    ]
