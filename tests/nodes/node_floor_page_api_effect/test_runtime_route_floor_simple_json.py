# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime serves floor-simple.json from the floor projection (OMN-23072, slice 1).

The failing control of the slice: boot the runtime's HTTP mount from the route node's
contract (the same discovery and mount bridge the deployed runtime runs), publish the
floor document the scheduled compute emits through the projection node, and read
``/floor/floor-simple.json``. Before the nodes exist the contract is absent and the
control fails; with an empty projection the route refuses by name and never answers
a zero floor.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
import pytest
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.services.health_checker import ServiceHealth

from omnimarket.handlers.floor_document_store import (
    reset_floor_document_store,
)
from tests.nodes.node_floor_page_api_effect.support import (
    document_event,
    project,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_floor_page_api_effect/contract.yaml"
)
ROUTE = "/floor/floor-simple.json"


@pytest.fixture(autouse=True)
def _empty_projection():
    reset_floor_document_store()
    yield
    reset_floor_document_store()


async def _serve_and_get(path: str) -> tuple[int, dict[str, object], Mapping[str, str]]:
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
        url = f"http://127.0.0.1:{port}{path}"
        async with aiohttp.ClientSession() as session, session.get(url) as response:
            return response.status, await response.json(), response.headers
    finally:
        await server.stop()


async def test_runtime_route_serves_the_projected_floor_document():
    computed_at = datetime(2026, 11, 8, 23, 30, tzinfo=UTC)
    event = document_event(computed_at)
    project(event)
    status, body, headers = await _serve_and_get(ROUTE)
    assert status == 200
    assert body == event.document.model_dump(mode="json")
    assert headers["X-Floor-Computed-At"] == "2026-11-08T23:30:00Z"


async def test_runtime_route_refuses_by_name_before_any_document_exists():
    status, body, _ = await _serve_and_get(ROUTE)
    assert status == 503
    assert body["code"] == "floor.document.not_projected"
