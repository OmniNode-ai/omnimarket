# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: the delegations route refuses before any projection read, and tenants stay apart."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from omnimarket.handlers.container.container_tenant_read_dependencies import (
    InMemoryTenantProjectionReader,
)
from omnimarket.models.model_tenant_delegation_projection import ModelDelegationRunRow
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
NODE = "node_tenant_delegations_read_effect"
PATH = "/v1/tenants/me/delegations"


def _run(run_id: str) -> ModelDelegationRunRow:
    return ModelDelegationRunRow(
        run_id=run_id,
        task_type="summarize",
        model="m",
        quality_gate_passed=True,
        occurred_at=datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
    )


def _reader() -> InMemoryTenantProjectionReader:
    return InMemoryTenantProjectionReader(
        runs={
            TENANT_A: (_run("a-1"), _run("a-2")),
            TENANT_B: (_run("b-1"), _run("b-2"), _run("b-3")),
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


async def test_each_tenant_sees_none_of_the_other_tenants_rows() -> None:
    reader = _reader()
    app = mounted_factory(NODE)(identity=seeded_identity(), reader=reader)
    async with client_for(app) as client:
        a = (await client.get(PATH, headers={"x-api-key": KEY_A})).json()
        b = (await client.get(PATH, headers={"x-api-key": KEY_B})).json()
    assert {r["run_id"] for r in a["runs"]} == {"a-1", "a-2"}
    assert {r["run_id"] for r in b["runs"]} == {"b-1", "b-2", "b-3"}
    assert reader.calls == [
        ("read_recent_delegations", TENANT_A),
        ("read_recent_delegations", TENANT_B),
    ]


async def test_a_tenant_in_a_request_cannot_change_the_tenant_read() -> None:
    reader = _reader()
    app = mounted_factory(NODE)(identity=seeded_identity(), reader=reader)
    async with client_for(app) as client:
        response = await client.get(
            f"{PATH}?tenant_id={TENANT_B}", headers={"x-api-key": KEY_A}
        )
    assert {r["run_id"] for r in response.json()["runs"]} == {"a-1", "a-2"}
    assert reader.calls == [("read_recent_delegations", TENANT_A)]


async def test_overlay_bound_in_memory_profile_serves_the_seeded_tenant_only() -> None:
    seed = json.dumps(
        {str(TENANT_A): {"runs": [{"run_id": "only-a", "quality_gate_passed": True}]}}
    )
    with installed(
        overlay(
            identity={KEY_A: f"{TENANT_A},test-tenant,principal-a"}, reader_seed=seed
        )
    ):
        app = mounted_factory(NODE)()
    async with client_for(app) as client:
        ok = await client.get(PATH, headers={"x-api-key": KEY_A})
        denied = await client.get(PATH, headers={"x-api-key": KEY_B})
    assert [r["run_id"] for r in ok.json()["runs"]] == ["only-a"]
    assert denied.status_code == 401
