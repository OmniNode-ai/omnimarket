# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: the privileges route refuses before any catalog read; the plans route stays anonymous."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI

from omnimarket.nodes.node_plan_list.handlers.http_app import (
    create_app as create_plans_app,
)
from omnimarket.nodes.node_usage_privileges.handlers.http_app import (
    create_app as create_privileges_app,
)
from omnimarket.nodes.node_usage_privileges.handlers.identity_binding import (
    bind_tenant_identity_resolver,
)
from tests.nodes.node_plan_list.fakes import FakePlanCatalogRepository
from tests.nodes.node_usage_privileges.fakes import (
    SEEDED_API_KEY,
    FailingTenantIdentity,
    FakeUsagePrivilegeRepository,
    SeededTenantIdentity,
)

pytestmark = pytest.mark.unit
PATH = "/v1/usage/privileges"


@pytest.fixture(autouse=True)
def _unbound() -> Iterator[None]:
    bind_tenant_identity_resolver(None)
    yield
    bind_tenant_identity_resolver(None)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://runtime",
    )


@pytest.mark.parametrize(
    "headers",
    [{}, {"x-api-key": SEEDED_API_KEY}, {"authorization": "Bearer abc"}],
)
async def test_no_resolver_bound_refuses_with_503_and_no_catalog_read(
    headers: dict[str, str],
) -> None:
    repo = FakeUsagePrivilegeRepository()
    async with _client(create_privileges_app(repository=repo)) as client:
        response = await client.get(PATH, headers=headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "OIDC authentication is not configured"}
    assert repo.calls == 0


async def test_unresolved_caller_without_credential_is_401_absent_and_no_read() -> None:
    repo = FakeUsagePrivilegeRepository()
    identity = SeededTenantIdentity()
    async with _client(create_privileges_app(repository=repo, identity=identity)) as c:
        response = await c.get(PATH)
    assert response.status_code == 401
    assert response.json() == {
        "detail": "Unauthorized",
        "code": "auth.credential.absent",
    }
    assert identity.calls == 1
    assert repo.calls == 0


@pytest.mark.parametrize(
    "headers",
    [{"x-api-key": "wrong"}, {"authorization": "Bearer abc"}, {"X-API-Key": "wrong"}],
)
async def test_unresolved_presented_credential_is_401_invalid_and_no_read(
    headers: dict[str, str],
) -> None:
    repo = FakeUsagePrivilegeRepository()
    async with _client(
        create_privileges_app(repository=repo, identity=SeededTenantIdentity())
    ) as client:
        response = await client.get(PATH, headers=headers)
    assert response.status_code == 401
    assert response.json() == {
        "detail": "Unauthorized",
        "code": "auth.credential.invalid",
    }
    assert repo.calls == 0


async def test_empty_credential_header_counts_as_absent() -> None:
    repo = FakeUsagePrivilegeRepository()
    async with _client(
        create_privileges_app(repository=repo, identity=SeededTenantIdentity())
    ) as client:
        response = await client.get(PATH, headers={"x-api-key": ""})
    assert response.json()["code"] == "auth.credential.absent"
    assert repo.calls == 0


async def test_resolver_failure_refuses_with_503_and_no_catalog_read() -> None:
    repo = FakeUsagePrivilegeRepository()
    async with _client(
        create_privileges_app(repository=repo, identity=FailingTenantIdentity())
    ) as client:
        response = await client.get(PATH, headers={"x-api-key": SEEDED_API_KEY})
    assert response.status_code == 503
    assert "fixture" not in response.text
    assert repo.calls == 0


async def test_anonymous_plans_succeeds_while_anonymous_privileges_is_refused() -> None:
    plans_repo = FakePlanCatalogRepository()
    privileges_repo = FakeUsagePrivilegeRepository()
    bind_tenant_identity_resolver(SeededTenantIdentity())
    async with (
        _client(create_plans_app(repository=plans_repo)) as plans,
        _client(create_privileges_app(repository=privileges_repo)) as privileges,
    ):
        anonymous_plans = await plans.get("/v1/plans")
        anonymous_privileges = await privileges.get(PATH)
    assert anonymous_plans.status_code == 200
    assert anonymous_privileges.status_code == 401
    assert plans_repo.calls == 1
    assert privileges_repo.calls == 0
