# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: the savings route refuses before any projection read, and tenants stay apart."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from omnimarket.handlers.container.container_tenant_read_dependencies import (
    InMemoryTenantProjectionReader,
)
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.models.model_tenant_delegation_projection import (
    ModelTenantSavingsAggregate,
)
from tests.nodes.tenant_read_support import (
    KEY_A,
    KEY_B,
    TENANT_A,
    TENANT_B,
    client_for,
    installed,
    mounted_factory,
    overlay,
    seeded_identity,
)

pytestmark = pytest.mark.unit
NODE = "node_tenant_savings_read_effect"
PATH = "/v1/tenants/me/savings"


def _reader() -> InMemoryTenantProjectionReader:
    return InMemoryTenantProjectionReader(
        savings={
            TENANT_A: ModelTenantSavingsAggregate(
                total_saved_usd=11.0, total_spend_usd=1.0, delegation_runs=4
            ),
            TENANT_B: ModelTenantSavingsAggregate(
                total_saved_usd=222.0, total_spend_usd=2.0, delegation_runs=9
            ),
        }
    )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-api-key": "not-a-seeded-key"},
        {"authorization": "Bearer header.payload.sig"},
    ],
)
async def test_a_caller_with_no_resolved_tenant_is_refused_before_any_read(
    headers: dict[str, str],
) -> None:
    reader = _reader()
    app = mounted_factory(NODE)(identity=seeded_identity(), reader=reader)
    async with client_for(app) as client:
        response = await client.get(PATH, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "tenant_not_provisioned"}
    assert reader.calls == []


async def test_no_identity_resolver_bound_refuses_every_request_before_any_read() -> (
    None
):
    reader = _reader()
    with installed(None):
        app = mounted_factory(NODE)(reader=reader)
    async with client_for(app) as client:
        for headers in ({}, {"x-api-key": KEY_A}, {"x-api-key": KEY_B}):
            response = await client.get(PATH, headers=headers)
            assert response.status_code == 503
            assert response.json() == {
                "detail": "OIDC authentication is not configured"
            }
    assert reader.calls == []


async def test_an_overlay_that_binds_the_reader_but_not_the_identity_still_refuses() -> (
    None
):
    with installed(overlay(identity=None, reader_seed="{}")):
        app = mounted_factory(NODE)()
    async with client_for(app) as client:
        response = await client.get(PATH, headers={"x-api-key": KEY_A})
    assert response.status_code == 503


async def test_a_resolver_that_fails_refuses_before_any_read() -> None:
    class Broken:
        async def resolve(
            self, headers: Mapping[str, str]
        ) -> ModelHttpTenantIdentity | None:
            raise RuntimeError("identity provider down")

    reader = _reader()
    app = mounted_factory(NODE)(identity=Broken(), reader=reader)
    async with client_for(app) as client:
        response = await client.get(PATH, headers={"x-api-key": KEY_A})
    assert response.status_code == 503
    assert reader.calls == []


async def test_each_tenant_reads_only_its_own_rows() -> None:
    reader = _reader()
    app = mounted_factory(NODE)(identity=seeded_identity(), reader=reader)
    async with client_for(app) as client:
        a = (await client.get(PATH, headers={"x-api-key": KEY_A})).json()
        b = (await client.get(PATH, headers={"x-api-key": KEY_B})).json()
    assert (a["total_saved_usd"], a["delegation_runs"]) == (11.0, 4)
    assert (b["total_saved_usd"], b["delegation_runs"]) == (222.0, 9)
    assert reader.calls == [("read_savings", TENANT_A), ("read_savings", TENANT_B)]


async def test_a_tenant_in_a_request_cannot_change_the_tenant_read() -> None:
    reader = _reader()
    app = mounted_factory(NODE)(identity=seeded_identity(), reader=reader)
    async with client_for(app) as client:
        response = await client.get(
            f"{PATH}?tenant_id={TENANT_B}",
            headers={"x-api-key": KEY_A, "x-tenant-id": str(TENANT_B)},
        )
    assert response.json()["total_saved_usd"] == 11.0
    assert reader.calls == [("read_savings", TENANT_A)]


async def test_overlay_bound_in_memory_profile_serves_the_seeded_tenant_only() -> None:
    seed = json.dumps(
        {str(TENANT_A): {"savings": {"total_saved_usd": 5.0, "delegation_runs": 2}}}
    )
    identity: dict[str, Any] = {KEY_A: f"{TENANT_A},test-tenant,principal-a"}
    with installed(overlay(identity=identity, reader_seed=seed)):
        app = mounted_factory(NODE)()
    async with client_for(app) as client:
        ok = await client.get(PATH, headers={"x-api-key": KEY_A})
        denied = await client.get(PATH, headers={"x-api-key": KEY_B})
    assert ok.status_code == 200
    assert ok.json()["total_saved_usd"] == 5.0
    assert denied.status_code == 401
