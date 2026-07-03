# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerWorktreePrune — event-driven prune of a single ticket/repo worktree.

Invoked by ``pr_lifecycle_orchestrator`` (POST_MERGE_TAIL) once per merged PR.
Replaces the polling GC (``scripts/prune-worktrees.sh``) whose two structural
defects this handler fixes (OMN-13859):

  * It required a resolvable ``@{u}`` upstream to prove "already merged", so a
    branch whose remote was deleted post-merge — the normal state — was skipped
    forever. Here the trigger event (PR merged/closed) IS the source of truth;
    upstream state is never consulted (rail #3).
  * It hardcoded the worktrees root. Here the root is resolved from the command
    or the ``ONEX_WORKTREES_ROOT`` env var, fail-loud (rail #4).

Safety rails (all fail toward *keep*, never toward *remove*):

  1. Never prune a worktree with uncommitted changes — flag SKIPPED_DIRTY.
  2. Never touch a canonical clone. The target must live under the worktrees
     root, and its ``.git`` must be a worktree gitlink *file* — a real clone
     (``.git`` directory) or any path escaping the root is REFUSED_OUTSIDE_ROOT.
  3. Do not require an ``@{u}`` upstream — the close event is authoritative.
  4. Respect ``ONEX_WORKTREES_ROOT``; never hardcode a path.

The GitHub/git side effects are behind ``ProtocolGitWorktreeAdapter`` so tests
inject a fake and exercise every rail without touching real git or the network.

Related:
    - OMN-13859: Event-driven worktree prune-on-PR-close.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from omnimarket.events.worktree_prune import (
    EnumPruneOutcome,
    ModelWorktreePruneCommand,
    ModelWorktreePruneResult,
)

logger = logging.getLogger(__name__)

HandlerType = Literal["NODE_HANDLER", "INFRA_HANDLER", "PROJECTION_HANDLER"]
HandlerCategory = Literal["EFFECT", "COMPUTE", "NONDETERMINISTIC_COMPUTE"]

_WORKTREES_ROOT_ENV = "ONEX_WORKTREES_ROOT"


class WorktreesRootUnresolvedError(RuntimeError):
    """Raised when the worktrees root cannot be resolved.

    A silent default would reintroduce the cross-machine breakage the OmniNode
    ruleset forbids (Rule 8) and could point the prune at the wrong tree.
    """


def resolve_worktrees_root(explicit: str | os.PathLike[str] | None = None) -> str:
    """Resolve the worktrees root, failing loud when it cannot be determined.

    Precedence: an explicit non-empty value, then the ``ONEX_WORKTREES_ROOT``
    env var. Never falls back to a hardcoded path (OMN-13859 rail #4).
    """
    candidate = str(explicit) if explicit else os.environ.get(_WORKTREES_ROOT_ENV)
    if not candidate:
        raise WorktreesRootUnresolvedError(
            f"{_WORKTREES_ROOT_ENV} is not set and no explicit worktrees_root was "
            "supplied — refusing to guess a worktrees location. Set "
            f"{_WORKTREES_ROOT_ENV} or pass worktrees_root on the command."
        )
    return candidate


# ---------------------------------------------------------------------------
# Adapter protocol — injected at construction; swapped for a fake in tests.
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolGitWorktreeAdapter(Protocol):
    """Minimal git worktree operations required by the prune effect.

    Every method is a real git side effect; the handler owns all *decision*
    logic (path safety, dirty classification) so tests can drive the rails with
    a pure fake.
    """

    def status_porcelain(self, worktree_path: str) -> str:
        """Return ``git status --porcelain`` output for the worktree.

        Empty (whitespace-only) means clean.
        """
        ...

    def git_common_dir(self, worktree_path: str) -> str:
        """Return the ABSOLUTE shared git dir for the worktree.

        Equivalent to ``git -C <worktree_path> rev-parse --git-common-dir``,
        resolved to an absolute path. Its parent is the canonical clone from
        which ``git worktree remove`` must be run.
        """
        ...

    def worktree_remove(self, canonical_root: str, worktree_path: str) -> None:
        """Remove ``worktree_path`` via ``git -C <canonical_root> worktree remove --force``."""
        ...


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerWorktreePrune:
    """Prune the git worktree for a single closed PR, scoped to ticket + repo."""

    def __init__(
        self,
        git_adapter: ProtocolGitWorktreeAdapter | None = None,
    ) -> None:
        self._git = git_adapter

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "EFFECT"

    def _result(
        self,
        command: ModelWorktreePruneCommand,
        *,
        outcome: EnumPruneOutcome,
        worktree_path: str | None = None,
        dirty_file_count: int = 0,
        detail: str = "",
        error: str | None = None,
    ) -> ModelWorktreePruneResult:
        return ModelWorktreePruneResult(
            correlation_id=command.correlation_id,
            ticket_id=command.ticket_id,
            repo=_bare_repo_name(command.repo),
            worktree_path=worktree_path,
            outcome=outcome,
            dirty_file_count=dirty_file_count,
            detail=detail,
            error=error,
            completed_at=datetime.now(tz=UTC),
        )

    async def handle(
        self, command: ModelWorktreePruneCommand
    ) -> ModelWorktreePruneResult:
        """Classify and (unless dry-run) prune one ticket/repo worktree.

        The method is total: every path returns a typed result, never raises,
        so the orchestrator can call it best-effort without a try/except of its
        own. Removal happens only on the single happy path where all rails pass.
        """
        repo_name = _bare_repo_name(command.repo)

        # Path-traversal guard on the untrusted segments before they touch the
        # filesystem. ticket_id / repo must be plain path segments.
        for label, segment in (("ticket_id", command.ticket_id), ("repo", repo_name)):
            if (
                not segment
                or "/" in segment
                or "\\" in segment
                or segment in {".", ".."}
            ):
                return self._result(
                    command,
                    outcome=EnumPruneOutcome.REFUSED_OUTSIDE_ROOT,
                    detail=f"refused: {label}={segment!r} is not a plain path segment",
                )

        try:
            root = Path(resolve_worktrees_root(command.worktrees_root)).resolve()
        except WorktreesRootUnresolvedError as exc:
            return self._result(
                command,
                outcome=EnumPruneOutcome.FAILED,
                detail="worktrees root unresolved",
                error=str(exc),
            )

        target = (root / command.ticket_id / repo_name).resolve()

        # Rail #2 — the resolved target must live strictly under the worktrees
        # root. This is what keeps canonical clones (which live at
        # $OMNI_HOME/<repo>, NOT under the worktrees root) untouchable, and
        # defeats any symlink / traversal escape.
        if target == root or not target.is_relative_to(root):
            return self._result(
                command,
                outcome=EnumPruneOutcome.REFUSED_OUTSIDE_ROOT,
                worktree_path=str(target),
                detail=f"refused: {target} is not strictly under worktrees root {root}",
            )

        if not target.exists():
            return self._result(
                command,
                outcome=EnumPruneOutcome.SKIPPED_NOT_FOUND,
                worktree_path=str(target),
                detail="no worktree at path (already pruned or never created)",
            )

        # Rail #2 — a genuine git worktree marks itself with a ``.git`` *file*
        # (a gitlink). A ``.git`` *directory* is a real clone; refuse to remove
        # it even if it somehow resolved under the root.
        git_marker = target / ".git"
        if git_marker.is_dir():
            return self._result(
                command,
                outcome=EnumPruneOutcome.REFUSED_OUTSIDE_ROOT,
                worktree_path=str(target),
                detail="refused: target has a .git directory — it is a canonical clone, not a worktree",
            )
        if not git_marker.is_file():
            return self._result(
                command,
                outcome=EnumPruneOutcome.SKIPPED_NOT_A_WORKTREE,
                worktree_path=str(target),
                detail="path is not a registered git worktree (no .git gitlink file)",
            )

        if self._git is None:
            return self._result(
                command,
                outcome=EnumPruneOutcome.FAILED,
                worktree_path=str(target),
                detail="misconfigured: no git worktree adapter injected",
                error="HandlerWorktreePrune requires an injected ProtocolGitWorktreeAdapter",
            )

        # Rail #1 — never prune uncommitted work. NOTE: we deliberately do NOT
        # consult @{u} / unpushed state here (rail #3): the PR-close event is the
        # source of truth, and a deleted post-merge remote must not block cleanup.
        try:
            porcelain = self._git.status_porcelain(str(target))
        except Exception as exc:
            return self._result(
                command,
                outcome=EnumPruneOutcome.FAILED,
                worktree_path=str(target),
                detail="failed to read git status",
                error=str(exc),
            )

        dirty_lines = [line for line in porcelain.splitlines() if line.strip()]
        if dirty_lines:
            logger.warning(
                "[WORKTREE-PRUNE] flagging dirty worktree (NOT removing): %s "
                "(%d uncommitted path(s), ticket=%s)",
                target,
                len(dirty_lines),
                command.ticket_id,
            )
            return self._result(
                command,
                outcome=EnumPruneOutcome.SKIPPED_DIRTY,
                worktree_path=str(target),
                dirty_file_count=len(dirty_lines),
                detail=(
                    f"flagged: {len(dirty_lines)} uncommitted path(s) — kept for "
                    "recovery, not removed"
                ),
            )

        if command.dry_run:
            return self._result(
                command,
                outcome=EnumPruneOutcome.DRY_RUN,
                worktree_path=str(target),
                detail="clean worktree — would remove (dry_run)",
            )

        try:
            common_dir = Path(self._git.git_common_dir(str(target)))
            canonical_root = common_dir.parent
            self._git.worktree_remove(str(canonical_root), str(target))
        except Exception as exc:
            logger.warning(
                "[WORKTREE-PRUNE] removal failed for %s: %s", target, exc, exc_info=True
            )
            return self._result(
                command,
                outcome=EnumPruneOutcome.FAILED,
                worktree_path=str(target),
                detail="git worktree remove failed",
                error=str(exc),
            )

        logger.info(
            "[WORKTREE-PRUNE] pruned clean worktree %s (ticket=%s repo=%s pr=%s)",
            target,
            command.ticket_id,
            repo_name,
            command.pr_number,
        )
        return self._result(
            command,
            outcome=EnumPruneOutcome.PRUNED,
            worktree_path=str(target),
            detail="removed clean, merged worktree",
        )


def _bare_repo_name(repo: str) -> str:
    """Strip an owner prefix (``OmniNode-ai/omnimarket`` -> ``omnimarket``)."""
    return repo.rsplit("/", 1)[-1].strip()


__all__: list[str] = [
    "HandlerWorktreePrune",
    "ProtocolGitWorktreeAdapter",
    "WorktreesRootUnresolvedError",
    "resolve_worktrees_root",
]
