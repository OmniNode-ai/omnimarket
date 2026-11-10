# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The savings read served by the runtime's real listener, in both test profiles.

The default profile binds the in-memory identity resolver and projection reader.
ONEX_TENANT_READ_TEST_PROFILE=redpanda runs the same listener beside an isolated,
auth-required broker through the existing harness: the reads publish nothing, so
the broker profile proves the mount serves while the real bus adapter is live.
Neither profile skips when its proof is unavailable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.config.model_kafka_event_bus_config import (
    ModelKafkaEventBusConfig,
)

from tests.integration.customer_path.redpanda_sasl_harness import (
    RedpandaSasl,
    resolve_broker,
    stop_redpanda,
)
from tests.nodes.tenant_read_support import probe_runtime_listener

pytestmark = pytest.mark.unit
NODES = ["node_tenant_savings_read_effect"]


@pytest.fixture(scope="module")
def profile_broker() -> Iterator[RedpandaSasl | None]:
    profile = os.environ.get("ONEX_TENANT_READ_TEST_PROFILE", "in-memory")
    if profile == "in-memory":
        yield None
    elif profile == "redpanda":
        broker = resolve_broker()
        try:
            yield broker
        finally:
            stop_redpanda(broker)
    else:
        pytest.fail(f"unknown tenant read test profile: {profile}")


async def test_runtime_listener_serves_the_mount(profile_broker, monkeypatch) -> None:
    if profile_broker is None:
        await probe_runtime_listener(NODES)
        return
    for key, value in profile_broker.env().items():
        monkeypatch.setenv(key, value)
    bus = EventBusKafka(
        ModelKafkaEventBusConfig(
            bootstrap_servers=profile_broker.bootstrap
        ).apply_environment_overrides()
    )
    await bus.start()
    try:
        await probe_runtime_listener(NODES)
    finally:
        await bus.close()
