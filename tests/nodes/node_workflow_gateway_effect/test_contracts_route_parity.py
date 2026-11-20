# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GET /v1/workflows/contracts keeps the behaviour of the hosted onex-api route.

The hosted onex-api route (the list_workflow_contracts operation of its workflows router) authenticates the
caller on the same terms as a submission, then lists the SERVABLE catalog to every authenticated caller: enabled
entries only, sorted by workflow type, each payload schema verbatim. It never filters by tenant, and the catalog
model carries no tenant field to filter on. These tests pin that parity so a later change cannot silently narrow the
answer to one tenant or widen it to fenced types or to unauthenticated callers.

Exemplar: test_error_chain_workflow_gateway.py in this directory (mounted app through httpx).
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest

from omnimarket.nodes.node_workflow_gateway_effect.handlers.handler_workflow_contracts_read import (
    HandlerWorkflowContractsRead,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.http_app import create_app
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_catalog import (
    WorkflowCatalog,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_http import (
    ModelWorkflowCaller,
    ModelWorkflowContractsCommand,
    ModelWorkflowHttpResponse,
)
from tests.nodes.node_workflow_gateway_effect.fakes import (
    API_KEY_HEADER,
    KEY_A,
    KEY_B,
    TENANT_A,
    TENANT_B,
    Gateway,
    build_gateway,
    sample_catalog,
)

pytestmark = pytest.mark.unit


def _client(gateway: Gateway) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(dependencies=gateway.deps))
    return httpx.AsyncClient(transport=transport, base_url="http://runtime")


async def _list_as(client: httpx.AsyncClient, key: str) -> httpx.Response:
    return await client.get("/v1/workflows/contracts", headers={API_KEY_HEADER: key})


async def test_every_authenticated_tenant_reads_the_same_servable_catalog() -> None:
    gateway = build_gateway()
    async with _client(gateway) as client:
        tenant_a = await _list_as(client, KEY_A)
        tenant_b = await _list_as(client, KEY_B)
    assert tenant_a.status_code == tenant_b.status_code == 200
    assert tenant_a.json() == tenant_b.json()
    assert gateway.publisher.published == []


async def test_the_catalog_is_enabled_entries_sorted_with_the_schema_verbatim() -> None:
    gateway = build_gateway()
    async with _client(gateway) as client:
        body = (await _list_as(client, KEY_A)).json()
    catalog = sample_catalog()
    servable = sorted(
        (entry for entry in catalog.workflows if entry.enabled),
        key=lambda entry: entry.workflow_type,
    )
    fenced = {entry.workflow_type for entry in catalog.workflows if not entry.enabled}
    assert fenced, "the fixture must carry a fenced entry for this pin to mean anything"
    assert body == {
        "contracts": [
            {
                "workflow_type": entry.workflow_type,
                "contract_id": entry.contract_id,
                "payload_schema": entry.payload_schema,
            }
            for entry in servable
        ]
    }
    assert {c["workflow_type"] for c in body["contracts"]}.isdisjoint(fenced)


async def test_the_route_never_answers_a_catalog_without_a_credential() -> None:
    gateway = build_gateway()
    async with _client(gateway) as client:
        absent = await client.get("/v1/workflows/contracts")
        invalid = await _list_as(client, "onxk_not_a_key")
    assert absent.status_code == 401
    assert absent.json() == {
        "detail": "Unauthorized",
        "code": "auth.credential.absent",
    }
    assert invalid.status_code == 401
    assert invalid.json() == {
        "detail": "Unauthorized",
        "code": "auth.credential.invalid",
    }
    assert "contracts" not in absent.json() | invalid.json()


class _TenantScopedContractsRead(HandlerWorkflowContractsRead):
    """A deliberately narrowed handler: the control that proves the parity pin can fail."""

    async def handle(
        self, request: ModelWorkflowContractsCommand
    ) -> ModelWorkflowHttpResponse:
        answer = await super().handle(request)
        contracts = answer.body["contracts"]
        keep = contracts[:1] if request.caller.tenant_id == TENANT_B else contracts
        return ModelWorkflowHttpResponse(
            status_code=answer.status_code, body={"contracts": keep}
        )


def _caller(tenant: UUID, slug: str) -> ModelWorkflowCaller:
    return ModelWorkflowCaller(
        tenant_id=tenant,
        tenant_slug=slug,
        principal_id=str(tenant),
        authenticated_user_id=slug,
    )


async def _answers(handler: HandlerWorkflowContractsRead) -> list[object]:
    callers = (_caller(TENANT_A, "tenant-a"), _caller(TENANT_B, "tenant-b"))
    return [
        (await handler.handle(ModelWorkflowContractsCommand(caller=c))).body
        for c in callers
    ]


async def test_control_a_tenant_scoped_handler_is_told_apart_from_the_real_one() -> (
    None
):
    catalog = WorkflowCatalog(sample_catalog())
    real = await _answers(HandlerWorkflowContractsRead(catalog=catalog))
    narrowed = await _answers(_TenantScopedContractsRead(catalog=catalog))
    assert real[0] == real[1]
    assert narrowed[0] != narrowed[1]
