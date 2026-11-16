# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SEAM: a real runtime HTTP listener -> the event bus -> the consumer node's input model.

Exemplar: tests/nodes/node_github_webhook_ingress_effect/test_http_runtime_profiles.py. It differs
by reading the published command back against the REAL consumer model (the delegate-skill
orchestrator's request) instead of folding it through a dispatch engine.

The default profile injects an in-memory event bus at the publisher port.
ONEX_WORKFLOW_GATEWAY_TEST_PROFILE=redpanda publishes through the production
``KafkaWorkflowPublisher`` to an isolated, auth-required broker via the existing harness.
In both profiles the declared factory path, the ASGI adapter, the typed handlers and the
runtime's mount bridge run; only the store and the credential table are in memory.
Neither profile skips when its proof is unavailable.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.config.model_kafka_event_bus_config import (
    ModelKafkaEventBusConfig,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.services.health_checker import ServiceHealth

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers import http_app
from omnimarket.nodes.node_workflow_gateway_effect.handlers.publisher_bus import (
    BusWorkflowPublisher,
    KafkaWorkflowPublisher,
)
from tests.integration.customer_path.redpanda_sasl_harness import (
    RedpandaSasl,
    resolve_broker,
    stop_redpanda,
)
from tests.nodes.node_workflow_gateway_effect.fakes import (
    API_KEY_HEADER,
    KEY_A,
    SKILL_PAYLOAD,
    SKILL_TOPIC,
    TENANT_A,
    build_gateway,
)

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_workflow_gateway_effect/contract.yaml"
)
BROKER_ENV = "KAFKA_BOOTSTRAP_SERVERS"


@pytest.fixture(scope="module")
def profile_broker() -> Iterator[RedpandaSasl | None]:
    profile = os.environ.get("ONEX_WORKFLOW_GATEWAY_TEST_PROFILE", "in-memory")
    if profile == "in-memory":
        yield None
    elif profile == "redpanda":
        broker = resolve_broker()
        try:
            yield broker
        finally:
            stop_redpanda(broker)
    else:
        pytest.fail(f"unknown workflow gateway test profile: {profile}")


async def test_runtime_mount_publishes_a_command_the_consumer_model_accepts(
    profile_broker: RedpandaSasl | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[ModelEventEnvelope[object]] = []
    got = asyncio.Event()

    async def receive(message: Any) -> None:
        envelope = ModelEventEnvelope.model_validate_json(message.value)
        received.append(envelope)
        got.set()

    if profile_broker is None:
        bus: EventBusInmemory | EventBusKafka = EventBusInmemory(
            environment="test", group="runtime-workflow-gateway"
        )
        publisher: BusWorkflowPublisher | KafkaWorkflowPublisher = BusWorkflowPublisher(
            bus
        )
    else:
        for key, value in profile_broker.env().items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv(BROKER_ENV, profile_broker.bootstrap)
        profile_broker.create_topic(SKILL_TOPIC)
        bus = EventBusKafka(
            ModelKafkaEventBusConfig(
                bootstrap_servers=profile_broker.bootstrap,
                auto_offset_reset="earliest",
            ).apply_environment_overrides()
        )
        publisher = KafkaWorkflowPublisher(
            bootstrap_servers_env=BROKER_ENV, timeout_seconds=30
        )
    await bus.start()
    await bus.subscribe(
        SKILL_TOPIC, group_id="workflow-gateway-proof", on_message=receive
    )

    gateway = build_gateway(publisher=publisher)
    original_factory = http_app.create_app
    # Only the dependency port changes; the declared factory path, the ASGI adapter, the
    # typed handlers and the runtime mount bridge all run.
    monkeypatch.setattr(
        http_app, "create_app", lambda: original_factory(dependencies=gateway.deps)
    )
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
        base = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/v1/workflows",
                headers={API_KEY_HEADER: KEY_A},
                json={"workflow_type": "delegate-skill", "payload": SKILL_PAYLOAD},
            ) as response:
                assert response.status == 202, await response.text()
                ack = await response.json()
            await asyncio.wait_for(got.wait(), timeout=30)
            # No credential: refused by the mount before anything is read or published.
            async with session.post(
                f"{base}/v1/workflows", json={"workflow_type": "delegate-skill"}
            ) as refused:
                assert refused.status == 401
            # A sibling path outside the mount is not answered by it.
            async with session.get(f"{base}/v1/tenants/me/nothing") as other:
                assert other.status == 404
    finally:
        await server.stop()
        await bus.close()

    assert len(received) == 1
    envelope = received[0]
    assert str(envelope.correlation_id) == ack["correlation_id"]
    # The seam: what the gateway put on the wire is exactly what the consumer's own
    # request model accepts, with the tenant stamped from the credential and not the body.
    request = ModelDelegateSkillRequest.model_validate(envelope.payload)
    assert str(request.tenant_id) == str(TENANT_A)
    assert request.source == SKILL_PAYLOAD["source"]
