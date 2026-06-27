# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_doc_freshness_sweep (OMN-13675, WS-5 Wave 1).

Variant A (pure COMPUTE): builds a synthetic ``$OMNI_HOME`` repo tree of real ``.md``
docs under ``tmp_path`` and drives ``NodeDocFreshnessSweep.handle`` in-process against
the REAL ``onex_change_control`` scanners (extract -> resolve -> freshness). No mocking
of the scanner boundary: a backticked file reference to a missing file genuinely
resolves to ``exists=False`` and yields a real ``BROKEN`` verdict.

The handler reaches ``onex_change_control``; when that package is unavailable the
handler returns ``status="error"`` and there is nothing real to assert, so the suite
skips rather than fakes a pass.

Negative control: a doc with a broken file reference must drive ``broken_count >= 1``
and ``status="issues_found"``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_doc_freshness_sweep.handlers.handler_doc_freshness_sweep import (
    _OCC_AVAILABLE,
    DocFreshnessSweepRequest,
    NodeDocFreshnessSweep,
)

pytestmark = pytest.mark.skipif(
    not _OCC_AVAILABLE,
    reason="onex_change_control scanners not installed — real doc-freshness scan "
    "cannot run; skipping rather than faking a pass (OMN-13675).",
)

_GOOD_DOC = "# Good\nReferences `src/real.py` which exists in the repo.\n"
_BROKEN_DOC = "# Bad\nReferences `src/missing_xyz.py` which is absent.\n"


def _build_repo(omni_home: Path, repo: str, docs: dict[str, str]) -> None:
    repo_root = omni_home / repo
    (repo_root / "src").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "real.py").write_text("x = 1\n", encoding="utf-8")
    docs_dir = repo_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for name, content in docs.items():
        (docs_dir / name).write_text(content, encoding="utf-8")


# (docs map, request_kwargs, expected_status, expected_min_broken, expected_min_docs)
CASES = [
    pytest.param(
        {"good.md": _GOOD_DOC},
        {"dry_run": True},
        "healthy",
        0,
        1,
        id="fresh-doc-healthy",
    ),
    pytest.param(
        {"bad.md": _BROKEN_DOC},
        {"dry_run": True},
        "issues_found",
        1,
        1,
        id="broken-reference-negative-control",
    ),
    pytest.param(
        {"good.md": _GOOD_DOC, "bad.md": _BROKEN_DOC},
        {"dry_run": True},
        "issues_found",
        1,
        2,
        id="mixed-tree-fresh-plus-broken",
    ),
    pytest.param(
        {"good.md": _GOOD_DOC, "bad.md": _BROKEN_DOC},
        {"broken_only": True, "dry_run": True},
        "issues_found",
        1,
        1,
        id="broken-only-filters-fresh-docs",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("docs", "kwargs", "expected_status", "min_broken", "min_docs"), CASES
)
def test_doc_freshness_sweep_multiparam(
    tmp_path: Path,
    docs: dict[str, str],
    kwargs: dict[str, Any],
    expected_status: str,
    min_broken: int,
    min_docs: int,
) -> None:
    omni_home = tmp_path / "omni_home"
    _build_repo(omni_home, "myrepo", docs)
    request = DocFreshnessSweepRequest(
        omni_home=str(omni_home), repos=["myrepo"], **kwargs
    )

    result = NodeDocFreshnessSweep().handle(request)

    assert result.status == expected_status
    assert result.repos_scanned == ["myrepo"]
    assert result.total_docs >= min_docs
    assert result.broken_count >= min_broken
    assert result.broken_reference_count >= min_broken
    # broken_only mode must not surface any non-broken doc.
    if kwargs.get("broken_only"):
        assert result.fresh_count == 0
        assert result.stale_count == 0


@pytest.mark.integration
def test_doc_freshness_claude_md_only_scopes_scan(tmp_path: Path) -> None:
    """claude_md_only restricts scanning to CLAUDE.md; absent -> no docs scanned."""
    omni_home = tmp_path / "omni_home"
    _build_repo(omni_home, "myrepo", {"bad.md": _BROKEN_DOC})
    # No CLAUDE.md in the repo -> claude_md_only must scan zero docs.
    result = NodeDocFreshnessSweep().handle(
        DocFreshnessSweepRequest(
            omni_home=str(omni_home),
            repos=["myrepo"],
            claude_md_only=True,
            dry_run=True,
        )
    )
    assert result.total_docs == 0
    assert result.broken_count == 0
    assert result.status == "healthy"

    # Now add a CLAUDE.md that references a missing file -> it is the only doc scanned.
    (omni_home / "myrepo" / "CLAUDE.md").write_text(_BROKEN_DOC, encoding="utf-8")
    result2 = NodeDocFreshnessSweep().handle(
        DocFreshnessSweepRequest(
            omni_home=str(omni_home),
            repos=["myrepo"],
            claude_md_only=True,
            dry_run=True,
        )
    )
    assert result2.total_docs == 1
    assert result2.broken_count == 1


@pytest.mark.integration
def test_doc_freshness_missing_repo_errors(tmp_path: Path) -> None:
    """A repo that does not exist under omni_home fails loud (status=error)."""
    omni_home = tmp_path / "omni_home"
    omni_home.mkdir()
    result = NodeDocFreshnessSweep().handle(
        DocFreshnessSweepRequest(
            omni_home=str(omni_home), repos=["nonexistent_repo"], dry_run=True
        )
    )
    assert result.status == "error"
    assert result.error is not None
