# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GET /v1/usage/privileges behaviour against the mounted app (ported route pins)."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

from omnimarket.nodes.node_usage_privileges.handlers.http_app import create_app
from omnimarket.nodes.node_usage_privileges.handlers.identity_binding import (
    bind_tenant_identity_resolver,
)
from omnimarket.nodes.node_usage_privileges.handlers.repository_usage_privilege_postgres import (
    USAGE_PRIVILEGES_SQL,
    RepositoryUsagePrivilegePostgres,
)
from omnimarket.nodes.node_usage_privileges.protocols.protocol_usage_privilege_repository import (
    ProtocolUsagePrivilegeRepository,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)
from tests.nodes.node_usage_privileges.fakes import (
    SEEDED_API_KEY,
    T0,
    FakeUsagePrivilegeRepository,
    SeededTenantIdentity,
)

pytestmark = pytest.mark.unit
NODE = (
    Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_usage_privileges"
)
SEEDED = {"x-api-key": SEEDED_API_KEY}


@pytest.fixture(autouse=True)
def _unbound() -> Iterator[None]:
    bind_tenant_identity_resolver(None)
    yield
    bind_tenant_identity_resolver(None)


def _client(
    repo: FakeUsagePrivilegeRepository,
    identity: ProtocolHttpTenantIdentity | None = None,
) -> httpx.AsyncClient:
    app = create_app(repository=repo, identity=identity)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    )


def test_fake_is_the_declared_repository_port() -> None:
    assert isinstance(FakeUsagePrivilegeRepository(), ProtocolUsagePrivilegeRepository)


def test_contract_pins_route_binding_and_narrowest_mount() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    route = contract["http_route"]
    assert (route["method"], route["path"], route["contract_id"]) == (
        "GET",
        "/v1/usage/privileges",
        "node_usage_privileges:1.0.0",
    )
    assert route["input_schema"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    }
    assert route["authorization"] == "tenant"
    # Never /v1/usage: the private /v1/usage/events routes sit beside this one.
    assert contract["http_mount"]["path_prefix"] == "/v1/usage/privileges"


async def test_lists_privileges_ordered_with_nullable_quota() -> None:
    repo = FakeUsagePrivilegeRepository()
    async with _client(repo, SeededTenantIdentity()) as client:
        response = await client.get("/v1/usage/privileges", headers=SEEDED)
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "plan_tier_code": "free",
            "resource_code": "events",
            "monthly_quota": 1000,
            "hard_limit": True,
            "created_at": "2025-01-15T12:00:00Z",
        },
        {
            "id": "22222222-2222-4222-8222-222222222222",
            "plan_tier_code": "pro",
            "resource_code": "events",
            "monthly_quota": None,
            "hard_limit": False,
            "created_at": "2025-01-15T12:00:00Z",
        },
    ]
    assert repo.calls == 1


async def test_catalog_has_no_tenant_filter() -> None:
    """Two different callers read the same global catalog."""
    repo = FakeUsagePrivilegeRepository()

    class Other(SeededTenantIdentity):
        pass

    async with _client(repo, SeededTenantIdentity()) as client:
        first = (await client.get("/v1/usage/privileges", headers=SEEDED)).json()
        second = (await client.get("/v1/usage/privileges", headers=SEEDED)).json()
    assert first == second
    assert repo.calls == 2


async def test_failed_catalog_read_answers_500_with_the_public_detail() -> None:
    async with _client(
        FakeUsagePrivilegeRepository(fail=True), SeededTenantIdentity()
    ) as client:
        response = await client.get("/v1/usage/privileges", headers=SEEDED)
    assert response.status_code == 500
    assert response.json() == {"detail": "failed to list usage privileges"}
    assert "fixture" not in response.text


async def test_docs_are_not_served_and_other_methods_are_refused() -> None:
    async with _client(
        FakeUsagePrivilegeRepository(), SeededTenantIdentity()
    ) as client:
        assert (await client.get("/openapi.json")).status_code == 404
        assert (
            await client.post("/v1/usage/privileges", headers=SEEDED)
        ).status_code == 405
        # The private sibling routes are not served by this mount.
        assert (await client.get("/v1/usage/events", headers=SEEDED)).status_code == 404


async def test_late_bound_resolver_serves_the_default_factory_app() -> None:
    repo = FakeUsagePrivilegeRepository()
    bind_tenant_identity_resolver(SeededTenantIdentity())
    async with _client(repo) as client:
        ok = await client.get("/v1/usage/privileges", headers=SEEDED)
        denied = await client.get("/v1/usage/privileges")
    assert ok.status_code == 200
    assert denied.status_code == 401
    assert repo.calls == 1


class _Pool:
    def __init__(self) -> None:
        self.sql: list[str] = []

    async def fetch(self, sql: str) -> list[tuple[object, ...]]:
        self.sql.append(sql)
        return [
            ("11111111-1111-4111-8111-111111111111", "free", "events", None, True, T0)
        ]


async def test_postgres_binding_reads_the_ordered_catalog_and_maps_rows() -> None:
    pool = _Pool()
    rows = await RepositoryUsagePrivilegePostgres(pool).fetch_usage_privileges()
    assert pool.sql == [USAGE_PRIVILEGES_SQL]
    assert rows[0].monthly_quota is None
    assert rows[0].hard_limit is True
    assert "ORDER BY plan_tier_code, resource_code" in USAGE_PRIVILEGES_SQL
    assert "tenant" not in USAGE_PRIVILEGES_SQL.lower()


def test_no_handler_module_reads_the_process_environment() -> None:
    for path in NODE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
                pytest.fail(f"{path.name} reads the process environment")
