# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-internal-ip OMN-14294 reason="e2e probe test fixture — lab Memgraph Tailscale IP used as a parameterizable default; overridden by ONEX_E2E_MEMGRAPH_BOLT_URI at runtime; not a runtime default"
# test-literal-ok: OMN-14294 companion exemption for test_no_hardcoded_literals gate (see onex-allow-file above for leak-gate)
"""Live-Memgraph integration test for node_architecture_graph_query_effect — OMN-14294.

The golden-chain suite (test_golden_chain_architecture_graph_query_effect.py) mocks
the graph driver, which is exactly what masked OMN-14294: ``neo4j.AsyncResult.data()``
is a coroutine method on the real async driver, but ``MagicMock().data()`` returns a
plain value synchronously, so ``rows = result.data()`` (missing ``await``) never
raised in the mocked suite even though it always failed with
``TypeError: 'coroutine' object is not iterable`` against a real driver.

This test exercises all 4 query operations against a real ``neo4j`` async driver
session talking to a live Memgraph instance, so this class of bug can't regress
silently behind a mock again.

Opt-in guard
--------------------------
Set OMN_ALLOW_LIVE_E2E_PROBE=true to execute this test against the live lane.
Without it the test skips (same gating convention as tests/integration/e2e_probe/).

Usage
--------------------------
  OMN_ALLOW_LIVE_E2E_PROBE=true \\
  uv run pytest tests/nodes/node_architecture_graph_query_effect/test_live_memgraph_integration.py -v -m e2e
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

from omnimarket.nodes.node_architecture_graph_query_effect.handlers import (
    HandlerArchitectureGraphQuery,
)
from omnimarket.nodes.node_architecture_graph_query_effect.models import (
    ModelArchitectureGraphQueryConfig,
    ModelArchitectureGraphQueryRequestedEvent,
)

# ---------------------------------------------------------------------------
# Opt-in guard — skip unless the flag is set (no live network access in CI)
# ---------------------------------------------------------------------------

_ALLOW_FLAG = "OMN_ALLOW_LIVE_E2E_PROBE"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get(_ALLOW_FLAG, "").lower() != "true",
        reason=(
            f"Requires {_ALLOW_FLAG}=true to run against a live Memgraph instance. "
            "Set this env var explicitly to execute the probe."
        ),
    ),
]

_DEFAULT_BOLT_URI = "bolt://100.109.203.94:7687"  # lab Memgraph (.201) over Tailscale  # onex-allow-test-fixture OMN-16156 reason="env-overridable live-integration test default for the real lab Memgraph"
_BOLT_URI = os.environ.get("ONEX_E2E_MEMGRAPH_BOLT_URI", _DEFAULT_BOLT_URI)


@pytest_asyncio.fixture
async def probe_marker() -> str:
    """Unique nonce so fixture nodes never collide with real architecture-graph
    data and can be reliably deleted regardless of pass/fail or concurrent runs.
    """
    return f"omn14294-probe-{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture
async def seeded_graph(probe_marker: str):
    """Seed a small, self-contained fixture graph via the real driver, then
    guarantee teardown via ``probe_marker`` regardless of test outcome.
    """
    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(_BOLT_URI, connection_timeout=10)
    await driver.verify_connectivity()

    node_a = f"{probe_marker}-a"
    node_b = f"{probe_marker}-b"
    repo_x = f"{probe_marker}-repo-x"
    repo_y = f"{probe_marker}-repo-y"
    module_x = f"{probe_marker}-module-x"
    module_y = f"{probe_marker}-module-y"
    repo_z = f"{probe_marker}-repo-z"
    node_z1 = f"{probe_marker}-z1"
    node_z2 = f"{probe_marker}-z2"

    async with driver.session() as session:
        # dependency_path / blast_radius fixture: A -[:DEPENDS_ON]-> B
        await session.run(
            "MERGE (a:ProbeNode {name: $node_a, probe_marker: $marker}) "
            "MERGE (b:ProbeNode {name: $node_b, probe_marker: $marker}) "
            "MERGE (a)-[:DEPENDS_ON]->(b)",
            node_a=node_a,
            node_b=node_b,
            marker=probe_marker,
        )
        # cross_repo_imports fixture: module_x (repo_x) -[:IMPORTS]-> module_y (repo_y)
        await session.run(
            "MERGE (x:ProbeModule {name: $module_x, repo: $repo_x, probe_marker: $marker}) "
            "MERGE (y:ProbeModule {name: $module_y, repo: $repo_y, probe_marker: $marker}) "
            "MERGE (x)-[:IMPORTS]->(y)",
            module_x=module_x,
            repo_x=repo_x,
            module_y=module_y,
            repo_y=repo_y,
            marker=probe_marker,
        )
        # circular_deps fixture: z1 -[:DEPENDS_ON]-> z2 -[:DEPENDS_ON]-> z1
        await session.run(
            "MERGE (z1:ProbeNode {name: $node_z1, repo: $repo_z, probe_marker: $marker}) "
            "MERGE (z2:ProbeNode {name: $node_z2, repo: $repo_z, probe_marker: $marker}) "
            "MERGE (z1)-[:DEPENDS_ON]->(z2) "
            "MERGE (z2)-[:DEPENDS_ON]->(z1)",
            node_z1=node_z1,
            node_z2=node_z2,
            repo_z=repo_z,
            marker=probe_marker,
        )

    try:
        yield {
            "node_a": node_a,
            "node_b": node_b,
            "repo_x": repo_x,
            "repo_z": repo_z,
        }
    finally:
        async with driver.session() as session:
            await session.run(
                "MATCH (n {probe_marker: $marker}) DETACH DELETE n",
                marker=probe_marker,
            )
        await driver.close()


@pytest_asyncio.fixture
async def query_handler():
    handler = HandlerArchitectureGraphQuery()
    config = ModelArchitectureGraphQueryConfig(bolt_uri=_BOLT_URI)
    await handler.initialize(config=config)
    try:
        yield handler
    finally:
        await handler.shutdown()


class TestArchitectureGraphQueryLiveMemgraph:
    """Real async-driver, real-Memgraph round-trip for all 4 query operations."""

    async def test_dependency_path_round_trips(
        self, query_handler, seeded_graph
    ) -> None:
        request = ModelArchitectureGraphQueryRequestedEvent(
            query_id=str(uuid.uuid4()),
            operation="dependency_path",
            from_node=seeded_graph["node_a"],
            to_node=seeded_graph["node_b"],
        )
        response = await query_handler.execute(request)

        assert response.status == "success", response.error_message
        assert response.path_length == 1
        assert [n.name for n in response.nodes] == [
            seeded_graph["node_a"],
            seeded_graph["node_b"],
        ]

    async def test_blast_radius_round_trips(self, query_handler, seeded_graph) -> None:
        request = ModelArchitectureGraphQueryRequestedEvent(
            query_id=str(uuid.uuid4()),
            operation="blast_radius",
            target=seeded_graph["node_b"],
        )
        response = await query_handler.execute(request)

        assert response.status == "success", response.error_message
        assert seeded_graph["node_a"] in [n.name for n in response.nodes]

    async def test_cross_repo_imports_round_trips(
        self, query_handler, seeded_graph
    ) -> None:
        request = ModelArchitectureGraphQueryRequestedEvent(
            query_id=str(uuid.uuid4()),
            operation="cross_repo_imports",
            repo=seeded_graph["repo_x"],
        )
        response = await query_handler.execute(request)

        assert response.status == "success", response.error_message
        assert len(response.edges) >= 1

    async def test_circular_deps_round_trips(self, query_handler, seeded_graph) -> None:
        request = ModelArchitectureGraphQueryRequestedEvent(
            query_id=str(uuid.uuid4()),
            operation="circular_deps",
            repo=seeded_graph["repo_z"],
        )
        response = await query_handler.execute(request)

        assert response.status == "success", response.error_message
        assert len(response.nodes) >= 2
