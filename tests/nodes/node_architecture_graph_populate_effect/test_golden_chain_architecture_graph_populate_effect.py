# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain tests for node_architecture_graph_populate_effect.

Verifies that:
- contract.yaml sources are parsed into MERGE Cypher statements
- Edge source_authority is properly classified
- Idempotent via MERGE semantics (not CREATE)
- Graph snapshot metadata fields are tracked
- Unit tests cover Cypher generation from sample contract.yaml data

Related: OMN-11918 — Memgraph Architecture Graph Populator (Task 2.3)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_architecture_graph_populate_effect.handlers import (
    HandlerArchitectureGraphPopulate,
)
from omnimarket.nodes.node_architecture_graph_populate_effect.models import (
    ModelArchitectureGraphPopulateConfig,
    ModelArchitectureGraphPopulateRequestedEvent,
    ModelArchitectureGraphPopulateResponseEvent,
    ModelGraphEdgeSpec,
    ModelGraphNodeSpec,
    ModelGraphPopulateSourceAuthority,
    ModelGraphSnapshotMeta,
)

_TEST_CONFIG = ModelArchitectureGraphPopulateConfig(bolt_uri="bolt://test-host:7687")

# Minimal contract.yaml content for testing
_SAMPLE_CONTRACT = {
    "name": "node_sample_effect",
    "node_type": "effect",
    "event_bus": {
        "subscribe_topics": ["onex.cmd.omnimarket.sample-cmd.v1"],
        "publish_topics": ["onex.evt.omnimarket.sample-event.v1"],
    },
}

_SAMPLE_PYPROJECT = {
    "project": {
        "name": "omnimarket",
        "dependencies": ["omnibase_core>=0.39.0", "omnibase_spi>=0.20.0"],
    }
}


def _make_handler_with_mock_driver() -> tuple[
    HandlerArchitectureGraphPopulate, MagicMock
]:
    mock_driver = MagicMock()
    handler = HandlerArchitectureGraphPopulate()
    handler._driver = mock_driver
    handler._initialized = True
    handler._config = _TEST_CONFIG
    return handler, mock_driver


@pytest.mark.unit
class TestArchitectureGraphPopulateEffect:
    """Golden chain tests for node_architecture_graph_populate_effect."""

    async def test_node_importable(self) -> None:
        from omnimarket.nodes import node_architecture_graph_populate_effect

        assert node_architecture_graph_populate_effect is not None

    async def test_handler_importable(self) -> None:
        assert HandlerArchitectureGraphPopulate is not None

    async def test_config_defaults(self) -> None:
        config = ModelArchitectureGraphPopulateConfig(bolt_uri="bolt://localhost:7687")
        assert config.graph_backend == "memgraph"
        assert config.timeout_seconds > 0
        assert config.bolt_uri == "bolt://localhost:7687"
        assert config.graph_schema_version == "1.0.0"

    async def test_source_authority_enum(self) -> None:
        assert ModelGraphPopulateSourceAuthority.AUTHORITATIVE == "authoritative"
        assert ModelGraphPopulateSourceAuthority.EVIDENCE == "evidence"

    async def test_graph_node_spec_model(self) -> None:
        node = ModelGraphNodeSpec(
            node_id="omnimarket",
            label="Repository",
            properties={"name": "omnimarket", "repo": "omnimarket"},
        )
        assert node.label == "Repository"
        assert node.properties["name"] == "omnimarket"

    async def test_graph_edge_spec_model(self) -> None:
        edge = ModelGraphEdgeSpec(
            source_id="node_sample_effect",
            target_id="onex.cmd.omnimarket.sample-cmd.v1",
            edge_type="SUBSCRIBES_TO",
            source_authority="authoritative",
        )
        assert edge.edge_type == "SUBSCRIBES_TO"
        assert edge.source_authority == "authoritative"

    async def test_snapshot_meta_model(self) -> None:
        meta = ModelGraphSnapshotMeta(
            graph_schema_version="1.0.0",
            graph_snapshot_id=str(uuid4()),
            populated_from_commit_set=["abc123"],
            repo_count=12,
            node_count=10,
            edge_count=25,
        )
        assert meta.graph_schema_version == "1.0.0"
        assert meta.repo_count == 12

    async def test_handler_not_initialized_returns_error(self) -> None:
        handler = HandlerArchitectureGraphPopulate()
        request = ModelArchitectureGraphPopulateRequestedEvent(
            populate_id=str(uuid4()),
            operation="populate_from_contracts",
            omni_home="/tmp/fake_home",
        )
        response = await handler.execute(request)
        assert response.status == "error"
        assert "not initialized" in (response.error_message or "").lower()

    async def test_cypher_merge_from_contract_yaml(self) -> None:
        """Verify MERGE statements are generated from contract.yaml source data."""
        handler, mock_driver = _make_handler_with_mock_driver()

        mock_session = MagicMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_session.run = AsyncMock(return_value=MagicMock())

        # Parse sample contract into node/edge specs
        node_specs, edge_specs = handler._parse_contract_data(
            repo="omnimarket",
            node_name="node_sample_effect",
            contract=_SAMPLE_CONTRACT,
        )

        # Should generate an ONEXNode for the node
        onex_nodes = [n for n in node_specs if n.label == "ONEXNode"]
        assert len(onex_nodes) == 1
        assert onex_nodes[0].properties["name"] == "node_sample_effect"
        assert onex_nodes[0].properties["repo"] == "omnimarket"

        # Should generate KafkaTopic nodes for sub/pub topics
        topic_nodes = [n for n in node_specs if n.label == "KafkaTopic"]
        assert len(topic_nodes) == 2

        # Should generate SUBSCRIBES_TO and PUBLISHES_TO edges
        sub_edges = [e for e in edge_specs if e.edge_type == "SUBSCRIBES_TO"]
        pub_edges = [e for e in edge_specs if e.edge_type == "PUBLISHES_TO"]
        assert len(sub_edges) == 1
        assert len(pub_edges) == 1

        # All contract-derived edges must be authoritative
        for edge in sub_edges + pub_edges:
            assert edge.source_authority == "authoritative"

    async def test_cypher_merge_statements_use_merge_not_create(self) -> None:
        """MERGE statements must be idempotent — never CREATE."""
        handler, _ = _make_handler_with_mock_driver()
        node = ModelGraphNodeSpec(
            node_id="test_repo",
            label="Repository",
            properties={"name": "test_repo"},
        )
        cypher = handler._build_node_merge_cypher(node)
        assert cypher.strip().startswith("MERGE")
        assert "CREATE" not in cypher

    async def test_cypher_edge_merge_uses_merge(self) -> None:
        """Edge MERGE must be idempotent."""
        handler, _ = _make_handler_with_mock_driver()
        edge = ModelGraphEdgeSpec(
            source_id="node_a",
            target_id="topic_b",
            edge_type="PUBLISHES_TO",
            source_authority="authoritative",
        )
        cypher = handler._build_edge_merge_cypher(edge)
        assert "MERGE" in cypher
        assert "CREATE" not in cypher

    async def test_pyproject_deps_classified_as_evidence(self) -> None:
        """pyproject.toml-derived DEPENDS_ON edges are evidence, not authoritative."""
        handler, _ = _make_handler_with_mock_driver()
        edges = handler._parse_pyproject_deps(
            repo="omnimarket",
            pyproject=_SAMPLE_PYPROJECT,
        )
        depends_edges = [e for e in edges if e.edge_type == "DEPENDS_ON"]
        assert len(depends_edges) >= 2
        for edge in depends_edges:
            assert edge.source_authority == "evidence"

    async def test_populate_from_contracts_returns_response(self) -> None:
        """Full populate operation returns a well-formed response."""
        handler, mock_driver = _make_handler_with_mock_driver()

        mock_session = MagicMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_session.run = AsyncMock(return_value=MagicMock())

        request = ModelArchitectureGraphPopulateRequestedEvent(
            populate_id=str(uuid4()),
            operation="populate_from_contracts",
            omni_home="/tmp/fake_home",
            dry_run=True,
        )
        response = await handler.execute(request)
        # dry_run skips actual graph writes but returns metadata
        assert isinstance(response, ModelArchitectureGraphPopulateResponseEvent)
        assert response.populate_id == request.populate_id

    async def test_response_includes_snapshot_meta(self) -> None:
        """Response must include graph snapshot tracking metadata."""
        handler, mock_driver = _make_handler_with_mock_driver()

        mock_session = MagicMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_session.run = AsyncMock(return_value=MagicMock())

        request = ModelArchitectureGraphPopulateRequestedEvent(
            populate_id=str(uuid4()),
            operation="populate_from_contracts",
            omni_home="/tmp/fake_home",
            dry_run=True,
        )
        response = await handler.execute(request)
        assert response.snapshot_meta is not None
        assert response.snapshot_meta.graph_schema_version == "1.0.0"
        assert response.snapshot_meta.graph_snapshot_id is not None

    async def test_filter_by_authority_level(self) -> None:
        """Edges can be filtered by source_authority after populate."""
        handler, mock_driver = _make_handler_with_mock_driver()

        mock_session = MagicMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_session.run = AsyncMock(return_value=MagicMock())

        request = ModelArchitectureGraphPopulateRequestedEvent(
            populate_id=str(uuid4()),
            operation="populate_from_contracts",
            omni_home="/tmp/fake_home",
            dry_run=True,
        )
        response = await handler.execute(request)
        # All returned edges must have source_authority set
        for edge in response.edges_written:
            assert edge.source_authority in ("authoritative", "evidence")
