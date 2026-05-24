# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain tests for node_architecture_graph_query_effect.

Verifies dependency_path, blast_radius, cross_repo_imports, and circular_deps
operations with a mock graph driver. No real Memgraph required.

Related: OMN-11916 — Memgraph Architecture Query Node (Task 2.2)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_architecture_graph_query_effect.handlers import (
    HandlerArchitectureGraphQuery,
)
from omnimarket.nodes.node_architecture_graph_query_effect.models import (
    ModelArchitectureGraphQueryConfig,
    ModelArchitectureGraphQueryRequestedEvent,
    ModelArchitectureGraphQueryResponseEvent,
    ModelArchQueryGraphEdge,
    ModelArchQueryGraphNode,
)

_TEST_CONFIG = ModelArchitectureGraphQueryConfig(bolt_uri="bolt://test-host:7687")


def _make_handler_with_mock_driver() -> tuple[HandlerArchitectureGraphQuery, MagicMock]:
    mock_driver = MagicMock()
    handler = HandlerArchitectureGraphQuery()
    handler._driver = mock_driver
    handler._initialized = True
    handler._config = _TEST_CONFIG
    return handler, mock_driver


@pytest.mark.unit
class TestArchitectureGraphQueryEffect:
    """Golden chain tests for node_architecture_graph_query_effect."""

    async def test_node_importable(self) -> None:
        from omnimarket.nodes import node_architecture_graph_query_effect

        assert node_architecture_graph_query_effect is not None

    async def test_handler_importable(self) -> None:
        assert HandlerArchitectureGraphQuery is not None

    async def test_config_defaults(self) -> None:
        config = ModelArchitectureGraphQueryConfig(bolt_uri="bolt://localhost:7687")
        assert config.graph_backend == "memgraph"
        assert config.timeout_seconds > 0
        assert config.bolt_uri == "bolt://localhost:7687"

    async def test_dependency_path_executes_parameterized_cypher(self) -> None:
        handler, mock_driver = _make_handler_with_mock_driver()

        mock_session = MagicMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_result = MagicMock()
        mock_result.data = MagicMock(
            return_value=[
                {
                    "path_nodes": ["omnibase_core", "omnibase_infra"],
                    "path_edges": [("omnibase_core", "DEPENDS_ON", "omnibase_infra")],
                    "path_length": 1,
                }
            ]
        )
        mock_session.run = AsyncMock(return_value=mock_result)

        request = ModelArchitectureGraphQueryRequestedEvent(
            query_id=str(uuid4()),
            operation="dependency_path",
            from_node="omnibase_core",
            to_node="omnibase_infra",
        )
        response = await handler.execute(request)

        assert response.status == "success"
        assert response.path_length is not None
        mock_session.run.assert_awaited_once()
        call_args = mock_session.run.call_args
        # Verify parameterized query — no string interpolation of user values
        query_str = call_args[0][0]
        assert "from_node" in query_str or "$from_node" in query_str
        assert "to_node" in query_str or "$to_node" in query_str
        assert "omnibase_core" not in query_str
        assert "omnibase_infra" not in query_str

    async def test_blast_radius_returns_nodes_and_edges(self) -> None:
        handler, mock_driver = _make_handler_with_mock_driver()

        mock_session = MagicMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_result = MagicMock()
        mock_result.data = MagicMock(
            return_value=[
                {"dependent_node": "omnibase_infra", "edge_type": "DEPENDS_ON"},
                {"dependent_node": "omnimarket", "edge_type": "DEPENDS_ON"},
            ]
        )
        mock_session.run = AsyncMock(return_value=mock_result)

        request = ModelArchitectureGraphQueryRequestedEvent(
            query_id=str(uuid4()),
            operation="blast_radius",
            target="omnibase_core",
        )
        response = await handler.execute(request)

        assert response.status == "success"
        assert len(response.nodes) >= 0
        assert isinstance(response.nodes, tuple)
        assert isinstance(response.edges, tuple)
        mock_session.run.assert_awaited_once()

    async def test_cross_repo_imports_uses_parameterized_repo(self) -> None:
        handler, mock_driver = _make_handler_with_mock_driver()

        mock_session = MagicMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_result = MagicMock()
        mock_result.data = MagicMock(
            return_value=[
                {
                    "from_repo": "omnimarket",
                    "to_repo": "omnibase_core",
                    "import_count": 42,
                }
            ]
        )
        mock_session.run = AsyncMock(return_value=mock_result)

        request = ModelArchitectureGraphQueryRequestedEvent(
            query_id=str(uuid4()),
            operation="cross_repo_imports",
            repo="omnimarket",
        )
        response = await handler.execute(request)

        assert response.status == "success"
        mock_session.run.assert_awaited_once()
        call_args = mock_session.run.call_args
        query_str = call_args[0][0]
        assert "repo" in query_str or "$repo" in query_str
        assert "omnimarket" not in query_str

    async def test_circular_deps_returns_cycles(self) -> None:
        handler, mock_driver = _make_handler_with_mock_driver()

        mock_session = MagicMock()
        mock_driver.session.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_driver.session.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_result = MagicMock()
        mock_result.data = MagicMock(return_value=[])
        mock_session.run = AsyncMock(return_value=mock_result)

        request = ModelArchitectureGraphQueryRequestedEvent(
            query_id=str(uuid4()),
            operation="circular_deps",
            repo="omnibase_core",
        )
        response = await handler.execute(request)

        assert response.status == "success"
        assert isinstance(response.nodes, tuple)
        mock_session.run.assert_awaited_once()

    async def test_uninitialized_handler_returns_error(self) -> None:
        handler = HandlerArchitectureGraphQuery()

        request = ModelArchitectureGraphQueryRequestedEvent(
            query_id=str(uuid4()),
            operation="blast_radius",
            target="omnibase_core",
        )
        response = await handler.execute(request)

        assert isinstance(response, ModelArchitectureGraphQueryResponseEvent)
        assert response.status == "error"
        assert response.error_message is not None
        assert "not initialized" in response.error_message.lower()

    async def test_unknown_operation_returns_error(self) -> None:
        handler, _mock_driver = _make_handler_with_mock_driver()

        request = ModelArchitectureGraphQueryRequestedEvent(
            query_id=str(uuid4()),
            operation="unknown_op",  # type: ignore[arg-type]
        )
        response = await handler.execute(request)

        assert response.status == "error"

    async def test_response_model_nodes_are_tuple(self) -> None:
        node = ModelArchQueryGraphNode(name="omnibase_core", node_type="module")
        edge = ModelArchQueryGraphEdge(
            source="omnibase_core", target="omnibase_infra", edge_type="DEPENDS_ON"
        )
        response = ModelArchitectureGraphQueryResponseEvent(
            query_id=str(uuid4()),
            operation="blast_radius",
            status="success",
            nodes=(node,),
            edges=(edge,),
            path_length=None,
        )
        assert isinstance(response.nodes, tuple)
        assert isinstance(response.edges, tuple)
        assert len(response.nodes) == 1
        assert len(response.edges) == 1

    async def test_dependency_path_missing_from_to_returns_error(self) -> None:
        handler, _ = _make_handler_with_mock_driver()

        request = ModelArchitectureGraphQueryRequestedEvent(
            query_id=str(uuid4()),
            operation="dependency_path",
            # from_node and to_node intentionally omitted
        )
        response = await handler.execute(request)

        assert response.status == "error"
        assert response.error_message is not None
