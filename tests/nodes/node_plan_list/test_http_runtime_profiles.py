# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GET /v1/plans through the runtime's real HTTP port and contract-declared mount.

The default profile binds the seeded in-memory repository at the declared
repository port. ONEX_CATALOG_HTTP_TEST_PROFILE=redpanda runs the same mount in a
runtime whose environment points at an isolated, auth-required broker through the
existing harness. The plan catalog publishes nothing, so the broker is the runtime's
bus, not a dependency of the route; neither profile skips when its proof is unavailable.
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

from omnimarket.nodes.node_plan_list.handlers import http_app
from tests.integration.customer_path.redpanda_sasl_harness import (
    RedpandaSasl,
    resolve_broker,
    stop_redpanda,
)
from tests.nodes.node_plan_list.fakes import FakePlanCatalogRepository

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_plan_list/contract.yaml"
)


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


async def test_runtime_mount_serves_the_plan_catalog(
    profile_broker: RedpandaSasl | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if profile_broker is not None:
        for key, value in profile_broker.env().items():
            monkeypatch.setenv(key, value)
    repo = FakePlanCatalogRepository()
    original_factory = http_app.create_app
    # Only the DI repository port changes; the declared factory path, the ASGI
    # adapter, the typed handler and the runtime's mount bridge all run.
    monkeypatch.setattr(
        http_app, "create_app", lambda: original_factory(repository=repo)
    )
    manifest = discover_contracts_from_paths([CONTRACT])
    assert manifest.total_errors == 0
    server = ServiceHealth(runtime=MagicMock(), port=0, host="127.0.0.1")
    try:
        await server.start()
        server.attach_manifest(manifest)
        assert server._mount_task is not None
        await server._mount_task
        assert server._runner is not None
        port = server._runner.addresses[0][1]
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/v1/plans") as response:
                assert response.status == 200
                body = await response.json()
            assert [plan["code"] for plan in body] == ["free", "pro", "team"]
            assert len(body[0]["entitlements"]) == 2
            # An unmounted sibling path is not answered by this mount.
            async with session.get(f"http://127.0.0.1:{port}/v1/usage/events") as other:
                assert other.status == 404
            async with session.get(f"http://127.0.0.1:{port}/health") as health:
                assert "status" in await health.json()
    finally:
        await server.stop()
    assert repo.calls == 1
