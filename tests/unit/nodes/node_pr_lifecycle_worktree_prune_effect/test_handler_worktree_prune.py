# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerWorktreePrune — the OMN-13859 safety rails.

The adversarial core of this ticket: prove the handler always fails toward
*keep*. Every rail is exercised with a pure fake git adapter and tmp_path
filesystem fixtures — no real git, no network.

Load-bearing proofs required by the ticket:
  * DIRTY worktree is FLAGGED, never removed (rail #1).
  * a canonical clone (.git directory) is REFUSED, never removed (rail #2).
  * a path escaping the worktrees root is REFUSED (rail #2 / traversal).
  * no @{u} upstream is ever consulted (rail #3) — a clean worktree with a
    deleted remote still prunes.
  * the worktrees root comes from ONEX_WORKTREES_ROOT / the command, never a
    hardcoded path (rail #4).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.events.worktree_prune import EnumPruneOutcome, ModelWorktreePruneCommand
from omnimarket.nodes.node_pr_lifecycle_worktree_prune_effect.handlers.handler_worktree_prune import (
    HandlerWorktreePrune,
    WorktreesRootUnresolvedError,
    resolve_worktrees_root,
)


class FakeGitAdapter:
    """Records calls and returns scripted git output; removes nothing real."""

    def __init__(self, porcelain: str = "", common_dir: str | None = None) -> None:
        self._porcelain = porcelain
        self._common_dir = common_dir
        self.status_calls: list[str] = []
        self.common_dir_calls: list[str] = []
        self.remove_calls: list[tuple[str, str]] = []

    def status_porcelain(self, worktree_path: str) -> str:
        self.status_calls.append(worktree_path)
        return self._porcelain

    def git_common_dir(self, worktree_path: str) -> str:
        self.common_dir_calls.append(worktree_path)
        # Default: canonical clone lives at <root>/<repo>/.git — i.e. the parent
        # of the worktree's grandparent (root/ticket/repo -> root/../<repo>).
        return self._common_dir or str(Path(worktree_path) / ".git")

    def worktree_remove(self, canonical_root: str, worktree_path: str) -> None:
        self.remove_calls.append((canonical_root, worktree_path))


def _make_worktree(root: Path, ticket: str, repo: str, *, gitlink: bool = True) -> Path:
    """Create a fake worktree dir. gitlink=True -> .git FILE (a real worktree);
    gitlink=False -> .git DIRECTORY (a canonical clone)."""
    wt = root / ticket / repo
    wt.mkdir(parents=True, exist_ok=True)
    marker = wt / ".git"
    if gitlink:
        marker.write_text("gitdir: /somewhere/.git/worktrees/x\n")
    else:
        marker.mkdir()
    return wt


def _command(root: Path, ticket: str = "OMN-13859", repo: str = "omnimarket", **kw):
    return ModelWorktreePruneCommand(
        correlation_id=uuid4(),
        ticket_id=ticket,
        repo=repo,
        worktrees_root=str(root),
        **kw,
    )


# ---------------------------------------------------------------------------
# Rail #1 — DIRTY worktree is flagged, never removed.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dirty_worktree_is_flagged_not_pruned(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-13859", "omnimarket", gitlink=True)
    adapter = FakeGitAdapter(porcelain=" M src/foo.py\n?? bar.txt\n")
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.SKIPPED_DIRTY
    assert result.dirty_file_count == 2
    # The critical safety assertion: nothing was removed.
    assert adapter.remove_calls == []


# ---------------------------------------------------------------------------
# Rail #2 — canonical-clone protection (.git directory) is refused.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_canonical_clone_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    # A .git *directory* marks a real clone, not a worktree.
    _make_worktree(root, "OMN-13859", "omnimarket", gitlink=False)
    adapter = FakeGitAdapter(porcelain="")
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.REFUSED_OUTSIDE_ROOT
    assert adapter.remove_calls == []
    # Never even ran status on a clone.
    assert adapter.status_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_path_escaping_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    root.mkdir(parents=True)
    adapter = FakeGitAdapter()
    handler = HandlerWorktreePrune(git_adapter=adapter)

    # A traversal ticket segment must be rejected before any filesystem action.
    result = await handler.handle(_command(root, ticket="../../etc"))

    assert result.outcome is EnumPruneOutcome.REFUSED_OUTSIDE_ROOT
    assert adapter.remove_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_symlink_escape_is_refused(tmp_path: Path) -> None:
    """A symlinked worktree pointing outside the root resolves out and is refused."""
    root = tmp_path / "worktrees"
    outside = tmp_path / "canonical" / "omnimarket"
    outside.mkdir(parents=True)
    (outside / ".git").mkdir()
    (root / "OMN-13859").mkdir(parents=True)
    link = root / "OMN-13859" / "omnimarket"
    link.symlink_to(outside, target_is_directory=True)
    adapter = FakeGitAdapter()
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.REFUSED_OUTSIDE_ROOT
    assert adapter.remove_calls == []


# ---------------------------------------------------------------------------
# Happy path + rail #3 (no upstream consulted).
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clean_worktree_is_pruned(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    wt = _make_worktree(root, "OMN-13859", "omnimarket", gitlink=True)
    # common-dir points at the canonical clone's .git; parent is the clone dir.
    canonical_git = tmp_path / "canonical" / "omnimarket" / ".git"
    canonical_git.mkdir(parents=True)
    adapter = FakeGitAdapter(porcelain="", common_dir=str(canonical_git))
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.PRUNED
    assert adapter.remove_calls == [(str(canonical_git.parent), str(wt.resolve()))]
    # Rail #3: no upstream / @{u} query surface exists on the adapter at all —
    # only status + common-dir + remove were used.
    assert adapter.status_calls  # dirty check ran (that is the ONLY gate)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prune_result_covers_declared_output_envelope(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    wt = _make_worktree(root, "OMN-13859", "omnimarket", gitlink=True)
    canonical_git = tmp_path / "canonical" / "omnimarket" / ".git"
    canonical_git.mkdir(parents=True)
    adapter = FakeGitAdapter(porcelain="", common_dir=str(canonical_git))
    handler = HandlerWorktreePrune(git_adapter=adapter)
    command = _command(root)

    result = await handler.handle(command)

    assert result.correlation_id == command.correlation_id
    assert result.ticket_id == "OMN-13859"
    assert result.worktree_path == str(wt.resolve())
    assert result.detail
    assert result.completed_at.tzinfo is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clean_worktree_dry_run_does_not_remove(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-13859", "omnimarket", gitlink=True)
    adapter = FakeGitAdapter(porcelain="")
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root, dry_run=True))

    assert result.outcome is EnumPruneOutcome.DRY_RUN
    assert adapter.remove_calls == []


# ---------------------------------------------------------------------------
# Idempotency / not-a-worktree / misconfig.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_worktree_is_skipped_not_found(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    root.mkdir(parents=True)
    adapter = FakeGitAdapter()
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.SKIPPED_NOT_FOUND
    assert adapter.remove_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dir_without_git_is_skipped_not_a_worktree(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    (root / "OMN-13859" / "omnimarket").mkdir(parents=True)  # no .git marker
    adapter = FakeGitAdapter()
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.SKIPPED_NOT_A_WORKTREE
    assert adapter.remove_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_adapter_fails_clean(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-13859", "omnimarket", gitlink=True)
    handler = HandlerWorktreePrune(git_adapter=None)

    result = await handler.handle(_command(root))

    assert result.outcome is EnumPruneOutcome.FAILED
    assert result.error is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_owner_prefix_is_stripped(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    _make_worktree(root, "OMN-13859", "omnimarket", gitlink=True)
    adapter = FakeGitAdapter(porcelain="")
    handler = HandlerWorktreePrune(git_adapter=adapter)

    # Pass a full slug; the handler must resolve the bare-name worktree path.
    result = await handler.handle(_command(root, repo="OmniNode-ai/omnimarket"))

    assert result.outcome is EnumPruneOutcome.PRUNED
    assert result.repo == "omnimarket"


# ---------------------------------------------------------------------------
# Rail #4 — worktrees-root resolution.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_worktrees_root_prefers_explicit() -> None:
    assert resolve_worktrees_root("/tmp/foo") == "/tmp/foo"


@pytest.mark.unit
def test_resolve_worktrees_root_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_WORKTREES_ROOT", "/tmp/env-root")
    assert resolve_worktrees_root() == "/tmp/env-root"


@pytest.mark.unit
def test_resolve_worktrees_root_fails_loud_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_WORKTREES_ROOT", raising=False)
    with pytest.raises(WorktreesRootUnresolvedError):
        resolve_worktrees_root()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unresolved_root_returns_failed_not_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_WORKTREES_ROOT", raising=False)
    adapter = FakeGitAdapter()
    handler = HandlerWorktreePrune(git_adapter=adapter)
    command = ModelWorktreePruneCommand(
        correlation_id=uuid4(), ticket_id="OMN-13859", repo="omnimarket"
    )

    result = await handler.handle(command)

    assert result.outcome is EnumPruneOutcome.FAILED
    assert adapter.remove_calls == []
