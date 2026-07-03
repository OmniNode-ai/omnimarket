# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage for node_pr_lifecycle_worktree_prune_effect (OMN-13859)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.events.worktree_prune import (
    EnumPruneOutcome,
    ModelWorktreePruneCommand,
)
from omnimarket.nodes.node_pr_lifecycle_worktree_prune_effect.handlers.handler_worktree_prune import (
    HandlerWorktreePrune,
)


class RecordingGitWorktreeAdapter:
    def __init__(self, canonical_git_dir: Path) -> None:
        self._canonical_git_dir = canonical_git_dir
        self.removed: list[tuple[str, str]] = []

    def status_porcelain(self, worktree_path: str) -> str:
        assert Path(worktree_path).name == "omnimarket"
        return ""

    def git_common_dir(self, worktree_path: str) -> str:
        assert Path(worktree_path).name == "omnimarket"
        return str(self._canonical_git_dir)

    def worktree_remove(self, canonical_root: str, worktree_path: str) -> None:
        self.removed.append((canonical_root, worktree_path))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_golden_chain_prunes_clean_worktree_without_upstream(
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "worktrees"
    worktree = worktrees_root / "OMN-13859" / "omnimarket"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/git/worktrees/omnimarket\n")
    canonical_git_dir = tmp_path / "canonical" / "omnimarket" / ".git"
    canonical_git_dir.mkdir(parents=True)
    adapter = RecordingGitWorktreeAdapter(canonical_git_dir)
    handler = HandlerWorktreePrune(git_adapter=adapter)

    result = await handler.handle(
        ModelWorktreePruneCommand(
            correlation_id=uuid4(),
            ticket_id="OMN-13859",
            repo="OmniNode-ai/omnimarket",
            worktrees_root=str(worktrees_root),
        )
    )

    assert result.outcome is EnumPruneOutcome.PRUNED
    assert result.ticket_id == "OMN-13859"
    assert result.repo == "omnimarket"
    assert result.worktree_path == str(worktree.resolve())
    assert adapter.removed == [(str(canonical_git_dir.parent), str(worktree.resolve()))]
