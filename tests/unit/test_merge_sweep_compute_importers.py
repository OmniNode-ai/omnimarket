# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Import-graph test: nothing outside the sweep node reads it (OMN-23854).

node_merge_sweep_compute is to be retired. Live nodes borrowed two shared
pieces from it: the GitHub transport error and the pull-request sweep models.
Those now live in ``omnimarket.models.github_landing`` and
``omnimarket.models.merge_sweep``. This test walks ``src`` with ``ast`` and
fails when any module outside the node itself and the deprecated triage
orchestrator imports ``omnimarket.nodes.node_merge_sweep_compute``.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
OLD_PACKAGE = "omnimarket.nodes.node_merge_sweep_compute"
ALLOWED_DIRS = (
    SRC_ROOT / "omnimarket" / "nodes" / "node_merge_sweep_compute",
    SRC_ROOT / "omnimarket" / "nodes" / "node_merge_sweep_triage_orchestrator",
)


def _imports_old_package(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == OLD_PACKAGE or node.module.startswith(OLD_PACKAGE + "."):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == OLD_PACKAGE or alias.name.startswith(
                    OLD_PACKAGE + "."
                ):
                    lines.append(node.lineno)
    return lines


def _offenders(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if any(path.is_relative_to(allowed) for allowed in ALLOWED_DIRS):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line in _imports_old_package(tree):
            found.append(f"{path.relative_to(root)}:{line}")
    return found


def test_no_module_outside_the_sweep_node_imports_it() -> None:
    assert _offenders(SRC_ROOT) == []


def test_the_walk_sees_an_importer_when_one_exists(tmp_path: Path) -> None:
    """Failing control: the same walk reports a planted importer."""
    planted = tmp_path / "omnimarket" / "nodes" / "node_other" / "handler.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(
        f"from {OLD_PACKAGE}.protocols import GitHubTransportError\n",
        encoding="utf-8",
    )
    assert _offenders(tmp_path) == ["omnimarket/nodes/node_other/handler.py:1"]


def test_new_homes_carry_the_moved_names() -> None:
    from omnimarket.models.github_landing.model_github_transport_error import (
        GitHubTransportError,
    )
    from omnimarket.models.merge_sweep.model_pr_info import ModelPRInfo

    assert issubclass(GitHubTransportError, Exception)
    assert ModelPRInfo.__module__ == "omnimarket.models.merge_sweep.model_pr_info"
