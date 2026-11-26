# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime must bind the gateway's concurrency on its actual command topic."""

from importlib.resources import as_file, files

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine


@pytest.mark.asyncio
async def test_gateway_command_wires_bounded_concurrent_consumption() -> None:
    resource = files("omnimarket.nodes.node_github_repo_gateway_effect")
    with as_file(resource.joinpath("contract.yaml")) as path:
        manifest = discover_contracts_from_paths([path])
        assert not manifest.errors
        (contract,) = manifest.contracts
        (command,) = contract.event_bus.subscribe_topics

        bus = EventBusInmemory(environment="local", group="gateway-concurrency-test")
        await bus.start()
        try:
            report = await wire_from_manifest(
                manifest, MessageDispatchEngine(), event_bus=bus, environment="local"
            )
            assert report.total_wired == 1
            assert report.total_failed == 0
            bounds = [
                bound
                for (topic, _group), bound in bus.consume_concurrency.items()
                if topic == command
            ]
        finally:
            await bus.close()

    assert bounds == [8], "gateway reads otherwise wait behind earlier records"
