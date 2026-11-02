# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A real runtime HTTP listener -> event bus -> the existing webhook consumer.

The default profile injects an in-memory bus at the declared publisher port.
ONEX_WEBHOOK_HTTP_TEST_PROFILE=redpanda uses the production no-argument ASGI
factory and an isolated, auth-required broker through the existing harness.
Neither profile skips when its proof is unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import aiohttp
import pytest
import yaml
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.dispatch.model_dispatch_route import ModelDispatchRoute
from omnibase_core.models.dispatch.model_handler_ref import ModelHandlerRef
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.enums import EnumDispatchStatus, EnumMessageCategory
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.config.model_kafka_event_bus_config import (
    ModelKafkaEventBusConfig,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.services.health_checker import ServiceHealth

from omnimarket.nodes.node_github_webhook_ingress_effect.handlers import (
    HandlerGitHubWebhookIngress,
    http_app,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.handlers.http_dependencies import (
    BusWebhookPublisher,
    load_http_config,
)
from tests.integration.customer_path.redpanda_sasl_harness import (
    RedpandaSasl,
    resolve_broker,
    stop_redpanda,
)

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_github_webhook_ingress_effect/contract.yaml"
)
SIGNING_KEY = b"runtime-mount-fixture-key"


@pytest.fixture(scope="module")
def profile_broker() -> Iterator[RedpandaSasl | None]:
    profile = os.environ.get("ONEX_WEBHOOK_HTTP_TEST_PROFILE", "in-memory")
    if profile == "in-memory":
        yield None
    elif profile == "redpanda":
        broker = resolve_broker()
        try:
            yield broker
        finally:
            stop_redpanda(broker)
    else:
        pytest.fail(f"unknown webhook test profile: {profile}")


async def test_runtime_mount_publishes_and_existing_node_folds_delivery(
    profile_broker, monkeypatch
):
    config = load_http_config()
    for key, value in {
        "secret": SIGNING_KEY.decode(),
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "tenant_slug": "fixture-tenant",
        "principal_id": "fixture-principal",
    }.items():
        monkeypatch.setenv(config.environment[key], value)

    if profile_broker is None:
        bus = EventBusInmemory(environment="test", group="runtime-webhook")
        original_factory = http_app.create_app
        # Only the DI publisher port changes. The actual ASGI adapter, typed
        # handler and production runtime mount bridge run in both profiles.
        monkeypatch.setattr(
            http_app,
            "create_app",
            lambda: original_factory(publisher=BusWebhookPublisher(bus)),
        )
    else:
        for key, value in profile_broker.env().items():
            monkeypatch.setenv(key, value)
        profile_broker.create_topic(config.delivery_topic)
        bus = EventBusKafka(
            ModelKafkaEventBusConfig(
                bootstrap_servers=profile_broker.bootstrap,
                auto_offset_reset="earliest",
            ).apply_environment_overrides()
        )
    await bus.start()
    contract = yaml.safe_load(CONTRACT.read_text())
    entry = contract["handler_routing"]["handlers"][0]
    dispatch = _make_dispatch_callback(
        HandlerGitHubWebhookIngress(webhook_secret=SIGNING_KEY.decode()),
        ModelHandlerRef(**entry["event_model"]),
        handler_node_kind=EnumNodeKind.EFFECT,
    )
    engine = MessageDispatchEngine()
    engine.register_dispatcher(
        dispatcher_id="webhook-http-proof",
        dispatcher=dispatch,
        category=EnumMessageCategory.COMMAND,
        message_types={entry["event_model"]["name"], entry["event_type"]},
    )
    engine.register_route(
        ModelDispatchRoute(
            route_id="webhook-http-proof-route",
            topic_pattern=config.delivery_topic,
            message_category=EnumMessageCategory.COMMAND,
            handler_id="webhook-http-proof",
        )
    )
    engine.freeze()
    delivery_id = str(uuid4())
    observations = []
    consumer_errors = []
    consumed = asyncio.Event()

    async def receive(message):
        envelope = ModelEventEnvelope.model_validate_json(message.value)
        if str(envelope.correlation_id) == delivery_id:
            try:
                assert str(message.headers.correlation_id) == delivery_id
                assert message.headers.message_id == envelope.envelope_id
                result = await engine.dispatch(
                    topic=config.delivery_topic, envelope=envelope
                )
                assert result.status == EnumDispatchStatus.SUCCESS, result.error_message
                observations.extend(result.output_events)
            except Exception as exc:
                consumer_errors.append(exc)
            finally:
                consumed.set()

    unsubscribe = await bus.subscribe(
        config.delivery_topic,
        group_id=f"webhook-proof-{uuid4().hex}",
        on_message=receive,
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
        body = json.dumps(
            {
                "action": "closed",
                "repository": {"full_name": "example/widgets"},
                "pull_request": {
                    "number": 7,
                    "state": "closed",
                    "merged": True,
                    "draft": False,
                    "title": "Example change",
                    "merged_at": "2026-11-01T12:00:00Z",
                    "closed_at": "2026-11-01T12:00:00Z",
                    "merge_commit_sha": "c" * 40,
                    "head": {"ref": "feature/example", "sha": "a" * 40},
                    "base": {"ref": "dev"},
                },
            }
        ).encode()
        headers = {
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": "sha256="
            + hmac.new(SIGNING_KEY, body, hashlib.sha256).hexdigest(),
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}{config.path}", data=body, headers=headers
            ) as response:
                assert response.status == 202, await response.text()
                assert await response.json() == {"ok": True, "delivery_id": delivery_id}
            await asyncio.wait_for(consumed.wait(), timeout=30)
            assert not consumer_errors, consumer_errors
            assert len(observations) == 2
            assert {e.repo for e in observations} == {"example/widgets"}
            assert {e.pr_number for e in observations} == {7}
            assert {e.topic for e in observations} == {
                "onex.evt.github.pr-status.v1",
                "onex.evt.github.pr-merged.v1",
            }
            # A failed signature and a ping must never add a second command.
            async with session.post(
                f"http://127.0.0.1:{port}{config.path}",
                data=body,
                headers=headers | {"X-Hub-Signature-256": "sha256=bad"},
            ) as response:
                assert response.status == 401
            async with session.post(
                f"http://127.0.0.1:{port}{config.path}",
                data=body,
                headers=headers | {"X-GitHub-Event": "ping"},
            ) as response:
                assert response.status == 200
            async with session.get(f"http://127.0.0.1:{port}/health") as response:
                assert "status" in await response.json()
    finally:
        await server.stop()
        await unsubscribe()
        await bus.close()
