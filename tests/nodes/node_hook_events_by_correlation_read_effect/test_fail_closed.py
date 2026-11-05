# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: the route refuses before any projection read, and tenants stay apart."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_hook_events_by_correlation_read_effect.handlers import (
    http_dependencies,
)
from omnimarket.nodes.node_hook_events_by_correlation_read_effect.handlers.http_app import (
    create_app,
)
from tests.nodes.node_hook_events_by_correlation_read_effect.support import (
    CID,
    KEY_A,
    KEY_B,
    ROUTE,
    SLUG_A,
    SLUG_B,
    FakeIdentity,
    FakeReader,
    client,
    row,
)

pytestmark = pytest.mark.unit
SEEDED = [
    row(SLUG_A, event_id="alpha-1", run_id="run-alpha"),
    row(SLUG_B, event_id="bravo-1", run_id="run-bravo"),
]


async def test_no_credential_is_401_absent_with_no_repository_call():
    reader = FakeReader(SEEDED)
    async with client(reader) as c:
        resp = await c.get(ROUTE, params={"correlation_id": CID})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized", "code": "auth.credential.absent"}
    assert reader.calls == []


@pytest.mark.parametrize(
    "headers",
    [{"x-api-key": "unknown"}, {"authorization": "Bearer header.payload.sig"}],
)
async def test_unresolved_credential_is_401_invalid_with_no_repository_call(headers):
    reader = FakeReader(SEEDED)
    async with client(reader) as c:
        resp = await c.get(ROUTE, params={"correlation_id": CID}, headers=headers)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized", "code": "auth.credential.invalid"}
    assert reader.calls == []


async def test_authentication_precedes_query_validation():
    reader = FakeReader(SEEDED)
    async with client(reader) as c:
        resp = await c.get(ROUTE)  # no credential and no correlation id
    assert resp.status_code == 401
    assert reader.calls == []


async def test_no_resolver_bound_refuses_every_request_with_no_repository_call():
    reader = FakeReader(SEEDED)
    assert http_dependencies.bound_tenant_identity_resolver() is None
    app = create_app(reader=reader)  # neither bound nor injected
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture.invalid"
    ) as c:
        for headers in ({}, {"x-api-key": KEY_A}, {"authorization": "Bearer a.b.c"}):
            resp = await c.get(ROUTE, params={"correlation_id": CID}, headers=headers)
            assert resp.status_code == 503
            assert resp.json() == {"detail": "OIDC authentication is not configured"}
    assert reader.calls == []


async def test_production_factory_with_nothing_bound_refuses():
    import httpx

    app = create_app()  # the runtime's no-argument call
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture.invalid"
    ) as c:
        resp = await c.get(
            ROUTE, params={"correlation_id": CID}, headers={"x-api-key": KEY_A}
        )
    assert resp.status_code == 503


async def test_a_process_bound_resolver_is_used_and_can_be_unbound():
    reader = FakeReader(SEEDED)
    http_dependencies.bind_tenant_identity_resolver(FakeIdentity())
    try:
        app = create_app(reader=reader)
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture.invalid"
        ) as c:
            ok = await c.get(
                ROUTE, params={"correlation_id": CID}, headers={"x-api-key": KEY_A}
            )
            assert ok.status_code == 200
    finally:
        http_dependencies.bind_tenant_identity_resolver(None)
    assert http_dependencies.bound_tenant_identity_resolver() is None


async def test_each_tenant_sees_only_its_own_rows():
    reader = FakeReader(SEEDED)
    async with client(reader) as c:
        a = (
            await c.get(
                ROUTE,
                params={"correlation_id": CID, "include_payload": "true"},
                headers={"x-api-key": KEY_A},
            )
        ).json()
        b = (
            await c.get(
                ROUTE, params={"correlation_id": CID}, headers={"x-api-key": KEY_B}
            )
        ).json()
    assert a["data_state"] == b["data_state"] == "found"
    assert a["tenant_scope"] == SLUG_A
    assert b["tenant_scope"] == SLUG_B
    assert [r["event_id"] for r in a["rows"]] == ["alpha-1"]
    assert [r["event_id"] for r in b["rows"]] == ["bravo-1"]
    assert {r["tenant_id"] for r in a["rows"]} == {SLUG_A}
    assert {r["tenant_id"] for r in b["rows"]} == {SLUG_B}
    assert "bravo" not in str(a)
    assert "alpha" not in str(b).replace("tenant-alpha", "")
    assert [c["tenant_slug"] for c in reader.calls] == [SLUG_A, SLUG_B]


async def test_a_tenant_with_no_rows_of_its_own_reads_not_found_not_the_others():
    reader = FakeReader([row(SLUG_A)])
    async with client(reader) as c:
        b = (
            await c.get(
                ROUTE, params={"correlation_id": CID}, headers={"x-api-key": KEY_B}
            )
        ).json()
    assert b["data_state"] == "not_found"
    assert b["rows"] == []
    assert b["tenant_scope"] == SLUG_B
