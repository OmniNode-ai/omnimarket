# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GET /v1/plans behaviour against the mounted app (ported from the onex-api route pins)."""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest
import yaml

from omnimarket.nodes.node_plan_list.handlers.http_app import create_app
from omnimarket.nodes.node_plan_list.handlers.repository_plan_catalog_postgres import (
    PLAN_CATALOG_SQL,
    RepositoryPlanCatalogPostgres,
)
from omnimarket.nodes.node_plan_list.protocols.protocol_plan_catalog_repository import (
    ProtocolPlanCatalogRepository,
)
from tests.nodes.node_plan_list.fakes import (
    FREE_ID,
    PRO_ID,
    TEAM_ID,
    FakePlanCatalogRepository,
)

pytestmark = pytest.mark.unit
NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_plan_list"


def _client(repo: FakePlanCatalogRepository | None = None) -> httpx.AsyncClient:
    app = create_app(repository=repo or FakePlanCatalogRepository())
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    )


def test_fake_is_the_declared_repository_port() -> None:
    assert isinstance(FakePlanCatalogRepository(), ProtocolPlanCatalogRepository)


def test_contract_pins_route_binding_and_narrowest_mount() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    route = contract["http_route"]
    # gateway-routes binding: GET /v1/plans -> node_plan_list:1.0.0, free-form input.
    assert (route["method"], route["path"], route["contract_id"]) == (
        "GET",
        "/v1/plans",
        "node_plan_list:1.0.0",
    )
    assert route["input_schema"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    }
    # Public by construction: it declares no credential requirement.
    assert route["authorization"] == "anonymous"
    # The prefix equals the route path, never a wider one.
    assert contract["http_mount"]["path_prefix"] == route["path"]
    assert contract["name"] == "node_plan_list"


async def test_lists_plans_with_entitlements_in_catalog_order() -> None:
    async with _client() as client:
        response = await client.get("/v1/plans")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert [p["code"] for p in body] == ["free", "pro", "team"]
    assert list(body[0]) == [
        "id",
        "code",
        "name",
        "description",
        "is_default",
        "created_at",
        "entitlements",
    ]
    assert body[0] == {
        "id": FREE_ID,
        "code": "free",
        "name": "Free",
        "description": "Starter plan",
        "is_default": True,
        "created_at": "2025-01-15T12:00:00Z",
        "entitlements": [
            {
                "metric_key": "api_requests",
                "window_key": "minute",
                "limit_value": 60,
                "hard_cap": True,
                "unit": "requests",
            },
            {
                "metric_key": "events",
                "window_key": "month",
                "limit_value": 1000,
                "hard_cap": False,
                "unit": None,
            },
        ],
    }
    assert body[1]["id"] == PRO_ID
    assert body[1]["description"] is None
    assert len(body[1]["entitlements"]) == 1


async def test_plan_without_entitlements_answers_an_empty_list() -> None:
    async with _client() as client:
        body = (await client.get("/v1/plans")).json()
    team = next(p for p in body if p["id"] == TEAM_ID)
    assert team["entitlements"] == []


async def test_empty_catalog_answers_an_empty_list() -> None:
    async with _client(FakePlanCatalogRepository(rows=[])) as client:
        response = await client.get("/v1/plans")
    assert (response.status_code, response.json()) == (200, [])


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-api-key": "onxk_test"},
        {"authorization": "Bearer not-a-token"},
        {"x-api-key": "onxk_test", "x-request-id": "client-idem-key-123"},
    ],
)
async def test_anonymous_route_ignores_a_presented_credential(
    headers: dict[str, str],
) -> None:
    repo = FakePlanCatalogRepository()
    async with _client(repo) as client:
        response = await client.get("/v1/plans", headers=headers)
    assert response.status_code == 200
    assert repo.calls == 1


async def test_failed_catalog_read_answers_500_with_the_public_detail() -> None:
    async with _client(FakePlanCatalogRepository(fail=True)) as client:
        response = await client.get("/v1/plans")
    assert response.status_code == 500
    assert response.json() == {"detail": "failed to list plans"}
    assert "fixture" not in response.text


async def test_docs_are_not_served_and_other_methods_are_refused() -> None:
    async with _client() as client:
        assert (await client.get("/openapi.json")).status_code == 404
        assert (await client.get("/docs")).status_code == 404
        refused = await client.post("/v1/plans")
    assert refused.status_code == 405


async def test_unconfigured_postgres_binding_builds_and_fails_the_read_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    app = create_app()  # the runtime's no-argument call must not raise
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.get("/v1/plans")
    assert response.status_code == 500
    assert response.json() == {"detail": "failed to list plans"}


class _Pool:
    def __init__(self) -> None:
        self.sql: list[str] = []

    async def fetch(self, sql: str) -> list[tuple[object, ...]]:
        from tests.nodes.node_plan_list.fakes import T0

        self.sql.append(sql)
        return [
            (
                FREE_ID,
                "free",
                "Free",
                None,
                True,
                T0,
                "events",
                "month",
                5,
                False,
                None,
            ),
            (PRO_ID, "pro", "Pro", "d", False, T0, None, None, None, None, None),
        ]


async def test_postgres_binding_reads_the_catalog_join_and_maps_rows() -> None:
    pool = _Pool()
    repo = RepositoryPlanCatalogPostgres(pool)
    rows = await repo.fetch_plan_rows()
    assert pool.sql == [PLAN_CATALOG_SQL]
    assert [r.code for r in rows] == ["free", "pro"]
    assert rows[0].limit_value == 5
    assert rows[1].metric_key is None
    for clause in (
        "LEFT JOIN plan_entitlements pe",
        "pt.is_default DESC",
        "pt.created_at ASC",
        "pe.metric_key ASC NULLS LAST",
        "pe.window_key ASC NULLS LAST",
    ):
        assert clause in PLAN_CATALOG_SQL


def test_no_handler_module_reads_the_process_environment() -> None:
    for path in NODE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
                pytest.fail(f"{path.name} reads the process environment")
