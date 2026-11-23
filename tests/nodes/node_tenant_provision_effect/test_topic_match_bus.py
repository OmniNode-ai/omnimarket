# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""(e) Each refusal arrives on its ``-completed`` topic with its ``outcome``, through the contract's ``topic_match`` routing.

The routing table and the topics are read from the node's ``contract.yaml``; the handlers are built from the contract's
own module and class names and subscribed on an in-memory bus, the way the runtime's contract loader wires them. A request
is published on a ``-requested`` topic and the answer is read from the ``-completed`` topic.
"""

from __future__ import annotations

import asyncio
import json
from importlib import import_module, resources
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    TenantIdentityPostgres,
)

from .postgres_support import identity_tables

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_NODE = "node_tenant_provision_effect"


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


async def _answer(
    pool: asyncpg.Pool, operation: str, request: dict[str, Any]
) -> dict[str, Any]:
    contract = _contract()
    assert contract["handler_routing"]["routing_strategy"] == "topic_match"
    entries = {e["operation"]: e for e in contract["handler_routing"]["handlers"]}
    entry = entries[operation]
    completed = contract["topics"][operation]["completed"]
    handler_class = getattr(
        import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    handler = handler_class(store=TenantIdentityPostgres(pool=pool))
    request_model = _model_class(entry["event_model"])

    bus = EventBusInmemory(environment="test", group="tenant-provision")
    await bus.start()
    done: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def on_command(message: Any) -> None:
        result = await handler.handle(request_model.model_validate_json(message.value))
        await bus.publish(completed, None, result.model_dump_json().encode())

    async def observe(message: Any) -> None:
        await done.put(json.loads(message.value))

    try:
        await bus.subscribe_ungrouped(completed, observe)
        await bus.subscribe_ungrouped(entry["topic"], on_command)
        await bus.publish(entry["topic"], None, _dump(request))
        return await asyncio.wait_for(done.get(), timeout=30)
    finally:
        await bus.close()


def _model_class(dotted: str) -> Any:
    module, _, name = dotted.rpartition(".")
    return getattr(import_module(module), name)


def _dump(request: dict[str, Any]) -> bytes:
    return json.dumps(request).encode()


async def test_each_provision_refusal_arrives_on_completed_with_its_outcome(
    port: int,
) -> None:
    async with identity_tables(port) as (admin, pool):
        await admin.execute(
            "INSERT INTO tenants (tenant_slug, slug, owner_email) VALUES ('taken-slug', 'taken-slug', 'o@example.invalid')"
        )
        created_id = str(uuid4())
        first = await _answer(
            pool,
            "tenant_provision",
            {"command_id": created_id, "tenant_slug": "fresh-one"},
        )
        assert first["outcome"] == "created"
        cases = {
            "refused_owner_not_supported": {
                "command_id": str(uuid4()),
                "tenant_slug": "owned-one",
                "owner_email": "x@example.invalid",
            },
            "refused_invalid_slug": {
                "command_id": str(uuid4()),
                "tenant_slug": "Bad_Slug",
            },
            "refused_slug_taken": {
                "command_id": str(uuid4()),
                "tenant_slug": "taken-slug",
            },
            "refused_command_id_reused": {
                "command_id": created_id,
                "tenant_slug": "another-one",
            },
        }
        for outcome, request in cases.items():
            answer = await _answer(pool, "tenant_provision", request)
            assert answer["outcome"] == outcome
            assert answer["command_id"] == request["command_id"]
        assert int(await admin.fetchval("SELECT count(*) FROM tenants")) == 2


async def test_a_read_refusal_and_a_miss_arrive_on_completed(port: int) -> None:
    async with identity_tables(port) as (_admin, pool):
        refused = await _answer(pool, "tenant_read", {"command_id": str(uuid4())})
        missing = await _answer(
            pool,
            "tenant_read",
            {"command_id": str(uuid4()), "tenant_slug": "nobody-here"},
        )
        assert refused["outcome"] == "refused_invalid_request"
        assert missing["outcome"] == "not_found"
