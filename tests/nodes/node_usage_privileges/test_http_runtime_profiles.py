# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Both catalog mounts through the runtime's real HTTP port, in one runtime.

The default profile binds the seeded in-memory repositories and the seeded-tenant
test resolver at the declared DI ports. ONEX_CATALOG_HTTP_TEST_PROFILE=redpanda runs
the same mounts in a runtime whose environment points at an isolated, auth-required
broker through the existing harness. The catalogs publish nothing, so the broker is
the runtime's bus, not a dependency of either route; neither profile skips.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
import pytest
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.services.health_checker import ServiceHealth

from omnimarket.nodes.node_plan_list.handlers import http_app as plans_http_app
from omnimarket.nodes.node_usage_privileges.handlers import (
    http_app as privileges_http_app,
)
from omnimarket.nodes.node_usage_privileges.handlers.identity_binding import (
    bind_tenant_identity_resolver,
)
from tests.integration.customer_path.redpanda_sasl_harness import (
    RedpandaSasl,
    resolve_broker,
    stop_redpanda,
)
from tests.nodes.node_plan_list.fakes import FakePlanCatalogRepository
from tests.nodes.node_usage_privileges.fakes import (
    SEEDED_API_KEY,
    FakeUsagePrivilegeRepository,
    SeededTenantIdentity,
)

pytestmark = pytest.mark.unit
NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
CONTRACTS = [
    NODES / "node_plan_list/contract.yaml",
    NODES / "node_usage_privileges/contract.yaml",
]


@pytest.fixture(scope="module")
def profile_broker() -> Iterator[RedpandaSasl | None]:
    profile = os.environ.get("ONEX_CATALOG_HTTP_TEST_PROFILE", "in-memory")
    if profile == "in-memory":
        yield None
    elif profile == "redpanda":
        broker = resolve_broker()
        try:
            yield broker
        finally:
            stop_redpanda(broker)
    else:
        pytest.fail(f"unknown catalog test profile: {profile}")


async def test_runtime_serves_both_mounts_and_privileges_fails_closed(
    profile_broker: RedpandaSasl | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if profile_broker is not None:
        for key, value in profile_broker.env().items():
            monkeypatch.setenv(key, value)
    plans_repo = FakePlanCatalogRepository()
    privileges_repo = FakeUsagePrivilegeRepository()
    plans_factory = plans_http_app.create_app
    privileges_factory = privileges_http_app.create_app
    monkeypatch.setattr(
        plans_http_app, "create_app", lambda: plans_factory(repository=plans_repo)
    )
    monkeypatch.setattr(
        privileges_http_app,
        "create_app",
        lambda: privileges_factory(repository=privileges_repo),
    )
    bind_tenant_identity_resolver(None)
    # Two mounts, neither prefix equals or covers the other or a runtime route.
    manifest = discover_contracts_from_paths(CONTRACTS)
    assert manifest.total_errors == 0
    server = ServiceHealth(runtime=MagicMock(), port=0, host="127.0.0.1")
    try:
        await server.start()
        server.attach_manifest(manifest)
        assert server._mount_task is not None
        await server._mount_task
        assert server._runner is not None
        base = f"http://127.0.0.1:{server._runner.addresses[0][1]}"
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/v1/plans") as plans:
                assert plans.status == 200
            # No resolver bound: refused before any catalog read.
            async with session.get(
                f"{base}/v1/usage/privileges", headers={"x-api-key": SEEDED_API_KEY}
            ) as unbound:
                assert unbound.status == 503
            assert privileges_repo.calls == 0
            bind_tenant_identity_resolver(SeededTenantIdentity())
            async with session.get(f"{base}/v1/usage/privileges") as anonymous:
                assert anonymous.status == 401
                assert (await anonymous.json())["code"] == "auth.credential.absent"
            assert privileges_repo.calls == 0
            async with session.get(
                f"{base}/v1/usage/privileges", headers={"x-api-key": SEEDED_API_KEY}
            ) as served:
                assert served.status == 200
                body = await served.json()
            assert [row["plan_tier_code"] for row in body] == ["free", "pro"]
            assert privileges_repo.calls == 1
    finally:
        bind_tenant_identity_resolver(None)
        await server.stop()
    assert plans_repo.calls == 1
