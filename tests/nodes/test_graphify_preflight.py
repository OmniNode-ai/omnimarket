# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for GraphifyAdapter preflight — probe() and get_import_graph()."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_dependency_health_sweep.engine.graphify_adapter import (
    GraphifyAdapter,
    ModelGraphifyProbeResult,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_graph_types import (
    ModelImportGraph,
)


@pytest.fixture
def two_file_root(tmp_path: Path) -> Path:
    """Create a small Python project: a.py imports b; c.py is unreachable."""
    (tmp_path / "a.py").write_text("import b\n")
    (tmp_path / "b.py").write_text("x = 1\n")
    (tmp_path / "c.py").write_text("y = 2\n")
    return tmp_path


class TestGraphifyProbeResult:
    def test_fields_present(self) -> None:
        result = ModelGraphifyProbeResult(
            available=True, version="0.8.2", commit_sha="abc123"
        )
        assert result.available is True
        assert result.version == "0.8.2"
        assert result.commit_sha == "abc123"

    def test_unavailable_probe(self) -> None:
        result = ModelGraphifyProbeResult(available=False, version="", commit_sha="")
        assert result.available is False


class TestGraphifyAdapterProbe:
    def test_probe_returns_model_graphify_probe_result(self) -> None:
        adapter = GraphifyAdapter()
        result = adapter.probe()
        assert isinstance(result, ModelGraphifyProbeResult)
        assert isinstance(result.available, bool)
        assert isinstance(result.version, str)
        assert isinstance(result.commit_sha, str)

    def test_probe_available_when_graphify_installed(self) -> None:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "graphify 0.8.2\n"
        with patch("subprocess.run", return_value=mock_proc):
            adapter = GraphifyAdapter()
            result = adapter.probe()
        assert result.available is True
        assert "0.8.2" in result.version

    def test_probe_unavailable_when_file_not_found(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            adapter = GraphifyAdapter()
            result = adapter.probe()
        assert result.available is False
        assert result.version == ""

    def test_probe_unavailable_when_nonzero_exit(self) -> None:
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        with patch("subprocess.run", return_value=mock_proc):
            adapter = GraphifyAdapter()
            result = adapter.probe()
        assert result.available is False


class TestGraphifyAdapterGetImportGraph:
    def test_returns_model_import_graph_regardless_of_availability(
        self, two_file_root: Path
    ) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            adapter = GraphifyAdapter()
            result = adapter.get_import_graph(root=two_file_root)
        assert isinstance(result, ModelImportGraph)
        assert isinstance(result.nodes, list)
        assert isinstance(result.edges, list)
        assert isinstance(result.orphan_modules, list)

    def test_ast_fallback_finds_import_edge(self, two_file_root: Path) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            adapter = GraphifyAdapter()
            result = adapter.get_import_graph(root=two_file_root)
        assert len(result.nodes) >= 2

    def test_ast_fallback_identifies_orphan_module(self, two_file_root: Path) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            adapter = GraphifyAdapter()
            result = adapter.get_import_graph(root=two_file_root)
        # c.py is not imported by anyone — it should appear in orphan_modules
        orphan_names = [Path(m).stem for m in result.orphan_modules]
        assert "c" in orphan_names

    def test_graphify_subprocess_timeout_raises_runtime_error(
        self, two_file_root: Path
    ) -> None:
        import subprocess

        adapter = GraphifyAdapter()
        # Pre-populate probe result so adapter believes graphify is available,
        # then force a timeout on the actual analysis subprocess call.
        adapter._probe_result = ModelGraphifyProbeResult(
            available=True, version="0.8.2", commit_sha="da4ec1d"
        )

        with (
            patch(
                "subprocess.run", side_effect=subprocess.TimeoutExpired("graphify", 120)
            ),
            pytest.raises(RuntimeError, match="timed out"),
        ):
            adapter.get_import_graph(root=two_file_root)

    def test_no_hardcoded_absolute_paths(self, two_file_root: Path) -> None:
        import omnimarket.nodes.node_dependency_health_sweep.engine.graphify_adapter as mod

        source = Path(mod.__file__).read_text()
        forbidden = [
            "/Users/",  # test-literal-ok: asserting source is free of hardcoded paths
            "/Volumes/",  # test-literal-ok: asserting source is free of hardcoded paths
        ]
        for pattern in forbidden:
            assert pattern not in source
