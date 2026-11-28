# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A repeated correlation_id on POST /v1/workflows through the mounted route (in-memory store).

The same decisions are proven on real Postgres in
test_integration_real_postgres_correlation_idempotency.py.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from omnimarket.nodes.node_workflow_gateway_effect.handlers.http_app import create_app
from tests.nodes.node_workflow_gateway_effect.fakes import (
    API_KEY_HEADER,
    KEY_A,
    KEY_B,
    SKILL_PAYLOAD,
    TENANT_A,
    Gateway,
    build_gateway,
)

pytestmark = pytest.mark.unit


def _client(gateway: Gateway) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(dependencies=gateway.deps))
    return httpx.AsyncClient(transport=transport, base_url="http://runtime")


def _body(corr: UUID, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "workflow_type": "delegate-skill",
        "correlation_id": str(corr),
        "payload": payload or SKILL_PAYLOAD,
    }


async def _post(client: httpx.AsyncClient, key: str, body: dict[str, Any]) -> Any:
    return await client.post("/v1/workflows", json=body, headers={API_KEY_HEADER: key})


async def test_repeat_with_same_payload_returns_the_original_job() -> None:
    gateway, corr = build_gateway(), uuid4()
    async with _client(gateway) as client:
        first = await _post(client, KEY_A, _body(corr))
        second = await _post(client, KEY_A, _body(corr))
    assert second.status_code == 202
    assert second.json()["workflow_id"] == first.json()["workflow_id"]
    assert len(gateway.publisher.published) == 1


async def test_repeat_with_a_different_payload_is_a_typed_conflict() -> None:
    gateway, corr = build_gateway(), uuid4()
    async with _client(gateway) as client:
        await _post(client, KEY_A, _body(corr))
        clash = await _post(
            client, KEY_A, _body(corr, {**SKILL_PAYLOAD, "prompt": "other"})
        )
    assert clash.status_code == 409
    assert clash.json()["code"] == "correlation_id_conflict"
    assert "other" not in clash.text


async def test_another_tenant_with_the_same_number_is_independent() -> None:
    gateway, corr = build_gateway(), uuid4()
    async with _client(gateway) as client:
        a = await _post(client, KEY_A, _body(corr))
        b = await _post(client, KEY_B, _body(corr))
    assert b.status_code == 202
    assert b.json()["workflow_id"] != a.json()["workflow_id"]


async def test_concurrent_repeats_make_one_job() -> None:
    gateway, corr = build_gateway(), uuid4()
    async with _client(gateway) as client:
        results = await asyncio.gather(
            *(_post(client, KEY_A, _body(corr)) for _ in range(10))
        )
    assert len({r.json()["workflow_id"] for r in results}) == 1
    assert len(gateway.publisher.published) == 1


async def test_repeats_leave_one_row_the_terminal_writer_can_finish() -> None:
    gateway, corr = build_gateway(), uuid4()
    async with _client(gateway) as client:
        first = await _post(client, KEY_A, _body(corr))
        await _post(client, KEY_A, _body(corr))
    workflow_id = UUID(first.json()["workflow_id"])
    gateway.store.apply_terminal(workflow_id, status="completed")
    row = await gateway.store.fetch(workflow_id=workflow_id, tenant_id=TENANT_A)
    assert row is not None
    assert row.status == "completed"
    assert len(gateway.store._rows) == 1


async def test_two_tenants_with_one_number_publish_distinct_wire_correlation_ids() -> (
    None
):
    """The runtime keys a job by the wire correlation_id alone, so tenant B's same number
    must not reach it as tenant A's number: it would be dropped as a repeat and stay published."""
    gateway, corr = build_gateway(), uuid4()
    async with _client(gateway) as client:
        a = await _post(client, KEY_A, _body(corr))
        b = await _post(client, KEY_B, _body(corr))
        a_again = await _post(client, KEY_A, _body(corr))
    assert a.status_code == b.status_code == 202
    assert a.json()["correlation_id"] == b.json()["correlation_id"] == str(corr)
    assert a_again.json()["workflow_id"] == a.json()["workflow_id"]
    wire_ids = [wire["correlation_id"] for _, _, wire in gateway.publisher.published]
    assert len(wire_ids) == 2
    assert len(set(wire_ids)) == 2
    assert "tenant" not in b.text.lower() or str(TENANT_A) not in b.text
