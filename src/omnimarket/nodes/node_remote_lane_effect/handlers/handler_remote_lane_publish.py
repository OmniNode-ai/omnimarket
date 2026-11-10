# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Preserve a lane commit, replay its delta, and publish against an exact head.

The recovery ref is create-only. A target push uses an explicit lease AND an
ancestry proof: a lease alone would permit replacing someone else's commit.
Replay runs real commit hooks in an isolated linked worktree. Failed replay or
push never changes the original worktree and always names the recovery branch.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..models.model_remote_lane_effect import SHA_RE
from ..models.model_remote_lane_publish import (
    ModelRemoteLanePublishRequest,
    ModelRemoteLanePublishResult,
    PublishOutcome,
)
from ..protocols import ProtocolRemoteLaneGit, RemoteLanePortError


class _PublishError(Exception):
    def __init__(self, outcome: PublishOutcome, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome


class HandlerRemoteLanePublish:
    """One contract operation owns preservation, bounded replay and pinned push."""

    def __init__(self, git: ProtocolRemoteLaneGit | None = None) -> None:
        from ..protocols.local_remote_lane_adapters import LocalRemoteLaneGit

        self._git = git if git is not None else LocalRemoteLaneGit()

    def _checked(
        self,
        request: ModelRemoteLanePublishRequest,
        *args: str,
        tree: str | None = None,
        failure: PublishOutcome = "replay-failed",
    ) -> str:
        try:
            result = self._git.run(
                ["git", "-C", tree or request.worktree, *args], request.timeout_s
            )
        except RemoteLanePortError as exc:
            raise _PublishError(failure, str(exc)) from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[-1200:]
            raise _PublishError(failure, f"git {args[0]}: {detail}")
        return result.stdout.strip()

    def _ancestor(
        self, request: ModelRemoteLanePublishRequest, before: str, after: str
    ) -> bool:
        try:
            result = self._git.run(
                [
                    "git",
                    "-C",
                    request.worktree,
                    "merge-base",
                    "--is-ancestor",
                    before,
                    after,
                ],
                request.timeout_s,
            )
        except RemoteLanePortError as exc:
            raise _PublishError("invalid-history", str(exc)) from exc
        if result.returncode not in (0, 1):
            raise _PublishError("invalid-history", result.stderr.strip()[-1200:])
        return result.returncode == 0

    def _head(self, request: ModelRemoteLanePublishRequest, branch: str) -> str | None:
        ref = f"refs/heads/{branch}"
        output = self._checked(
            request, "ls-remote", "--refs", "origin", ref, failure="target-unreadable"
        )
        rows = [line.split() for line in output.splitlines()]
        matches = [
            row[0]
            for row in rows
            if len(row) == 2 and row[1] == ref and SHA_RE.fullmatch(row[0])
        ]
        if not output:
            return None
        if len(matches) != 1:
            raise _PublishError(
                "target-unreadable", "remote returned an invalid branch head"
            )
        return matches[0]

    def _preserve(self, request: ModelRemoteLanePublishRequest, branch: str) -> None:
        try:
            if self._head(request, branch) == request.commit_sha:
                return
            self._checked(
                request,
                "push",
                "--porcelain",
                f"--force-with-lease=refs/heads/{branch}:",
                "origin",
                f"{request.commit_sha}:refs/heads/{branch}",
                failure="recovery-push-failed",
            )
        except _PublishError as exc:
            # A concurrent identical request, or an acknowledgement lost after
            # receive-pack committed the ref, is already durable. Never replace
            # a recovery ref pointing at anything else.
            try:
                if self._head(request, branch) == request.commit_sha:
                    return
            except _PublishError:
                pass
            raise _PublishError("recovery-push-failed", str(exc)) from exc

    def _ensure_commit(self, request: ModelRemoteLanePublishRequest, sha: str) -> None:
        try:
            self._checked(request, "cat-file", "-e", f"{sha}^{{commit}}")
        except _PublishError:
            # Fetch only the exact advertised commit, never a moving FETCH_HEAD.
            self._checked(
                request,
                "fetch",
                "--no-tags",
                "origin",
                sha,
                failure="target-unreadable",
            )
            self._checked(
                request,
                "cat-file",
                "-e",
                f"{sha}^{{commit}}",
                failure="target-unreadable",
            )

    def _replay_tree(self, request: ModelRemoteLanePublishRequest, head: str) -> str:
        """Merge the entire lane delta once, including overlapping phase commits.

        The explicit base keeps changes from before dispatch out of the replay.
        A second request merges the same delta to the same tree, so it can
        recognize a previously replayed (and therefore differently hashed) commit.
        """
        try:
            result = self._git.run(
                [
                    "git",
                    "-C",
                    request.worktree,
                    "merge-tree",
                    "--write-tree",
                    f"--merge-base={request.base_sha}",
                    head,
                    request.commit_sha,
                ],
                request.timeout_s,
            )
        except RemoteLanePortError as exc:
            raise _PublishError("replay-failed", str(exc)) from exc
        if result.returncode:
            raise _PublishError(
                "conflict" if result.returncode == 1 else "replay-failed",
                (result.stderr + result.stdout).strip()[-1200:],
            )
        tree = result.stdout.splitlines()[0] if result.stdout else ""
        if not SHA_RE.fullmatch(tree):
            raise _PublishError("replay-failed", "merge-tree returned no tree object")
        return tree

    @contextmanager
    def _candidate(
        self,
        request: ModelRemoteLanePublishRequest,
        head: str,
        cleanup: list[str],
    ) -> Iterator[str]:
        if self._ancestor(request, head, request.commit_sha):
            yield request.commit_sha
            return
        merged_tree = self._replay_tree(request, head)
        if merged_tree == self._checked(request, "rev-parse", f"{head}^{{tree}}"):
            yield head
            return
        path = Path(tempfile.mkdtemp(prefix="publish-", dir=request.replay_root))
        added = False
        try:
            self._checked(request, "worktree", "add", "--detach", str(path), head)
            added = True
            self._checked(
                request, "read-tree", "--reset", "-u", merged_tree, tree=str(path)
            )
            self._checked(request, "commit", "-C", request.commit_sha, tree=str(path))
            yield self._checked(request, "rev-parse", "HEAD", tree=str(path))
        finally:
            if added:
                try:
                    self._checked(request, "worktree", "remove", "--force", str(path))
                except _PublishError as exc:
                    cleanup.append(f"keep replay worktree {path}: {exc}")
            else:
                try:
                    path.rmdir()
                except OSError as exc:
                    cleanup.append(f"keep replay directory {path}: {exc}")

    def handle(
        self, request: ModelRemoteLanePublishRequest
    ) -> ModelRemoteLanePublishResult:
        recovery = f"recovery/{request.lane}/{request.commit_sha}"
        preserved = False
        original_dirty = True
        cleanup: list[str] = []
        head = request.observed_head
        attempts = 0
        outcome: PublishOutcome = "invalid-history"
        message = ""
        published: str | None = None
        try:
            if (
                not Path(request.worktree).is_absolute()
                or not Path(request.replay_root).is_absolute()
            ):
                raise _PublishError(
                    "invalid-history", "worktree and replay_root must be absolute paths"
                )
            if request.branch == recovery:
                raise _PublishError(
                    "invalid-history", "target and recovery branch must differ"
                )
            self._checked(
                request,
                "check-ref-format",
                f"refs/heads/{request.branch}",
                failure="invalid-history",
            )
            self._checked(
                request,
                "check-ref-format",
                f"refs/heads/{recovery}",
                failure="invalid-history",
            )
            self._checked(
                request,
                "cat-file",
                "-e",
                f"{request.commit_sha}^{{commit}}",
                failure="invalid-history",
            )
            original_dirty = bool(
                self._checked(
                    request,
                    "status",
                    "--porcelain",
                    "--untracked-files=normal",
                    failure="invalid-history",
                )
            )
            if request.base_sha == request.commit_sha or not self._ancestor(
                request, request.base_sha, request.commit_sha
            ):
                raise _PublishError(
                    "invalid-history",
                    "lane base must be a strict ancestor of its commit",
                )
            self._preserve(request, recovery)
            preserved = True
            for attempt in range(1, request.max_attempts + 1):
                attempts = attempt
                current = self._head(request, request.branch)
                if current is None:
                    raise _PublishError(
                        "target-unreadable", "target branch no longer exists"
                    )
                head = current
                self._ensure_commit(request, head)
                if self._ancestor(request, request.commit_sha, head):
                    outcome, published = "already-published", head
                    break
                with self._candidate(request, head, cleanup) as candidate:
                    if candidate == head:
                        outcome, published = "already-published", head
                        break
                    # A pinned push may advance only from a head it contains.
                    # This proof is mandatory even though the lease is exact.
                    if not self._ancestor(request, head, candidate):
                        raise _PublishError(
                            "invalid-history",
                            "candidate does not contain the pinned PR head",
                        )
                    try:
                        self._checked(
                            request,
                            "push",
                            "--porcelain",
                            f"--force-with-lease=refs/heads/{request.branch}:{head}",
                            "origin",
                            f"{candidate}:refs/heads/{request.branch}",
                            failure="push-failed",
                        )
                    except _PublishError as exc:
                        moved = self._head(request, request.branch)
                        if moved == candidate:
                            outcome, published = "published", candidate
                            break
                        if moved == head:
                            raise exc
                        continue
                    outcome, published = "published", candidate
                    break
            else:
                raise _PublishError(
                    "head-keeps-moving", "target head moved during every pinned push"
                )
            message = f"{outcome} branch={request.branch} head={published}; original work on {recovery}"
        except _PublishError as exc:
            outcome = exc.outcome
            message = f"{exc}; recovery branch={recovery} pushed={preserved}"
        except OSError as exc:
            outcome = "replay-failed"
            message = f"{exc}; recovery branch={recovery} pushed={preserved}"
        return ModelRemoteLanePublishResult(
            outcome=outcome,
            message=message,
            original_sha=request.commit_sha,
            observed_head=head,
            published_sha=published,
            recovery_branch=recovery,
            recovery_pushed=preserved,
            keep_worktree=not preserved or original_dirty or bool(cleanup),
            attempts=attempts,
            cleanup_errors=tuple(cleanup),
        )
