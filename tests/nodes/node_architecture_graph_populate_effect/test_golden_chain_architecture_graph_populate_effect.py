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

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import yaml

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


@pytest.fixture
def node_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_architecture_graph_populate_effect"
    )


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.mark.unit
class TestArchitectureGraphPopulateEffect:
    """Golden chain tests for node_architecture_graph_populate_effect."""

    def test_contract_declares_populated_topic(self, contract_path: Path) -> None:
        """OMN-13781 state-coverage gate: prove the populated-event topic is
        really contract-declared by parsing the live contract.yaml, not by
        repeating the literal in a self-tautological assertion."""
        contract = yaml.safe_load(contract_path.read_text())
        publish_topics = contract["event_bus"]["publish_topics"]
        assert "onex.evt.omnimarket.architecture-graph-populated.v1" in publish_topics

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

    async def test_collect_contract_data_ignores_vendored_venv_copies(
        self, tmp_path: Path
    ) -> None:
        """OMN-14583: a contract.yaml vendored into a consuming repo's own
        .venv (i.e. an installed dependency's node contract, not a native
        node of that repo) must never be attributed to the consuming repo.

        Before this fix, _collect_contract_data walked
        repo_path.rglob("contract.yaml") with no .venv exclusion, so every
        repo that depends on omnimarket as a pip dependency would have its
        own venv-vendored copy of every omnimarket node contract ingested
        and misattributed as repo=<consuming repo>."""
        handler, _ = _make_handler_with_mock_driver()

        repo_root = tmp_path / "sample_repo"

        # Native node — really owned by sample_repo, lives under src/.
        native_dir = repo_root / "src" / "sample_repo" / "nodes" / "node_native_thing"
        native_dir.mkdir(parents=True)
        (native_dir / "contract.yaml").write_text(
            yaml.dump({"name": "node_native_thing", "node_type": "effect"})
        )

        # Vendored dependency copy — an installed OTHER package's node,
        # nested under sample_repo's own .venv. Must be excluded entirely.
        vendored_dir = (
            repo_root
            / ".venv"
            / "lib"
            / "python3.12"
            / "site-packages"
            / "omnimarket"
            / "nodes"
            / "node_delegation_orchestrator"
        )
        vendored_dir.mkdir(parents=True)
        (vendored_dir / "contract.yaml").write_text(
            yaml.dump(
                {"name": "node_delegation_orchestrator", "node_type": "orchestrator"}
            )
        )

        nodes, _edges = handler._collect_contract_data(repo_root, "sample_repo")
        onex_node_names = {n.properties["name"] for n in nodes if n.label == "ONEXNode"}

        assert "node_native_thing" in onex_node_names
        assert "node_delegation_orchestrator" not in onex_node_names

    async def test_cypher_merge_statements_use_merge_not_create(self) -> None:
        """MERGE statements must be idempotent — never CREATE."""
        handler, _ = _make_handler_with_mock_driver()
        cypher = handler._build_node_batch_cypher("Repository")
        assert "MERGE" in cypher
        assert "CREATE" not in cypher
        # OMN-14295: batched — one UNWIND MERGE per label, not per node.
        assert "UNWIND" in cypher

    async def test_cypher_edge_merge_uses_merge(self) -> None:
        """Edge MERGE must be idempotent."""
        handler, _ = _make_handler_with_mock_driver()
        cypher = handler._build_edge_batch_cypher("PUBLISHES_TO")
        assert "MERGE" in cypher
        assert "CREATE" not in cypher
        assert "UNWIND" in cypher

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

    async def test_import_edges_create_matching_python_module_nodes(
        self, tmp_path: Path
    ) -> None:
        """OMN-14295: IMPORTS edges must MERGE against a real PythonModule
        node, not a node_id nothing else ever creates. Before this fix,
        _collect_import_edges returned only edges; the edge's MATCH
        (a {node_id: source_id}) always found zero rows because no node spec
        for that source_id was ever written, so the edge silently never
        landed."""
        handler, _ = _make_handler_with_mock_driver()

        repo_root = tmp_path / "sample_repo"
        module_dir = repo_root / "src" / "sample_repo" / "handlers"
        module_dir.mkdir(parents=True)
        (module_dir / "handler_foo.py").write_text("import omnibase_core\n")

        nodes, edges = handler._collect_import_edges(repo_root, "sample_repo")

        import_edges = [e for e in edges if e.edge_type == "IMPORTS"]
        assert len(import_edges) == 1
        module_nodes = [n for n in nodes if n.label == "PythonModule"]
        assert len(module_nodes) == 1

        # The edge's source_id must resolve to an actual node in the same
        # batch — this is exactly what the edge MERGE's MATCH depends on.
        assert import_edges[0].source_id == module_nodes[0].node_id
        assert import_edges[0].target_id == "omnibase_core"
        assert module_nodes[0].properties["repo"] == "sample_repo"

    async def test_import_edges_do_not_collide_on_bare_filename(
        self, tmp_path: Path
    ) -> None:
        """Two same-named files in different subdirectories (e.g. two
        handlers/__init__.py) must produce distinct PythonModule node_ids —
        the prior bare-filename-stem module_id collided them together."""
        handler, _ = _make_handler_with_mock_driver()

        repo_root = tmp_path / "sample_repo"
        src = repo_root / "src" / "sample_repo"
        (src / "node_a" / "handlers").mkdir(parents=True)
        (src / "node_b" / "handlers").mkdir(parents=True)
        (src / "node_a" / "handlers" / "__init__.py").write_text(
            "import omnibase_core\n"
        )
        (src / "node_b" / "handlers" / "__init__.py").write_text(
            "import omnibase_core\n"
        )

        nodes, _edges = handler._collect_import_edges(repo_root, "sample_repo")
        module_ids = {n.node_id for n in nodes if n.label == "PythonModule"}
        assert len(module_ids) == 2

    async def test_write_to_graph_batches_by_label_and_edge_type(self) -> None:
        """OMN-14295: writes must be UNWIND-batched, not one round trip per
        node/edge — assert the mock session.run call count reflects
        (label groups + edge_type groups), not len(nodes) + len(edges)."""
        handler, mock_driver = _make_handler_with_mock_driver()

        mock_session = MagicMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_result = MagicMock()
        mock_result.consume = AsyncMock(return_value=MagicMock())
        mock_session.run = AsyncMock(return_value=mock_result)

        nodes = [
            ModelGraphNodeSpec(
                node_id=f"repo_{i}", label="Repository", properties={"name": f"r{i}"}
            )
            for i in range(5)
        ] + [
            ModelGraphNodeSpec(
                node_id=f"node_{i}", label="ONEXNode", properties={"name": f"n{i}"}
            )
            for i in range(3)
        ]
        edges = [
            ModelGraphEdgeSpec(
                source_id=f"repo_{i}",
                target_id=f"node_{i % 3}",
                edge_type="CONTAINS",
                source_authority="authoritative",
            )
            for i in range(3)
        ]
        snapshot_meta = ModelGraphSnapshotMeta(
            graph_schema_version="1.0.0",
            graph_snapshot_id=str(uuid4()),
            repo_count=5,
            node_count=len(nodes),
            edge_count=len(edges),
        )

        await handler._write_to_graph(nodes, edges, snapshot_meta)

        # 2 label groups (Repository, ONEXNode) + 1 edge_type group
        # (CONTAINS) + 1 snapshot-meta write = 4 calls total — not
        # len(nodes) + len(edges) + 1 = 9.
        assert mock_session.run.await_count == 4
        for call in mock_session.run.await_args_list[:3]:
            cypher = call.args[0]
            assert "UNWIND" in cypher or "GraphSnapshot" in cypher

    async def test_write_to_graph_retries_transient_memgraph_error(self) -> None:
        """OMN-14295 harvest: a Memgraph TransientError on a batch write must
        be retried, not propagated on the first failure (adapted from
        omniarchon's retry_on_transient_error)."""
        handler, mock_driver = _make_handler_with_mock_driver()

        mock_session = MagicMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_result = MagicMock()
        mock_result.consume = AsyncMock(return_value=MagicMock())
        mock_session.run = AsyncMock(
            side_effect=[
                Exception("Memgraph.TransientError: conflicting transactions"),
                mock_result,  # retried node-batch write succeeds
                mock_result,  # snapshot-meta write
            ]
        )

        nodes = [
            ModelGraphNodeSpec(
                node_id="repo_x", label="Repository", properties={"name": "x"}
            )
        ]
        snapshot_meta = ModelGraphSnapshotMeta(
            graph_schema_version="1.0.0",
            graph_snapshot_id=str(uuid4()),
            repo_count=1,
            node_count=1,
            edge_count=0,
        )

        await handler._write_to_graph(nodes, [], snapshot_meta)

        # First call raised a transient error, second (retried) succeeded,
        # third is the snapshot-meta write.
        assert mock_session.run.await_count == 3

    async def test_write_to_graph_does_not_retry_non_transient_error(self) -> None:
        """A non-transient error (e.g. a real syntax error) must propagate
        immediately — the retry is scoped to transient conflicts only."""
        handler, mock_driver = _make_handler_with_mock_driver()

        mock_session = MagicMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_session.run = AsyncMock(
            side_effect=Exception("Memgraph.ClientError: syntax error")
        )

        nodes = [
            ModelGraphNodeSpec(
                node_id="repo_x", label="Repository", properties={"name": "x"}
            )
        ]
        snapshot_meta = ModelGraphSnapshotMeta(
            graph_schema_version="1.0.0",
            graph_snapshot_id=str(uuid4()),
            repo_count=1,
            node_count=1,
            edge_count=0,
        )

        with pytest.raises(Exception, match="syntax error"):
            await handler._write_to_graph(nodes, [], snapshot_meta)

        # No retry — exactly one attempt.
        assert mock_session.run.await_count == 1

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
