# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17694: the handler-coverage pass must walk the repository once.

``CrossReferenceEngine._pass_untested_handler`` asked, per contract handler,
"does any test file mention this stem?" — and answered it by re-walking the
whole repository from scratch, twice for the plain test globs and once more for
the golden-chain glob. With 408 contract handlers that is up to 1224 full
traversals for one sweep, and in a worktree lane the traversal crosses a 1.4 GB
``.venv``. That is where the sweep's 24-42 minute wall clock came from, and the
wall clock is what made the pre-commit gate's lock queue unsurvivable.

The corpus is now read once per pass. These tests pin that, and pin the two
verdict properties the change must not disturb.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_dependency_health_sweep.engine import scan_inputs
from omnimarket.nodes.node_dependency_health_sweep.engine.cross_reference import (
    CrossReferenceEngine,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_finding import (
    EnumDepHealthFindingType,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_graph_types import (
    ModelImportGraph,
    ModelTopologyGraph,
)

pytestmark = pytest.mark.unit


def _empty_graphs() -> tuple[ModelImportGraph, ModelTopologyGraph]:
    """Graphs that make every pass except the coverage pass a no-op."""
    return (
        ModelImportGraph(nodes=[], edges=[], orphan_modules=[]),
        ModelTopologyGraph(
            nodes=[],
            pub_edges=[],
            sub_edges=[],
            orphan_topics=[],
            undeclared_topics=[],
            topic_sources={},
            undeclared_topic_sources={},
        ),
    )


def _make_repo(root: Path, *, handler_count: int) -> tuple[Path, list[str]]:
    """A repo with ``handler_count`` contract handlers and no coverage at all."""
    src = root / "src" / "pkg"
    src.mkdir(parents=True)
    handlers: list[str] = []
    for index in range(handler_count):
        path = src / f"handler_{index}.py"
        path.write_text("def handle() -> None: ...\n")
        handlers.append(str(path))
    (root / "tests").mkdir()
    return root / "src", handlers


def _analyze(repo_root: Path, handlers: list[str]) -> list[str]:
    import_graph, topology = _empty_graphs()
    findings = CrossReferenceEngine().analyze(
        import_graph=import_graph,
        topology=topology,
        repo_label="omnimarket",
        repo_root=repo_root,
        contract_handler_paths=handlers,
    )
    return [
        f.symbol or ""
        for f in findings
        if f.finding_type is EnumDepHealthFindingType.UNTESTED_HANDLER
    ]


def test_repository_is_walked_once_not_once_per_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect, measured: traversals must not scale with handler count."""
    repo_root, handlers = _make_repo(tmp_path / "repo", handler_count=25)

    walks = 0
    real = scan_inputs.iter_coverage_corpus_files

    def counting(search_root: Path) -> list[Path]:
        nonlocal walks
        walks += 1
        return real(search_root)

    monkeypatch.setattr(scan_inputs, "iter_coverage_corpus_files", counting)

    _analyze(repo_root, handlers)

    assert walks == 1, f"25 handlers triggered {walks} repository traversals"


def test_a_handler_named_in_a_test_is_covered(tmp_path: Path) -> None:
    repo_root, handlers = _make_repo(tmp_path / "repo", handler_count=2)
    (repo_root.parent / "tests" / "test_handler_0.py").write_text(
        "from pkg import handler_0\n"
    )

    untested = _analyze(repo_root, handlers)

    assert "handler_0" not in untested
    assert "handler_1" in untested


def test_a_handler_named_only_in_a_golden_chain_fixture_is_covered(
    tmp_path: Path,
) -> None:
    repo_root, handlers = _make_repo(tmp_path / "repo", handler_count=1)
    (repo_root.parent / "tests" / "test_golden_chain_pkg.py").write_text(
        "CHAIN = ['handler_0']\n"
    )

    assert _analyze(repo_root, handlers) == []


def test_a_trailing_glob_test_file_still_counts(tmp_path: Path) -> None:
    """``*_test.py`` is the second glob the pass has always honoured."""
    repo_root, handlers = _make_repo(tmp_path / "repo", handler_count=1)
    (repo_root.parent / "tests" / "handler_0_test.py").write_text("handler_0\n")

    assert _analyze(repo_root, handlers) == []


def test_a_vendored_test_file_is_not_coverage_for_our_handler(
    tmp_path: Path,
) -> None:
    """A third-party package that happens to name our stem proves nothing."""
    repo_root, handlers = _make_repo(tmp_path / "repo", handler_count=1)
    vendored = repo_root.parent / ".venv" / "lib" / "site-packages" / "thirdparty"
    vendored.mkdir(parents=True)
    (vendored / "test_handler_0.py").write_text("handler_0\n")

    assert _analyze(repo_root, handlers) == ["handler_0"]


def test_non_source_directories_are_pruned_from_the_corpus(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_real.py").write_text("x\n")
    for pruned in (".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"):
        directory = root / pruned / "nested"
        directory.mkdir(parents=True)
        (directory / "test_vendored.py").write_text("x\n")

    found = [p.name for p in scan_inputs.iter_coverage_corpus_files(root)]

    assert found == ["test_real.py"]


def test_coverage_search_root_climbs_out_of_src(tmp_path: Path) -> None:
    """Tests live beside ``src/``, not inside it."""
    assert scan_inputs.coverage_search_root(tmp_path / "repo" / "src") == (
        tmp_path / "repo"
    )
    assert scan_inputs.coverage_search_root(tmp_path / "repo") == tmp_path / "repo"
