# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for GraphifyRunner — stable run() interface over the adapter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from omnimarket.nodes.node_dependency_health_sweep.engine.graphify_runner import (
    GraphifyRunner,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_graph_types import (
    ModelImportGraph,
)


@pytest.fixture
def import_chain_root(tmp_path: Path) -> Path:
    """a.py imports b; c.py is unreachable."""
    (tmp_path / "a.py").write_text("import b\n")
    (tmp_path / "b.py").write_text("x = 1\n")
    (tmp_path / "c.py").write_text("y = 2\n")
    return tmp_path


class TestGraphifyRunner:
    def test_run_returns_model_import_graph(self, import_chain_root: Path) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            runner = GraphifyRunner()
            result = runner.run(root=import_chain_root)
        assert isinstance(result, ModelImportGraph)

    def test_run_contains_edge_from_a_to_b(self, import_chain_root: Path) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            runner = GraphifyRunner()
            result = runner.run(root=import_chain_root)
        # Edges are (importer, importee) pairs; a imports b
        importers = [e[0] for e in result.edges]
        assert any("a" in imp for imp in importers), (
            f"Expected edge from a, got edges: {result.edges}"
        )

    def test_run_identifies_orphan_c(self, import_chain_root: Path) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            runner = GraphifyRunner()
            result = runner.run(root=import_chain_root)
        orphan_stems = [Path(m).stem for m in result.orphan_modules]
        assert "c" in orphan_stems, (
            f"Expected c in orphans, got: {result.orphan_modules}"
        )

    def test_run_does_not_expose_graphify_internals(
        self, import_chain_root: Path
    ) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            runner = GraphifyRunner()
            result = runner.run(root=import_chain_root)
        # Result must be ModelImportGraph — no raw subprocess output leaks
        assert isinstance(result, ModelImportGraph)
        assert not hasattr(result, "stdout")
        assert not hasattr(result, "stderr")

    def test_no_hardcoded_absolute_paths(self) -> None:
        import omnimarket.nodes.node_dependency_health_sweep.engine.graphify_runner as mod

        source = Path(mod.__file__).read_text()
        assert "/Users/" not in source
        assert "/Volumes/" not in source

    def test_runner_delegates_to_adapter(self, import_chain_root: Path) -> None:
        from unittest.mock import MagicMock

        mock_graph = ModelImportGraph(nodes=["x"], edges=[], orphan_modules=[])
        mock_adapter = MagicMock()
        mock_adapter.get_import_graph.return_value = mock_graph

        runner = GraphifyRunner(adapter=mock_adapter)
        result = runner.run(root=import_chain_root)

        mock_adapter.get_import_graph.assert_called_once_with(root=import_chain_root)
        assert result is mock_graph
