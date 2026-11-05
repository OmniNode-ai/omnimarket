# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real runtime HTTP listener serves the route from the contract's mount.

The default profile binds the in-memory fakes at the declared DI ports.
ONEX_HOOK_EVENTS_HTTP_TEST_PROFILE=redpanda additionally starts an isolated,
auth-required broker through the existing harness and runs the same mount under
the canonical Kafka event-bus configuration. This route consumes and produces no
topic, so the broker proves the mount boots and serves beside the real bus
adapter; it does not carry the read. Neither profile skips when its proof is
unavailable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
import pytest
import yaml
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.config.model_kafka_event_bus_config import (
    ModelKafkaEventBusConfig,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.services.health_checker import ServiceHealth

from omnimarket.nodes.node_hook_events_by_correlation_read_effect.handlers import (
    http_app,
)
from tests.integration.customer_path.redpanda_sasl_harness import (
    RedpandaSasl,
    resolve_broker,
    stop_redpanda,
)
from tests.nodes.node_hook_events_by_correlation_read_effect.support import (
    CID,
    KEY_A,
    KEY_B,
    ROUTE,
    SLUG_A,
    SLUG_B,
    FakeIdentity,
    FakeReader,
    row,
)

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_hook_events_by_correlation_read_effect/contract.yaml"
)


@pytest.fixture(scope="module")
def profile_broker() -> Iterator[RedpandaSasl | None]:
    profile = os.environ.get("ONEX_HOOK_EVENTS_HTTP_TEST_PROFILE", "in-memory")
    if profile == "in-memory":
        yield None
    elif profile == "redpanda":
        broker = resolve_broker()
        try:
            yield broker
        finally:
            stop_redpanda(broker)
    else:
        pytest.fail(f"unknown hook-events test profile: {profile}")


async def test_runtime_mount_serves_the_route_in_the_profile(
    profile_broker, monkeypatch
):
    reader = FakeReader(
        [row(SLUG_A, event_id="alpha-1"), row(SLUG_B, event_id="bravo-1")]
    )
    identity = FakeIdentity()
    original_factory = http_app.create_app
    # Only the DI ports change. The ASGI adapter, typed handler and the
    # production runtime mount bridge run in both profiles.
    monkeypatch.setattr(
        http_app,
        "create_app",
        lambda: original_factory(reader=reader, identity=identity),
    )
    bus = None
    if profile_broker is not None:
        for key, value in profile_broker.env().items():
            monkeypatch.setenv(key, value)
        bus = EventBusKafka(
            ModelKafkaEventBusConfig(
                bootstrap_servers=profile_broker.bootstrap
            ).apply_environment_overrides()
        )
        await bus.start()

    manifest = discover_contracts_from_paths([CONTRACT])
    assert manifest.total_errors == 0
    server = ServiceHealth(runtime=MagicMock(), port=0, host="127.0.0.1")
    try:
        await server.start()
        server.attach_manifest(manifest)
        assert server._mount_task is not None
        await server._mount_task
        assert server._runner is not None
        port = server._runner.addresses[0][1]
        url = f"http://127.0.0.1:{port}{ROUTE}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"correlation_id": CID}) as response:
                assert response.status == 401
                assert (await response.json())["code"] == "auth.credential.absent"
            assert reader.calls == []
            async with session.get(
                url, params={"correlation_id": CID}, headers={"x-api-key": KEY_A}
            ) as response:
                assert response.status == 200
                body = await response.json()
            assert body["data_state"] == "found"
            assert [r["event_id"] for r in body["rows"]] == ["alpha-1"]
            async with session.get(
                url, params={"correlation_id": CID}, headers={"x-api-key": KEY_B}
            ) as response:
                body = await response.json()
            assert [r["event_id"] for r in body["rows"]] == ["bravo-1"]
            async with session.get(f"http://127.0.0.1:{port}/health") as response:
                assert "status" in await response.json()
    finally:
        await server.stop()
        if bus is not None:
            await bus.close()


async def test_mount_prefix_equals_the_route_path_and_never_covers_a_parent():
    contract = yaml.safe_load(CONTRACT.read_text())
    prefix = contract["http_mount"]["path_prefix"]
    assert prefix == contract["http_route"]["path"] == ROUTE
    assert prefix not in ("/", "/v1", "/v1/projections")
    assert not prefix.endswith("/")
    assert discover_contracts_from_paths([CONTRACT]).total_errors == 0
