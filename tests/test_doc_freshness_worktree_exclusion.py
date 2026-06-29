"""Worktree-exclusion tests for node_doc_freshness_sweep (OMN-13521).

The freshness sweep walks the canonical repo clones, which contain nested git
worktrees under ``.claude/worktrees/`` and ``omni_worktrees/``. Those are
duplicate copies of the same source tree; scanning them inflated the md-file
count by ~50% (omniclaude) to ~90% (omnimarket) and multiplied the resolver
cost. ``_collect_md_files`` must skip nested-worktree directories.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_doc_freshness_sweep.handlers.handler_doc_freshness_sweep import (
    _collect_md_files,
)


@pytest.mark.unit
class TestCollectMdFilesExcludesNestedWorktrees:
    """`_collect_md_files` must not return docs from nested worktree copies."""

    def test_excludes_claude_worktrees(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        canonical = repo / "docs" / "guide.md"
        canonical.parent.mkdir(parents=True)
        canonical.write_text("# guide\n", encoding="utf-8")

        nested = repo / ".claude" / "worktrees" / "OMN-1" / "docs" / "guide.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("# dup\n", encoding="utf-8")

        out = _collect_md_files(repo, claude_md_only=False)
        rels = {str(p.relative_to(repo)) for p in out}
        assert "docs/guide.md" in rels
        assert all(".claude/worktrees" not in r for r in rels)

    def test_excludes_omni_worktrees(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        canonical = repo / "README.md"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text("# readme\n", encoding="utf-8")

        nested = repo / "omni_worktrees" / "OMN-2" / "repo" / "README.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("# dup\n", encoding="utf-8")

        out = _collect_md_files(repo, claude_md_only=False)
        rels = {str(p.relative_to(repo)) for p in out}
        assert "README.md" in rels
        assert all("omni_worktrees" not in r for r in rels)

    def test_excludes_nested_claude_md_in_worktrees(self, tmp_path: Path) -> None:
        """claude_md_only mode must also skip worktree copies of CLAUDE.md."""
        repo = tmp_path / "repo"
        canonical = repo / "CLAUDE.md"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text("# canonical\n", encoding="utf-8")

        nested = repo / ".claude" / "worktrees" / "OMN-3" / "CLAUDE.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("# dup\n", encoding="utf-8")

        out = _collect_md_files(repo, claude_md_only=True)
        rels = {str(p.relative_to(repo)) for p in out}
        assert rels == {"CLAUDE.md"}

    def test_top_level_doc_named_worktrees_kept(self, tmp_path: Path) -> None:
        """A real doc whose name merely contains 'worktrees' is NOT excluded.

        Exclusion is path-segment scoped, so ``docs/using_worktrees.md`` (a
        legitimate doc) survives — only an actual ``worktrees`` directory
        segment is pruned.
        """
        repo = tmp_path / "repo"
        doc = repo / "docs" / "using_worktrees.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("# worktrees guide\n", encoding="utf-8")

        out = _collect_md_files(repo, claude_md_only=False)
        rels = {str(p.relative_to(repo)) for p in out}
        assert "docs/using_worktrees.md" in rels
