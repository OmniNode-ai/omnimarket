# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerDepHealthSweep repo-label computation — OMN-11047.

OMN-14295: the ASTImportScanner tests that used to live in this file moved to
tests/analysis/test_ast_import_scanner.py alongside the scanner itself (now
shared via omnimarket.analysis, so node_architecture_graph_populate_effect
can reuse it too). What remains here is genuinely handler-specific
(repo_label / contract-path computation), not scanner behavior.
"""

from __future__ import annotations

from pathlib import Path


class TestRepoLabel:
    """Verify the handler computes repo_label correctly via the handler's logic."""

    def test_label_strips_src_suffix(self) -> None:
        """If the resolved root ends with /src, the label should be the parent name."""
        # Simulate the handler logic directly (no need to invoke full handler)
        from pathlib import PurePosixPath

        def compute_label(resolved: PurePosixPath) -> str:
            return resolved.parent.name if resolved.name == "src" else resolved.name

        root_with_src = PurePosixPath("/some/path/omnimarket/src")
        assert compute_label(root_with_src) == "omnimarket"

    def test_label_uses_name_when_not_src(self) -> None:
        from pathlib import PurePosixPath

        def compute_label(resolved: PurePosixPath) -> str:
            return resolved.parent.name if resolved.name == "src" else resolved.name

        root_without_src = PurePosixPath("/some/path/omnimarket")
        assert compute_label(root_without_src) == "omnimarket"

    def test_handler_repo_label_not_src(self, tmp_path: Path) -> None:
        """End-to-end: passing a /src path to the handler must not label it 'src'."""
        from unittest.mock import MagicMock, patch

        from omnimarket.nodes.node_dependency_health_sweep.handlers.handler_dep_health_sweep import (
            HandlerDepHealthSweep,
        )
        from omnimarket.nodes.node_dependency_health_sweep.models import (
            ModelDepHealthSweepRequest,
        )

        # Build a minimal src tree so handler doesn't bail on missing dir
        src_dir = tmp_path / "myrepo" / "src"
        src_dir.mkdir(parents=True)
        (src_dir / "dummy.py").write_text("x = 1\n")

        mock_graph = MagicMock()
        mock_graph.edges = []
        mock_graph.orphan_modules = []
        mock_graph.nodes = []

        mock_topology = MagicMock()
        mock_topology.handler_modules = []

        mock_cross_ref = MagicMock()
        mock_cross_ref.analyze.return_value = []

        handler = HandlerDepHealthSweep()

        captured_labels: list[str] = []

        def capturing_analyze(**kwargs: object) -> list[object]:
            captured_labels.append(str(kwargs.get("repo_label", "")))
            return []

        with (
            patch.object(handler._graphify_runner, "run", return_value=mock_graph),
            patch.object(handler._topology_parser, "parse", return_value=mock_topology),
            patch.object(
                handler._cross_ref_engine,
                "analyze",
                side_effect=lambda **kw: capturing_analyze(**kw),
            ),
        ):
            handler.handle(
                ModelDepHealthSweepRequest(
                    repo_roots=[str(src_dir)],
                )
            )

        assert captured_labels, "analyze() was never called"
        for label in captured_labels:
            assert label != "src", (
                f"repo_label was 'src' — OMN-11047 fix not applied. "
                f"Expected 'myrepo', got {label!r}"
            )
            assert label == "myrepo", f"Expected 'myrepo', got {label!r}"

    def test_contract_handler_paths_do_not_duplicate_src(self, tmp_path: Path) -> None:
        """Passing a /src repo root must not produce a src/src handler path."""
        from omnimarket.nodes.node_dependency_health_sweep.handlers.handler_dep_health_sweep import (
            HandlerDepHealthSweep,
        )

        src_dir = tmp_path / "myrepo" / "src"
        contract_dir = src_dir / "omnimarket" / "nodes" / "node_example"
        contract_dir.mkdir(parents=True)
        (contract_dir / "contract.yaml").write_text(
            "\n".join(
                [
                    "handler_routing:",
                    "  handlers:",
                    "    - handler_module: omnimarket.nodes.node_example.handlers.handler_example",
                    "",
                ]
            )
        )

        handler = HandlerDepHealthSweep()
        paths = handler._collect_contract_handler_paths([str(src_dir)])

        assert paths == [
            str(src_dir / "omnimarket/nodes/node_example/handlers/handler_example.py")
        ]
        assert all("/src/src/" not in path for path in paths)
