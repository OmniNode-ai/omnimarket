# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Content-reachability classifier for merge-time worktree closeout (OMN-15251).

``dirty`` and ``unpushed`` are *filesystem* facts, not *content* facts. The
OMN-13859 handler classified any dirty worktree as ``SKIPPED_DIRTY`` and emitted
a decision request — which is correct only when the dirty content is genuinely
unlanded.

Live evidence (audited 2026-07-27, OMN-15251): the four retained OMN-14974
worktrees were all flagged dirty/unpushed, and **not one carried a byte absent
from ``origin/dev``**. Every hunk had landed via omnibase_infra#2490 and
omninode_infra#684-#697. Two of the four held content dev had deliberately
*reverted*, so promoting them would have regressed dev. The closeout produced
four operator decisions with nothing behind them, and that noise is exactly what
buries a real LOST_WORK entry.

The fix is a third classification keyed on content, not on filesystem state:

  * ``PRUNED``            - no dirty paths at all.
  * ``PRUNED_SUPERSEDED`` - dirty, but every dirty path's working content is
                            reachable from the merge target. Safe to remove.
  * ``SKIPPED_DIRTY``     - dirty with at least one path NOT reachable from the
                            merge target. Real recoverability debt: never
                            removed, always reported with the offending paths.

Fail-closed is preserved end to end: no merge target, an adapter error, a
deleted file, or any doubt whatsoever resolves to ``SKIPPED_DIRTY``. The
classifier may only ever *shrink* the set of things that get deleted relative to
"is it clean"; it can never authorize removing content it has not positively
proven reachable.

Related:
    - OMN-15251: merge-time worktree closeout mechanism + dirty-worktree audit.
    - OMN-13859: the event-driven prune this extends.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.events.worktree_prune import EnumPruneOutcome, ModelWorktreePruneCommand
from omnimarket.nodes.node_pr_lifecycle_worktree_prune_effect.handlers.handler_worktree_prune import (
    HandlerWorktreePrune,
)

_MERGE_TARGET = "origin/dev"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class FakeContentGitAdapter:
    """Fake git adapter with a scripted merge-target blob table.

    ``ref_contents`` maps a repo-relative path to the content at the merge
    target; a missing key means the path does not exist at that ref.
    ``working_contents`` is the same for the working tree.
    """

    def __init__(
        self,
        *,
        porcelain: str = "",
        ref_contents: dict[str, str] | None = None,
        working_contents: dict[str, str] | None = None,
        history: dict[str, list[str]] | None = None,
        raise_on_content: bool = False,
    ) -> None:
        self._porcelain = porcelain
        self._ref_contents = ref_contents or {}
        self._working_contents = working_contents or {}
        self._history = history or {}
        self._raise_on_content = raise_on_content
        self.remove_calls: list[tuple[str, str]] = []
        self.content_calls: list[tuple[str, str]] = []
        self.history_calls: list[tuple[str, str]] = []

    def content_sha_in_ref_history(
        self, worktree_path: str, ref: str, rel_path: str, content_sha: str
    ) -> bool:
        if self._raise_on_content:
            raise RuntimeError("git rev-list exploded")
        self.history_calls.append((ref, rel_path))
        return any(
            _sha(body) == content_sha for body in self._history.get(rel_path, [])
        )

    def status_porcelain(self, worktree_path: str) -> str:
        return self._porcelain

    def git_common_dir(self, worktree_path: str) -> str:
        return str(Path(worktree_path) / ".git")

    def worktree_remove(self, canonical_root: str, worktree_path: str) -> None:
        self.remove_calls.append((canonical_root, worktree_path))

    def content_sha_at_ref(
        self, worktree_path: str, ref: str, rel_path: str
    ) -> str | None:
        if self._raise_on_content:
            raise RuntimeError("git cat-file exploded")
        self.content_calls.append((ref, rel_path))
        content = self._ref_contents.get(rel_path)
        return None if content is None else _sha(content)

    def working_content_sha(self, worktree_path: str, rel_path: str) -> str | None:
        if self._raise_on_content:
            raise RuntimeError("git hash-object exploded")
        content = self._working_contents.get(rel_path)
        return None if content is None else _sha(content)


def _make_worktree(root: Path, ticket: str, repo: str) -> Path:
    wt = root / ticket / repo
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
    return wt


def _command(
    root: Path,
    *,
    ticket: str = "OMN-14974",
    repo: str = "omnibase_infra",
    merge_target_ref: str | None = _MERGE_TARGET,
    dry_run: bool = False,
) -> ModelWorktreePruneCommand:
    return ModelWorktreePruneCommand(
        correlation_id=uuid4(),
        ticket_id=ticket,
        repo=repo,
        worktrees_root=str(root),
        merge_target_ref=merge_target_ref,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# The F-09 case: dirty, but every byte already on the merge target.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dirty_but_fully_landed_worktree_is_pruned_as_superseded(
    tmp_path: Path,
) -> None:
    """The live OMN-14974 case: modified paths identical to the merge target.

    Pre-fix this returned SKIPPED_DIRTY and manufactured an operator decision
    over content that was already on dev.
    """
    root = tmp_path / "worktrees"
    wt = _make_worktree(root, "OMN-14974", "omninode_infra")
    landed = {
        "k8s/onex-dev/runtime/patch-recovery-drift-omninode-runtime.yaml": "memory: 2Gi\n",
        "aws/cluster-dev/managed-data-plane.auto.tfvars": "broker = m5.large\n",
    }
    adapter = FakeContentGitAdapter(
        porcelain=(
            " M k8s/onex-dev/runtime/patch-recovery-drift-omninode-runtime.yaml\n"
            " M aws/cluster-dev/managed-data-plane.auto.tfvars\n"
        ),
        ref_contents=landed,
        working_contents=landed,
    )
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(
        _command(root, ticket="OMN-14974", repo="omninode_infra")
    )

    assert result.outcome is EnumPruneOutcome.PRUNED_SUPERSEDED
    assert result.unreachable_paths == ()
    assert adapter.remove_calls, "superseded worktree must actually be removed"
    assert adapter.remove_calls[0][1] == str(wt)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_untracked_file_identical_to_merge_target_is_superseded(
    tmp_path: Path,
) -> None:
    """The live omnibase_infra case: a merged file that was never ``git add``ed.

    ``tests/integration/runtime/test_delegation_dispatch_port_handler_compat.py``
    was byte-identical to origin/dev (landed by #2490) yet showed as ``??``.
    """
    root = tmp_path / "worktrees"
    body = "def test_compat() -> None:\n    assert True\n"
    path = "tests/integration/runtime/test_delegation_dispatch_port_handler_compat.py"
    _make_worktree(root, "OMN-14974", "omnibase_infra")
    adapter = FakeContentGitAdapter(
        porcelain=f"?? {path}\n",
        ref_contents={path: body},
        working_contents={path: body},
    )
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.PRUNED_SUPERSEDED
    assert adapter.remove_calls


# ---------------------------------------------------------------------------
# Genuine LOST_WORK must still be preserved — the rail that matters most.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dirty_path_absent_from_merge_target_is_preserved(
    tmp_path: Path,
) -> None:
    """Real unlanded work: never removed, and the path is named for the operator."""
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-14974", "omnibase_infra")
    adapter = FakeContentGitAdapter(
        porcelain="?? src/omnibase_infra/brand_new_unlanded.py\n",
        ref_contents={},  # absent at the merge target
        working_contents={"src/omnibase_infra/brand_new_unlanded.py": "real work\n"},
    )
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.SKIPPED_DIRTY
    assert result.unreachable_paths == ("src/omnibase_infra/brand_new_unlanded.py",)
    assert not adapter.remove_calls, "unlanded work must never be removed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_unreachable_path_among_many_blocks_the_whole_prune(
    tmp_path: Path,
) -> None:
    """Reachability is conjunctive — a single divergent path preserves the tree."""
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-14974", "omnibase_infra")
    adapter = FakeContentGitAdapter(
        porcelain=" M landed.py\n M diverged.py\n",
        ref_contents={"landed.py": "same\n", "diverged.py": "on dev\n"},
        working_contents={"landed.py": "same\n", "diverged.py": "LOCAL EDIT\n"},
    )
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.SKIPPED_DIRTY
    assert result.unreachable_paths == ("diverged.py",)
    assert not adapter.remove_calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_locally_deleted_file_is_not_superseded(tmp_path: Path) -> None:
    """A local deletion is a divergence from the merge target, not a landing."""
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-14974", "omnibase_infra")
    adapter = FakeContentGitAdapter(
        porcelain=" D important.py\n",
        ref_contents={"important.py": "still on dev\n"},
        working_contents={},  # gone locally
    )
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.SKIPPED_DIRTY
    assert result.unreachable_paths == ("important.py",)
    assert not adapter.remove_calls


# ---------------------------------------------------------------------------
# Fail-closed rails: doubt always resolves to *keep*.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_absent_merge_target_ref_falls_back_to_skipped_dirty(
    tmp_path: Path,
) -> None:
    """With no merge target there is nothing to prove reachability against."""
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-14974", "omnibase_infra")
    adapter = FakeContentGitAdapter(
        porcelain=" M anything.py\n",
        ref_contents={"anything.py": "x\n"},
        working_contents={"anything.py": "x\n"},
    )
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root, merge_target_ref=None))

    assert result.outcome is EnumPruneOutcome.SKIPPED_DIRTY
    assert not adapter.remove_calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_probe_failure_falls_back_to_skipped_dirty(
    tmp_path: Path,
) -> None:
    """An adapter error must never be read as 'reachable'."""
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-14974", "omnibase_infra")
    adapter = FakeContentGitAdapter(porcelain=" M anything.py\n", raise_on_content=True)
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.SKIPPED_DIRTY
    assert not adapter.remove_calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_adapter_without_content_probe_falls_back_to_skipped_dirty(
    tmp_path: Path,
) -> None:
    """An adapter predating the content probe must degrade to the old behaviour."""
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-14974", "omnibase_infra")

    class LegacyAdapter:
        def __init__(self) -> None:
            self.remove_calls: list[tuple[str, str]] = []

        def status_porcelain(self, worktree_path: str) -> str:
            return " M anything.py\n"

        def git_common_dir(self, worktree_path: str) -> str:
            return str(Path(worktree_path) / ".git")

        def worktree_remove(self, canonical_root: str, worktree_path: str) -> None:
            self.remove_calls.append((canonical_root, worktree_path))

    adapter = LegacyAdapter()
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.SKIPPED_DIRTY
    assert not adapter.remove_calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_superseded_reports_without_removing(tmp_path: Path) -> None:
    """dry_run must classify as superseded but leave the tree on disk."""
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-14974", "omnibase_infra")
    adapter = FakeContentGitAdapter(
        porcelain=" M landed.py\n",
        ref_contents={"landed.py": "same\n"},
        working_contents={"landed.py": "same\n"},
    )
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root, dry_run=True))

    assert result.outcome is EnumPruneOutcome.DRY_RUN
    assert "superseded" in result.detail.lower()
    assert not adapter.remove_calls


# ---------------------------------------------------------------------------
# Porcelain parsing — a misparse silently mis-scopes the reachability proof.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_renamed_path_is_checked_at_its_destination(tmp_path: Path) -> None:
    """``R  old -> new`` must prove reachability of ``new``, not ``old``."""
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-14974", "omnibase_infra")
    adapter = FakeContentGitAdapter(
        porcelain="R  docs/old_name.md -> docs/new_name.md\n",
        ref_contents={"docs/new_name.md": "body\n"},
        working_contents={"docs/new_name.md": "body\n"},
    )
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.PRUNED_SUPERSEDED
    assert ("origin/dev", "docs/new_name.md") in adapter.content_calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quoted_porcelain_path_is_unquoted_before_probing(
    tmp_path: Path,
) -> None:
    """git quotes paths with spaces/specials; probing the quoted literal misses."""
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-14974", "omnibase_infra")
    adapter = FakeContentGitAdapter(
        porcelain=' M "docs/a file.md"\n',
        ref_contents={"docs/a file.md": "body\n"},
        working_contents={"docs/a file.md": "body\n"},
    )
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.PRUNED_SUPERSEDED
    assert ("origin/dev", "docs/a file.md") in adapter.content_calls


# ---------------------------------------------------------------------------
# Regression: the clean path must be untouched by any of the above.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clean_worktree_still_prunes_without_any_content_probe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-14974", "omnibase_infra")
    adapter = FakeContentGitAdapter(porcelain="")
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.PRUNED
    assert adapter.content_calls == [], "clean path must not probe content at all"
    assert adapter.remove_calls


# ---------------------------------------------------------------------------
# History reachability — the "worktree is simply BEHIND the merge target" case.
#
# Three of the four live OMN-14974 worktrees held content that was not equal to
# origin/dev HEAD but WAS an earlier version of the very same file on dev (dev
# had moved ahead). Nothing is lost by deleting those: the exact bytes remain
# recoverable from dev's history. Equality-to-HEAD alone cannot see this, so it
# preserved all three and left the operator decision in place.
# ---------------------------------------------------------------------------


FakeHistoryGitAdapter = FakeContentGitAdapter


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_superseded_by_newer_merge_target_version_is_pruned(
    tmp_path: Path,
) -> None:
    """Worktree holds an older revision of a file dev has since advanced."""
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-14974", "omninode_infra")
    adapter = FakeHistoryGitAdapter(
        porcelain=" M k8s/onex-dev/runtime/configmap.yaml\n",
        ref_contents={"k8s/onex-dev/runtime/configmap.yaml": "NEW dev content\n"},
        working_contents={"k8s/onex-dev/runtime/configmap.yaml": "older content\n"},
        history={"k8s/onex-dev/runtime/configmap.yaml": ["older content\n"]},
    )
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(
        _command(root, ticket="OMN-14974", repo="omninode_infra")
    )

    assert result.outcome is EnumPruneOutcome.PRUNED_SUPERSEDED
    assert result.unreachable_paths == ()
    assert adapter.remove_calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_never_on_merge_target_history_is_still_preserved(
    tmp_path: Path,
) -> None:
    """Truly local content is not in history either — still LOST_WORK."""
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-14974", "omninode_infra")
    adapter = FakeHistoryGitAdapter(
        porcelain=" M k8s/onex-dev/runtime/configmap.yaml\n",
        ref_contents={"k8s/onex-dev/runtime/configmap.yaml": "dev content\n"},
        working_contents={"k8s/onex-dev/runtime/configmap.yaml": "MY LOCAL EDIT\n"},
        history={"k8s/onex-dev/runtime/configmap.yaml": ["dev content\n", "older\n"]},
    )
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(
        _command(root, ticket="OMN-14974", repo="omninode_infra")
    )

    assert result.outcome is EnumPruneOutcome.SKIPPED_DIRTY
    assert result.unreachable_paths == ("k8s/onex-dev/runtime/configmap.yaml",)
    assert not adapter.remove_calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_history_probe_failure_preserves_the_worktree(tmp_path: Path) -> None:
    """A history-probe error must never be read as 'reachable'."""
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-14974", "omninode_infra")
    adapter = FakeHistoryGitAdapter(
        porcelain=" M f.yaml\n",
        ref_contents={"f.yaml": "dev\n"},
        working_contents={"f.yaml": "old\n"},
        raise_on_content=True,
    )
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(
        _command(root, ticket="OMN-14974", repo="omninode_infra")
    )

    assert result.outcome is EnumPruneOutcome.SKIPPED_DIRTY
    assert not adapter.remove_calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_head_equality_short_circuits_before_history_probe(
    tmp_path: Path,
) -> None:
    """The cheap HEAD check must run first; history is only the fallback."""
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-14974", "omninode_infra")
    adapter = FakeHistoryGitAdapter(
        porcelain=" M f.yaml\n",
        ref_contents={"f.yaml": "same\n"},
        working_contents={"f.yaml": "same\n"},
        history={},
    )
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(
        _command(root, ticket="OMN-14974", repo="omninode_infra")
    )

    assert result.outcome is EnumPruneOutcome.PRUNED_SUPERSEDED
    assert adapter.history_calls == [], "no history scan needed when HEAD matches"
