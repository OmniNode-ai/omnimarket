# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A bus command reaches the handler through the contract's topic_match route and the answer arrives on completed.

The topics and the handler are read from the node's ``contract.yaml``; a command is published on the requested topic of an
in-memory bus and the typed answer is read from the completed topic.
"""

from __future__ import annotations

import asyncio
import json
from importlib import import_module, resources
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_tenant_topic_provision_effect.models.model_tenant_topic_provision import (
    ModelTenantTopicProvisionCommand,
)
from tests.nodes.node_tenant_topic_provision_effect.fakes import (
    TEMPLATE,
    FakeBroker,
    FakeMirrorSource,
    FakeRegistry,
    wire,
)

pytestmark = pytest.mark.unit

_NODE = "node_tenant_topic_provision_effect"


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


async def test_a_bus_command_creates_the_tenant_topics_and_answers_on_completed() -> (
    None
):
    contract = _contract()
    (route,) = contract["handler_routing"]["handlers"]
    completed = contract["topics"]["tenant_topic_provision"]["completed"]
    assert completed == "onex.evt.omnimarket.tenant-topic-provision-completed.v1"
    assert completed in contract["event_bus"]["publish_topics"]
    handler_class = getattr(
        import_module(route["handler"]["module"]), route["handler"]["name"]
    )
    broker = FakeBroker()
    registry = FakeRegistry(("acme",))
    handler = handler_class(
        mirror_source=FakeMirrorSource(),
        registry=registry,
        lister=broker,
        creator=broker,
        topic_template=TEMPLATE,
        num_partitions=1,
        replication_factor=1,
    )
    bus = EventBusInmemory(environment="test", group="tenant-topic-provision")
    await bus.start()
    done: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def on_command(message: Any) -> None:
        command = ModelTenantTopicProvisionCommand.model_validate_json(message.value)
        result = await handler.handle(command)
        await bus.publish(completed, None, result.model_dump_json().encode())

    async def observe(message: Any) -> None:
        await done.put(json.loads(message.value))

    try:
        await bus.subscribe_ungrouped(completed, observe)
        await bus.subscribe_ungrouped(route["topic"], on_command)
        payload = {"command_id": str(uuid4()), "tenant_slug": "acme"}
        await bus.publish(route["topic"], None, json.dumps(payload).encode())
        answer = await asyncio.wait_for(done.get(), timeout=30)
    finally:
        await bus.close()
    assert answer["outcome"] == "provisioned"
    assert sorted(broker.live) == sorted(wire("acme"))
