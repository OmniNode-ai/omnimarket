# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three routes on the runtime's real HTTP port, over a real event bus.

POST publishes credential-registered, a stand-in for the projection consumer folds
it into the read model, GET lists it, DELETE revokes it and publishes
credential-revoked. The default profile uses the in-memory bus.
ONEX_INFERENCE_CREDENTIAL_HTTP_TEST_PROFILE=redpanda uses the canonical Kafka bus
against an isolated, auth-required broker through the existing harness. Neither
profile skips when its proof is unavailable.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

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

from omnimarket.events.topics import (
    CREDENTIAL_REGISTERED_TOPIC_V1,
    CREDENTIAL_REVOKED_TOPIC_V1,
)
from omnimarket.handlers.inference_credentials_http.bindings import (
    bind_inference_credential_ports,
    reset_inference_credential_ports,
)
from tests.integration.customer_path.redpanda_sasl_harness import (
    RedpandaSasl,
    resolve_broker,
    stop_redpanda,
)
from tests.nodes.inference_credential_http_support import (
    CONTRACTS,
    GOOD_BODY,
    KEY_A,
    KEY_B,
    ROUTE,
    FakeIdentity,
    FakeReadModel,
    FakeSecretSource,
    FakeSecretStore,
    resolved_model,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def profile_broker() -> Iterator[RedpandaSasl | None]:
    profile = os.environ.get("ONEX_INFERENCE_CREDENTIAL_HTTP_TEST_PROFILE", "in-memory")
    if profile == "in-memory":
        yield None
    elif profile == "redpanda":
        broker = resolve_broker()
        try:
            yield broker
        finally:
            stop_redpanda(broker)
    else:
        pytest.fail(f"unknown credential test profile: {profile}")


def _payload(envelope: ModelEventEnvelope[Any]) -> dict[str, Any]:
    payload = envelope.payload
    return payload if isinstance(payload, dict) else payload.model_dump(mode="json")


async def test_the_three_routes_on_the_runtime_port(
    profile_broker: RedpandaSasl | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if profile_broker is None:
        bus: Any = EventBusInmemory(environment="test", group="inference-credentials")
    else:
        for key, value in profile_broker.env().items():
            monkeypatch.setenv(key, value)
        for topic in (CREDENTIAL_REGISTERED_TOPIC_V1, CREDENTIAL_REVOKED_TOPIC_V1):
            profile_broker.create_topic(topic)
        bus = EventBusKafka(
            ModelKafkaEventBusConfig(
                bootstrap_servers=profile_broker.bootstrap, auto_offset_reset="earliest"
            ).apply_environment_overrides()
        )
    await bus.start()
    store = FakeSecretStore()
    read_model = FakeReadModel()
    reset_inference_credential_ports()
    bind_inference_credential_ports(
        identity=FakeIdentity(),
        secret_store=store,
        event_bus=bus,
        read_model=read_model,
        secret_source=FakeSecretSource(),
        model_discoverer=resolved_model,
    )
    registered: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    revoked: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    group = f"credential-proof-{uuid4().hex}"

    async def on_registered(message: Any) -> None:
        body = _payload(ModelEventEnvelope.model_validate_json(message.value))
        if body.get("tenant_id") and body["api_key_ref"] not in {
            r["api_key_ref"] for r in read_model.rows
        }:
            read_model.rows.append(
                {
                    "tenant_id": body["tenant_id"],
                    "api_key_ref": body["api_key_ref"],
                    "name": body["name"],
                    "provider": body["provider"],
                    "created_at": datetime.now(UTC),
                    "revoked_at": None,
                }
            )
        await registered.put(body)

    async def on_revoked(message: Any) -> None:
        body = _payload(ModelEventEnvelope.model_validate_json(message.value))
        for row in read_model.rows:
            if row["api_key_ref"] == body["api_key_ref"]:
                row["revoked_at"] = datetime.now(UTC)
        await revoked.put(body)

    unsubscribe_registered = await bus.subscribe(
        CREDENTIAL_REGISTERED_TOPIC_V1,
        group_id=group + "-reg",
        on_message=on_registered,
    )
    unsubscribe_revoked = await bus.subscribe(
        CREDENTIAL_REVOKED_TOPIC_V1, group_id=group + "-rev", on_message=on_revoked
    )
    manifest = discover_contracts_from_paths(list(CONTRACTS.values()))
    assert manifest.total_errors == 0
    server = ServiceHealth(runtime=MagicMock(), port=0, host="127.0.0.1")
    try:
        await server.start()
        server.attach_manifest(manifest)
        assert server._mount_task is not None
        await server._mount_task
        assert server._runner is not None
        base = f"http://127.0.0.1:{server._runner.addresses[0][1]}{ROUTE}"
        headers_a = {"x-api-key": KEY_A, "Idempotency-Key": "proof-1"}
        async with aiohttp.ClientSession() as session:
            async with session.post(base, json=GOOD_BODY) as response:
                assert response.status == 401
            async with session.post(
                base, json=GOOD_BODY, headers=headers_a
            ) as response:
                assert response.status == 201, await response.text()
                created = await response.json()
            assert GOOD_BODY["key_value"] not in str(created)
            event = await asyncio.wait_for(registered.get(), timeout=30)
            assert event["api_key_ref"] == created["api_key_ref"]
            assert store.values == {created["api_key_ref"]: GOOD_BODY["key_value"]}
            async with session.get(base, headers={"x-api-key": KEY_A}) as response:
                assert [i["api_key_ref"] for i in await response.json()] == [
                    created["api_key_ref"]
                ]
            async with session.get(base, headers={"x-api-key": KEY_B}) as response:
                assert await response.json() == []
            target = f"{base}/{created['api_key_ref']}"
            async with session.delete(target, headers={"x-api-key": KEY_B}) as response:
                assert response.status == 404
            assert created["api_key_ref"] in store.values
            async with session.delete(target, headers={"x-api-key": KEY_A}) as response:
                assert response.status == 200, await response.text()
                assert (await response.json())["status"] == "revocation-published"
            gone = await asyncio.wait_for(revoked.get(), timeout=30)
            assert gone["api_key_ref"] == created["api_key_ref"]
            assert created["api_key_ref"] not in store.values
            async with session.get(base, headers={"x-api-key": KEY_A}) as response:
                [item] = await response.json()
                assert item["revoked_at"] is not None
            async with session.get(
                f"http://127.0.0.1:{server._runner.addresses[0][1]}/health"
            ) as response:
                assert "status" in await response.json()
    finally:
        await server.stop()
        await unsubscribe_registered()
        await unsubscribe_revoked()
        await bus.close()
        reset_inference_credential_ports()
