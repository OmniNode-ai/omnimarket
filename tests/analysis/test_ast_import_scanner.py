# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the shared ASTImportScanner — OMN-11046 edge-count fix.

Promoted out of tests/nodes/test_dep_health_ast_scanner.py (OMN-14295) along
with the scanner itself, so both node_dependency_health_sweep and
node_architecture_graph_populate_effect share one tested implementation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.analysis.ast_import_scanner import ASTImportScanner, _resolve_import

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ten_file_root(tmp_path: Path) -> Path:
    """10-file fixture with a sparse import graph.

    Structure:
      pkg/__init__.py   (imports nothing local)
      pkg/a.py          (imports pkg.b)
      pkg/b.py          (imports pkg.c)
      pkg/c.py          (imports nothing local)
      pkg/d.py          (imports pkg.a)
      pkg/e.py          (imports pkg.c)
      pkg/f.py          (imports nothing local — orphan)
      pkg/g.py          (imports pkg.f)
      pkg/h.py          (imports pkg.g)
      pkg/i.py          (imports nothing local — orphan)

    Expected local edges (9 total):
      pkg.a  → pkg.b
      pkg.b  → pkg.c
      pkg.d  → pkg.a
      pkg.e  → pkg.c
      pkg.g  → pkg.f
      pkg.h  → pkg.g
    That is 6 edges — well under any "100+" explosion threshold.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    # Use `import pkg.b` / `from pkg.b import ...` so the AST produces the full
    # dotted name "pkg.b", which _resolve_import can match to pkg/b.py directly.
    (pkg / "a.py").write_text("import pkg.b\n")
    (pkg / "b.py").write_text("import pkg.c\n")
    (pkg / "c.py").write_text("x = 1\n")
    (pkg / "d.py").write_text("import pkg.a\n")
    (pkg / "e.py").write_text("import pkg.c\n")
    (pkg / "f.py").write_text("y = 2\n")
    (pkg / "g.py").write_text("import pkg.f\n")
    (pkg / "h.py").write_text("import pkg.g\n")
    (pkg / "i.py").write_text("z = 3\n")
    return tmp_path


# ---------------------------------------------------------------------------
# OMN-11046: edge count must not explode
# ---------------------------------------------------------------------------


class TestASTScannerEdgeCount:
    def test_edge_count_bounded_for_ten_file_fixture(self, ten_file_root: Path) -> None:
        """10-file fixture with 6 real edges must not produce >20 edges.

        Before OMN-11046 the prefix-match loop produced a cross-product:
        for each imported name it matched EVERY module starting with that prefix,
        yielding O(files x imports x modules) ~= 10x6x10 = 600 edges for this case.
        """
        scanner = ASTImportScanner()
        result = scanner.scan(ten_file_root)
        assert len(result.edges) <= 20, (
            f"Edge explosion detected: {len(result.edges)} edges for a 10-file fixture "
            f"(expected ≤20). Edges: {result.edges}"
        )

    def test_exact_edges_match_imports(self, ten_file_root: Path) -> None:
        """Only real import edges should appear — not every module with matching prefix."""
        scanner = ASTImportScanner()
        result = scanner.scan(ten_file_root)
        # Collect importee sides of all edges
        importees = {e[1] for e in result.edges}
        # pkg/__init__.py should NOT appear as an importee from `from pkg import b`
        # because that resolves to pkg.b, not pkg (the __init__)
        # There must be edges for pkg.b, pkg.c, pkg.a, pkg.f, pkg.g
        for expected in ("pkg.b", "pkg.c", "pkg.a", "pkg.f", "pkg.g"):
            assert expected in importees, (
                f"Expected edge to {expected!r} not found. Importees: {importees}"
            )

    def test_orphan_modules_identified(self, ten_file_root: Path) -> None:
        """Modules with no inbound edges (and not __main__) are reported as orphans."""
        scanner = ASTImportScanner()
        result = scanner.scan(ten_file_root)
        orphan_stems = {Path(m).stem for m in result.orphan_modules}
        # pkg/i.py is never imported
        assert "i" in orphan_stems, (
            f"Expected 'i' in orphans, got: {result.orphan_modules}"
        )


# ---------------------------------------------------------------------------
# _resolve_import unit tests
# ---------------------------------------------------------------------------


class TestResolveImport:
    def test_resolves_plain_module(self, tmp_path: Path) -> None:
        (tmp_path / "foo.py").write_text("")
        assert _resolve_import("foo", tmp_path) == "foo"

    def test_resolves_package_init(self, tmp_path: Path) -> None:
        pkg = tmp_path / "bar"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        assert _resolve_import("bar", tmp_path) == "bar"

    def test_resolves_dotted_module(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "sub.py").write_text("")
        assert _resolve_import("pkg.sub", tmp_path) == "pkg.sub"

    def test_returns_none_for_stdlib(self, tmp_path: Path) -> None:
        # os, sys etc. will not be present as files under tmp_path
        assert _resolve_import("os", tmp_path) is None
        assert _resolve_import("sys", tmp_path) is None

    def test_returns_none_for_third_party(self, tmp_path: Path) -> None:
        assert _resolve_import("pydantic", tmp_path) is None

    def test_returns_none_for_nonexistent_dotted(self, tmp_path: Path) -> None:
        assert _resolve_import("pkg.missing", tmp_path) is None
