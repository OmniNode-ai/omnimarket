# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26474: each command arrives on its completed topic through the contract's topic_match routing, on an in-memory bus.

The wiring is read from the contract file: each subscribed topic is routed to the handler its handler_routing entry
names, the handler runs on the entry's event model, and its result is published to the topic the contract's
published_events map declares for the result class.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from tests.nodes.node_tenant_api_key_effect.support import (
    DEV_OVERLAY,
    digest,
    store_for,
)
from tests.nodes.scratch_identity_postgres import add_tenant, scratch_identity_database

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_tenant_api_key_effect/contract.yaml"
)


def _load(path: str) -> Any:
    module, _, name = path.rpartition(".")
    return getattr(importlib.import_module(module), name)


async def _wire(
    bus: EventBusInmemory, dsn: str
) -> asyncio.Queue[tuple[str, dict[str, Any]]]:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    completed_for = {e["event_type"]: e["topic"] for e in contract["published_events"]}
    answers: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    for entry in contract["handler_routing"]["handlers"]:
        handler_class = _load(
            f"{entry['handler']['module']}.{entry['handler']['name']}"
        )
        kwargs: dict[str, Any] = {"store": store_for(dsn)}
        if "overlay" in inspect.signature(handler_class).parameters:
            kwargs["overlay"] = DEV_OVERLAY
        handler = handler_class(**kwargs)
        model = _load(entry["event_model"])

        async def on_command(
            message: Any, handler: Any = handler, model: Any = model
        ) -> None:
            result = await handler.handle(model.model_validate_json(message.value))
            await bus.publish(
                completed_for[type(result).__name__],
                None,
                result.model_dump_json().encode(),
            )

        await bus.subscribe_ungrouped(entry["topic"], on_command)
    for topic in completed_for.values():

        async def observe(message: Any, topic: str = topic) -> None:
            await answers.put((topic, json.loads(message.value)))

        await bus.subscribe_ungrouped(topic, observe)
    return answers


async def test_register_and_revoke_each_arrive_on_their_completed_topic_through_topic_match() -> (
    None
):
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        bus = EventBusInmemory(environment="test", group="tenant-api-key")
        await bus.start()
        try:
            answers = await _wire(bus, dsn)
            register_id = uuid4()
            await bus.publish(
                "onex.cmd.omnimarket.tenant-api-key-register-requested.v1",
                None,
                json.dumps(
                    {
                        "correlation_id": str(register_id),
                        "tenant_id": str(tenant),
                        "name": "laptop",
                        "key_hash": digest("bus-key"),
                        "expires_at": None,
                    }
                ).encode(),
            )
            topic, registered = await asyncio.wait_for(answers.get(), timeout=30)
            assert topic == "onex.evt.omnimarket.tenant-api-key-register-completed.v1"
            assert registered["outcome"] == "registered"
            assert registered["correlation_id"] == str(register_id)
            revoke_id = uuid4()
            await bus.publish(
                "onex.cmd.omnimarket.tenant-api-key-revoke-requested.v1",
                None,
                json.dumps(
                    {
                        "correlation_id": str(revoke_id),
                        "tenant_id": str(tenant),
                        "credential_id": registered["credential_id"],
                    }
                ).encode(),
            )
            topic, revoked = await asyncio.wait_for(answers.get(), timeout=30)
            assert topic == "onex.evt.omnimarket.tenant-api-key-revoke-completed.v1"
            assert revoked["outcome"] == "revoked"
            assert revoked["correlation_id"] == str(revoke_id)
        finally:
            await bus.close()
