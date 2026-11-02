# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host contracts on the declared, authenticated Redpanda lane."""

import os
from contextlib import nullcontext
from pathlib import Path

import pytest

from tests.bus_lane_guard import declared_test_lane
from tests.unit.nodes.node_host_profile_compute.test_host_declaration_bus import (
    NODES,
    exercise,
)

pytestmark = [pytest.mark.integration, pytest.mark.kafka]


@pytest.mark.parametrize("node", NODES)
@pytest.mark.parametrize("source", ["inmemory", "overlay"])
async def test_host_declaration_runtime_redpanda_two_profiles(
    node: str,
    source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Use the same declared, authenticated lane transport as lab-work and the dev runtime.
    # The generic Kafka test fixture is plaintext; it cannot exercise a SASL deployment.
    from omnibase_infra.cli.delegate_lane import resolve_lane_selection_at
    from omnibase_infra.cli.delegate_lane_credentials import (
        resolve_lane_client_transport_for,
    )
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.lane_client_transport_binding import (
        bind_lane_client_transport,
    )

    target = resolve_lane_selection_at(
        declared_test_lane(), Path(os.environ["OMNI_HOME"])
    )
    transport = resolve_lane_client_transport_for(
        lane_target=target, onex_home=Path.home() / ".onex", environ=os.environ
    )
    with (
        bind_lane_client_transport(transport)
        if transport is not None
        else nullcontext()
    ):
        bus = EventBusKafka.from_bootstrap(target.bootstrap_servers)
        await bus.start()
        try:
            await exercise(node, source, tmp_path, monkeypatch, bus)
        finally:
            await bus.close()
