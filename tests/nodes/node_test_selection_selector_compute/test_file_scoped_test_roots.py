# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A changed file under a file-scoped test root selects itself and its importers, not the root.

``tests/unit/`` alone records 5305 s against the selector's 1800 s bound, so a change to any one file
under it selected the whole directory and took the full suite (reason ``narrowed_selection_too_heavy``).
A root named in ``file_scoped_test_roots`` selects the changed test file, the tests that import a
changed helper (transitively, from any directory), and for a nested ``conftest.py`` or ``__init__.py``
the directory it sits in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_test_selection_selector_compute.adapters.adjacency_file import (
    load_adjacency_map,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_path_resolution import (
    HandlerTestPathResolution,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelAdjacencyEntry,
    ModelAdjacencyMap,
    ModelPathResolutionRequest,
    ModelThresholds,
)
from scripts.ci.detect_test_paths import REPO_ROOT

pytestmark = pytest.mark.unit

ALWAYS = "tests/gates/"


def _adjacency(roots: list[str]) -> ModelAdjacencyMap:
    return ModelAdjacencyMap(
        schema_version=1,
        shared_modules=[],
        thresholds=ModelThresholds(modules_changed_for_full_suite=6),
        test_infrastructure_paths=[],
        adjacency={"nodes": ModelAdjacencyEntry()},
        always_selected_paths=[ALWAYS],
        node_scoped_modules=["nodes"],
        file_scoped_test_roots=roots,
    )


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree(root: Path) -> None:
    """tests/unit with a helper in one node directory, an importer in another and a bystander."""
    _write(root, "tests/__init__.py")
    _write(root, "tests/gates/test_gate.py", "def test_g() -> None: ...\n")
    _write(root, "tests/unit/__init__.py")
    _write(root, "tests/unit/nodes/__init__.py")
    _write(root, "tests/unit/nodes/node_a/__init__.py")
    _write(root, "tests/unit/nodes/node_b/__init__.py")
    _write(root, "tests/unit/nodes/node_c/__init__.py")
    _write(
        root,
        "tests/unit/nodes/node_a/fakes.py",
        "def make() -> int:\n    return 1\n",
    )
    _write(
        root,
        "tests/unit/nodes/node_a/test_a.py",
        "def test_a() -> None: ...\n",
    )
    _write(
        root,
        "tests/unit/nodes/node_b/test_b.py",
        "from tests.unit.nodes.node_a.fakes import make\n\n\n"
        "def test_b() -> None:\n    assert make()\n",
    )
    _write(
        root,
        "tests/unit/nodes/node_c/test_c.py",
        "def test_c() -> None: ...\n",
    )
    _write(root, "tests/unit/nodes/node_c/conftest.py", "")


def _select(root: Path, roots: list[str], *changed: str) -> tuple[str, ...]:
    return (
        HandlerTestPathResolution()
        .handle(
            ModelPathResolutionRequest(
                changed_files=changed,
                adjacency=_adjacency(roots),
                repo_root=str(root),
            )
        )
        .selected_paths
    )


def test_changed_test_file_under_a_scoped_root_selects_only_itself(
    tmp_path: Path,
) -> None:
    """Known good: the unit directory is not selected as a whole."""
    _tree(tmp_path)
    selected = _select(tmp_path, ["tests/unit/"], "tests/unit/nodes/node_a/test_a.py")
    assert "tests/unit/nodes/node_a/test_a.py" in selected
    assert "tests/unit/" not in selected
    assert "tests/unit/nodes/node_b/test_b.py" not in selected
    assert "tests/unit/nodes/node_c/test_c.py" not in selected


def test_changed_helper_still_selects_the_importer_in_another_directory(
    tmp_path: Path,
) -> None:
    """Known bad: the test a broken helper breaks lives in another node's directory."""
    _tree(tmp_path)
    selected = _select(tmp_path, ["tests/unit/"], "tests/unit/nodes/node_a/fakes.py")
    assert "tests/unit/nodes/node_b/test_b.py" in selected
    assert "tests/unit/" not in selected


def test_unscoped_root_keeps_the_whole_directory(tmp_path: Path) -> None:
    """Control: with no scoped root the change selects tests/unit/ as before."""
    _tree(tmp_path)
    selected = _select(tmp_path, [], "tests/unit/nodes/node_a/test_a.py")
    assert "tests/unit/" in selected


def test_nested_conftest_selects_its_directory_not_the_tree(tmp_path: Path) -> None:
    _tree(tmp_path)
    selected = _select(tmp_path, ["tests/unit/"], "tests/unit/nodes/node_c/conftest.py")
    assert "tests/unit/nodes/node_c/" in selected
    assert "tests/" not in selected
    assert "tests/unit/" not in selected


def test_root_conftest_still_selects_the_whole_tree(tmp_path: Path) -> None:
    _tree(tmp_path)
    _write(tmp_path, "tests/conftest.py")
    selected = _select(tmp_path, ["tests/unit/"], "tests/conftest.py")
    assert "tests/" in selected


def test_shipped_adjacency_scopes_the_unit_tree() -> None:
    adjacency = load_adjacency_map(
        REPO_ROOT / "scripts" / "ci" / "test_selection_adjacency.yaml"
    )
    assert "tests/unit/" in adjacency.file_scoped_test_roots
