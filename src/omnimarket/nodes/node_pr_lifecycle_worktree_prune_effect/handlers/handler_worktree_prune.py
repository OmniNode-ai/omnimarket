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


@runtime_checkable
class ProtocolGitContentProbe(Protocol):
    """Content-reachability probe for the OMN-15251 superseded classifier.

    Split from ``ProtocolGitWorktreeAdapter`` so an adapter predating this
    ticket still satisfies the base protocol; the handler feature-detects the
    probe and degrades to plain SKIPPED_DIRTY when it is absent.
    """

    def content_sha_at_ref(
        self, worktree_path: str, ref: str, rel_path: str
    ) -> str | None:
        """Return a content hash for ``rel_path`` at ``ref``, or None if absent there."""
        ...

    def working_content_sha(self, worktree_path: str, rel_path: str) -> str | None:
        """Return a content hash for ``rel_path`` in the working tree, or None if absent."""
        ...

    def content_sha_in_ref_history(
        self, worktree_path: str, ref: str, rel_path: str, content_sha: str
    ) -> bool:
        """True when ``content_sha`` is a version of ``rel_path`` somewhere in ``ref``'s history.

        Equality with the ref *head* is the common case, but a worktree that
        simply fell behind holds an EARLIER version of a file the merge target
        has since advanced. Those bytes are still fully recoverable from the
        merge target's history, so deleting the worktree loses nothing.
        """
        ...


def _unquote_porcelain_path(raw: str) -> str:
    """Decode a ``git status --porcelain`` path token.

    git wraps paths containing spaces or specials in double quotes with C-style
    escapes. Probing the quoted literal would look up a path that does not
    exist, read as "absent at the merge target", and preserve a worktree that
    was in fact fully landed — a false BLOCKED, the exact noise OMN-15251 exists
    to remove.
    """
    token = raw.strip()
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        try:
            return token[1:-1].encode().decode("unicode_escape")
        except UnicodeDecodeError:
            return token[1:-1]
    return token


def _porcelain_paths(porcelain: str) -> tuple[str, ...]:
    """Extract the repo-relative path each porcelain entry ultimately refers to.

    For a rename/copy (``R  old -> new``) the destination is what the working
    tree now holds, so reachability must be proven for ``new``.
    """
    paths: list[str] = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        # Status is the first two columns; the path payload follows a space.
        payload = line[3:] if len(line) > 3 else line.strip()
        if " -> " in payload:
            payload = payload.split(" -> ", 1)[1]
        path = _unquote_porcelain_path(payload)
        if path:
            paths.append(path)
    return tuple(paths)


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
        unreachable_paths: tuple[str, ...] = (),
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
            unreachable_paths=unreachable_paths,
            detail=detail,
            error=error,
            completed_at=datetime.now(tz=UTC),
        )

    def _unreachable_dirty_paths(
        self,
        *,
        worktree_path: str,
        merge_target_ref: str | None,
        dirty_paths: tuple[str, ...],
    ) -> tuple[str, ...] | None:
        """Return dirty paths NOT provably reachable from the merge target.

        Returns ``None`` when reachability cannot be established at all (no
        merge target, no content probe on the adapter, or a probe error). The
        caller must treat ``None`` as "preserve" — it is deliberately distinct
        from ``()`` ("checked, and everything is landed"), because collapsing
        the two would let an inconclusive probe authorize a deletion.
        """
        if not merge_target_ref:
            return None
        probe = self._git
        if not isinstance(probe, ProtocolGitContentProbe):
            return None

        unreachable: list[str] = []
        for rel_path in dirty_paths:
            try:
                working = probe.working_content_sha(worktree_path, rel_path)
                landed = probe.content_sha_at_ref(
                    worktree_path, merge_target_ref, rel_path
                )
            except Exception as exc:  # any probe failure ⇒ preserve, never delete
                logger.warning(
                    "[WORKTREE-PRUNE] content probe failed for %s (%s) — preserving "
                    "worktree %s",
                    rel_path,
                    exc,
                    worktree_path,
                )
                return None
            # A locally deleted file is a divergence no history lookup can
            # excuse — the worktree asserts an absence the merge target does not.
            if working is None:
                unreachable.append(rel_path)
                continue
            # Cheap path first: identical to the merge-target head.
            if landed is not None and working == landed:
                continue
            # Fallback: the exact bytes may still live in the merge target's
            # history (the worktree simply fell behind). Recoverable from the
            # mainline ⇒ nothing is lost by removing the worktree.
            try:
                in_history = probe.content_sha_in_ref_history(
                    worktree_path, merge_target_ref, rel_path, working
                )
            except Exception as exc:  # any probe failure ⇒ preserve, never delete
                logger.warning(
                    "[WORKTREE-PRUNE] history probe failed for %s (%s) — preserving "
                    "worktree %s",
                    rel_path,
                    exc,
                    worktree_path,
                )
                return None
            if not in_history:
                unreachable.append(rel_path)
        return tuple(unreachable)

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

        # OMN-15251 — `dirty` is a filesystem fact, not a content fact. Before
        # manufacturing an operator decision, ask the question that actually
        # matters: is any of this content absent from the merge target? Only a
        # positive proof of full reachability downgrades dirty to superseded;
        # every inconclusive answer preserves the worktree.
        superseded = False
        if dirty_lines:
            dirty_paths = _porcelain_paths(porcelain)
            unreachable = self._unreachable_dirty_paths(
                worktree_path=str(target),
                merge_target_ref=command.merge_target_ref,
                dirty_paths=dirty_paths,
            )
            if unreachable is None or unreachable:
                reason = (
                    "reachability inconclusive (no merge target, no content probe, "
                    "or probe error)"
                    if unreachable is None
                    else f"{len(unreachable)} path(s) not on {command.merge_target_ref}"
                )
                logger.warning(
                    "[WORKTREE-PRUNE] flagging dirty worktree (NOT removing): %s "
                    "(%d uncommitted path(s), %s, ticket=%s)",
                    target,
                    len(dirty_lines),
                    reason,
                    command.ticket_id,
                )
                return self._result(
                    command,
                    outcome=EnumPruneOutcome.SKIPPED_DIRTY,
                    worktree_path=str(target),
                    dirty_file_count=len(dirty_lines),
                    unreachable_paths=unreachable or (),
                    detail=(
                        f"flagged: {len(dirty_lines)} uncommitted path(s), {reason} — "
                        "kept for recovery, not removed"
                    ),
                )
            superseded = True
            logger.info(
                "[WORKTREE-PRUNE] dirty worktree is SUPERSEDED — all %d path(s) "
                "already on %s: %s (ticket=%s)",
                len(dirty_lines),
                command.merge_target_ref,
                target,
                command.ticket_id,
            )

        if command.dry_run:
            return self._result(
                command,
                outcome=EnumPruneOutcome.DRY_RUN,
                worktree_path=str(target),
                dirty_file_count=len(dirty_lines),
                detail=(
                    f"superseded worktree ({len(dirty_lines)} dirty path(s) all on "
                    f"{command.merge_target_ref}) — would remove (dry_run)"
                    if superseded
                    else "clean worktree — would remove (dry_run)"
                ),
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
            "[WORKTREE-PRUNE] pruned %s worktree %s (ticket=%s repo=%s pr=%s)",
            "superseded" if superseded else "clean",
            target,
            command.ticket_id,
            repo_name,
            command.pr_number,
        )
        return self._result(
            command,
            outcome=(
                EnumPruneOutcome.PRUNED_SUPERSEDED
                if superseded
                else EnumPruneOutcome.PRUNED
            ),
            worktree_path=str(target),
            dirty_file_count=len(dirty_lines),
            detail=(
                f"removed superseded worktree — all {len(dirty_lines)} dirty path(s) "
                f"already reachable from {command.merge_target_ref}"
                if superseded
                else "removed clean, merged worktree"
            ),
        )


def _bare_repo_name(repo: str) -> str:
    """Strip an owner prefix (``OmniNode-ai/omnimarket`` -> ``omnimarket``)."""
    return repo.rsplit("/", 1)[-1].strip()


__all__: list[str] = [
    "HandlerWorktreePrune",
    "ProtocolGitContentProbe",
    "ProtocolGitWorktreeAdapter",
    "WorktreesRootUnresolvedError",
    "resolve_worktrees_root",
]
