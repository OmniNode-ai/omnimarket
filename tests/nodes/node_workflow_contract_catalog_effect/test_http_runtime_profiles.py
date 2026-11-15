# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime's real HTTP listener serves the catalog route under both profiles.

The production no-argument ASGI factory is mounted through the runtime's own
health server from the node contract, with the catalog read from a lane overlay
document and the caller resolved by a bound test resolver.

* in-memory (default): the overlay binds the catalog as an in-memory document.
* ``ONEX_WORKFLOW_CONTRACTS_HTTP_TEST_PROFILE=redpanda``: the overlay binds the
  catalog as a file document, and the isolated auth-required broker of the
  customer-path harness is up and its environment is set while the mount serves.
  This route reads no topic and publishes none, so the broker is not on its
  request path; the profile proves the mount boots and serves in that
  environment.

Neither profile skips when its proof is unavailable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.services.health_checker import ServiceHealth

from omnimarket.handlers.container.container_overlay_dependencies import bind_overlay
from omnimarket.nodes.node_workflow_contract_catalog_effect.handlers import (
    http_dependencies,
)
from tests.integration.customer_path.redpanda_sasl_harness import (
    RedpandaSasl,
    resolve_broker,
    stop_redpanda,
)
from tests.nodes.node_workflow_contract_catalog_effect.catalog_fixtures import (
    DELEGATION,
    FENCED,
    TENANT_A_KEY,
    SeededIdentity,
    catalog_document,
)
from tests.overlay_bound_support import durable_overlay, in_memory_overlay

pytestmark = pytest.mark.unit

CONTRACT = http_dependencies.CONTRACT_PATH
ROUTE = "/v1/workflows/contracts"


@pytest.fixture(scope="module")
def profile_broker() -> Iterator[RedpandaSasl | None]:
    profile = os.environ.get("ONEX_WORKFLOW_CONTRACTS_HTTP_TEST_PROFILE", "in-memory")
    if profile == "in-memory":
        yield None
    elif profile == "redpanda":
        broker = resolve_broker()
        try:
            yield broker
        finally:
            stop_redpanda(broker)
    else:
        pytest.fail(f"unknown workflow contracts test profile: {profile}")


async def test_runtime_mount_serves_catalog_to_a_resolved_tenant_only(
    profile_broker: RedpandaSasl | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = http_dependencies.load_http_route()
    text = yaml.safe_dump(catalog_document())
    if profile_broker is None:
        bind_overlay(in_memory_overlay(documents={config.catalog_document: text}))
    else:
        for key, value in profile_broker.env().items():
            monkeypatch.setenv(key, value)
        path = tmp_path / "workflow_contracts.yaml"
        path.write_text(text)
        bind_overlay(
            durable_overlay(document_paths={config.catalog_document: str(path)})
        )
    http_dependencies.bind_tenant_identity(SeededIdentity())

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
        url = f"http://127.0.0.1:{port}{ROUTE}"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers={"x-api-key": TENANT_A_KEY}
            ) as response:
                assert response.status == 200, await response.text()
                body = await response.json()
            advertised = [c["workflow_type"] for c in body["contracts"]]
            assert advertised == ["alpha-flow", DELEGATION]
            assert FENCED not in advertised
            async with session.get(url) as response:
                assert response.status == 401
                assert (await response.json())["code"] == "auth.credential.absent"
            async with session.get(url, headers={"x-api-key": "bad"}) as response:
                assert response.status == 401
                assert (await response.json())["code"] == "auth.credential.invalid"
            # With the resolver unbound the same mount refuses every request.
            http_dependencies.bind_tenant_identity(None)
            async with session.get(
                url, headers={"x-api-key": TENANT_A_KEY}
            ) as response:
                assert response.status == 503
            async with session.get(f"http://127.0.0.1:{port}/health") as response:
                assert "status" in await response.json()
    finally:
        http_dependencies.bind_tenant_identity(None)
        bind_overlay(None)
        await server.stop()
